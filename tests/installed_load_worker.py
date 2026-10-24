"""Run fixture events against the explicitly selected installed interpreter."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from worktree_controller import environment, resource_admission

SCRIPT = Path(__file__).resolve()


def boundary(value):
    os.write(1, (json.dumps(value) + "\n").encode())
    os.read(0, 1)


def waiting(phase, path):
    from worktree_controller.writer_session import run_main

    signal.signal(signal.SIGINT, signal.default_int_handler)
    if phase == "report":
        original = sys.stderr

        class Report:
            def write(self, value):
                result = original.write(value)
                original.flush()
                if '"RESOURCE_WAIT"' in value:
                    boundary({"phase": "report"})
                return result

            def flush(self):
                return original.flush()

        sys.stderr = Report()
    else:
        original_admit, original_sleep = resource_admission.admit_execution, time.sleep
        polls = 0

        def admit(*args, cancel=False, **kwargs):
            nonlocal polls
            if not cancel:
                polls += 1
                if phase == "admitted" and polls == 2:
                    boundary({"phase": "before-admission"})
            result = original_admit(*args, cancel=cancel, **kwargs)
            if not cancel and polls == 2:
                if phase == "admitted":
                    bind_provider(path, phase)
                boundary({"phase": "repoll", "decision": result["decision"]})
            return result

        resource_admission.admit_execution = admit
        time.sleep = lambda seconds: original_sleep(min(seconds, 0.05))
    return run_main([
        "--path", str(path), "--client-id", "canary", "--session-id", phase,
        "--run-id", phase, "--", sys.executable, "-I", "-B", "-c",
        "from pathlib import Path;Path('must-not-run.txt').write_text('executed')",
    ])


def bind_provider(path, phase):
    """A real provider acquires and binds while the terminal wait returns."""
    from worktree_controller.git_backend import inventory
    from worktree_controller.lifecycle import update_external_binding
    from worktree_controller.store import MetadataStore
    from worktree_controller.writer_lease import RUNTIME_BINDING_PREFIX
    from worktree_controller.writer_session import admission, describe

    granted = admission(path, client_id="canary", selector=str(os.getpid()),
                        session_id=phase, run_id=phase)
    assert granted["admission"] == "GRANTED", granted
    projected = describe(path)
    snapshot = MetadataStore(inventory(path).common_dir).load()
    record = snapshot.records[projected["allocation_id"]]
    binding = RUNTIME_BINDING_PREFIX + json.dumps(
        {"holder": projected["lease"]["holder"], "lease_id": granted["lease_id"], "kind": "command"},
        sort_keys=True, separators=(",", ":"),
    )
    update_external_binding(Path(projected["repository_path"]), target=path, owner=record.owner,
                            expected_record_version=record.record_version, binding=binding, action="add")


def line(child):
    assert select.select([child.stdout], [], [], 10)[0], "fixture boundary timed out"
    data = b""
    while not data.endswith(b"\n"):
        byte = os.read(child.stdout.fileno(), 1)
        assert byte, "fixture ended before boundary"
        data += byte
    return data.decode().strip()


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream:
            with suppress(BrokenPipeError):
                stream.close()


def start(mode, *args):
    return subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", str(SCRIPT), mode, *map(str, args)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )


def pool(target, contracting):
    keep = threading.Event()
    threading.Thread(target=keep.wait, daemon=True).start()
    with target.open("rb"):
        threads = []
        for _ in range(5 if contracting else 0):
            event = threading.Event()
            thread = threading.Thread(target=event.wait, daemon=True)
            thread.start()
            threads.append((event, thread))
        print("ready", flush=True)
        for value in sys.stdin:
            if value.strip() == "grow":
                event = threading.Event()
                thread = threading.Thread(target=event.wait, daemon=True)
                thread.start()
                threads.append((event, thread))
            else:
                event, thread = threads.pop()
                event.set()
                thread.join()
            print("changed", flush=True)


def exiting():
    ready = threading.Event()

    def tail():
        ready.set()
        select.select([0], [], [], 30)
        os._exit(0)

    threading.Thread(target=tail).start()
    ready.wait()
    print("ready", flush=True)
    # Linux individual-thread exit leaves a zombie leader with a live thread.
    number = {"x86_64": 60, "aarch64": 93}.get(platform.machine())
    assert number is not None, "fixture requires a known Linux SYS_exit number"
    ctypes.CDLL(None).syscall(number, 0)
    raise AssertionError("SYS_exit returned")


def scan_pool(mode, root):
    target = root / "resource"
    target.write_bytes(b"fixture")
    contracting = mode == "contracting"
    child = start("child-pool", target, str(int(contracting)))
    original = environment._strict_process_references
    observations = []
    try:
        assert line(child) == "ready"

        def resize(directory, pid, roots, budget):
            if pid == str(child.pid):
                before = asdict(environment._process_identity(directory, pid))
                child.stdin.write(b"shrink\n" if contracting else b"grow\n")
                child.stdin.flush()
                assert line(child) == "changed"
                after = asdict(environment._process_identity(directory, pid))
                # Python join can finish before the kernel decrements nr_threads.
                expected = before["threads"] + (-1 if contracting else 1)
                deadline = time.monotonic() + 2
                while after["threads"] != expected:
                    assert time.monotonic() < deadline, (before, after)
                    time.sleep(0.001)
                    after = asdict(environment._process_identity(directory, pid))
                observations.append({"before": before, "after": after})
            return original(directory, pid, roots, budget)

        environment._strict_process_references = resize
        for _ in range(5):
            rows = environment.runtime_references(root, strict=True)
            assert any(row["pid"] == str(child.pid) and row["kind"] == "fd"
                       and row["path"] == str(target) for row in rows), rows
        expected = [6, 5, 4, 3, 2] if contracting else [3, 4, 5, 6, 7]
        assert [row["after"]["threads"] for row in observations] == expected, observations
        return {"observations": observations, "references": rows}
    finally:
        environment._strict_process_references = original
        stop(child)


def scan_exit(mode, root):
    transient = mode == "transient"
    child = start("child-exiting")
    original, original_sleep = environment._strict_process_references, time.sleep
    original_path = environment.Path

    class FixtureProcesses:
        def iterdir(self):
            return iter((Path(f"/proc/{child.pid}"),))

    # Enumerate only our child; its identity, permissions and exit still use real procfs.
    environment.Path = lambda value: FixtureProcesses() if value == "/proc" else Path(value)
    issues, rescans = [], []
    try:
        assert line(child) == "ready"
        deadline = time.monotonic() + 5
        while True:
            identity = environment._current_process_identity(Path(f"/proc/{child.pid}"))
            if identity is not None and identity.state == "Z" and identity.threads > 1:
                break
            assert time.monotonic() < deadline, identity
            original_sleep(0.005)

        def references(directory, pid, roots, budget):
            result = original(directory, pid, roots, budget)
            if pid == str(child.pid):
                issues.append([asdict(issue) for issue in result[1]])
            return result

        def complete_exit(seconds):
            rescans.append(seconds)
            if transient and child.poll() is None:
                child.stdin.write(b"x")
                child.stdin.flush()
                # Popen.wait's polling must not enter this scheduling hook.
                environment.time.sleep = original_sleep
                try:
                    child.wait(timeout=5)
                finally:
                    environment.time.sleep = complete_exit
            return original_sleep(seconds)

        environment._strict_process_references = references
        environment.time.sleep = complete_exit
        try:
            rows = environment.runtime_references(root, strict=True)
        except environment.ControllerError as exc:
            assert not transient and exc.code == "RUNTIME_SCAN_UNCERTAIN", exc
            assert f"pid={child.pid}" in str(exc), exc
            outcome = {"code": exc.code, "message": str(exc)}
        else:
            assert transient and not rows, rows
            outcome = {"references": rows}
        assert issues and any(issue["errno"] == 2 for issue in issues[0]), issues
        assert any(issue["errno"] in (1, 13) for issue in issues[0]), issues
        assert len(issues) == (1 if transient else environment._MAX_PROCESS_ATTEMPTS), issues
        assert len(rescans) == (1 if transient else environment._MAX_PROCESS_ATTEMPTS - 1), rescans
        return {"identity": asdict(identity), "issues": issues, "rescans": rescans, "outcome": outcome}
    finally:
        environment.Path = original_path
        environment._strict_process_references = original
        environment.time.sleep = original_sleep
        stop(child)


def scan_denied(root):
    child = start("child-denied")
    original = environment._strict_process_references
    observations = []
    try:
        assert line(child) == "ready"

        def denied(directory, pid, roots, budget):
            result = original(directory, pid, roots, budget)
            if pid == str(child.pid):
                observations.append([asdict(issue) for issue in result[1]])
            return result

        environment._strict_process_references = denied
        try:
            environment.runtime_references(root, strict=True)
        except environment.ControllerError as exc:
            assert exc.code == "RUNTIME_SCAN_UNCERTAIN" and f"pid={child.pid}" in str(exc), exc
            outcome = {"code": exc.code, "message": str(exc)}
        else:
            raise AssertionError("real live permission denial was accepted")
        assert len(observations) == 1 and observations[0], observations
        assert all(issue["errno"] in (1, 13) for issue in observations[0]), observations
        return {"issues": observations, "outcome": outcome}
    finally:
        environment._strict_process_references = original
        stop(child)


def main():
    mode = sys.argv[1]
    assert Path(environment.__file__).is_relative_to(Path(sys.executable).parent.parent), environment.__file__
    if mode == "requests":
        print(json.dumps(resource_admission.AdmissionLedger().read()))
    elif mode == "child-holder":
        print("ready", flush=True)
        time.sleep(120)
    elif mode == "child-group":
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", str(SCRIPT), "child-holder"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert line(child) == "ready"
        print(json.dumps({"member_pid": child.pid}), flush=True)
        time.sleep(120)
    elif mode == "child-pool":
        pool(Path(sys.argv[2]), bool(int(sys.argv[3])))
    elif mode == "child-exiting":
        exiting()
    elif mode == "child-denied":
        assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0
        print("ready", flush=True)
        time.sleep(30)
    elif mode.startswith("wait-"):
        return waiting(mode.removeprefix("wait-"), Path(sys.argv[2]))
    else:
        root = Path(sys.argv[2])
        root.mkdir()
        if mode in {"growing", "contracting"}:
            result = scan_pool(mode, root)
        elif mode in {"transient", "persistent"}:
            result = scan_exit(mode, root)
        else:
            assert mode == "denied", mode
            result = scan_denied(root)
        print(json.dumps({"mode": mode, "package": environment.__file__, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
