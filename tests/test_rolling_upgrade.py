"""Rolling upgrade: a live executor session is not a reason to refuse activation.

These cover the read-only writer projection, the version-pinned entry point, and the gate that replaced private client assumptions.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from tests.test_write_admission import ROOT
from tests.test_writer_lease import POLICY_V6
from worktree_controller import writer_lease, writer_session

GATE = ROOT / "scripts/check-writer-rollout"
ACTIVATE = ROOT / "scripts/controller-activate"
LAUNCHER = ROOT / "scripts/controller-runtime"


def load_activate(*, toolchain_environment=False):
    loader = importlib.machinery.SourceFileLoader("controller_activate", str(ACTIVATE))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("controller_activate", loader)
    )
    loader.exec_module(module)
    if not toolchain_environment:
        module.TOOLCHAIN_COMPATIBILITY = None  # Unit fixtures declare their own catalog.
    return module


def test_activation_reads_original_bytes_despite_replacement_refs(tmp_path, monkeypatch):
    module = load_activate()
    with GitRepository(linked=0) as repository:
        contract = repository.repo / "tool_cli_contract.json"
        contract.write_text('{"original": true}\n')
        git(repository.repo, "add", "tool_cli_contract.json")
        git(repository.repo, "commit", "-m", "original contract")
        original = git(repository.repo, "rev-parse", "HEAD")
        original_blob = git(repository.repo, "rev-parse", "HEAD:tool_cli_contract.json")
        contract.write_text('{"replacement": true}\n')
        git(repository.repo, "commit", "-am", "replacement contract")
        replacement = git(repository.repo, "rev-parse", "HEAD")
        replacement_blob = git(repository.repo, "rev-parse", "HEAD:tool_cli_contract.json")
        git(repository.repo, "replace", original, replacement)
        git(repository.repo, "replace", original_blob, replacement_blob)
        assert "replacement" in git(repository.repo, "show", f"{original}:tool_cli_contract.json")
        monkeypatch.setattr(module, "CONTROLLER_REPO", repository.repo)
        expected = b'{"original": true}\n'
        assert module.release_contract_blob(original) == expected
        assert module.git_bytes("cat-file", "blob", original_blob) == expected
        source = module.frozen_source(tmp_path, original)
        assert (source / "tool_cli_contract.json").read_bytes() == expected


def release_dimension_fixture(tmp_path, monkeypatch):
    module = load_activate()
    frozen = (ROOT / "tool_cli_contract.json").read_bytes()
    contract = json.loads(frozen)
    package = tmp_path / "worktree_controller"
    package.mkdir()
    (package / "tool_cli_contract.json").write_bytes(frozen)
    monkeypatch.setattr(module, "release_contract_blob", lambda oid: frozen)
    decision = {
        key: value
        for key, value in contract.items()
        if key not in {"tool_version", "schema_version", "metadata_schema_version"}
        and key.endswith(("_version", "_versions"))
    }
    decision["code"] = "CAPABILITIES"
    payload = {"tool_version": contract["tool_version"], "errors": [], "decision": decision}
    return module, contract, package, payload


def test_release_gate_checks_four_independent_dimensions(tmp_path, monkeypatch):
    module, contract, package, payload = release_dimension_fixture(tmp_path, monkeypatch)
    module.verify_release_dimensions(
        "frozen", f"worktree-controller {contract['tool_version']}", payload, package
    )
    for key in ("contract_version", "capabilities_version", "storage_layout_version"):
        changed = deepcopy(payload)
        changed["decision"][key] += 1
        with pytest.raises(module.Abort, match=key):
            module.verify_release_dimensions(
                "frozen", f"worktree-controller {contract['tool_version']}", changed, package
            )
    with pytest.raises(module.Abort, match="software version"):
        module.verify_release_dimensions("frozen", "worktree-controller 1.10.0", payload, package)


def test_release_gate_checks_capability_presence_and_exact_package(tmp_path, monkeypatch):
    module, contract, package, payload = release_dimension_fixture(tmp_path, monkeypatch)
    changed = deepcopy(payload)
    del changed["decision"]["retained_archive_version"]
    with pytest.raises(module.Abort, match="retained_archive_version"):
        module.verify_release_dimensions(
            "frozen", f"worktree-controller {contract['tool_version']}", changed, package
        )
    (package / "tool_cli_contract.json").write_text("{}")
    with pytest.raises(module.Abort, match="installed release contract"):
        module.verify_release_dimensions(
            "frozen", f"worktree-controller {contract['tool_version']}", payload, package
        )


@pytest.fixture
def toolchain_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    module, contract, _package, payload = release_dimension_fixture(tmp_path, monkeypatch)
    with GitRepository(linked=0) as repository:
        remote = repository.root / "origin.git"
        git(repository.root, "init", "--bare", str(remote))
        git(repository.repo, "remote", "add", "origin", str(remote))
        catalog = repository.repo / "toolchain_compatibility.json"
        document = {
            "schema_version": 1,
            "toolchain_contract_version": 6,
            "runtime_requirements": {"worktree-controller": {
                "cli_contract_versions": [contract["contract_version"]],
                "storage_layout_versions": [contract["storage_layout_version"]],
                "capability_versions": {"capabilities_version": 1, "writer_session_version": 2},
            }},
        }

        def publish(value=None):
            value = document if value is None else value
            catalog.write_text(value if isinstance(value, str) else json.dumps(value) + "\n")
            git(repository.repo, "add", catalog.name)
            git(repository.repo, "commit", "--allow-empty", "-m", "review catalog")
            git(repository.repo, "push", "origin", "main")

        publish()
        module.TOOLCHAIN_COMPATIBILITY = catalog
        module.ROOT = tmp_path / "installation"
        module.VERSIONS = module.ROOT / "versions"
        module.ACTIVE = module.ROOT / "active"
        module.ACTIVATION_LOCK = module.ROOT / "activation.lock"
        module.LOG_DIR = tmp_path / "logs"
        previous = module.VERSIONS / ("a" * 40) / "worktree-controller"
        runtime = module.VERSIONS / ("b" * 40) / "worktree-controller"
        previous.mkdir(parents=True)
        runtime.mkdir(parents=True)
        module.ACTIVE.symlink_to(previous)
        module.GIT_SOURCE = "file:///fixture/source"
        monkeypatch.setattr(module, "release_contract", lambda oid: contract)
        monkeypatch.setattr(module, "verify_runtime", lambda *args, **kwargs: "verified fixture")
        monkeypatch.setattr(module, "verify_release_lineage", lambda *args: None)
        monkeypatch.setattr(module, "verify_rollback_lineage", lambda *args: None)
        monkeypatch.setattr(module, "verify_resource_compatibility", lambda *args: None)
        monkeypatch.setattr(module, "temporary_directory", lambda *args: tmp_path / "unused")
        monkeypatch.setattr(module, "frozen_source", lambda *args: tmp_path / "source")
        monkeypatch.setattr(module, "stage", lambda *args: runtime)
        monkeypatch.setattr(module, "preflight", lambda *args: None)
        monkeypatch.setattr(module, "post_activation_check", lambda *args: None)
        monkeypatch.setattr(module, "bind_runtime_source", lambda *args: None)
        original_run = module.run

        def run(command, **kwargs):
            if command[-2:] == ["capabilities", "--json"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            return original_run(command, **kwargs)

        monkeypatch.setattr(module, "run", run)
        yield SimpleNamespace(module=module, catalog=catalog, document=document,
                              requirements=document["runtime_requirements"]["worktree-controller"],
                              runtime=runtime, previous=previous, contract=contract,
                              payload=payload, publish=publish, repository=repository)


def toolchain_command(item, monkeypatch, action="activate"):
    monkeypatch.setattr(sys, "argv", ["controller-activate", action, "--version", "b" * 40,
                                     "--toolchain-compatibility", str(item.catalog)])
    return item.module.main()


@pytest.mark.parametrize("action", ["stage", "bootstrap", "activate", "rollback"])
def test_toolchain_accepts_a_reviewed_target_on_each_deployment_path(toolchain_candidate, monkeypatch, action):
    item = toolchain_candidate
    if action == "bootstrap":
        item.module.ACTIVE.unlink()
    assert toolchain_command(item, monkeypatch, action) == 0
    assert item.module.ACTIVE.resolve() == (item.previous if action == "stage" else item.runtime)


@pytest.mark.parametrize("action", ["stage", "bootstrap", "activate", "rollback"])
def test_toolchain_refuses_an_unreviewed_target_before_switching(toolchain_candidate, monkeypatch, action, capsys):
    item = toolchain_candidate
    item.requirements["cli_contract_versions"] = [item.contract["contract_version"] - 1]
    item.publish()
    if action == "bootstrap":
        item.module.ACTIVE.unlink()
    monkeypatch.setattr(item.module, "preflight", lambda *args: pytest.fail("must refuse before rollout"))
    assert toolchain_command(item, monkeypatch, action) == 2
    assert "TOOLCHAIN_RUNTIME_UNREVIEWED: unreviewed contract_version" in capsys.readouterr().out
    if action == "bootstrap":
        assert not item.module.ACTIVE.is_symlink()
    else:
        assert item.module.ACTIVE.resolve() == item.previous
    assert not (item.module.ROOT / ".active.pending").exists()


def test_toolchain_accepts_explicitly_reviewed_future_cli_without_inferring_a_range(toolchain_candidate):
    item = toolchain_candidate
    item.contract["contract_version"] += 1
    item.payload["decision"]["contract_version"] += 1
    with pytest.raises(item.module.Abort, match="unreviewed contract_version"):
        item.module.switch(item.runtime, expected=item.previous)
    item.requirements["cli_contract_versions"].append(item.contract["contract_version"])
    item.publish()
    item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.runtime


@pytest.mark.parametrize("dimension", ["storage_layout_versions", "capability_versions"])
def test_toolchain_checks_storage_and_capabilities_independently(toolchain_candidate, dimension):
    item = toolchain_candidate
    item.requirements[dimension] = [99] if dimension == "storage_layout_versions" else {"capabilities_version": 99}
    item.publish()
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_RUNTIME_UNREVIEWED"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("change", ["missing", "boolean", "frozen", "software", "errors"])
def test_toolchain_refuses_inconsistent_staged_evidence(toolchain_candidate, change):
    item = toolchain_candidate
    if change == "missing":
        del item.payload["decision"]["writer_session_version"]
    elif change == "boolean":
        item.payload["decision"]["capabilities_version"] = True
    elif change == "frozen":
        item.contract["writer_session_version"] += 1
    elif change == "software":
        item.payload["tool_version"] = "0.0.0"
    else:
        item.payload["errors"] = [{"code": "REFUSED"}]
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_RUNTIME_INVALID"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("value", [
    "{", '{"schema_version":1,"schema_version":1}', [],
    {"schema_version": True, "toolchain_contract_version": 6},
    {"schema_version": 1, "toolchain_contract_version": 7},
    {"schema_version": 1, "toolchain_contract_version": 6, "runtime_requirements": []},
])
def test_toolchain_refuses_invalid_catalogs_without_switching(toolchain_candidate, value):
    item = toolchain_candidate
    item.publish(value)
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_CATALOG_INVALID"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("key,value", [
    ("cli_contract_versions", [True]), ("cli_contract_versions", []),
    ("cli_contract_versions", [51, 51]), ("storage_layout_versions", ["4"]),
    ("capability_versions", {}), ("capability_versions", {"capabilities_version": True}),
    ("capability_versions", {"capabilities_version": 1, "not-a-version": 1}),
])
def test_toolchain_refuses_malformed_requirements(toolchain_candidate, key, value):
    item = toolchain_candidate
    item.requirements[key] = value
    item.publish()
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_CATALOG_INVALID"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("condition", ["missing", "untracked", "dirty", "unpublished", "branch", "unreachable"])
def test_toolchain_requires_an_available_published_owner_catalog(toolchain_candidate, monkeypatch, condition):
    item = toolchain_candidate
    if condition == "missing":
        item.catalog.unlink()
    elif condition == "untracked":
        item.catalog = item.repository.repo / "untracked.json"
        item.catalog.write_text(json.dumps(item.document))
        item.module.TOOLCHAIN_COMPATIBILITY = item.catalog
    elif condition == "dirty":
        item.catalog.write_text(item.catalog.read_text() + "\n")
    elif condition == "unpublished":
        git(item.repository.repo, "commit", "--allow-empty", "-m", "not published")
    elif condition == "branch":
        git(item.repository.repo, "checkout", "-b", "candidate")
    else:
        original_run = item.module.run
        def run(command, **kwargs):
            if "ls-remote" in command:
                raise item.module.Abort("fixture inaccessible remote")
            return original_run(command, **kwargs)
        monkeypatch.setattr(item.module, "run", run)
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_CATALOG_(UNAVAILABLE|UNPUBLISHED)"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


def test_toolchain_observation_refuses_concurrent_catalog_change(toolchain_candidate, monkeypatch):
    item = toolchain_candidate
    original_run = item.module.run
    def run(command, **kwargs):
        result = original_run(command, **kwargs)
        if "ls-remote" in command:
            item.catalog.write_text(item.catalog.read_text() + "\n")
        return result
    monkeypatch.setattr(item.module, "run", run)
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_CATALOG_CHANGED"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


def test_toolchain_rechecks_the_owner_at_the_switch_after_a_ready_gate(toolchain_candidate, monkeypatch):
    item = toolchain_candidate
    def gate(*args):
        item.requirements["cli_contract_versions"] = [item.contract["contract_version"] - 1]
        item.publish()
    monkeypatch.setattr(item.module, "preflight", gate)
    assert toolchain_command(item, monkeypatch) == 2
    assert item.module.ACTIVE.resolve() == item.previous


def test_toolchain_environment_declares_the_catalog_without_selecting_another_runtime(tmp_path, monkeypatch):
    path = tmp_path / "owner/toolchain_compatibility.json"
    monkeypatch.setenv("CONTROLLER_ROLLOUT_TOOLCHAIN_COMPATIBILITY", str(path))
    module = load_activate(toolchain_environment=True)
    assert path == module.TOOLCHAIN_COMPATIBILITY


def test_toolchain_uses_exact_catalog_bytes_with_crlf(toolchain_candidate):
    item = toolchain_candidate
    item.publish(json.dumps(item.document, indent=2).replace("\n", "\r\n") + "\r\n")
    item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.runtime


def test_toolchain_read_failure_is_a_bounded_refusal(toolchain_candidate, monkeypatch):
    item = toolchain_candidate
    original_read = Path.read_bytes

    def read(path):
        if path == item.catalog:
            raise PermissionError("fixture private read error")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_CATALOG_UNAVAILABLE") as failure:
        item.module.switch(item.runtime, expected=item.previous)
    assert "private read error" not in str(failure.value)
    assert item.module.ACTIVE.resolve() == item.previous


def test_toolchain_cli_allowlist_cannot_be_overridden_by_capability_binding(toolchain_candidate):
    item = toolchain_candidate
    item.requirements["cli_contract_versions"] = [item.contract["contract_version"] - 1]
    item.requirements["capability_versions"]["contract_version"] = item.contract["contract_version"]
    item.publish()
    with pytest.raises(item.module.Abort, match="unreviewed contract_version"):
        item.module.switch(item.runtime, expected=item.previous)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("condition", ["malformed", "unavailable"])
def test_toolchain_staged_read_failure_keeps_the_active_runtime(toolchain_candidate, monkeypatch, condition):
    item = toolchain_candidate
    original_run = item.module.run

    def run(command, **kwargs):
        if command[-2:] == ["capabilities", "--json"]:
            if condition == "unavailable":
                raise item.module.Abort("fixture private runtime error")
            return subprocess.CompletedProcess(command, 0, "{", "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(item.module, "run", run)
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_RUNTIME_UNAVAILABLE") as failure:
        item.module.switch(item.runtime, expected=item.previous)
    assert "private runtime error" not in str(failure.value)
    assert item.module.ACTIVE.resolve() == item.previous


@pytest.mark.parametrize("review_previous", [True, False])
def test_toolchain_automatic_restoration_requires_a_reviewed_previous_runtime(toolchain_candidate, monkeypatch, review_previous):
    item = toolchain_candidate
    previous_contract = deepcopy(item.contract)
    previous_contract["contract_version"] -= 1
    previous_contract["tool_version"] = "1.19.12"
    previous_payload = deepcopy(item.payload)
    previous_payload["decision"]["contract_version"] -= 1
    previous_payload["tool_version"] = previous_contract["tool_version"]
    monkeypatch.setattr(item.module, "release_contract", lambda oid: (
        item.contract if oid == "b" * 40 else previous_contract
    ))
    original_run = item.module.run

    def run(command, **kwargs):
        if command == [item.previous / "bin/worktree-controller", "capabilities", "--json"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(previous_payload), "")
        return original_run(command, **kwargs)

    def postcheck(runtime, oid):
        if runtime == item.runtime:
            raise item.module.Abort("fixture post-activation failure")

    monkeypatch.setattr(item.module, "run", run)
    monkeypatch.setattr(item.module, "post_activation_check", postcheck)
    if review_previous:
        item.requirements["cli_contract_versions"].append(previous_contract["contract_version"])
        item.publish()
    assert toolchain_command(item, monkeypatch) == (3 if review_previous else 2)
    assert item.module.ACTIVE.resolve() == (item.previous if review_previous else item.runtime)


def test_toolchain_layout_rollback_refuses_before_mutating_layout(toolchain_candidate, monkeypatch):
    item = toolchain_candidate
    item.requirements["cli_contract_versions"] = [item.contract["contract_version"] - 1]
    item.publish()
    migration = SimpleNamespace(
        document={"runtimes": {"before": str(item.previous), "after": str(item.runtime),
                              "before_oid": "a" * 40}},
        rollback=lambda: pytest.fail("an unreviewed runtime cannot change layout"),
    )
    monkeypatch.setattr(item.module.LayoutMigration, "load", lambda *args: migration)
    monkeypatch.setattr(item.module, "previous_contract", lambda oid: {"entrypoint": "worktree-controller"})
    arguments = SimpleNamespace(action="rollback", layout_receipt=Path("unused"), layout_receipt_sha256="unused")
    with pytest.raises(item.module.Abort, match="TOOLCHAIN_RUNTIME_UNREVIEWED"):
        item.module.recover_layout(arguments)
    assert item.module.ACTIVE.resolve() == item.previous


def test_release_lineage_rejects_isolated_candidate(monkeypatch):
    from types import SimpleNamespace

    module = load_activate()
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(module.Abort, match="not a descendant"):
        module.verify_release_lineage("active", "isolated")
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, "verify_release_progression", lambda *args: None)
    module.verify_release_lineage("active", "successor")


def test_rollback_lineage_rejects_unrelated_candidate(monkeypatch):
    from types import SimpleNamespace

    module = load_activate()
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(module.Abort, match="not an ancestor"):
        module.verify_rollback_lineage("active", "isolated")
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    module.verify_rollback_lineage("active", "previous")


def test_release_progression_rejects_capability_loss_with_unchanged_cli_contract(monkeypatch):
    module = load_activate()
    previous = {
        "tool_version": "1.9.4",
        "contract_version": 35,
        "retained_archive_version": 1,
        "storage_layout": {"version": 3},
    }
    isolated = {
        "tool_version": "1.10.0",
        "contract_version": 35,
        "storage_layout": {"version": 4},
    }
    monkeypatch.setattr(
        module, "release_contract", lambda oid: previous if oid == "active" else isolated
    )
    with pytest.raises(module.Abort, match="retained_archive_version"):
        module.verify_release_progression("active", "isolated")


@pytest.fixture
def task():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, payload = acquire_one(
            repository, task="rolling", authority="rolling", owner="rolling-owner"
        )
        yield repository, path, payload["decision"]["allocation_id"]


def holder(path, *, client_id="executor-a", session_id=None, run_id=None, repository=None):
    writer_lease.acquire_writer_lease(
        repository.repo,
        target=path,
        client_id=client_id,
        holder_pid=os.getpid(),
        session_id=session_id,
        run_id=run_id,
    )


# --- the read-only projection ----------------------------------------------


def test_projection_reports_the_allocation_without_a_lease(task):
    _repository, path, allocation = task
    decision = writer_session.describe(path)
    assert decision["code"] == "WRITER_SESSION_DESCRIBED"
    assert decision["admission"] == "NOT_APPLICABLE"
    assert decision["allocation_id"] == allocation
    assert decision["writer_requirement"] == "REQUIRED"
    assert decision["lease"] is None
    assert decision["holder_matches"] is None


def test_projection_reports_the_complete_recorded_identity(task):
    repository, path, allocation = task
    holder(path, repository=repository, session_id="s-1", run_id="r-1")
    decision = writer_session.describe(path)
    assert decision["allocation_id"] == allocation
    assert decision["lease"]["holder"] == {
        "client_id": "executor-a",
        "pid": os.getpid(),
        "boot_id": writer_lease.boot_id(),
        "start_ticks": writer_lease.start_ticks(os.getpid()),
        "session_id": "s-1",
        "run_id": "r-1",
    }


def test_projection_agrees_only_with_the_recorded_holder(task):
    repository, path, _ = task
    holder(path, repository=repository, session_id="s-1")
    assert writer_session.describe(path, selector=str(os.getpid()))["holder_matches"] is True
    assert writer_session.describe(path, selector="1")["holder_matches"] is False
    assert writer_session.describe(path, selector="ancestor:no-such")["holder_matches"] is False
    assert writer_session.describe(path, selector="not-a-selector")["holder_matches"] is False


def test_projection_grants_nothing_and_mutates_nothing(task, monkeypatch):
    repository, path, allocation = task
    holder(path, repository=repository)
    from tests.test_retention_admission import _store

    before = _store(repository).load()
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v1")
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", "{}")
    assert writer_session.main(["describe", str(path)]) == 0
    after = _store(repository).load()
    assert after.writer_leases[allocation] == before.writer_leases[allocation]
    assert after.records[allocation] == before.records[allocation]


def test_projection_is_not_applicable_outside_governance(tmp_path):
    assert writer_session.describe(tmp_path)["code"] == "OUTSIDE_CONTROLLER_GOVERNANCE"


def test_projection_reports_a_dedicated_role_requirement(task):
    repository, _path, _ = task
    decision = writer_session.describe(repository.repo)
    assert decision["writer_requirement"] == "NOT_APPLICABLE"
    assert decision["lease"] is None


# --- version pinning --------------------------------------------------------


def pinned_home(tmp_path, active_name="one"):
    home = tmp_path / "pinned"
    versions = home / ".local/share/worktree-controller/versions"
    for name, reported in (("one", "1.1.1"), ("two", "2.2.2")):
        binary = versions / name / "worktree-controller/bin/worktree-controller"
        binary.parent.mkdir(parents=True)
        binary.write_text(f'#!/bin/sh\nprintf "worktree-controller {reported}\\n"\n')
        binary.chmod(0o755)
    (home / ".local/bin").mkdir(parents=True)
    active = home / ".local/share/worktree-controller/active"
    active.symlink_to(versions / active_name / "worktree-controller")
    return home


def launch(home, name="worktree-controller", arguments=()):
    entry = home / ".local/bin" / name
    entry.write_text(LAUNCHER.read_text())
    entry.chmod(0o755)
    return subprocess.run(
        [str(entry), *arguments],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_launcher_resolves_the_active_runtime(tmp_path):
    home = pinned_home(tmp_path)
    assert launch(home).stdout.strip() == "worktree-controller 1.1.1"


def test_launcher_follows_a_repointed_active_runtime(tmp_path):
    home = pinned_home(tmp_path)
    load_activate()  # importable without side effects
    active = home / ".local/share/worktree-controller/active"
    active.unlink()
    active.symlink_to(home / ".local/share/worktree-controller/versions/two/worktree-controller")
    assert launch(home).stdout.strip() == "worktree-controller 2.2.2"


def test_launcher_refuses_an_unsupported_entry_point(tmp_path):
    home = pinned_home(tmp_path)
    assert launch(home, name="worktree-controller-other").returncode == 2


def test_one_invocation_never_spans_two_versions(tmp_path):
    """A command already running finishes on the version it started with."""
    home = pinned_home(tmp_path)
    versions = home / ".local/share/worktree-controller/versions"
    started = tmp_path / "resolved"
    slow = versions / "slow/worktree-controller/bin/worktree-controller"
    slow.parent.mkdir(parents=True)
    slow.write_text(f'#!/bin/sh\ntouch {started}\nsleep 3\nprintf "worktree-controller slow\\n"\n')
    slow.chmod(0o755)
    active = home / ".local/share/worktree-controller/active"
    active.unlink()
    active.symlink_to(versions / "slow/worktree-controller")
    entry = home / ".local/bin/worktree-controller"
    entry.write_text(LAUNCHER.read_text())
    entry.chmod(0o755)
    running = subprocess.Popen(
        [str(entry), "--version"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        text=True,
    )
    # Only flip once this invocation has provably resolved and started running.
    for _attempt in range(600):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists(), "the pinned invocation never started"
    module = load_activate()
    module.ROOT = home / ".local/share/worktree-controller"
    module.ACTIVE = active
    module.ACTIVATION_LOCK = module.ROOT / "activation.lock"
    module.switch(versions / "two/worktree-controller")
    assert running.communicate(timeout=30)[0].strip() == "worktree-controller slow"
    assert launch(home).stdout.strip() == "worktree-controller 2.2.2"


def test_switch_is_atomic_and_reports_the_active_version(tmp_path):
    home = pinned_home(tmp_path)
    module = load_activate()
    versions = home / ".local/share/worktree-controller/versions"
    module.ROOT = home / ".local/share/worktree-controller"
    module.VERSIONS = versions
    module.ACTIVE = module.ROOT / "active"
    module.ACTIVATION_LOCK = module.ROOT / "activation.lock"
    module.switch(versions / "two/worktree-controller")
    assert module.active_version() == "two"
    assert module.ACTIVE.is_symlink()
    assert not (module.ROOT / ".active.pending").exists()


@pytest.mark.parametrize("failure", [None, "gate", "smoke", "pointer-race"])
def test_first_bootstrap_is_gated_and_restores_only_its_own_pointer(tmp_path, monkeypatch, failure):
    module = load_activate()
    home = pinned_home(tmp_path)
    module.ROOT = home / ".local/share/worktree-controller"
    module.ACTIVE = module.ROOT / "active"
    module.ACTIVATION_LOCK = module.ROOT / "activation.lock"
    module.ACTIVE.unlink()
    runtime = module.ROOT / "versions/two/worktree-controller"
    previous = module.ROOT / "versions/one/worktree-controller"
    calls = []

    def preflight(*args):
        calls.append("gate")
        assert not module.ACTIVE.exists()
        if failure == "gate":
            raise module.Abort("fixture gate blocker")
        if failure == "pointer-race":
            module.ACTIVE.symlink_to(previous)

    def smoke(*args):
        calls.append("smoke")
        assert module.ACTIVE.resolve() == runtime
        if failure == "smoke":
            raise module.Abort("fixture smoke failure")

    monkeypatch.setattr(module, "preflight", preflight)
    monkeypatch.setattr(module, "post_activation_check", smoke)
    monkeypatch.setattr(module, "verify_resource_compatibility", lambda *args: None)
    if failure:
        with pytest.raises(module.Abort):
            module.bootstrap_runtime(tmp_path, runtime, "two", [])
        if failure == "pointer-race":
            assert module.ACTIVE.resolve() == previous
        else:
            assert not module.ACTIVE.exists() and not module.ACTIVE.is_symlink()
    else:
        module.bootstrap_runtime(tmp_path, runtime, "two", [])
        assert module.ACTIVE.resolve() == runtime
    assert calls == (["gate", "smoke"] if failure in {None, "smoke"} else ["gate"])


def test_bootstrap_refuses_an_existing_or_dangling_active_pointer(tmp_path, monkeypatch):
    module = load_activate()
    module.ACTIVE = tmp_path / "active"
    module.ACTIVE.symlink_to(tmp_path / "missing-runtime")
    monkeypatch.setattr(module, "preflight", lambda *args: pytest.fail("must not run the gate"))
    with pytest.raises(module.Abort, match="absent active pointer"):
        module.bootstrap_runtime(tmp_path, tmp_path, "target", [])


def test_rollout_resolves_the_shared_namespace_from_a_linked_checkout(task, tmp_path):
    _repository, path, _allocation = task
    payload, code = run_gate(gate_home(tmp_path), path)
    assert code == 0 and payload["decision"] == "CLIENTS_READY", payload


def test_rollout_resolves_a_separate_git_directory(tmp_path):
    from tests.test_shadow import git

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        git(repository.repo, "init", "--separate-git-dir", str(repository.root / "git-state"))
        # Git worktree list needs this explicit canonical mapping for a separate
        # common directory; the rollout gate must then use that common directory.
        git(repository.repo, "config", "core.worktree", str(repository.repo))
        adopt_canonical(repository)
        payload, code = run_gate(gate_home(tmp_path), repository.repo)
        assert code == 0 and payload["decision"] == "CLIENTS_READY", payload


@pytest.mark.parametrize("previous_compatible", [False, True])
def test_failed_activation_rechecks_current_state_before_rollback(tmp_path, monkeypatch, previous_compatible):
    module = load_activate()
    module.GIT_SOURCE = "file:///fixture/source"
    home = pinned_home(tmp_path)
    module.ROOT = home / ".local/share/worktree-controller"
    module.VERSIONS = module.ROOT / "versions"
    module.ACTIVE = module.ROOT / "active"
    module.ACTIVATION_LOCK = module.ROOT / "activation.lock"
    module.LOG_DIR = tmp_path / "logs"
    monkeypatch.setattr(module, "temporary_directory", lambda prefix: tmp_path / "unused-work")
    monkeypatch.setattr(module, "frozen_source", lambda work, oid: work / oid)
    monkeypatch.setattr(module, "verify_runtime", lambda *args: "fixture")
    monkeypatch.setattr(module, "verify_release_lineage", lambda *args: None)
    monkeypatch.setattr(module, "verify_resource_compatibility", lambda runtime: None)
    checks = []
    def preflight(source, consumers, oid, receipt, digest):
        checks.append(("gate", oid))
        assert source.name == oid
        if oid == "one" and not previous_compatible:
            raise module.Abort("previous runtime cannot read the current transaction")
    def postcheck(runtime, oid):
        checks.append(("postcheck", oid))
        if oid == "two":
            raise module.Abort("candidate postcheck failed")
    monkeypatch.setattr(module, "preflight", preflight)
    monkeypatch.setattr(module, "post_activation_check", postcheck)
    monkeypatch.setattr(sys, "argv", ["controller-activate", "activate", "--version", "two"])
    code = module.main()
    assert checks[:3] == [("gate", "two"), ("postcheck", "two"), ("gate", "one")]
    if previous_compatible:
        assert code == 3 and module.active_version() == "one"
        assert checks[-1] == ("postcheck", "one")
    else:
        assert code == 2 and module.active_version() == "two"
        assert len(checks) == 3  # No unverified downgrade and no old-runtime postcheck.


def test_temporary_directory_reads_the_helper_record(tmp_path, monkeypatch):
    """The helper answers with JSON; the path comes from the record, not the reply."""
    module = load_activate()
    module.TEMP_HELPER = tmp_path / "site-helper"
    # The site helper returns /tmp paths even when pytest's basetemp is elsewhere.
    with TemporaryDirectory(prefix="controller-helper-record-", dir="/tmp") as temporary_root:
        created = Path(temporary_root) / "made"
        created.mkdir()
        replies = {
            "good": json.dumps({"command": "create", "ok": True, "path": str(created)}),
            "noise": "warning: something\n" + json.dumps({"ok": True, "path": str(created)}),
        }

        class Reply:
            def __init__(self, text):
                self.stdout = text

        for key in ("good", "noise"):
            monkeypatch.setattr(module, "run", lambda *a, _t=replies[key], **k: Reply(_t))
            assert module.temporary_directory("probe") == created


@pytest.mark.parametrize(
    "reply",
    ["", "not json", json.dumps({"ok": True}), json.dumps({"path": "relative/path"})],
)
def test_temporary_directory_refuses_an_unusable_reply(monkeypatch, reply):
    module = load_activate()
    module.TEMP_HELPER = Path("/absolute/site-helper")

    class Reply:
        stdout = reply

    monkeypatch.setattr(module, "run", lambda *a, **k: Reply())
    with pytest.raises(module.Abort):
        module.temporary_directory("probe")


def test_temporary_directory_refuses_a_path_that_was_not_created(tmp_path, monkeypatch):
    module = load_activate()
    module.TEMP_HELPER = tmp_path / "site-helper"

    class Reply:
        stdout = json.dumps({"ok": True, "path": "/tmp/never-created-by-this-test-12345"})

    monkeypatch.setattr(module, "run", lambda *a, **k: Reply())
    with pytest.raises(module.Abort):
        module.temporary_directory("probe")


def test_rollout_defaults_do_not_require_private_site_resources(monkeypatch):
    for name in (
        "CONTROLLER_ROLLOUT_REPO",
        "CONTROLLER_ROLLOUT_GIT_SOURCE",
        "CONTROLLER_ROLLOUT_INDEX",
        "CONTROLLER_ROLLOUT_GOVERNED",
        "CONTROLLER_ROLLOUT_TEMP_HELPER",
    ):
        monkeypatch.delenv(name, raising=False)
    module = load_activate()
    assert module.CONTROLLER_REPO == ROOT
    assert module.GIT_SOURCE == ""
    assert module.DEFAULT_INDEX == ""
    assert module.GOVERNED == ()
    assert module.TEMP_HELPER is None


def test_rollout_temporary_directory_has_a_standalone_fallback(monkeypatch):
    module = load_activate()
    module.TEMP_HELPER = None
    directory = module.temporary_directory("controller-test")
    try:
        assert directory.is_dir()
        assert directory in module._created_temp_dirs
    finally:
        module.cleanup_temporary_directory(directory)
    assert not directory.exists()


# --- the gate that replaced private client assumptions ------------------------------


def gate_home(tmp_path, *, staged="target"):
    home = tmp_path / "gate-home"
    (home / "bin").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    expected = (ROOT / "scripts/controller-runtime").read_bytes()
    for path in [
        home / "bin/controller-runtime",
        *(
            home / ".local/bin" / name
            for name in ("worktree-controller", "worktree-session", "worktree-session-end")
        ),
    ]:
        path.write_bytes(expected)
        path.chmod(0o755)
    binary = (
        home
        / ".local/share/worktree-controller/versions"
        / staged
        / "worktree-controller/bin/worktree-controller"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nprintf "worktree-controller 9.9.9\\n"\n')
    binary.chmod(0o755)
    capability = home / "consumer-capabilities"
    capability.write_text('#!/bin/sh\ncat "$HOME/consumer.json"\n')
    capability.chmod(0o755)
    (home / "consumer.json").write_text(
        json.dumps(
            {
                "writer_session_version": 2,
                "host_managed_writer_session_version": 2,
                "holder_aware_write_guard": True,
                "active_writer_sessions": 0,
            }
        )
    )
    return home


def run_gate(home, repository, *, version="target", processes=()):
    loader = importlib.machinery.SourceFileLoader("rollout_gate_fixture", str(GATE))
    gate = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(gate)
    # A unit fixture must not race real host maintenance or ignore a real live
    # invocation. Exercise the actual gate with an explicit process inventory.
    with pytest.MonkeyPatch.context() as patch, redirect_stdout(io.StringIO()) as output:
        patch.setenv("HOME", str(home))
        patch.setattr(gate, "own_processes", lambda: iter(processes))
        patch.setattr(sys, "argv", [
            str(GATE),
            "--home",
            str(home),
            "--consumer",
            str(home / "consumer-capabilities"),
            "--repo",
            str(repository),
            "--target-version",
            version,
        ])
        code = gate.main()
    return json.loads(output.getvalue()), code


def test_a_live_client_id_process_alone_never_blocks(task, tmp_path):
    repository, _path, _ = task
    payload, code = run_gate(
        gate_home(tmp_path), repository.repo, processes=((Path("/proc/98765"), "codex"),)
    )
    assert payload["decision"] == "CLIENTS_READY", payload
    assert code == 0
    assert not any(blocker.startswith("PROCESS_STILL_RUNNING") for blocker in payload["blockers"])


def test_gate_refuses_an_unobservable_process(task, tmp_path):
    # Without a process identity the gate cannot prove the switch is safe, so an
    # unobservable process still blocks (fail closed).
    repository, _, _ = task
    payload, code = run_gate(
        gate_home(tmp_path), repository.repo, processes=((Path("/proc/98765"), None),)
    )
    assert code == 2 and "PROCESS_SCAN_UNAVAILABLE:98765" in payload["blockers"]


def test_a_live_controller_invocation_is_inventory_not_a_blocker(task, tmp_path):
    # The pinned entry point resolves the active symlink once per invocation and
    # the switch is an atomic replace, so a running command finishes on the
    # version it started; a genuine mutation is fenced separately by its
    # exclusive repository lock. A live invocation is therefore inventory, never
    # a process-count blocker.
    repository, _, _ = task
    payload, code = run_gate(
        gate_home(tmp_path), repository.repo, processes=((Path("/proc/98765"), "worktree-contro"),)
    )
    assert code == 0 and payload["decision"] == "CLIENTS_READY", payload
    assert not any(blocker.startswith("LIVE_CONTROLLER_INVOCATION") for blocker in payload["blockers"])
    assert [row["pid"] for row in payload["live_controller_invocations"]] == ["98765"]


def test_a_shared_accounting_lock_does_not_block(task, tmp_path):
    # admission-queue accounting and observation hold a shared flock and leave
    # repository metadata unchanged (1.28.1), so they must not read as a
    # mutation; only an exclusive holder fences the switch.
    repository, _, _ = task
    lock = repository.repo / ".git/worktree-controller/v1/repo.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            payload, code = run_gate(gate_home(tmp_path), repository.repo)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert code == 0 and payload["decision"] == "CLIENTS_READY", payload
    assert not any(blocker.startswith("CONTROLLER_MUTATION_IN_FLIGHT") for blocker in payload["blockers"])


def test_an_exclusive_mutation_lock_blocks(task, tmp_path):
    # A genuine mutation holds the exclusive repository lock; the shared probe
    # fails against it, so the gate still refuses the switch.
    repository, _, _ = task
    lock = repository.repo / ".git/worktree-controller/v1/repo.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            payload, code = run_gate(gate_home(tmp_path), repository.repo)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert code == 2
    assert f"CONTROLLER_MUTATION_IN_FLIGHT:{repository.repo.name}" in payload["blockers"]


def test_typed_cleanup_fences_activation_before_any_journal_exists(task, tmp_path):
    from worktree_controller.store import MetadataStore

    repository, _, _ = task
    store = MetadataStore(repository.repo / ".git")
    with store.cleanup_lock():
        assert not list(store.transactions_dir.glob("*.json"))
        payload, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 2
    assert f"CONTROLLER_MUTATION_IN_FLIGHT:{repository.repo.name}" in payload["blockers"]


def test_idle_cleanup_journal_directory_does_not_fence_activation(task, tmp_path):
    from worktree_controller.store import MetadataStore

    repository, _, _ = task
    store = MetadataStore(repository.repo / ".git")
    with store.cleanup_lock():
        pass
    payload, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 0 and payload["decision"] == "CLIENTS_READY", payload


def test_a_compatible_surviving_writer_is_reported_not_blocked(task, tmp_path):
    repository, path, allocation = task
    holder(path, repository=repository)
    payload, _code = run_gate(gate_home(tmp_path), repository.repo)
    assert payload["decision"] == "CLIENTS_READY"
    assert [entry["allocation_id"] for entry in payload["surviving_writers"]] == [allocation]


def test_a_surviving_writer_with_an_older_schema_blocks(task, tmp_path):
    repository, path, allocation = task
    holder(path, repository=repository, client_id="executor-b")
    lease = repository.repo / f".git/worktree-controller/v1/writer-leases/{allocation}.json"
    data = json.loads(lease.read_text())
    data["schema_version"] = 1
    lease.write_text(json.dumps(data))
    payload, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 2
    assert (
        f"SURVIVING_WRITER_UNVERIFIABLE:{repository.repo.name}:{allocation}" in payload["blockers"]
    )


def test_an_unpinned_entry_point_blocks(task, tmp_path):
    repository, _path, _ = task
    home = gate_home(tmp_path)
    (home / ".local/bin/worktree-controller").write_text("#!/bin/sh\nexit 0\n")
    payload, code = run_gate(home, repository.repo)
    assert code == 2
    assert "CONTROLLER_ENTRYPOINT_NOT_PINNED:worktree-controller" in payload["blockers"]


def test_a_missing_staged_runtime_blocks(task, tmp_path):
    repository, _path, _ = task
    payload, code = run_gate(gate_home(tmp_path), repository.repo, version="absent")
    assert code == 2
    assert "STAGED_RUNTIME_MISSING:absent" in payload["blockers"]


def test_active_host_sessions_block_an_unproven_contract_switch(task, tmp_path):
    repository, _path, _ = task
    home = gate_home(tmp_path)
    (home / "consumer.json").write_text(
        json.dumps(
            {
                "writer_session_version": 2,
                "host_managed_writer_session_version": 2,
                "holder_aware_write_guard": True,
                "active_writer_sessions": 3,
            }
        )
    )
    payload, code = run_gate(home, repository.repo)
    assert code == 2
    assert f"CONSUMER_SESSION_ACTIVE:{home / 'consumer-capabilities'}:3" in payload["blockers"]


@pytest.mark.parametrize("protocols, compatible", [([1, 2], True), ([2], True), ([3], False), ([True], False), ([], False)])
def test_gate_checks_surviving_protocols_not_session_count(task, tmp_path, protocols, compatible):
    repository, _, _ = task
    home = gate_home(tmp_path)
    capability = json.loads((home / "consumer.json").read_text())
    capability.update(active_writer_sessions=3, active_writer_session_versions=protocols,
                      controller_activation_compatible=True, software_version="future-additive-value")
    (home / "consumer.json").write_text(json.dumps(capability))
    payload, code = run_gate(home, repository.repo)
    assert (code == 0) is compatible, payload


def test_a_publication_lease_blocks(task, tmp_path):
    repository, _path, _ = task
    lease = repository.repo / ".git/worktree-controller/v1/publication-lease.json"
    lease.write_text("{}")
    payload, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 2
    assert f"PUBLICATION_LEASE_HELD:{repository.repo.name}" in payload["blockers"]


def test_an_unfinished_transaction_blocks(task, tmp_path):
    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "pending.json").write_text("{}")
    payload, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 2
    assert f"CONTROLLER_TRANSACTION_UNFINISHED:{repository.repo.name}" in payload["blockers"]


@pytest.mark.parametrize("valid", [True, False])
def test_gate_consumes_exact_staged_recovery_check(task, tmp_path, monkeypatch, valid):
    import runpy
    from types import SimpleNamespace

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "original.json").write_text("{}")
    receipt = tmp_path / "receipt.json"
    module = runpy.run_path(str(GATE))
    runtime = tmp_path / "staged/bin/worktree-controller"
    calls = []

    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "decision": {
                        "code": "ROLLOUT_RECOVERY_COMPATIBLE",
                        "dry_run": True,
                        "transactions": [{"transaction_id": "original" if valid else "another"}],
                    }
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    blockers = []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, receipt=receipt, receipt_sha="a" * 64
    )
    assert bool(blockers) is not valid
    assert calls == [
        [
            str(runtime),
            "resource-recover",
            "--repo",
            str(repository.repo),
            "--stage",
            "rollout-check",
            "--dry-run",
            "--json",
            "--preservation-receipt",
            str(receipt),
            "--preservation-receipt-sha256",
            "a" * 64,
        ]
    ]
    assert (transactions / "original.json").read_text() == "{}"


def test_busy_mutation_defers_journal_diagnosis_until_the_next_complete_check(
    task, tmp_path, monkeypatch,
):
    import fcntl
    import runpy

    repository, _path, _ = task
    common = repository.repo / ".git/worktree-controller/v1"
    transactions = common / "transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    journal = transactions / "in-flight.json"
    journal.write_text('{"command":"acquire","phase":"METADATA_WRITTEN"}\n')
    lease = common / "publication-lease.json"
    lease.write_text("{}")
    error = {"code": "ROLLOUT_TRANSACTION_UNSUPPORTED", "message": "unsupported intermediate journal"}
    module = runpy.run_path(str(GATE))
    runtime = tmp_path / "staged/bin/worktree-controller"
    original_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        calls.append(command)
        return SimpleNamespace(returncode=2, stdout=json.dumps({
            "decision": {"code": "ERROR", "transaction_diagnostics": [{
                "transaction_id": "in-flight", "command": "acquire",
                "phase": "METADATA_WRITTEN", "error": error, "recovery_entry": None,
            }]},
            "errors": [error],
        }))

    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    with (common / "repo.lock").open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        module["metadata_quiet"](
            repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
        )
        assert blockers == [f"CONTROLLER_MUTATION_IN_FLIGHT:{repository.repo.name}"]
        assert diagnostics == calls == []
        assert journal.read_text() == '{"command":"acquire","phase":"METADATA_WRITTEN"}\n'
        assert lease.read_text() == "{}"

    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
    )
    assert blockers == [
        f"CONTROLLER_TRANSACTION_UNFINISHED:{repository.repo.name}",
        f"PUBLICATION_LEASE_HELD:{repository.repo.name}",
    ]
    assert diagnostics[0]["error"] == error
    assert len(calls) == 1

    journal.unlink()
    lease.unlink()
    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
    )
    assert blockers == diagnostics == []
    assert len(calls) == 1


@pytest.mark.parametrize("before,verified,after", [
    ({"original"}, {"original", "new"}, {"original", "new"}),
    ({"original", "finished"}, {"original"}, {"original"}),
    ({"original"}, {"original"}, set()),
    ({"original"}, {"original"}, {"original", "new"}),
])
def test_gate_blocks_observed_transaction_set_changes_until_a_fresh_check(
    task, tmp_path, monkeypatch, before, verified, after,
):
    import runpy

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    for name in before:
        (transactions / f"{name}.json").write_text("{}")
    module = runpy.run_path(str(GATE))
    original_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        calls.append(command)
        checked = verified if len(calls) == 1 else after
        if len(calls) == 1:
            for name in before - after:
                (transactions / f"{name}.json").unlink()
            for name in after - before:
                (transactions / f"{name}.json").write_text("{}")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "decision": {
                "code": "ROLLOUT_RECOVERY_COMPATIBLE", "dry_run": True,
                "transactions": [{"transaction_id": name} for name in sorted(checked)],
            },
            "errors": [],
        }))

    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    runtime = tmp_path / "staged/bin/worktree-controller"
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
    )
    assert blockers == [f"CONTROLLER_TRANSACTION_SET_CHANGED:{repository.repo.name}"]
    assert diagnostics[0]["error"]["code"] == "ROLLOUT_TRANSACTION_SET_CHANGED"
    assert len(calls) == 1

    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
    )
    assert blockers == diagnostics == []
    assert len(calls) == (2 if after else 1)
    assert {path.stem for path in transactions.iterdir()} == after
    assert all(path.read_text() == "{}" for path in transactions.iterdir())


def test_gate_rechecks_an_initially_empty_transaction_set(task, tmp_path, monkeypatch):
    import runpy

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    module = runpy.run_path(str(GATE))
    original_iterdir, original_run = Path.iterdir, subprocess.run
    appeared = False
    calls = []

    def iterdir(path):
        nonlocal appeared
        entries = list(original_iterdir(path))
        if path == transactions and not appeared:
            appeared = True
            (transactions / "new.json").write_text("{}")
        return iter(entries)

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "decision": {
                "code": "ROLLOUT_RECOVERY_COMPATIBLE", "dry_run": True,
                "transactions": [{"transaction_id": "new"}],
            },
        }))

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    runtime = tmp_path / "staged/bin/worktree-controller"
    module["metadata_quiet"](
        repository.repo, blockers, runtime=runtime, diagnostics=diagnostics,
    )
    assert blockers == [f"CONTROLLER_TRANSACTION_SET_CHANGED:{repository.repo.name}"]
    assert diagnostics[0]["error"]["code"] == "ROLLOUT_TRANSACTION_SET_CHANGED"
    assert not calls
    blockers = []
    module["metadata_quiet"](repository.repo, blockers, runtime=runtime)
    assert not blockers and len(calls) == 1
    assert (transactions / "new.json").read_text() == "{}"


@pytest.mark.parametrize("variant", [
    "failed", "malformed-json", "incompatible", "dry-run-false", "errors", "diagnostics",
    "duplicate", "invalid-id", "invalid-list", "unrelated", "after-unreadable", "after-invalid-directory",
])
def test_transaction_churn_never_downgrades_failed_or_unverifiable_checks(
    task, tmp_path, monkeypatch, variant,
):
    import runpy

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "original.json").write_text("{}")
    error = {"code": "ROLLOUT_RELEASE_RECOVERY_REQUIRED", "message": "exact release replay required"}
    reply = {
        "decision": {
            "code": "ROLLOUT_RECOVERY_COMPATIBLE", "dry_run": True,
            "transactions": [{"transaction_id": "original"}],
        },
        "errors": [],
    }
    decision = reply["decision"]
    if variant == "incompatible":
        decision["code"] = "ERROR"
    elif variant == "dry-run-false":
        decision["dry_run"] = False
    elif variant == "errors":
        reply["errors"] = [error]
    elif variant == "diagnostics":
        decision["transaction_diagnostics"] = [{"transaction_id": "original", "error": error}]
    elif variant == "duplicate":
        decision["transactions"] *= 2
    elif variant == "invalid-id":
        decision["transactions"] = [{"transaction_id": None}]
    elif variant == "invalid-list":
        decision["transactions"] = {"original": {}}
    elif variant == "unrelated":
        decision["transactions"] = [{"transaction_id": "unobserved"}]
    module = runpy.run_path(str(GATE))
    original_run, original_iterdir = subprocess.run, Path.iterdir
    scans = 0

    def iterdir(path):
        nonlocal scans
        if path == transactions:
            scans += 1
            if variant == "after-unreadable" and scans == 2:
                raise PermissionError("fixture transaction directory is unreadable")
        return original_iterdir(path)

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        if variant == "after-invalid-directory":
            transactions.rename(transactions.with_name("transactions-saved"))
            transactions.write_text("invalid directory")
        else:
            (transactions / "new.json").write_text("{}")
        return SimpleNamespace(
            returncode=2 if variant == "failed" else 0,
            stdout="not JSON" if variant == "malformed-json" else json.dumps(reply),
        )

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=tmp_path / "staged", diagnostics=diagnostics,
    )
    assert blockers == [f"CONTROLLER_TRANSACTION_UNFINISHED:{repository.repo.name}"]
    assert diagnostics[0]["error"]["code"] != "ROLLOUT_TRANSACTION_SET_CHANGED"
    if variant in {"errors", "diagnostics"}:
        assert diagnostics[0]["error"] == error
    saved = transactions.with_name("transactions-saved") if variant == "after-invalid-directory" else transactions
    assert (saved / "original.json").read_text() == "{}"


def test_activation_forwards_recovery_baseline_to_frozen_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    module = load_activate()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "decision": "CLIENTS_READY",
            "consumer_capabilities": {str(tmp_path / "consumer"): {}},
        }))

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "log", lambda text: None)
    module.preflight(tmp_path, [tmp_path / "consumer"], "oid", tmp_path / "receipt", "a" * 64)
    assert "--recovery-preservation-receipt" in calls[0]
    assert str(tmp_path / "receipt") in calls[0]
    assert "--recovery-preservation-receipt-sha256" in calls[0]
    assert "a" * 64 in calls[0]


@pytest.mark.parametrize("structured", [True, False])
def test_gate_retains_staged_recovery_errors(task, tmp_path, monkeypatch, structured):
    import runpy

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    journal = transactions / "interrupted.json"
    journal.write_text("{}")
    error = {"code": "ROLLOUT_RELEASE_RECOVERY_REQUIRED", "message": "exact release replay required"}
    diagnostic = {
        "transaction_id": "interrupted", "command": "release", "phase": "RELEASED_RECORDED",
        "error": error, "recovery_entry": "release",
    }
    reply = {"decision": {"code": "ERROR"}, "errors": [error]}
    if structured:
        reply["decision"]["transaction_diagnostics"] = [diagnostic]
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        return SimpleNamespace(returncode=2, stdout=json.dumps(reply))

    module = runpy.run_path(str(GATE))
    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=tmp_path / "staged", diagnostics=diagnostics,
    )
    assert blockers == [f"CONTROLLER_TRANSACTION_UNFINISHED:{repository.repo.name}"]
    assert diagnostics[0]["repository"] == str(repository.repo)
    assert diagnostics[0]["error"] == error
    if structured:
        assert diagnostics[0] == {"repository": str(repository.repo), **diagnostic}
    else:
        assert diagnostics[0]["transaction_id"] is None
    assert journal.read_text() == "{}"


@pytest.mark.parametrize("reply", [
    "not JSON", '{"decision": []}', '{"decision": {}}',
    '{"decision": {"transaction_diagnostics": [{}]}}',
])
def test_gate_reports_failed_or_incompatible_recovery_without_admitting(task, tmp_path, monkeypatch, reply):
    import runpy

    repository, _path, _ = task
    transactions = repository.repo / ".git/worktree-controller/v1/transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "pending.json").write_text("{}")
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        return SimpleNamespace(returncode=2, stdout=reply)

    module = runpy.run_path(str(GATE))
    monkeypatch.setattr(subprocess, "run", run)
    blockers, diagnostics = [], []
    module["metadata_quiet"](
        repository.repo, blockers, runtime=tmp_path / "staged", diagnostics=diagnostics,
    )
    assert blockers == [f"CONTROLLER_TRANSACTION_UNFINISHED:{repository.repo.name}"]
    assert diagnostics[0]["error"]["code"] in {"ROLLOUT_CHECK_FAILED", "ROLLOUT_CHECK_INCOMPATIBLE"}
    assert diagnostics[0]["error"]["message"]


@pytest.fixture
def activation_wait(tmp_path, monkeypatch):
    home = pinned_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    module = load_activate()
    module.GIT_SOURCE = "file:///fixture/source"
    module.LOG_DIR = tmp_path / "logs"
    module.GOVERNED = (tmp_path / "repository",)
    monkeypatch.setattr(module, "temporary_directory", lambda prefix: tmp_path / "unused-work")
    monkeypatch.setattr(module, "frozen_source", lambda work, oid: tmp_path / "source")
    monkeypatch.setattr(module, "verify_runtime", lambda *args: "fixture")
    monkeypatch.setattr(module, "verify_release_lineage", lambda *args: None)
    monkeypatch.setattr(module, "verify_rollback_lineage", lambda *args: None)
    monkeypatch.setattr(module, "verify_resource_compatibility", lambda runtime: None)
    postchecks = []
    monkeypatch.setattr(module, "post_activation_check", lambda *args: postchecks.append(args))
    return SimpleNamespace(module=module, home=home, root=tmp_path, postchecks=postchecks)


def wait_command(item, monkeypatch, seconds=None, action="activate"):
    arguments = ["controller-activate", action, "--version", "two"]
    if seconds is not None:
        arguments += ["--wait-ready-seconds", str(seconds)]
    arguments += [
        "--consumer", str(item.root / "consumer"),
        "--recovery-preservation-receipt", str(item.root / "receipt"),
        "--recovery-preservation-receipt-sha256", "a" * 64,
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    return item.module.main()


def scripted_rollout(item, monkeypatch, replies, *, drift=None, returncode=None):
    clock = SimpleNamespace(now=0)
    calls = []
    replies = iter(replies)

    def change_pointer():
        item.module.ACTIVE.unlink()
        item.module.ACTIVE.symlink_to(item.module.VERSIONS / "two/worktree-controller")

    def sleep(seconds):
        clock.now += seconds
        if drift == "sleep":
            change_pointer()

    def run(command, **kwargs):
        calls.append((command, kwargs))
        clock.now += 1
        reply = next(replies)
        if isinstance(reply, dict) and reply.get("decision") == "CLIENTS_READY":
            reply = {"consumer_capabilities": {str(item.root / "consumer"): {}}, **reply}
        if drift == "gate":
            change_pointer()
        code = returncode
        if code is None:
            code = 0 if isinstance(reply, dict) and reply.get("decision") == "CLIENTS_READY" else 2
        return subprocess.CompletedProcess(command, code, json.dumps(reply), "")

    monkeypatch.setattr(item.module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(item.module, "run", run)
    return calls


@pytest.mark.parametrize("blocker", [
    "LIVE_CONTROLLER_INVOCATION:98765", "CONTROLLER_MUTATION_IN_FLIGHT:repository",
    "CONTROLLER_TRANSACTION_SET_CHANGED:repository",
])
@pytest.mark.parametrize("action", ["activate", "rollback"])
def test_temporary_rollout_occupancy_retries_the_complete_frozen_gate(
    activation_wait, monkeypatch, blocker, action,
):
    item = activation_wait
    blocked = {"decision": "BLOCKED", "blockers": [blocker]}
    calls = scripted_rollout(item, monkeypatch, [blocked, blocked, {"decision": "CLIENTS_READY"}])
    assert wait_command(item, monkeypatch, 10, action) == 0
    assert item.module.active_version() == "two"
    assert len(item.postchecks) == 1 and len(calls) == 3
    assert calls[0][0] == calls[1][0] == calls[2][0]
    command = list(map(str, calls[0][0]))
    assert str(item.root / "source/scripts/check-writer-rollout") in command
    assert command[command.index("--target-version") + 1] == "two"
    assert command[command.index("--consumer") + 1] == str(item.root / "consumer")
    assert command[command.index("--repo") + 1] == str(item.root / "repository")
    assert command[command.index("--recovery-preservation-receipt") + 1] == str(item.root / "receipt")
    assert command[command.index("--recovery-preservation-receipt-sha256") + 1] == "a" * 64
    assert [kwargs["timeout"] for _, kwargs in calls] == [10, 7, 4]
    assert all(kwargs["process_group"] for _, kwargs in calls)


@pytest.mark.parametrize("blocker", [
    "LIVE_CONTROLLER_INVOCATION:98765", "CONTROLLER_TRANSACTION_SET_CHANGED:repository",
])
def test_default_activation_still_refuses_temporary_occupancy_without_waiting(
    activation_wait, monkeypatch, blocker,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{
        "decision": "BLOCKED", "blockers": [blocker],
    }])
    assert wait_command(item, monkeypatch) == 2
    assert item.module.active_version() == "one" and not item.postchecks
    assert len(calls) == 1 and calls[0][1]["process_group"] is False


@pytest.mark.parametrize("blocker", [
    "CONTROLLER_MUTATION_IN_FLIGHT:repository", "CONTROLLER_TRANSACTION_SET_CHANGED:repository",
])
def test_persistent_rollout_occupancy_exhausts_one_budget_without_switching(
    activation_wait, monkeypatch, capsys, blocker,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{
        "decision": "BLOCKED", "blockers": [blocker],
    }] * 2)
    assert wait_command(item, monkeypatch, 5) == 2
    assert item.module.active_version() == "one" and not item.postchecks
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert "timed out after 5s" in output and blocker in output


@pytest.mark.parametrize("blockers", [
    ["CONSUMER_SESSION_ACTIVE:fixture:1"],
    ["CONTROLLER_TRANSACTION_UNFINISHED:repository"],
    ["PUBLICATION_LEASE_HELD:repository"],
    ["PROCESS_SCAN_UNAVAILABLE:98765"],
    ["LIVE_CONTROLLER_INVOCATION:98765", "STAGED_RUNTIME_MISMATCH:two"],
    ["LIVE_CONTROLLER_INVOCATION:"],
    ["LIVE_CONTROLLER_INVOCATION:not-a-pid"],
    ["LIVE_CONTROLLER_INVOCATION:0"],
    ["CONTROLLER_MUTATION_IN_FLIGHT:"],
    ["CONTROLLER_TRANSACTION_SET_CHANGED:"],
    ["CONTROLLER_TRANSACTION_SET_CHANGED:repository", "CONTROLLER_TRANSACTION_UNFINISHED:repository"],
])
def test_wait_refuses_unknown_permanent_or_mixed_blockers_without_retrying(
    activation_wait, monkeypatch, blockers,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{"decision": "BLOCKED", "blockers": blockers}])
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks


def test_transaction_set_retry_stops_on_a_later_recovery_failure(
    activation_wait, monkeypatch,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [
        {"decision": "BLOCKED", "blockers": ["CONTROLLER_TRANSACTION_SET_CHANGED:repository"]},
        {"decision": "BLOCKED", "blockers": ["CONTROLLER_TRANSACTION_UNFINISHED:repository"]},
    ])
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 2 and item.module.active_version() == "one" and not item.postchecks


def test_activation_logs_transaction_diagnostics_without_switching_or_retrying(
    activation_wait, monkeypatch,
):
    item = activation_wait
    diagnostic = {
        "repository": "repository", "transaction_id": "interrupted", "command": "release",
        "phase": "RELEASED_RECORDED", "recovery_entry": "release",
        "error": {"code": "ROLLOUT_RELEASE_RECOVERY_REQUIRED", "message": "exact replay required\nnext"},
    }
    calls = scripted_rollout(item, monkeypatch, [{
        "decision": "BLOCKED", "blockers": ["CONTROLLER_TRANSACTION_UNFINISHED:repository"],
        "transaction_diagnostics": [diagnostic],
    }])
    messages = []
    monkeypatch.setattr(item.module, "log", messages.append)
    assert wait_command(item, monkeypatch, 10) == 2
    line, = [message for message in messages if "transaction diagnostic:" in message]
    assert json.loads(line.split("transaction diagnostic: ", 1)[1]) == diagnostic
    assert "\n" not in line
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks


@pytest.mark.parametrize("reply", [
    [],
    {"decision": "CLIENTS_READY", "blockers": ["LIVE_CONTROLLER_INVOCATION:98765"]},
    {"decision": "BLOCKED", "blockers": "LIVE_CONTROLLER_INVOCATION:98765"},
    {"decision": "BLOCKED", "blockers": [None]},
    {"decision": "BLOCKED", "blockers": []},
])
def test_wait_never_treats_an_invalid_gate_decision_as_retry_authority(
    activation_wait, monkeypatch, reply,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [reply])
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks


@pytest.mark.parametrize("returncode", [0, 1, -signal.SIGKILL])
def test_wait_never_retries_an_unexpected_gate_exit_code(
    activation_wait, monkeypatch, returncode,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{
        "decision": "BLOCKED", "blockers": ["LIVE_CONTROLLER_INVOCATION:98765"],
    }], returncode=returncode)
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks


def test_wait_refuses_readiness_that_arrives_after_the_budget(
    activation_wait, monkeypatch, capsys,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{"decision": "CLIENTS_READY"}])
    assert wait_command(item, monkeypatch, 1) == 2
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks
    assert "timed out after 1s" in capsys.readouterr().out


def test_wait_keeps_a_single_gate_timeout_distinct_from_the_remaining_budget(
    activation_wait, monkeypatch, capsys,
):
    item = activation_wait
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        assert kwargs["timeout"] == 300
        raise subprocess.TimeoutExpired(command, 300)

    monkeypatch.setattr(item.module, "run", run)
    assert wait_command(item, monkeypatch, 600) == 2
    assert len(calls) == 1 and item.module.active_version() == "one" and not item.postchecks
    assert "300s command limit" in capsys.readouterr().out


@pytest.mark.parametrize("drift", ["gate", "sleep"])
@pytest.mark.parametrize("blocker", [
    "LIVE_CONTROLLER_INVOCATION:98765", "CONTROLLER_TRANSACTION_SET_CHANGED:repository",
])
def test_wait_preserves_another_activators_pointer_instead_of_switching_or_restoring(
    activation_wait, monkeypatch, capsys, drift, blocker,
):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{
        "decision": "BLOCKED", "blockers": [blocker],
    }], drift=drift)
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 1 and item.module.active_version() == "two" and not item.postchecks
    assert "active pointer changed while waiting" in capsys.readouterr().out


def test_wait_rechecks_pointer_after_a_ready_gate(activation_wait, monkeypatch):
    item = activation_wait
    calls = scripted_rollout(item, monkeypatch, [{"decision": "CLIENTS_READY"}], drift="gate")
    assert wait_command(item, monkeypatch, 10) == 2
    assert len(calls) == 1 and item.module.active_version() == "two" and not item.postchecks


def test_wait_timeout_terminates_only_its_gate_and_child_processes(
    activation_wait, monkeypatch, capsys,
):
    item = activation_wait
    script = item.root / "source/scripts/check-writer-rollout"
    script.parent.mkdir(parents=True)
    marker = item.root / "child.json"
    script.write_text(
        "import json, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "raw = pathlib.Path(f'/proc/{child.pid}/stat').read_text()\n"
        "ticks = int(raw[raw.rfind(')') + 2:].split()[19])\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'pid': child.pid, 'ticks': ticks}}))\n"
        "time.sleep(30)\n"
    )
    canary = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"HOME": str(item.home), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_command(item, monkeypatch, 3) == 2
        assert item.module.active_version() == "one" and not item.postchecks
        assert "timed out after 3s" in capsys.readouterr().out
        assert canary.poll() is None
        child = json.loads(marker.read_text())
        status = Path(f"/proc/{child['pid']}/stat")
        deadline = time.monotonic() + 5
        while True:
            try:
                raw = status.read_text()
            except (FileNotFoundError, ProcessLookupError):
                break
            fields = raw[raw.rfind(")") + 2:].split()
            if int(fields[19]) != child["ticks"] or fields[0] in {"Z", "X"}:
                break
            assert time.monotonic() < deadline, "gate child did not exit"
            time.sleep(0.01)
    finally:
        canary.terminate()
        canary.wait(timeout=5)
        if marker.exists():
            child = json.loads(marker.read_text())
            try:
                raw = Path(f"/proc/{child['pid']}/stat").read_text()
                fields = raw[raw.rfind(")") + 2:].split()
                if int(fields[19]) == child["ticks"] and fields[0] != "Z":
                    os.kill(child["pid"], signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass


@pytest.mark.parametrize("action,seconds,extra", [
    ("activate", "0", []), ("activate", "-1", []), ("activate", "3601", []),
    ("activate", "nan", []), ("stage", "5", []), ("bootstrap", "5", []),
    ("status", "5", []), ("sync-skill", "5", []),
    ("activate", "5", ["--migrate-namespace-from", "legacy"]),
    ("rollback", "5", ["--layout-receipt", "/fixture/receipt"]),
])
def test_wait_option_rejects_unsupported_actions_and_budgets_before_writing(
    tmp_path, monkeypatch, action, seconds, extra,
):
    module = load_activate()
    module.LOG_DIR = tmp_path / "logs"
    monkeypatch.setattr(sys, "argv", ["controller-activate", action, "--wait-ready-seconds", seconds, *extra])
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2 and not module.LOG_DIR.exists()


@pytest.mark.parametrize("capabilities,compatible", [
    ({}, False),
    ({"resource_admission_version": 1, "execution_admission_version": 1}, False),
    ({"resource_admission_version": 1, "execution_admission_version": 1,
      "host_execution_admission_version": 1, "additive_future_field": True}, True),
])
def test_version_switch_requires_support_for_existing_host_requests(tmp_path, monkeypatch, capabilities, compatible):
    from types import SimpleNamespace

    from worktree_controller.resource_admission import host_execution

    host_execution("admit", target=tmp_path, session_id="fixture", run_id="fixture")
    module = load_activate()
    monkeypatch.setattr(module, "run", lambda *args, **kwargs:
        SimpleNamespace(stdout=json.dumps({"decision": capabilities})))
    if compatible:
        module.verify_resource_compatibility(tmp_path)
    else:
        with pytest.raises(module.Abort, match="cannot preserve"):
            module.verify_resource_compatibility(tmp_path)


@pytest.mark.parametrize(
    "pending,capability,current_proof",
    [(True, 0, True), (True, 1, True), (False, 0, True), (True, 0, False)],
)
def test_runtime_switch_preserves_unfinished_no_effect_settlement(
    tmp_path, monkeypatch, pending, capability, current_proof
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from worktree_controller.resource_admission import AdmissionLedger

    module = load_activate()
    marker = tmp_path / "ledger"
    marker.touch()
    row = {
        "state": "RESERVED" if pending else "WAITING",
        "request": {"kind": "checkout"},
        "transaction_id": "attempt",
        "history": [
            {
                "terminal_evidence": "ACQUIRE_NO_EFFECT_SUPERSEDED",
                "transaction_id": "attempt" if current_proof else "previous",
            }
        ],
    }
    monkeypatch.setattr(
        module,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "decision": {
                        "resource_admission_version": 1,
                        "acquire_no_effect_recovery_version": capability,
                    }
                }
            )
        ),
    )
    monkeypatch.setattr(AdmissionLedger, "__init__", lambda self: setattr(self, "path", marker))
    monkeypatch.setattr(AdmissionLedger, "locked", lambda self: nullcontext())
    monkeypatch.setattr(AdmissionLedger, "read", lambda self: {"requests": {"fixture": row}})
    if pending and current_proof and not capability:
        with pytest.raises(module.Abort, match="cannot preserve"):
            module.verify_resource_compatibility(tmp_path)
    else:
        module.verify_resource_compatibility(tmp_path)


def test_an_incomplete_recorded_identity_blocks(task, tmp_path):
    repository, path, allocation = task
    holder(path, repository=repository)
    lease = repository.repo / f".git/worktree-controller/v1/writer-leases/{allocation}.json"
    payload = json.loads(lease.read_text())
    payload["holder"]["boot_id"] = None
    lease.write_text(json.dumps(payload))
    result, code = run_gate(gate_home(tmp_path), repository.repo)
    assert code == 2
    assert f"LEASE_IDENTITY_INCOMPLETE:{repository.repo.name}:{allocation}" in result["blockers"]


def test_a_governed_repository_is_required(tmp_path):
    home = gate_home(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--home",
            str(home),
            "--consumer",
            str(home / "consumer-capabilities"),
            "--target-version",
            "target",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HOME": str(home)},
    )
    assert "GOVERNED_REPOSITORY_REQUIRED" in json.loads(result.stdout)["blockers"]
