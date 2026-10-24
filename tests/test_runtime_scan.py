"""Deterministic procfs races and visibility boundaries for typed cleanup."""

from __future__ import annotations

import errno
import io
import os
from dataclasses import replace
from pathlib import Path

import pytest

from worktree_controller import environment, writer_lease
from worktree_controller.models import ControllerError, WriterHolder


def process_stat(pid, *, ticks=100, state="S", threads=1, ppid=0):
    fields = ["0"] * 50
    fields[0], fields[17], fields[19] = state, str(threads), str(ticks)
    fields[1] = str(ppid)
    return f"{pid} (comm with ) spaces) " + " ".join(fields) + "\n"


@pytest.fixture
def procfs(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(
        environment, "Path", lambda value: proc if value == "/proc" else Path(value)
    )

    def create(pid=42, *, ticks=100, state="S", threads=1, uids=None, ppid=0):
        process = proc / str(pid)
        process.mkdir()
        (process / "stat").write_text(
            process_stat(pid, ticks=ticks, state=state, threads=threads, ppid=ppid)
        )
        (process / "status").write_text(
            "Uid:\t" + "\t".join(map(str, uids or (os.getuid(),) * 4)) + "\n"
        )
        (process / "cwd").symlink_to(tmp_path)
        (process / "exe").symlink_to(tmp_path / "python")
        (process / "fd").mkdir()
        (process / "maps").write_bytes(b"00001000-00002000 r--p 00000000 00:00 0\n")
        return process

    return root, proc, create


def scan(root):
    return environment.runtime_references(root, strict=True)


def deny_links(monkeypatch, error=errno.EACCES):
    original = os.readlink

    def denied(path, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise OSError(error, "synthetic procfs refusal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", denied)


def assert_uncertain(root, *details):
    with pytest.raises(ControllerError) as exc:
        scan(root)
    assert exc.value.code == "RUNTIME_SCAN_UNCERTAIN"
    for detail in details:
        assert detail in str(exc.value)


@pytest.mark.parametrize(("raw", "roots", "expected"), [
    ("/checkout", ("/checkout",), True),
    ("/checkout/", ("/checkout",), True),
    ("/checkout/file", ("/checkout",), True),
    ("/checkout/.venv/lib.so (deleted)", ("/checkout",), True),
    ("/checkout/file (deleted) (deleted)", ("/checkout/file (deleted)",), True),
    ("/checkout-other/file", ("/checkout",), False),
    ("/checkout-other", ("/checkout",), False),
    ("/check", ("/checkout",), False),
    ("/other/checkout/file", ("/checkout",), False),
    ("/checkout/../other", ("/checkout",), True),
    ("/checkout//./file", ("/checkout",), True),
    ("/./checkout/file", ("/checkout",), True),
    ("//checkout/file", ("/checkout",), True),
    ("///checkout/file", ("/checkout",), True),
    ("/checkout/.", ("/checkout",), True),
    ("/checkout/.hidden", ("/checkout",), True),
    ("/checkout/space dir/你好\nfile", ("/checkout",), True),
    ("/other/file", ("/checkout", "/other"), True),
    ("/other/file", ("relative", "/other"), False),
    ("/other/file", ("/other", "relative"), True),
    ("/checkout/file", ("//checkout",), False),
    ("/file", ("//",), False),
    ("/file", ("/",), True),
    ("//./file", ("/",), True),
    ("socket:[1234]", ("/",), False),
    ("checkout/file", ("/checkout",), False),
    ("/checkout/file", (), False),
])
def test_runtime_path_membership_preserves_posix_commonpath_rules(raw, roots, expected):
    assert environment._path_is_within(raw, tuple(Path(root) for root in roots)) is expected


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("kind", ["cwd", "exe", "fd", "mmap"])
@pytest.mark.parametrize("suffix", ["/file (deleted)", "-other/file", "//./file"])
def test_runtime_scan_keeps_deleted_references_and_path_boundaries(procfs, kind, suffix, strict):
    root, _, create = procfs
    process = create()
    raw = f"{root}{suffix}"
    if kind == "mmap":
        (process / "maps").write_text(f"00001000-00002000 r--p 00000000 00:00 1 {raw}\n")
    else:
        link = process / "fd/7" if kind == "fd" else process / kind
        link.unlink(missing_ok=True)
        link.symlink_to(raw)
    references = environment.runtime_references(root, strict=strict)
    if suffix.startswith("-other"):
        assert references == ()
    else:
        assert references == ({"pid": "42", "kind": kind, "path": raw},)


def test_readable_no_reference(procfs):
    root, _, create = procfs
    create()
    assert scan(root) == ()


@pytest.mark.parametrize("error", [errno.ESRCH, errno.EIO])
def test_legacy_scan_handles_process_exit_during_mapping_read(procfs, monkeypatch, error):
    root, _, create = procfs
    process = create()
    original = Path.open

    class MappingStream(io.BytesIO):
        def __next__(self):
            raise OSError(error, "synthetic mapping read failure")

    def opened(path, *args, **kwargs):
        if path == process / "maps":
            return MappingStream()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    if error == errno.ESRCH:
        assert environment.runtime_references(root) == ()
    else:
        with pytest.raises(ControllerError) as caught:
            environment.runtime_references(root)
        assert caught.value.code == "RUNTIME_SCAN_ERROR"
        assert caught.value.__cause__.errno == error


@pytest.mark.parametrize("kind", ["cwd", "exe", "fd", "mmap"])
def test_readable_positive_reference(procfs, kind):
    root, _, create = procfs
    process = create()
    target = root / "resource"
    if kind == "mmap":
        (process / "maps").write_text(f"00001000-00002000 r--p 00000000 00:00 1 {target}\n")
    else:
        link = process / "fd/7" if kind == "fd" else process / kind
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    assert {row["kind"] for row in scan(root)} == {kind}


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_same_uid_inaccessible_relevant_process(procfs, monkeypatch, error):
    root, _, create = procfs
    process = create()
    (process / "cwd").unlink()
    (process / "cwd").symlink_to(root)
    deny_links(monkeypatch, error)
    assert_uncertain(root, "pid=42", "start_ticks=100", f"cwd:{errno.errorcode[error]}({error})")


def test_live_inaccessible_process_is_not_proven_irrelevant_by_other_readable_fields(
    procfs, monkeypatch
):
    root, _, create = procfs
    create()  # Even an unrelated exe/maps does not establish where hidden cwd/fds point.
    deny_links(monkeypatch)
    assert_uncertain(root, "EACCES(13)")


@pytest.mark.parametrize("state", ["Z", "X"])
def test_inaccessible_irrelevant_terminal_process(procfs, monkeypatch, state):
    root, _, create = procfs
    create(state=state)
    deny_links(monkeypatch)
    assert scan(root) == ()


def test_zombie_leader_with_live_threads_is_not_excluded(procfs, monkeypatch):
    root, _, create = procfs
    create(state="Z", threads=2)
    deny_links(monkeypatch)
    assert_uncertain(root, "pid=42", "EACCES(13)")


def test_foreign_uid_exclusion_is_the_existing_declared_scope(procfs, monkeypatch):
    root, _, create = procfs
    create(uids=(os.getuid() + 1,) * 4)
    deny_links(monkeypatch)
    assert scan(root) == ()


def test_saved_uid_cannot_be_mistaken_for_a_foreign_process(procfs, monkeypatch):
    root, _, create = procfs
    create(uids=(os.getuid() + 1, os.getuid() + 1, os.getuid(), os.getuid() + 1))
    deny_links(monkeypatch)
    assert_uncertain(root, "pid=42")


@pytest.mark.parametrize("error", [errno.ENOENT, errno.ESRCH])
def test_vanished_pid_between_enumeration_and_open(procfs, monkeypatch, error):
    root, proc, create = procfs
    process = create()
    original = os.open

    def vanished(path, *args, **kwargs):
        if path == process and process.exists():
            process.rename(proc / "exited")
            raise OSError(error, "process exited")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "open", vanished)
    assert scan(root) == ()


@pytest.mark.parametrize("ticks", [100, 101])
def test_pid_reuse_scans_replacement_even_with_same_start_tick(procfs, monkeypatch, ticks):
    root, proc, create = procfs
    process = create()
    original = os.readlink
    replaced = False

    def reused(path, *args, **kwargs):
        nonlocal replaced
        if path == "cwd" and not replaced:
            replaced = True
            process.rename(proc / "old")
            new = create(ticks=ticks)
            (new / "cwd").unlink()
            (new / "cwd").symlink_to(root)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", reused)
    assert scan(root) == ({"pid": "42", "kind": "cwd", "path": str(root)},)


def test_repeated_pid_reuse_remains_uncertain(procfs, monkeypatch):
    root, proc, create = procfs
    process = create()
    original = os.readlink
    generation = 0

    def reused(path, *args, **kwargs):
        nonlocal generation
        if path == "cwd":
            process.rename(proc / f"old-{generation}")
            generation += 1
            create(ticks=100 + generation)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", reused)
    assert_uncertain(root, "PID_REUSED")
    assert generation == 3


def test_process_exits_during_reference_scan(procfs, monkeypatch):
    root, proc, create = procfs
    process = create()
    original = os.readlink

    def exiting(path, *args, **kwargs):
        if path == "cwd":
            process.rename(proc / "exited")
            raise FileNotFoundError(errno.ENOENT, "process exited")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", exiting)
    assert scan(root) == ()


def test_process_becomes_single_thread_zombie_during_scan(procfs, monkeypatch):
    root, _, create = procfs
    process = create()
    original = os.readlink

    def exiting(path, *args, **kwargs):
        if path == "cwd":
            (process / "stat").write_text(process_stat(42, state="Z"))
            raise PermissionError(errno.EACCES, "terminal process")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", exiting)
    assert scan(root) == ()


def test_reference_from_replaced_pid_is_not_attributed_to_new_process(procfs, monkeypatch):
    root, proc, create = procfs
    process = create()
    (process / "cwd").unlink()
    (process / "cwd").symlink_to(root)
    original = os.readlink
    replaced = False

    def reused(path, *args, **kwargs):
        nonlocal replaced
        value = original(path, *args, **kwargs)
        if path == "cwd" and not replaced:
            process.rename(proc / "old")
            create(ticks=101)
            replaced = True
        return value

    monkeypatch.setattr(environment.os, "readlink", reused)
    assert scan(root) == ()


def test_process_exits_during_identity_recheck(procfs, monkeypatch):
    root, proc, create = procfs
    process = create()
    original = environment._proc_bytes
    reads = 0

    def exiting(directory, name):
        nonlocal reads
        if name == "stat":
            reads += 1
            if reads == 2:
                process.rename(proc / "exited")
                raise ProcessLookupError(errno.ESRCH, "process exited")
        return original(directory, name)

    monkeypatch.setattr(environment, "_proc_bytes", exiting)
    assert scan(root) == ()


def test_missing_live_process_field_is_not_a_vanished_pid(procfs):
    root, _, create = procfs
    process = create()
    (process / "cwd").unlink()
    assert_uncertain(root, "cwd:ENOENT(2)", "start_ticks=100")


def test_missing_stat_with_existing_process_is_uncertain(procfs):
    root, _, create = procfs
    process = create()
    (process / "stat").unlink()
    assert_uncertain(root, "identity-recheck", "ENOENT(2)")


def test_fd_close_confirmation_permission_error_is_preserved(procfs, monkeypatch):
    root, _, create = procfs
    process = create()
    (process / "fd/7").symlink_to(root)
    original_link, original_stat = os.readlink, os.stat

    def missing(path, *args, **kwargs):
        if path == "7":
            raise FileNotFoundError(errno.ENOENT, "transient descriptor lookup")
        return original_link(path, *args, **kwargs)

    def denied(path, *args, **kwargs):
        if path == "7":
            raise PermissionError(errno.EACCES, "cannot confirm descriptor closed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", missing)
    monkeypatch.setattr(environment.os, "stat", denied)
    assert_uncertain(root, "fd:EACCES(13)")


def test_descriptor_closes_during_scan(procfs, monkeypatch):
    root, _, create = procfs
    process = create()
    descriptor = process / "fd/7"
    descriptor.symlink_to(root / "file")
    original = os.readlink

    def closed(path, *args, **kwargs):
        if path == "7":
            descriptor.unlink()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", closed)
    assert scan(root) == ()


@pytest.mark.parametrize("field", ["stat", "status", "maps"])
def test_malformed_proc_entry_is_uncertain(procfs, field):
    root, _, create = procfs
    process = create()
    (process / field).write_text("malformed\n")
    assert_uncertain(root, "MALFORMED_PROC_ENTRY")


def test_process_path_symlink_race_is_not_followed(procfs, monkeypatch):
    root, proc, create = procfs
    process = create()
    replacement = create(pid=43)
    original = os.readlink

    def swapped(path, *args, **kwargs):
        if path == "cwd" and not process.is_symlink():
            process.rename(proc / "old")
            process.symlink_to(replacement)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", swapped)
    assert_uncertain(root, "identity-recheck", "ENOTDIR")


@pytest.mark.parametrize("field", ["stat", "status", "maps", "fd"])
def test_regular_proc_field_symlink_is_not_followed(procfs, field):
    root, proc, create = procfs
    process = create()
    original = process / field
    original.rename(proc / "moved")
    original.symlink_to(proc / "moved")
    assert_uncertain(root, "ENOTDIR" if field == "fd" else "ELOOP")


@pytest.mark.parametrize("symlink", [False, True])
def test_checkout_path_race_is_uncertain(procfs, monkeypatch, symlink):
    root, _, create = procfs
    create()
    original = os.readlink

    def swapped(path, *args, **kwargs):
        if path == "cwd":
            old = root.with_name("moved")
            root.rename(old)
            if symlink:
                root.symlink_to(old)
            else:
                root.mkdir()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", swapped)
    assert_uncertain(root, "checkout path/identity changed")


def test_positive_reference_takes_precedence_over_uncertainty(procfs):
    root, _, create = procfs
    process = create()
    (process / "exe").unlink()  # A live process's missing exe is uncertain.
    (process / "cwd").unlink()
    (process / "cwd").symlink_to(root)
    assert scan(root) == ({"pid": "42", "kind": "cwd", "path": str(root)},)


def test_unresolved_processes_are_aggregated(procfs, monkeypatch):
    root, _, create = procfs
    create()
    create(pid=43)
    deny_links(monkeypatch)
    assert_uncertain(root, "pid=42", "pid=43", "EACCES(13)")


@pytest.fixture
def managed_holder(monkeypatch):
    monkeypatch.setattr(environment, "boot_id", lambda: "fixture-boot")
    return WriterHolder("manual", 42, "fixture-boot", 100, "fixture-session", "fixture-run")


def test_authority_inaccessible_managed_holder_is_uncertain(procfs, managed_holder, monkeypatch):
    root, _, create = procfs
    create()
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root, holders=(managed_holder,))
    assert result["managed_visibility_uncertainties"][0]["pid"] == 42
    assert result["host_visibility_gaps"] == []


