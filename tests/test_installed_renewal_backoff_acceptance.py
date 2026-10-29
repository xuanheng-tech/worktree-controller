"""Real expiry and public reconciliation preserve an installed release's authority."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_controlled import _create_reproducible_venv
from tests.test_installed_conflict_retry_acceptance import (
    FILE,
    OWNER,
    abort,
    claim,
    controller,
    enqueue,
    finalizer,
    item,
    publish,
    release,
)
from tests.test_installed_load_acceptance import fixture_environment as fixture_environment
from tests.test_installed_load_acceptance import installed as installed
from tests.test_retention_convergence import POLICY_V8
from tests.test_shadow import git

TTL = 10
BINDING = "fixture-workspace:renewal"
POLICY = (POLICY_V8.replace("intent_ttl_seconds = 3600", f"intent_ttl_seconds = {TTL}")
          .replace("grace_minutes = 30", "grace_minutes = 1"))


def allocation_identity(row):
    return {key: row[key] for key in (
        "allocation_id", "task_key", "authority_key", "owner", "path", "branch", "head",
        "lifecycle", "record_version", "scope_paths", "external_bindings", "disposable_resources",
        "handoff_commit", "task_commit", "finalizer_commit", "finalizer_record_id",
    )}


def release_writer(case, path, lease):
    holder = lease["holder"]
    controller(case, "writer-lease-release", "--path", path, "--lease-id", lease["lease_id"],
               "--holder-pid", holder["pid"], "--holder-boot-id", holder["boot_id"],
               "--holder-start-ticks", holder["start_ticks"], "--client-id", holder["client_id"],
               "--session-id", holder["session_id"], "--run-id", holder["run_id"])


def writer(case, path, run):
    lease = controller(case, "writer-lease-acquire", "--path", path, "--owner", OWNER,
                       "--client-id", "acceptance", "--holder-pid", os.getpid(),
                       "--session-id", "renewal-source", "--run-id", run)["decision"]
    controller(case, "write-guard", "--path", path, "--task-key", "renewal-source",
               "--authority-key", "renewal-source", "--owner", OWNER, "--holder", os.getpid(),
               "--client-id", "acceptance", "--session-id", "renewal-source", "--run-id", run,
               "--operation", "feature-write")
    return lease


def register(case, path):
    return controller(case, "resource-register", "--path", path, "--owner", OWNER,
                      "--expected-record-version", item(case, path)["record_version"],
                      "--resource-path", ".venv", "--kind", "python-venv", "--reproducible",
                      "--creator", "acceptance fixture", "--teardown-policy", "on-release")


def handoff(case):
    path = case["source"]
    controller(case, "handoff", "--path", path, "--expected-record-version",
               item(case, path)["record_version"], "--kind", "integration-ready",
               "--commit", case["head"], "--finalizer-record-id", case["source_finalizer"],
               "--finalizer-commit", case["head"], "--scope", FILE)


def close_source(case):
    """Only the fixture owner repairs/classifies its environment, then uses guarded release."""
    path = case["source"]
    row = item(case, path)
    if BINDING in row["external_bindings"]:
        controller(case, "binding-remove", "--path", path, "--owner", OWNER,
                   "--expected-record-version", row["record_version"], "--binding", BINDING)
    row = item(case, path)
    if row["lifecycle"] == "HANDOFF_READY":
        controller(case, "reopen", "--path", path, "--owner", OWNER,
                   "--expected-record-version", row["record_version"])
    lease = writer(case, path, "cleanup")
    try:
        (path / ".venv/pyvenv.cfg").write_text(case["venv_config"])
        if not item(case, path)["disposable_resources"]:
            register(case, path)
        controller(case, "resource-clean", "--path", path, "--owner", OWNER,
                   "--expected-record-version", item(case, path)["record_version"],
                   "--resource-path", ".venv")
    finally:
        release_writer(case, path, lease)
    assert not (path / ".venv").exists()
    handoff(case)
    release(case, path)


@contextmanager
def managed_case(installed, environment, root, *, bound=False):
    configured = os.environ.get("GIT_FINALIZER_ACCEPTANCE")
    if not configured:
        pytest.skip("set GIT_FINALIZER_ACCEPTANCE for installed release renewal acceptance")
    entry = Path(configured)
    assert entry.is_absolute() and entry.is_file() and os.access(entry, os.X_OK)
    repo, remote = root / "repo", root / "remote.git"
    git(root, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Acceptance Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    policy = repo / ".agents/worktree-policy.toml"
    policy.parent.mkdir()
    policy.write_text(POLICY)
    (repo / FILE).write_text('VALUE = "base"\n')
    (repo / ".gitignore").write_text(".venv\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "release-fixture"\nversion = "0.1.0"\n')
    (repo / "uv.lock").write_text('version = 1\nrevision = 3\nrequires-python = ">=3.12"\n')
    git(repo, "add", ".agents/worktree-policy.toml", FILE, ".gitignore", "pyproject.toml", "uv.lock")
    git(repo, "commit", "-m", "fixture baseline before governance")
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    binaries = root / "bin"
    binaries.mkdir()
    (binaries / "worktree-controller").symlink_to(installed)
    case = {"installed": installed, "finalizer": entry, "repo": repo, "root": root,
            "environment": {**environment, "PATH": str(binaries) + os.pathsep + environment["PATH"]},
            "source": root / "renewal-source"}
    controller(case, "adopt", "--path", repo, "--task-key", "control", "--authority-key", "control",
               "--module", "fixture", "--role", "control-plane", "--owner", OWNER, "--lifecycle", "ACTIVE")
    acquired = controller(case, "acquire", "--path", case["source"], "--task-key", "renewal-source",
                          "--authority-key", "renewal-source", "--module", "fixture", "--role", "feature",
                          "--owner", OWNER, "--branch", "feature/renewal-source", "--base-ref", "origin/main",
                          "--scope", FILE)["decision"]
    case["allocation"] = acquired["allocation_id"]
    try:
        lease = writer(case, case["source"], "prepare")
        try:
            path = case["source"]
            (path / FILE).write_text('VALUE = "source"\n')
            venv = _create_reproducible_venv(path)
            case["venv_config"] = (venv / "pyvenv.cfg").read_text()
            if bound:
                register(case, path)
            else:
                # A real registration refusal, without patching installed code or private state.
                (venv / "pyvenv.cfg").unlink()
            committed, _ = finalizer(case, path, "source-commit", "--mode", "commit-only",
                                     "--message", "fixture release renewal", "--", FILE)
            case["head"] = committed["commit"]["sha"]
            _, case["source_finalizer"] = finalizer(
                case, path, "source-publish", "--publish-existing-branch", case["head"],
                "--remote", "origin", "--remote-branch", "feature/renewal-source")
        finally:
            release_writer(case, case["source"], lease)
        if bound:
            controller(case, "binding-add", "--path", path, "--owner", OWNER,
                       "--expected-record-version", item(case, path)["record_version"], "--binding", BINDING)
        handoff(case)
        intent_id = enqueue(case, case["allocation"], "renewal-integration")
        candidate, _ = claim(case, intent_id, "candidate")
        prepared, _ = publish(case, candidate, 'VALUE = "source"\n', "fixture-main")
        case["published"] = prepared["candidate_oid"]
        row = item(case, case["source"])
        assert row["release_intent_status"] == "BLOCKED_RETRYABLE", row
        assert row["release_intent_blocker"] == ("EXTERNAL_BINDING" if bound else "IGNORED_CONTENT"), row
        assert controller(case, "status")["repository"]["release_intent_ttl_seconds"] == TTL
        yield case
    finally:
        for row in controller(case, "status")["worktrees"]:
            if not row["canonical"] and row["role"] == "integration":
                abort(case, Path(row["path"]))
                release(case, Path(row["path"]))
        for row in controller(case, "integration-queue")["decision"]["intents"]:
            if row["effective_state"] not in {"RELEASED", "ABANDONED"}:
                controller(case, "integration-abandon", "--intent-id", row["intent_id"],
                           "--owner", OWNER, "--reason", "fixture acceptance finished")
        if case["source"].exists():
            if "source_finalizer" in case:
                close_source(case)
            else:
                # A setup refusal before any write can close with its exact no-change evidence.
                row = item(case, case["source"])
                assert row["clean"] and row["head"] == row["base_commit"]
                controller(case, "complete-no-change", "--path", case["source"], "--owner", OWNER,
                           "--expected-record-version", row["record_version"])
                release(case, case["source"])
        final = controller(case, "status")
        assert len(final["worktrees"]) == 1 and final["repository"]["writer_leases_recorded"] == 0
        assert final["repository"]["active_release_intent_count"] == 0


def reconcile_cli(case, subcommand, *arguments):
    argv = [str(case["installed"]), "reconcile", subcommand, "--repo", str(case["repo"]),
            "--json", *map(str, arguments)]
    result = subprocess.run(argv, cwd=case["root"], env=case["environment"], capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.stdout, (result.returncode, result.stderr)
    payload = json.loads(result.stdout)
    with (case["root"] / "controller-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": argv, "returncode": result.returncode,
                                 "payload": payload}) + "\n")
    assert result.returncode == 0 and not payload["errors"], (payload, result.stderr)
    return payload


def reconcile(case, *arguments):
    return reconcile_cli(case, "release-intents", "--actor", "fixture",
                         "--trigger", "capacity-pressure", *arguments)["decision"]


def expire(case):
    row = item(case, case["source"])
    intent_id = row["release_intent_id"]
    expiry = datetime.fromisoformat(row["release_intent_expires_at"])
    deadline = time.monotonic() + TTL + 5
    while datetime.now(expiry.tzinfo) < expiry:
        assert time.monotonic() < deadline, "fixture intent did not reach its real expiry"
        time.sleep(0.1)
    expired = reconcile(case, "--intent-id", intent_id)
    assert expired["renewed_intents"] == []
    assert expired["outcomes"] == [{"intent_id": intent_id, "allocation_id": case["allocation"],
                                    "status": "STALE", "result": "STALE",
                                    "blocker": "RELEASE_INTENT_EXPIRED"}], expired
    return intent_id


def renewal(case, previous):
    decision = reconcile(case)
    renewed, = decision["renewed_intents"]
    assert renewed["previous_intent_id"] == previous and "deferred" not in renewed, decision
    result = renewed["result"]
    assert result["code"] == "RELEASE_INTENT_CREATED" and result["intent_id"] != previous, decision
    return result, decision


def backoff(case, previous):
    decision = reconcile(case)
    assert decision["renewed_intents"] == [
        {"previous_intent_id": previous, "deferred": "RENEWAL_BACKOFF"}], decision
    assert decision["outcomes"] == []


def test_installed_first_expiry_renews_immediately(installed, fixture_environment, tmp_path):
    with managed_case(installed, fixture_environment, tmp_path) as case:
        before = allocation_identity(item(case, case["source"]))
        previous = expire(case)
        started = time.monotonic()
        result, decision = renewal(case, previous)
        assert time.monotonic() - started < TTL
        assert result["expected_head"] == before["head"]
        assert result["expected_record_version"] == before["record_version"]
        assert decision["outcomes"][0]["result"] == "BLOCKED_RETRYABLE", decision
        assert allocation_identity(item(case, case["source"])) == before
        assert (case["source"] / ".venv/payload.bin").read_bytes() == b"reproducible fixture"


def test_installed_retention_backoff_keeps_unchanged_handoff(installed, fixture_environment, tmp_path):
    with managed_case(installed, fixture_environment, tmp_path) as case:
        path = case["source"]
        before = allocation_identity(item(case, path))
        first = item(case, path)["release_intent_id"]
        # The public convergence policy has a one-minute minimum grace; wait for it normally.
        deadline = time.monotonic() + 80
        while True:
            preview = reconcile_cli(case, "retention", "--actor", "lifecycle-reconciler")
            row, = preview["decision"]["findings"]
            # The exact preview now exposes this deliberately unregistered environment.
            if row["outcome"] == "BLOCKED":
                assert row["code"] == "IGNORED_CONTENT", row
                break
            assert row["outcome"] == "IDLE_BELOW_THRESHOLD", row
            assert time.monotonic() < deadline, "fixture did not reach retention grace"
            time.sleep(0.5)
        for _ in range(2):
            applied = reconcile_cli(case, "retention", "--actor", "lifecycle-reconciler", "--apply")
            row, = applied["decision"]["findings"]
            assert row["outcome"] == "BLOCKED" and row["code"] == "RESOURCE_IDENTITY_MISMATCH", row
            assert allocation_identity(item(case, path)) == before
        second = item(case, path)["release_intent_id"]
        assert second != first
        assert expire(case) == second
        started = time.monotonic()
        for _ in range(3):
            backoff(case, second)
            applied = reconcile_cli(case, "retention", "--actor", "lifecycle-reconciler", "--apply")
            row, = applied["decision"]["findings"]
            assert row["outcome"] == "BLOCKED" and row["code"] == "RENEWAL_BACKOFF", row
            observed = item(case, path)
            assert observed["release_intent_id"] == second and observed["release_intent_status"] == "STALE"
            assert allocation_identity(observed) == before
            assert controller(case, "status")["repository"]["active_release_intent_count"] == 0
            assert (path / ".venv/payload.bin").read_bytes() == b"reproducible fixture"
        assert time.monotonic() - started < TTL


def test_installed_changed_record_renews_and_releases(installed, fixture_environment, tmp_path):
    with managed_case(installed, fixture_environment, tmp_path, bound=True) as case:
        path = case["source"]
        first = expire(case)
        second, _ = renewal(case, first)
        assert expire(case) == second["intent_id"]
        backoff(case, second["intent_id"])
        before = item(case, path)
        started = time.monotonic()
        cleared = controller(case, "binding-remove", "--path", path, "--owner", OWNER,
                             "--expected-record-version", before["record_version"], "--binding", BINDING)
        record = cleared["decision"]["record_version"]
        assert record == before["record_version"] + 1
        result, decision = renewal(case, second["intent_id"])
        assert time.monotonic() - started < TTL
        assert result["expected_record_version"] == record and result["expected_head"] == before["head"]
        outcome, = decision["outcomes"]
        assert outcome["intent_id"] == result["intent_id"] and outcome["result"] == "RELEASED", decision
        assert not path.exists() and not (path / ".venv").exists()
        closed, = controller(case, "lifecycle-history", "--release-receipt-id",
                             outcome["release_receipt_id"])["decision"]["closed_releases"]
        assert closed["boundary"] == "CLOSED" and closed["proof_status"] == "COMPLETE", closed
        assert closed["allocation_id"] == case["allocation"] and closed["released_head"] == case["head"]
        assert closed["release_intent_id"] == result["intent_id"] and closed["candidate_oid"] == case["published"]
        assert git(case["repo"], "rev-parse", "origin/main") == case["published"]
