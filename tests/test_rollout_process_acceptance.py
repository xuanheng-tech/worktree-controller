"""Opt-in rollout acceptance using installed runtimes and process barriers.

Only scheduling is instrumented: the frozen gate, staged responses, release checks,
switch CAS and post-check run normally against disposable fixture state. Restoration
cases briefly hide the fixture's Git directory so the installed post-check really fails.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import runpy
import select
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def channel(config):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(30)
    connection.connect("\0" + config["socket"])
    return connection, connection.makefile("rb")


def event(link, kind, **fields):
    connection, stream = link
    connection.sendall(json.dumps({"kind": kind, **fields}).encode() + b"\n")
    if stream.readline() != b"ok\n":
        raise RuntimeError("fixture barrier closed before acknowledgement")


def process_identity(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    return {"pid": pid, "ticks": int(raw[raw.rfind(")") + 2:].split()[19])}


def process_stopped(identity):
    try:
        raw = Path(f"/proc/{identity['pid']}/stat").read_text()
    except FileNotFoundError:
        return True
    fields = raw[raw.rfind(")") + 2:].split()
    return int(fields[19]) != identity["ticks"] or fields[0] == "Z"


def gate_worker(original, config_path):
    config = json.loads(Path(config_path).read_text())
    link = channel(config)
    module = load_script(original, "acceptance_frozen_gate")
    run = subprocess.run

    def observed(command, **kwargs):
        if "resource-recover" not in command:
            return run(command, **kwargs)
        event(link, "probe-before", argv=list(map(str, command)))
        # Retain the normal process-group inheritance and actual installed CLI output.
        options = dict(kwargs)
        timeout, check = options.pop("timeout"), options.pop("check", False)
        if options.pop("capture_output", False):
            options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with subprocess.Popen(command, **options) as child:
            event(link, "probe-started", identity=process_identity(child.pid))
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except BaseException:
                child.kill()
                child.communicate()
                raise
        event(link, "probe-after", returncode=child.returncode, payload=json.loads(stdout))
        if check and child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)

    module.subprocess.run = observed
    try:
        event(link, "gate-start", identity=process_identity(os.getpid()), argv=sys.argv[1:])
        return module.main()
    finally:
        link[1].close()
        link[0].close()


def activation_worker(config_path):
    config = json.loads(Path(config_path).read_text())
    link = channel(config)
    module = load_script(ROOT / "scripts/controller-activate", "acceptance_activation")
    freeze, run, postcheck = module.frozen_source, module.run, module.post_activation_check

    def instrumented_source(work, oid):
        source = freeze(work, oid)
        gate = source / "scripts/check-writer-rollout"
        original = gate.with_name("check-writer-rollout.original")
        gate.rename(original)
        event(link, "frozen-gate", oid=oid, sha256=hashlib.sha256(original.read_bytes()).hexdigest())
        gate.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from tests.test_rollout_process_acceptance import gate_worker\n"
            f"raise SystemExit(gate_worker({str(original)!r}, {str(config_path)!r}))\n"
        )
        return source

    def observed(command, **kwargs):
        result = run(command, **kwargs)
        if any(str(part).endswith("/scripts/check-writer-rollout") for part in command):
            event(link, "gate-result", returncode=result.returncode,
                  payload=json.loads(result.stdout), argv=list(map(str, command)))
        elif "status" in command:
            event(link, "post-check-cli", returncode=result.returncode,
                  payload=json.loads(result.stdout), argv=list(map(str, command)))
        return result

    def observed_postcheck(runtime, oid):
        event(link, "post-check-before", oid=oid)
        try:
            postcheck(runtime, oid)
        except (module.Abort, OSError, subprocess.SubprocessError, ValueError) as failure:
            event(link, "post-check-after", oid=oid, ok=False, reason=str(failure))
            raise
        event(link, "post-check-after", oid=oid, ok=True)

    module.frozen_source, module.run = instrumented_source, observed
    module.post_activation_check = observed_postcheck
    module.verify_runtime(module.ACTIVE.resolve(strict=True), module.active_version())
    sys.argv = ["controller-activate", config["action"], "--version", config["target"],
                "--wait-ready-seconds", str(config["budget"]), "--consumer", config["consumer"]]
    try:
        return module.main()
    finally:
        link[1].close()
        link[0].close()


class RolloutFixture:
    def __init__(self, installed, previous, repository, directory):
        self.installed, self.previous = installed, previous
        self.repository, self.directory = repository, directory
        self.home = directory / "home"
        self.home.mkdir()
        self.environment = {
            key: value for key, value in os.environ.items()
            if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "TZ", "UV_CACHE_DIR"}
        }
        self.environment.update(
            HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
            NO_PROXY="*", no_proxy="*",
        )
        temporary = directory / "tmp"
        temporary.mkdir()
        self.environment["TMPDIR"] = str(temporary)
        self.root = self.home / ".local/share/worktree-controller"
        self.runtimes = []
        for binary in (previous, installed):
            runtime = binary.parent.parent
            oid = runtime.parent.name
            if len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid):
                raise AssertionError("rollout acceptance requires commit-pinned installed runtimes")
            alias = self.root / "versions" / oid / "worktree-controller"
            alias.mkdir(parents=True)
            for name in ("bin", "lib", "uv-receipt.toml"):
                (alias / name).symlink_to(runtime / name)
            self.runtimes.append(alias)
        self.active = self.root / "active"
        self.active.symlink_to(self.runtimes[0])
        self.action, self.target_runtime = "activate", self.runtimes[1]
        launcher = (ROOT / "scripts/controller-runtime").read_bytes()
        for relative in ("bin/controller-runtime", ".local/bin/worktree-controller",
                         ".local/bin/worktree-session", ".local/bin/worktree-session-end"):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(launcher)
            path.chmod(0o755)
        self.consumer = directory / "consumer"
        self.consumer_calls = directory / "consumer-calls"
        self.declare_consumer()
        self.pending, self.identities, self.probes, self.results = [], [], [], []
        self.postchecks, self.postcheck_queries, self.frozen_gates = [], [], []
        self.hidden_git = repository.repo / ".git-postcheck-hidden"
        self.catalog_owner = None
        self.gate_groups = []
        self.attempts, self.lock_handle = 0, None
        self.cli("adopt", "--path", repository.repo, "--task-key", "fixture-control",
                 "--authority-key", "fixture-control", "--module", "repository",
                 "--role", "control-plane", "--owner", "fixture-owner", "--lifecycle", "ACTIVE")
        self.version = self.cli("capabilities")["tool_version"]
        self.add_transaction("seed")

    def declare_consumer(self, minimum=2, *, extra_requirements=None):
        capability = {"writer_session_version": 2, "holder_aware_write_guard": True,
                      "host_managed_writer_session_version": 2, "active_writer_sessions": 0,
                      "required_controller_capabilities": {"host_handoff_continuation_version": minimum}}
        capability["required_controller_capabilities"].update(extra_requirements or {})
        self.consumer.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"with Path({str(self.consumer_calls)!r}).open('a') as stream: stream.write('called\\n')\n"
            f"print({json.dumps(capability)!r})\n"
        )
        self.consumer.chmod(0o755)

    def publish_catalog(self, *, review_previous=True):
        from tests.test_shadow import git

        current = self.cli("capabilities")["decision"]
        versions = [current["contract_version"]]
        layouts = [current["storage_layout_version"]]
        if review_previous:
            previous = self.cli("capabilities", binary=self.previous)["decision"]
            versions.append(previous["contract_version"])
            if previous["storage_layout_version"] not in layouts:
                layouts.append(previous["storage_layout_version"])
        document = {"schema_version": 1, "toolchain_contract_version": 6,
                    "runtime_requirements": {"worktree-controller": {
                        "cli_contract_versions": versions, "storage_layout_versions": layouts,
                        "capability_versions": {"capabilities_version": 1},
                    }}}
        catalog = self.catalog_owner.repo / "toolchain_compatibility.json"
        catalog.write_text(json.dumps(document, indent=2) + "\n")
        git(self.catalog_owner.repo, "add", "toolchain_compatibility.json")
        git(self.catalog_owner.repo, "commit", "-m", "publish fixture compatibility review")
        git(self.catalog_owner.repo, "push", "--no-follow-tags", "origin", "HEAD:refs/heads/main")
        assert git(self.catalog_owner.repo, "ls-remote", "origin", "refs/heads/main").split()[0] == git(
            self.catalog_owner.repo, "rev-parse", "HEAD",
        )

    def configure_catalog(self):
        from tests.test_shadow import GitRepository, git

        self.catalog_owner = GitRepository(linked=0, policy=False)
        remote = self.catalog_owner.root / "published.git"
        git(self.catalog_owner.root, "init", "--bare", str(remote))
        git(self.catalog_owner.repo, "remote", "add", "origin", remote.as_uri())
        self.publish_catalog()
        self.environment["CONTROLLER_ROLLOUT_TOOLCHAIN_COMPATIBILITY"] = str(
            self.catalog_owner.repo / "toolchain_compatibility.json",
        )

    def cli(self, *arguments, crash=None, binary=None):
        environment = dict(self.environment)
        if crash:
            environment["WORKTREE_CONTROLLER_TEST_CRASH_AT"] = crash
        result = subprocess.run(
            [str(binary or self.installed), *map(str, arguments), "--repo", str(self.repository.repo), "--json"],
            env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
        if crash:
            assert result.returncode == 97, result.stdout + result.stderr
            return None
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert not payload["errors"], payload
        return payload

    def add_transaction(self, task):
        self.cli("acquire", "--path", self.repository.root / task, "--task-key", task,
                 "--authority-key", task, "--module", "fixture", "--role", "feature",
                 "--owner", "fixture-owner", "--branch", f"feature/{task}",
                 "--base-ref", "HEAD", crash="acquire_prepared")

    def interrupt_release(self):
        task = f"release-{len(self.pending)}"
        target = self.repository.root / task
        self.cli("acquire", "--path", target, "--task-key", task, "--authority-key", task,
                 "--module", "fixture", "--role", "feature", "--owner", "fixture-owner",
                 "--branch", f"feature/{task}", "--base-ref", "HEAD")
        result = self.cli("complete-no-change", "--path", target,
                          "--expected-record-version", 1, "--owner", "fixture-owner")
        request = ("release", "--path", target, "--expected-record-version",
                   result["decision"]["record_version"], "--owner-release", "fixture-owner")
        self.cli(*request, crash="release_metadata_written")
        self.pending.append(request)

    def complete_release(self):
        self.cli(*self.pending[-1])
        self.pending.pop()

    def hold_lock(self):
        self.lock_handle = (self.repository.repo / ".git/worktree-controller/v1/repo.lock").open("rb")
        fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def schedule(self, message, scenario):
        with (self.directory / "process-events.jsonl").open("a") as stream:
            stream.write(json.dumps(message, sort_keys=True) + "\n")
        kind = message["kind"]
        if kind == "gate-start":
            self.attempts += 1
            self.identities.append(message["identity"])
            self.gate_groups.append(message["identity"])
            if scenario == "lock" and self.attempts == 1:
                self.hold_lock()
        elif kind == "probe-before":
            if scenario == "lock" and self.attempts == 1:
                self.lock_handle.close()
                self.lock_handle = None
            if scenario == "churn" or (scenario in {"add", "failure"} and self.attempts == 1):
                self.add_transaction(f"added-{self.attempts}")
            elif scenario == "complete" and self.attempts == 1:
                self.complete_release()
            elif scenario == "failure" and self.attempts == 2:
                self.interrupt_release()
            elif scenario == "timeout":
                self.hold_lock()
        elif kind == "probe-started":
            self.identities.append(message["identity"])
        elif kind == "probe-after":
            self.probes.append(message)
            if scenario in {"after", "drift"} and self.attempts == 1:
                self.add_transaction("after-probe")
            if scenario == "drift" and self.attempts == 1:
                replacement = self.root / "other-activator.pending"
                replacement.symlink_to(self.target_runtime)
                os.replace(replacement, self.active)
        elif kind == "gate-result":
            self.results.append(message)
        elif kind == "frozen-gate":
            expected = subprocess.check_output([
                "git", "-C", str(ROOT), "show",
                f"{message['oid']}:scripts/check-writer-rollout",
            ])
            assert message["sha256"] == hashlib.sha256(expected).hexdigest()
            self.frozen_gates.append(message["oid"])
        elif kind == "post-check-before":
            if scenario.startswith("restore-") and message["oid"] == self.target_runtime.parent.name:
                assert self.active.resolve() == self.target_runtime
                os.replace(self.repository.repo / ".git", self.hidden_git)
        elif kind == "post-check-cli":
            self.postcheck_queries.append(message)
        elif kind == "post-check-after":
            self.postchecks.append(message)
            if self.hidden_git.exists():
                os.replace(self.hidden_git, self.repository.repo / ".git")
                assert not message["ok"]
                if scenario == "restore-recovery":
                    self.interrupt_release()
                elif scenario == "restore-consumer":
                    self.declare_consumer(extra_requirements={"canonical_refresh_version": 1})
                elif scenario == "restore-catalog":
                    self.publish_catalog(review_previous=False)
                elif scenario == "restore-drift":
                    replacement = self.root / "other-activator.pending"
                    replacement.symlink_to(self.runtimes[0])
                    os.replace(replacement, self.active)

    def run(self, scenario):
        if scenario == "complete":
            self.interrupt_release()
        config = self.directory / "config.json"
        socket_name = f"wc-rollout-{os.getpid()}-{uuid.uuid4().hex}"
        budget = 1 if scenario == "timeout" else 5 if scenario == "churn" else 20
        config.write_text(json.dumps({"socket": socket_name, "target": self.target_runtime.parent.name,
                                     "action": self.action,
                                     "budget": budget, "consumer": str(self.consumer)}))
        source_url = subprocess.check_output(["git", "-C", str(ROOT), "remote", "get-url", "origin"], text=True).strip()
        environment = {**self.environment, "CONTROLLER_ROLLOUT_REPO": str(ROOT),
                       "CONTROLLER_ROLLOUT_GIT_SOURCE": source_url,
                       "CONTROLLER_ROLLOUT_GOVERNED": str(self.repository.repo)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            # Abstract Linux sockets keep IPC names below the filesystem path limit
            # and disappear on close; no listener or socket file survives the fixture.
            server.bind("\0" + socket_name)
            server.listen()
            log = self.directory / "activation.log"
            peers = {}
            with log.open("w") as output, subprocess.Popen(
                [sys.executable, str(Path(__file__)), "activation-worker", str(config)],
                env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            ) as worker:
                deadline = time.monotonic() + 60
                try:
                    while worker.poll() is None:
                        assert time.monotonic() < deadline, log.read_text()
                        ready, _, _ = select.select([server, *peers], [], [], 0.1)
                        for connection in ready:
                            if connection is server:
                                client, _ = server.accept()
                                client.settimeout(30)
                                peers[client] = client.makefile("rb")
                                continue
                            try:
                                line = peers[connection].readline()
                            except ConnectionResetError:
                                line = b""
                            if not line:
                                peers.pop(connection).close()
                                connection.close()
                                continue
                            self.schedule(json.loads(line), scenario)
                            try:
                                connection.sendall(b"ok\n")
                            except (BrokenPipeError, ConnectionResetError):
                                peers.pop(connection).close()
                                connection.close()
                    code = worker.wait(timeout=5)
                finally:
                    if worker.poll() is None:
                        os.killpg(worker.pid, signal.SIGKILL)
                        worker.wait(timeout=5)
                    for identity in self.gate_groups:
                        try:
                            current = process_identity(identity["pid"])
                            if current == identity and os.getpgid(identity["pid"]) == identity["pid"]:
                                os.killpg(identity["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except FileNotFoundError:
                            pass
                    for connection, stream in peers.items():
                        stream.close()
                        connection.close()
                    if self.lock_handle:
                        self.lock_handle.close()
                        self.lock_handle = None
            self.output = log.read_text()
        assert all(process_stopped(identity) for identity in self.identities), self.identities
        assert not list(Path(self.environment.get("TMPDIR", "/tmp")).glob("controller-gate-*"))
        return code

    def ready(self):
        assert len(self.results) == len(self.consumer_calls.read_text().splitlines()) == 2
        first, second = self.results
        assert first["argv"] == second["argv"]
        assert first["payload"]["blockers"] == [f"CONTROLLER_TRANSACTION_SET_CHANGED:{self.repository.repo.name}"]
        assert first["payload"]["transaction_diagnostics"][0]["error"]["code"] == "ROLLOUT_TRANSACTION_SET_CHANGED"
        assert first["returncode"] == 2 and second["returncode"] == 0
        assert second["payload"]["decision"] == "CLIENTS_READY"
        assert all(p["returncode"] == 0 and p["payload"]["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
                   and p["payload"]["tool_version"] == self.version for p in self.probes)
        assert self.active.resolve() == self.runtimes[1]
        assert "post-activation verification passed" in self.output

    def close(self):
        if self.hidden_git.exists():
            os.replace(self.hidden_git, self.repository.repo / ".git")
        while self.pending:
            self.complete_release()
        status = self.cli("status")
        assert status["repository"]["worktree_count"] == 1
        assert status["repository"]["writer_leases_recorded"] == status["repository"]["runtime_bound"] == 0
        assert status["repository"]["cleanup_pending"] == 0
        checked = self.cli("resource-recover", "--stage", "rollout-check", "--dry-run")
        assert checked["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
        if self.catalog_owner is not None:
            self.catalog_owner.close()


@pytest.fixture
def rollout(tmp_path):
    configured = [os.environ.get(name) for name in (
        "WORKTREE_CONTROLLER_ACCEPTANCE", "WORKTREE_CONTROLLER_ROLLOUT_PREVIOUS",
    )]
    if not all(configured):
        pytest.skip("set both installed Controller pins for rollout process acceptance")
    from tests.test_retention_admission import POLICY_V5
    from tests.test_shadow import GitRepository

    installed, previous = map(Path, configured)
    assert all(p.is_absolute() and p.is_file() and os.access(p, os.X_OK) for p in (installed, previous))
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        fixture = RolloutFixture(installed, previous, repository, tmp_path)
        try:
            yield fixture
        finally:
            fixture.close()


def test_installed_rollout_retries_added_transactions(rollout):
    assert rollout.run("add") == 0, rollout.output
    rollout.ready()


def test_installed_rollout_retries_completed_transactions(rollout):
    assert rollout.run("complete") == 0, rollout.output
    rollout.ready()


def test_installed_rollout_rechecks_addition_after_probe(rollout):
    assert rollout.run("after") == 0, rollout.output
    rollout.ready()


def test_installed_rollout_churn_exhausts_budget(rollout):
    assert rollout.run("churn") == 2, rollout.output
    assert rollout.attempts >= 2
    assert all(result["payload"]["blockers"] == [
        f"CONTROLLER_TRANSACTION_SET_CHANGED:{rollout.repository.repo.name}"
    ] for result in rollout.results)
    assert "timed out after 5s" in rollout.output
    assert rollout.active.resolve() == rollout.runtimes[0]


def test_installed_rollout_stops_on_real_recovery_error(rollout):
    assert rollout.run("failure") == 2, rollout.output
    assert rollout.attempts == len(rollout.results) == 2
    diagnostic, = rollout.results[-1]["payload"]["transaction_diagnostics"]
    assert diagnostic["error"]["code"] == "ROLLOUT_RELEASE_RECOVERY_REQUIRED"
    assert rollout.probes[-1]["returncode"] == 2
    assert rollout.active.resolve() == rollout.runtimes[0]


def test_installed_rollout_preserves_pointer_drift(rollout):
    assert rollout.run("drift") == 2, rollout.output
    assert rollout.attempts == 1 and "active pointer changed while waiting" in rollout.output
    assert rollout.active.resolve() == rollout.runtimes[1]
    assert "active runtime is now" not in rollout.output


def test_installed_rollout_timeout_kills_only_its_gate_tree(rollout):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          start_new_session=True) as canary:
        try:
            assert rollout.run("timeout") == 2, rollout.output
            assert rollout.attempts == 1 and len(rollout.identities) == 2
            assert "timed out after 1s" in rollout.output
            assert rollout.active.resolve() == rollout.runtimes[0]
            assert canary.poll() is None
        finally:
            canary.terminate()
            canary.wait(timeout=5)


@pytest.fixture
def rollback(rollout):
    rollout.action, rollout.target_runtime = "rollback", rollout.runtimes[0]
    rollout.active.unlink()
    rollout.active.symlink_to(rollout.runtimes[1])
    rollout.version = rollout.cli("capabilities", binary=rollout.previous)["tool_version"]
    return rollout


def assert_rollback_ready(rollback):
    assert rollback.results[-1]["payload"]["decision"] == "CLIENTS_READY"
    assert rollback.results[-1]["returncode"] == 0
    assert len(rollback.consumer_calls.read_text().splitlines()) == rollback.attempts
    assert rollback.probes and all(
        probe["returncode"] == 0
        and probe["payload"]["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
        and probe["payload"]["tool_version"] == rollback.version for probe in rollback.probes
    )
    assert rollback.active.resolve() == rollback.target_runtime
    assert "post-activation verification passed" in rollback.output


def test_installed_rollback_succeeds_with_frozen_target_gate(rollback):
    assert rollback.run("stable") == 0, rollback.output
    assert rollback.attempts == len(rollback.results) == 1
    assert_rollback_ready(rollback)


def test_installed_rollback_retries_real_mutation_lock(rollback):
    assert rollback.run("lock") == 0, rollback.output
    assert rollback.attempts == len(rollback.results) == 2
    first, second = rollback.results
    assert first["argv"] == second["argv"]
    assert first["returncode"] == 2
    assert first["payload"]["blockers"] == [f"CONTROLLER_MUTATION_IN_FLIGHT:{rollback.repository.repo.name}"]
    assert_rollback_ready(rollback)


def test_installed_rollback_preserves_legacy_transaction_refusal(rollback):
    assert rollback.run("add") == 2, rollback.output
    assert rollback.attempts == len(rollback.results) == 1
    assert rollback.results[0]["payload"]["blockers"] == [
        f"CONTROLLER_TRANSACTION_UNFINISHED:{rollback.repository.repo.name}"
    ]
    diagnostic, = rollback.results[0]["payload"]["transaction_diagnostics"]
    assert diagnostic["error"]["code"] == "ROLLOUT_CHECK_INCOMPATIBLE"
    assert rollback.probes[0]["returncode"] == 0
    assert rollback.probes[0]["payload"]["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
    assert rollback.active.resolve() == rollback.runtimes[1]
    assert "active runtime is now" not in rollback.output


def test_installed_rollback_stops_on_real_recovery_error(rollback):
    rollback.interrupt_release()
    assert rollback.run("stable") == 2, rollback.output
    assert rollback.attempts == len(rollback.results) == 1
    diagnostic, = rollback.results[0]["payload"]["transaction_diagnostics"]
    assert diagnostic["error"]["code"] == "ROLLOUT_RELEASE_RECOVERY_REQUIRED"
    assert rollback.probes[0]["returncode"] == 2
    assert rollback.active.resolve() == rollback.runtimes[1]


def test_installed_rollback_enforces_consumer_minimum(rollback):
    available = rollback.cli("capabilities", binary=rollback.previous)["decision"]
    rollback.declare_consumer(available["host_handoff_continuation_version"] + 1)
    assert rollback.run("stable") == 2, rollback.output
    assert rollback.attempts == len(rollback.results) == 1
    # The old frozen gate accepts this declaration; the current activation entry must enforce it.
    assert rollback.results[0]["payload"]["decision"] == "CLIENTS_READY"
    assert rollback.results[0]["returncode"] == 0
    assert "CONSUMER_REQUIRES_CAPABILITY:" in rollback.output
    assert rollback.active.resolve() == rollback.runtimes[1]
    assert "active runtime is now" not in rollback.output


def test_installed_rollback_refuses_non_ancestor(rollout):
    rollout.action = "rollback"
    assert rollout.run("stable") == 2, rollout.output
    assert "rollback target is not an ancestor" in rollout.output
    assert rollout.attempts == 0 and not rollout.results
    assert rollout.active.resolve() == rollout.runtimes[0]


def test_installed_rollback_preserves_pointer_drift(rollback):
    assert rollback.run("drift") == 2, rollback.output
    assert rollback.attempts == 1 and "active pointer changed while waiting" in rollback.output
    assert rollback.active.resolve() == rollback.target_runtime
    assert "active runtime is now" not in rollback.output


def test_installed_rollback_timeout_kills_only_its_gate_tree(rollback):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          start_new_session=True) as canary:
        try:
            assert rollback.run("timeout") == 2, rollback.output
            assert rollback.attempts == 1 and len(rollback.identities) == 2
            assert "timed out after 1s" in rollback.output
            assert rollback.active.resolve() == rollback.runtimes[1]
            assert canary.poll() is None
        finally:
            canary.terminate()
            canary.wait(timeout=5)


def assert_real_postcheck_failure(rollout):
    failed = rollout.postchecks[0]
    assert failed["oid"] == rollout.target_runtime.parent.name and not failed["ok"]
    assert "activated Controller cannot read" in failed["reason"]
    query = rollout.postcheck_queries[0]
    assert query["returncode"] != 0 and query["payload"]["errors"]
    assert query["argv"][0] == str(rollout.target_runtime / "bin/worktree-controller")
    assert not rollout.hidden_git.exists()
    assert "post-activation verification failed" in rollout.output
    assert rollout.results[0]["payload"]["decision"] == "CLIENTS_READY"
    assert rollout.results[0]["returncode"] == 0


def assert_previous_gate(rollout):
    assert rollout.frozen_gates == [runtime.parent.name for runtime in reversed(rollout.runtimes)]
    assert rollout.results[-1]["argv"][2:4] == ["--target-version", rollout.runtimes[0].parent.name]
    assert rollout.probes[-1]["payload"]["tool_version"] == rollout.cli(
        "capabilities", binary=rollout.previous,
    )["tool_version"]


def test_installed_restoration_succeeds_with_frozen_previous_gate(rollout):
    assert rollout.run("restore-stable") == 3, rollout.output
    assert_real_postcheck_failure(rollout)
    assert_previous_gate(rollout)
    assert rollout.active.resolve() == rollout.runtimes[0]
    assert len(rollout.results) == len(rollout.consumer_calls.read_text().splitlines()) == 2
    assert rollout.results[-1]["payload"]["decision"] == "CLIENTS_READY"
    assert rollout.results[-1]["returncode"] == 0
    assert rollout.postchecks[-1] == {"kind": "post-check-after", "oid": rollout.runtimes[0].parent.name, "ok": True}
    assert rollout.postcheck_queries[-1]["returncode"] == 0
    assert "rolled back to the previously active runtime" in rollout.output


def test_installed_restoration_refuses_new_recovery_error(rollout):
    assert rollout.run("restore-recovery") == 2, rollout.output
    assert_real_postcheck_failure(rollout)
    assert_previous_gate(rollout)
    assert rollout.results[-1]["returncode"] == 2
    diagnostic, = rollout.results[-1]["payload"]["transaction_diagnostics"]
    assert diagnostic["error"]["code"] == "ROLLOUT_RELEASE_RECOVERY_REQUIRED"
    assert rollout.probes[-1]["returncode"] == 2
    assert rollout.active.resolve() == rollout.target_runtime and len(rollout.postchecks) == 1
    assert "rolled back to the previously active runtime" not in rollout.output


def test_installed_restoration_enforces_changed_consumer_minimum(rollout):
    assert rollout.cli("capabilities")["decision"]["canonical_refresh_version"] == 1
    assert rollout.cli("capabilities", binary=rollout.previous)["decision"].get("canonical_refresh_version", 0) == 0
    assert rollout.run("restore-consumer") == 2, rollout.output
    assert_real_postcheck_failure(rollout)
    assert_previous_gate(rollout)
    assert rollout.results[-1]["returncode"] == 0
    assert rollout.results[-1]["payload"]["decision"] == "CLIENTS_READY"
    assert "CONSUMER_REQUIRES_CAPABILITY:" in rollout.output and "canonical_refresh_version>=1" in rollout.output
    assert rollout.active.resolve() == rollout.target_runtime and len(rollout.postchecks) == 1


def test_installed_restoration_rechecks_published_catalog(rollout):
    rollout.configure_catalog()
    assert rollout.run("restore-catalog") == 2, rollout.output
    assert_real_postcheck_failure(rollout)
    assert len(rollout.results) == len(rollout.consumer_calls.read_text().splitlines()) == 1
    assert "TOOLCHAIN_RUNTIME_UNREVIEWED: unreviewed contract_version:" in rollout.output
    assert "TOOLCHAIN_CATALOG_UNPUBLISHED" not in rollout.output
    assert rollout.active.resolve() == rollout.target_runtime and len(rollout.postchecks) == 1


def test_installed_restoration_preserves_concurrent_pointer(rollout):
    assert rollout.run("restore-drift") == 2, rollout.output
    assert_real_postcheck_failure(rollout)
    assert_previous_gate(rollout)
    assert rollout.results[-1]["returncode"] == 0
    assert rollout.results[-1]["payload"]["decision"] == "CLIENTS_READY"
    assert rollout.active.resolve() == rollout.runtimes[0]
    assert "active runtime changed since preflight" in rollout.output
    assert rollout.output.count("active runtime is now ") == 1 and len(rollout.postchecks) == 1
    assert "rolled back to the previously active runtime" not in rollout.output


def test_rollout_selection_requires_every_selected_case(tmp_path):
    helpers = runpy.run_path(str(ROOT / "scripts/check-cross-tool"))
    selected = helpers["TESTS"] + helpers["ROLLOUT_TESTS"]
    cases = [f'<testcase name="{node.rsplit("::", 1)[1]}" />' for node in selected]
    report = tmp_path / "pytest.xml"
    report.write_text("<testsuite>" + "".join(cases[:-1]) + "</testsuite>")
    with pytest.raises(AssertionError, match="all selected cases to pass without skips"):
        helpers["verify_results"](report, selected)
    report.write_text("<testsuite>" + "".join(cases) + "</testsuite>")
    assert helpers["verify_results"](report, selected) == {"passed": len(selected), "failed": 0, "skipped": 0}


def test_invalid_rollout_pin_stops_before_creating_output(tmp_path):
    destination = tmp_path / "untouched"
    script = ROOT / "scripts/check-cross-tool"
    result = subprocess.run(
        [sys.executable, str(script), "--work-dir", str(destination),
         "--controller", str(script), "--controller-version", "fixture",
         "--git-finalizer", str(script), "--finalizer-version", "fixture",
         "--retirement-companion", str(script),
         "--rollout-previous-controller", str(tmp_path / "missing")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 2 and "rollout runtime pins" in result.stderr
    assert not destination.exists() and not result.stdout


if __name__ == "__main__":
    if sys.argv[1] != "activation-worker":
        raise SystemExit("unknown fixture worker")
    raise SystemExit(activation_worker(sys.argv[2]))