def test_authority_readable_live_holder_blocks_without_path_reference(procfs, managed_holder):
    root, _, create = procfs
    create()
    result = environment.runtime_authority_evidence(root, holders=(managed_holder,))
    assert result["managed_references"][0]["classification"] == "MANAGED_REFERENCE"
    assert result["observed_references"] == []


@pytest.mark.parametrize("foreign", [False, True])
def test_authority_inaccessible_descendant_blocks(procfs, managed_holder, monkeypatch, foreign):
    root, _, create = procfs
    create()
    create(pid=43, ticks=200, ppid=42, uids=(os.getuid() + 1,) * 4 if foreign else None)
    create(pid=44, ticks=300, ppid=43)
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root, holders=(managed_holder,))
    blocked = {row["pid"]: row for row in result["managed_visibility_uncertainties"]}
    assert set(blocked) == {42, 43, 44}
    assert [row["pid"] for row in blocked[44]["lineage"]] == [44, 43, 42]
    assert result["host_visibility_gaps"] == []


def test_authority_inaccessible_host_has_warning_only(procfs, monkeypatch):
    root, _, create = procfs
    create()
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root)
    assert result["host_visibility_gaps"][0]["classification"] == "HOST_VISIBILITY_GAP"
    assert (
        result["managed_visibility_uncertainties"]
        == result["managed_references"]
        == result["scan_uncertainties"]
        == []
    )


def test_authority_readable_positive_remains_observed_with_host_gap(procfs, monkeypatch):
    root, _, create = procfs
    process = create()
    (process / "maps").write_text(f"00001000-00002000 r--p 00000000 00:00 1 {root}/resource\n")
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root)
    assert result["observed_references"][0]["classification"] == "OBSERVED_REFERENCE"
    assert result["host_visibility_gaps"]


def test_authority_positive_is_preserved_when_identity_recheck_becomes_unreadable(
    procfs, monkeypatch
):
    root, _, create = procfs
    process = create()
    (process / "cwd").unlink()
    (process / "cwd").symlink_to(root)

    def denied(path):
        raise PermissionError(errno.EACCES, "identity recheck became unreadable")

    monkeypatch.setattr(environment, "_current_process_identity", denied)
    result = environment.runtime_authority_evidence(root)
    assert result["observed_references"][0]["classification"] == "OBSERVED_REFERENCE"
    assert result["observed_references"][0]["path"] == str(root)


def test_authority_readable_no_reference_passes(procfs):
    root, _, create = procfs
    create()
    result = environment.runtime_authority_evidence(root)
    assert all(
        result[key] == []
        for key in (
            "managed_references",
            "observed_references",
            "managed_visibility_uncertainties",
            "host_visibility_gaps",
            "scan_uncertainties",
        )
    )


def test_authority_holder_pid_reuse_is_not_downgraded(procfs, managed_holder, monkeypatch):
    root, _, create = procfs
    create(ticks=101)
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root, holders=(managed_holder,))
    assert result["managed_visibility_uncertainties"][0]["lineage_issue"] == "HOLDER_PID_REUSED"
    assert result["host_visibility_gaps"] == []


def test_authority_lineage_reparent_race_is_not_downgraded(procfs, managed_holder, monkeypatch):
    root, _, create = procfs
    create()
    child = create(pid=43, ticks=200, ppid=42)
    original = environment._current_process_identity
    reads = 0

    def reparent(path):
        nonlocal reads
        if path == child:
            reads += 1
            if reads == 2:
                (child / "stat").write_text(process_stat(43, ticks=200, ppid=0))
        return original(path)

    monkeypatch.setattr(environment, "_current_process_identity", reparent)
    deny_links(monkeypatch)
    result = environment.runtime_authority_evidence(root, holders=(managed_holder,))
    child_evidence = next(
        row for row in result["managed_visibility_uncertainties"] if row["pid"] == 43
    )
    assert child_evidence["lineage_issue"] == "LINEAGE_IDENTITY_CHANGED"
    assert result["host_visibility_gaps"] == []


def test_authority_malformed_host_entry_is_not_a_permission_warning(procfs):
    root, _, create = procfs
    process = create()
    (process / "maps").write_text("malformed\n")
    result = environment.runtime_authority_evidence(root)
    assert result["scan_uncertainties"][0]["issues"][0]["code"] == "MALFORMED_PROC_ENTRY"
    assert result["host_visibility_gaps"] == []


def _live_child():
    import select
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        if child.stdout is None or not select.select([child.stdout], [], [], 5)[0]:
            raise RuntimeError("test child did not become ready")
        if child.stdout.readline() != "ready\n":
            raise RuntimeError("test child readiness response was invalid")
        return child
    except BaseException:
        child.kill()
        child.wait(timeout=5)
        raise
    finally:
        if child.stdout is not None:
            child.stdout.close()


def test_transient_vanished_fd_is_rescanned(monkeypatch, tmp_path):
    calls = []
    real = environment._strict_process_references

    def flaky(directory, pid, roots, budget):
        calls.append(pid)
        if len(calls) == 1:
            return [], [environment._RuntimeIssue("fd", "ENOENT", errno.ENOENT)]
        return real(directory, pid, roots, budget)

    monkeypatch.setattr(environment, "_strict_process_references", flaky)
    monkeypatch.setattr(environment, "_VANISHED_RESCAN_SECONDS", 0)
    child = _live_child()
    try:
        result = environment._inspect_runtime_process(
            Path(f"/proc/{child.pid}"), (tmp_path,), [0, 0]
        )
    finally:
        child.kill()
        child.wait()
    assert len(calls) == 2
    assert result.issues == ()


def test_persistent_vanished_fd_on_a_live_process_stays_uncertain(monkeypatch, tmp_path):
    def always(directory, pid, roots, budget):
        return [], [environment._RuntimeIssue("fd", "ENOENT", errno.ENOENT)]

    monkeypatch.setattr(environment, "_strict_process_references", always)
    monkeypatch.setattr(environment, "_VANISHED_RESCAN_SECONDS", 0)
    child = _live_child()
    try:
        result = environment._inspect_runtime_process(
            Path(f"/proc/{child.pid}"), (tmp_path,), [0, 0]
        )
    finally:
        child.kill()
        child.wait()
    assert result.issues


EXITING_ISSUES = (
    environment._RuntimeIssue("cwd", "ENOENT", errno.ENOENT),
    environment._RuntimeIssue("fd", "EACCES", errno.EACCES),
)


@pytest.mark.parametrize("transient", [True, False])
def test_exiting_process_with_denied_fd_directory_is_rescanned(monkeypatch, tmp_path, transient):
    # An exiting task loses cwd/exe and, without its mm, also denies its fd directory.
    calls = []
    real = environment._strict_process_references

    def exiting(directory, pid, roots, budget):
        calls.append(pid)
        if len(calls) == 1 or not transient:
            return [], list(EXITING_ISSUES)
        return real(directory, pid, roots, budget)

    monkeypatch.setattr(environment, "_strict_process_references", exiting)
    monkeypatch.setattr(environment, "_VANISHED_RESCAN_SECONDS", 0)
    child = _live_child()
    try:
        result = environment._inspect_runtime_process(
            Path(f"/proc/{child.pid}"), (tmp_path,), [0, 0]
        )
    finally:
        child.kill()
        child.wait()
    if transient:
        assert len(calls) == 2 and result.issues == ()
    else:
        assert len(calls) == environment._MAX_PROCESS_ATTEMPTS
        assert result.issues == EXITING_ISSUES


def test_permission_denial_alone_is_not_rescanned(monkeypatch, tmp_path):
    calls = []

    def denied(directory, pid, roots, budget):
        calls.append(pid)
        return [], [environment._RuntimeIssue("fd", "EACCES", errno.EACCES)]

    monkeypatch.setattr(environment, "_strict_process_references", denied)
    monkeypatch.setattr(environment, "_VANISHED_RESCAN_SECONDS", 0)
    child = _live_child()
    try:
        result = environment._inspect_runtime_process(
            Path(f"/proc/{child.pid}"), (tmp_path,), [0, 0]
        )
    finally:
        child.kill()
        child.wait()
    assert len(calls) == 1 and [issue.code for issue in result.issues] == ["EACCES"]


@pytest.mark.parametrize(("before", "after", "same"), [(3, 7, True), (3, 1, False), (1, 2, False), (1, 1, True)])
def test_thread_pool_resizing_keeps_process_identity(before, after, same):
    identity = environment._ProcessIdentity((1, 2), 100, "S", before, (1000,) * 4, 1, 1)
    assert environment._same_process(identity, replace(identity, threads=after)) is same


def test_resizing_thread_pool_during_scan_is_not_uncertain(procfs, monkeypatch):
    root, _, create = procfs
    create(threads=4)
    calls = []
    real = environment._process_identity

    def resizing(directory, pid):
        calls.append(pid)
        return replace(real(directory, pid), threads=4 + len(calls))

    monkeypatch.setattr(environment, "_process_identity", resizing)
    assert scan(root) == ()


def test_lenient_scan_refuses_a_checkout_replaced_during_the_scan(procfs, monkeypatch):
    root, _, create = procfs
    create()
    original = environment._lenient_runtime_references

    def replacing(path):
        result = original(path)
        root.rename(root.with_name("moved"))
        root.mkdir()
        return result

    monkeypatch.setattr(environment, "_lenient_runtime_references", replacing)
    with pytest.raises(ControllerError) as caught:
        environment.runtime_references(root)
    assert caught.value.code == "RUNTIME_SCAN_ERROR"


def test_lenient_scan_treats_denied_same_uid_entries_as_a_visibility_gap(procfs):
    root, _, create = procfs
    process = create()
    for name in ("cwd", "exe"):
        (process / name).unlink()
    (process / "fd").chmod(0)
    (process / "maps").chmod(0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root bypasses procfs-like permissions")
        assert environment.runtime_references(root) == ()
        with pytest.raises(ControllerError) as strict:
            environment.runtime_references(root, strict=True)
        assert strict.value.code == "RUNTIME_SCAN_UNCERTAIN"
    finally:
        (process / "fd").chmod(0o700)
        (process / "maps").chmod(0o600)


@pytest.mark.parametrize(("positive", "uncertain", "code"), [
    (False, False, None),
    (False, True, "RUNTIME_SCAN_UNCERTAIN"),
    (True, False, "RUNTIME_OCCUPIED"),
    (True, True, "RUNTIME_OCCUPIED"),
])
def test_runtime_clear_distinguishes_occupancy_from_uncertainty(procfs, positive, uncertain, code):
    root, _, create = procfs
    process = create()
    if positive:
        (process / "cwd").unlink()
        (process / "cwd").symlink_to(root)
    if uncertain:
        (process / "maps").write_text("malformed\n")
    if code is None:
        writer_lease.assert_runtime_clear(root, ())
    else:
        with pytest.raises(ControllerError) as refused:
            writer_lease.assert_runtime_clear(root, ())
        assert refused.value.code == code
