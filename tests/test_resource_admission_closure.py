"""M2 Resource Admission Closure: producer invariant, inspection, orphan release.

These tests pin the admission-side scope invariant (no home-root or ancestor host
reservations), the read-only inspection contract that makes typed recovery usable
without reading the private ledger layout, and the supported release path for a
reservation whose target has vanished.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.test_resource_admission import (
    RUNTIME_BINDING_PREFIX,
    _host_reservation,
    _typed,
)
from tests.test_resource_admission import host as _shared_host
from worktree_controller import resource_admission as resources
from worktree_controller.models import ControllerError

# Re-export the shared host fixture; tests receive it as their `host` argument.
host = _shared_host


def _dead_row(host, session):  # noqa: F811
    key, _child = _host_reservation(host, session)
    return key


# --- producer invariant --------------------------------------------------------


def test_admit_refuses_home_root(host, tmp_path):
    _, ledger, _ = host
    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("admit", target=Path.home(), session_id="home-direct")
    assert blocked.value.code == "INVALID_REQUEST"
    assert "home root" in str(blocked.value)
    assert not ledger.root.exists() or not resources.AdmissionLedger().path.exists() or \
        all(row["request"].get("target_path") != str(Path.home())
            for row in resources.AdmissionLedger().read()["requests"].values())


def test_admit_refuses_home_ancestors(host):
    home = Path.home()
    for ancestor in (*home.parents, Path(os.path.relpath(os.sep))):
        target = Path(os.sep) if str(ancestor) in {"", "."} else ancestor
        if not target.is_dir():
            continue
        with pytest.raises(ControllerError) as blocked:
            resources.host_execution("admit", target=target, session_id="ancestor")
        assert blocked.value.code == "INVALID_REQUEST"


def test_admit_refuses_home_via_symlinked_alias(host, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(Path.home())
    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("admit", target=alias, session_id="alias")
    assert blocked.value.code == "INVALID_REQUEST"


def test_admit_allows_concrete_home_subdirectory(host, tmp_path):
    _, ledger, _ = host
    work = tmp_path / "session-cwd"
    work.mkdir()
    decision = resources.host_execution("admit", target=work, session_id="bound-ok")["decision"]
    assert decision["request_ids"]
    row = ledger.read()["requests"][decision["request_ids"][0]]
    assert row["request"]["target_path"] == str(work.resolve(strict=True))


# --- inspection contract -------------------------------------------------------


def test_inspect_publishes_digest_proof_and_guidance_without_mutating(host):
    _, ledger, _ = host
    key = _dead_row(host, "inspect-dead")
    path = ledger.path
    path.write_bytes(path.read_bytes())  # ensure the file exists for the byte comparison
    before = path.read_bytes()

    decision = resources.host_execution("inspect")["decision"]

    assert decision["code"] == "HOST_EXECUTION_INSPECTION" and decision["read_only"] is True
    row = next(item for item in decision["rows"] if item["request_id"] == key)
    assert row["state"] == "ALLOCATED" and row["target_observation"] == "PRESENT"
    assert row["holder_sha256"] == resources.canonical_holder_digest(
        json.loads(row["holder_canonical_json"]))
    assert json.loads(row["holder_canonical_json"]) == ledger.read()["requests"][key]["holder"]
    assert row["dead_holder_proof"]["pid_present"] is False
    assert row["recovery"]["supported_action"] == "clear --recover-dead-holder"
    assert row["recovery"]["expected_holder_sha256"] == row["holder_sha256"]
    assert row["recovery"]["terminal_evidence_template"]["session_id"] == "inspect-dead"
    assert path.read_bytes() == before


def test_inspect_separates_live_reserved_and_lease_bound(host):
    _, ledger, _ = host
    live_key, child = _host_reservation(host, "inspect-live", live=True)
    try:
        work = Path.home() / "ok-sub"
        work.mkdir()
        reserved = resources.host_execution("admit", target=work,
                                            session_id="inspect-reserved")["decision"]["request_ids"][0]
        rows = {row["request_id"]: row
                for row in resources.host_execution("inspect")["decision"]["rows"]}
        assert rows[live_key]["recovery"]["reason"] == "RUNTIME_OCCUPIED"
        assert rows[live_key]["recovery"]["supported_action"] is None
        assert rows[reserved]["recovery"]["supported_action"] == "cancel"
        assert rows[reserved]["state"] == "WAITING" or rows[reserved]["state"] == "RESERVED"
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_inspect_filters_by_identity_and_hides_other_uid_rows(host):
    _, ledger, _ = host
    key = _dead_row(host, "filter-me")
    foreign = json.loads(json.dumps(ledger.read()["requests"][key]))
    foreign["request"].update(session_id="not-mine", run_id="run-not-mine")
    foreign["request"]["owner"] = os.getuid() + 1
    foreign_key = resources.fingerprint(foreign["request"])
    with ledger.locked():
        state = ledger.read()
        state["requests"][foreign_key] = foreign
        ledger._save(state)
    rows = resources.host_execution("inspect", session_id="filter-me")["decision"]["rows"]
    assert [row["request_id"] for row in rows] == [key]
    all_rows = resources.host_execution("inspect")["decision"]["rows"]
    assert foreign_key not in [row["request_id"] for row in all_rows]


def test_canonical_holder_digest_is_the_documented_representation(host):
    _, ledger, _ = host
    key = _dead_row(host, "digest-shape")
    holder = ledger.read()["requests"][key]["holder"]
    canonical = json.dumps(holder, sort_keys=True, separators=(",", ":"))
    assert resources.canonical_holder_digest(holder) == resources.canonical_holder_digest(
        json.loads(json.dumps(dict(reversed(list(holder.items()))))))
    decision = resources.host_execution("inspect", request_id=key)["decision"]
    assert decision["rows"][0]["holder_canonical_json"] == canonical


# --- typed recovery and orphan release -----------------------------------------


def test_typed_clear_releases_orphan_with_missing_target(host, tmp_path):
    _, ledger, _ = host
    key = _dead_row(host, "orphan-gone")
    row = ledger.read()["requests"][key]
    moved = json.loads(json.dumps(row["request"]))
    moved["target_path"] = str(tmp_path / "vanished")
    moved["path_identity"] = [12345, 67890]
    new_key = resources.fingerprint(moved)
    binding = RUNTIME_BINDING_PREFIX + json.dumps(
        {"holder": row["holder"], "lease_id": None, "kind": "command"},
        sort_keys=True, separators=(",", ":"))
    with ledger.locked():
        state = ledger.read()
        settled = {**row, "request": moved, "runtime_bindings": [binding]}
        state["requests"].pop(key)
        state["requests"][new_key] = settled
        ledger._save(state)
    key = new_key

    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("clear", request_id=key)
    assert blocked.value.code == "RESOURCE_IDENTITY_CHANGED"

    result = _typed(host, key)
    assert result["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    saved = json.loads(Path(result["decision"]["recovery"]["path"]).read_text())
    assert saved["target_observation"] == "ABSENT"
    assert ledger.read()["requests"][key]["state"] == "RELEASED"
    replay = _typed(host, key)
    assert replay["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    assert "recovery" not in replay["decision"]


def test_typed_recovery_replays_after_crash_between_evidence_and_settle(host):
    _, ledger, _ = host
    key = _dead_row(host, "crash-replay")
    row = ledger.read()["requests"][key]
    holder = row["holder"]
    evidence = {"kind": "aw-terminal-record", "status": "EXITED",
                "ended_at": "2026-09-24T00:00:00+08:00",
                "session_id": row["request"]["session_id"],
                "run_id": row["request"]["run_id"], "end_reason": "shell_exit"}
    stale = {
        "schema_version": 1, "contract": "host-execution-recovery/v1", "request_id": key,
        "original_request": {
            "repository_id": row["request"]["repository_id"],
            "client_id": row["request"]["client_id"],
            "session_id": row["request"]["session_id"],
            "run_id": row["request"]["run_id"],
            "target_path": row["request"]["target_path"],
            "memory_bytes": row["request"]["memory_bytes"]},
        "original_holder_digest": resources.canonical_holder_digest(holder),
        "target_observation": "PRESENT",
        "dead_holder_proof": {
            "recorded_pid": holder["pid"], "recorded_boot_id": holder["boot_id"],
            "recorded_start_ticks": holder["start_ticks"], "observed_boot_id": holder["boot_id"],
            "observed_start_ticks": None, "pid_present": False, "pid_reused": False,
            "boot_changed": False},
        "terminal_evidence": evidence,
        "terminal_evidence_digest": resources.canonical_holder_digest(evidence),
        "released_state": "RELEASED", "recovered_at_ns": 1,
    }
    from worktree_controller import cleanup_evidence as e

    directory = ledger.root / "host-recoveries"
    written = e.write(directory, f"{key}.json", stale)

    result = _typed(host, key, evidence=evidence)

    assert result["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    recovery = result["decision"]["recovery"]
    assert recovery["path"] == written["path"] and recovery["sha256"] == written["sha256"]
    assert json.loads(Path(recovery["path"]).read_text())["recovered_at_ns"] == 1
    assert ledger.read()["requests"][key]["state"] == "RELEASED"


def test_digest_mismatch_and_wrong_evidence_stay_fail_closed(host):
    _, ledger, _ = host
    key = _dead_row(host, "mismatch")
    with pytest.raises(ControllerError) as blocked:
        _typed(host, key, digest=False)
    assert blocked.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"
    foreign = {"kind": "aw-terminal-record", "status": "EXITED", "ended_at": "x",
               "session_id": "other-session", "run_id": "run-other"}
    with pytest.raises(ControllerError) as blocked:
        _typed(host, key, evidence=foreign)
    assert blocked.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert not (ledger.root / "host-recoveries").exists() or not list(
        (ledger.root / "host-recoveries").glob("*.json"))


# --- concurrency and accounting --------------------------------------------------


def _child_clear(repo_env: dict[str, str], key: str, holder_digest: str, evidence: dict) -> tuple[int, str]:
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "from worktree_controller import resource_admission as r;"
        "d = r.host_execution('clear', request_id=sys.argv[2],"
        " recover_dead_holder=True, expected_holder_sha256=sys.argv[3],"
        " terminal_evidence=json.loads(sys.argv[4]))['decision'];"
        "print(d['code'])"
    )
    result = subprocess.run([sys.executable, "-c", code, str(resources.__file__.rsplit("/", 2)[0]),
                             key, holder_digest, json.dumps(evidence)],
                            capture_output=True, text=True, env=repo_env, timeout=60)
    return result.returncode, result.stdout.strip() or result.stderr[-200:]


def test_concurrent_typed_clears_converge_with_one_recovery(host):
    _, ledger, _ = host
    key = _dead_row(host, "race")
    row = ledger.read()["requests"][key]
    digest = resources.canonical_holder_digest(row["holder"])
    evidence = {"kind": "aw-terminal-record", "status": "EXITED",
                "ended_at": "2026-09-24T00:00:00+08:00",
                "session_id": row["request"]["session_id"],
                "run_id": row["request"]["run_id"], "end_reason": "shell_exit"}
    env = {**os.environ, "XDG_STATE_HOME": str(ledger.root.parents[2]),
           "PYTHONPATH": str(Path(resources.__file__).parent.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _child_clear(env, key, digest, evidence), range(2)))
    assert all(code == 0 and out.endswith("HOST_EXECUTION_CLEARED") for code, out in results), results
    assert ledger.read()["requests"][key]["state"] == "RELEASED"
    recoveries = list((ledger.root / "host-recoveries").glob("*.json"))
    assert len(recoveries) == 1


def test_unattempted_reservation_cancels_once_and_rejects_re_releases(host, tmp_path):
    _, ledger, _ = host
    work = tmp_path / "accounting"
    work.mkdir()
    admitted = resources.host_execution("admit", target=work, session_id="accounting",
                                        run_id="run-a")["decision"]
    key = admitted["request_ids"][0]
    cancelled = resources.host_execution("cancel", request_id=key)
    assert cancelled["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    assert ledger.read()["requests"][key]["state"] == "CANCELLED"
    again = resources.host_execution("cancel", request_id=key)
    assert again["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("clear", request_id=key)
    assert blocked.value.code == "ADMISSION_RECOVERY_REQUIRED"
    rows = resources.host_execution("inspect", session_id="accounting")["decision"]["rows"]
    assert rows[0]["recovery"]["reason"] == "ALREADY_TERMINAL"
    assert rows[0]["accounted_memory_bytes"] == 0


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("by_request_id", [False, True])
def test_unattempted_missing_target_cancels_with_original_identity(
    host, tmp_path, waiting, by_request_id,
):
    counters, ledger, _ = host
    if waiting:
        counters["memory"] = 0
    work = tmp_path / "unattempted"
    work.mkdir()
    identity = {"client_id": "codex", "session_id": "missing-target", "run_id": "run-a"}
    key = resources.host_execution("admit", target=work, **identity)["decision"]["request_ids"][0]
    original = ledger.read()["requests"][key]
    assert original["state"] == ("WAITING" if waiting else "RESERVED")
    work.rmdir()
    before_inspect = ledger.path.read_bytes()
    inspected = resources.host_execution("inspect", request_id=key)["decision"]["rows"][0]
    assert inspected["target_observation"] == "ABSENT"
    assert inspected["recovery"]["supported_action"] == "cancel"
    assert ledger.path.read_bytes() == before_inspect
    selector = {"request_id": key} if by_request_id else {"target": work, **identity}
    cancelled = resources.host_execution("cancel", **selector)["decision"]
    assert cancelled["code"] == "HOST_EXECUTION_CLEARED"
    assert cancelled["request_ids"] == [key] and cancelled["task_completed"] is False
    row = ledger.read()["requests"][key]
    assert row["state"] == "CANCELLED" and row["request"] == original["request"]
    assert row["history"][:-1] == original["history"]
    terminal_bytes = ledger.path.read_bytes()
    resources.host_execution("cancel", **selector)
    assert ledger.path.read_bytes() == terminal_bytes
    inspected = resources.host_execution("inspect", request_id=key)["decision"]["rows"][0]
    assert inspected["accounted_memory_bytes"] == 0
    with pytest.raises(ControllerError):
        resources.host_execution("clear", request_id=key)
    assert ledger.path.read_bytes() == terminal_bytes


@pytest.mark.parametrize("binding", ["holder", "runtime_bindings", "lease_id", "allocation_id"])
def test_missing_target_cancel_keeps_attempted_or_bound_reservation(host, tmp_path, binding):
    _, ledger, _ = host
    work = tmp_path / "bound"
    work.mkdir()
    key = resources.host_execution("admit", target=work, session_id="bound")["decision"]["request_ids"][0]
    with ledger.locked():
        state = ledger.read()
        row = state["requests"][key]
        if binding == "allocation_id":
            row["request"][binding] = "bound-allocation"
        else:
            row[binding] = ["runtime:unverified"] if binding == "runtime_bindings" else "bound"
        ledger._save(state)
    work.rmdir()
    before = ledger.path.read_bytes()
    with pytest.raises(ControllerError):
        resources.host_execution("cancel", request_id=key)
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["changed", "uncertain", "foreign", "broad"])
def test_missing_target_cancel_preserves_identity_and_scope_guards(host, tmp_path, monkeypatch, failure):
    _, ledger, _ = host
    work = tmp_path / "guarded"
    work.mkdir()
    key = resources.host_execution("admit", target=work, session_id="guarded")["decision"]["request_ids"][0]
    if failure == "changed":
        work.rename(tmp_path / "original")
        work.mkdir()
    elif failure == "uncertain":
        original_lstat = os.lstat

        def uncertain(path, *args, **kwargs):
            if Path(path) == work:
                raise PermissionError("fixture cannot observe target")
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", uncertain)
    else:
        work.rmdir()
        with ledger.locked():
            state = ledger.read()
            request = state["requests"][key]["request"]
            if failure == "foreign":
                request["owner"] = os.getuid() + 1
            else:
                missing_home = tmp_path / "absent-home"
                monkeypatch.setattr(Path, "home", classmethod(lambda cls: missing_home))
                request["target_path"] = str(missing_home)
            ledger._save(state)
    before = ledger.path.read_bytes()
    with pytest.raises((ControllerError, PermissionError)):
        resources.host_execution("cancel", request_id=key)
    assert ledger.path.read_bytes() == before


def test_missing_target_cancel_by_identity_refuses_wrong_target(host, tmp_path):
    _, ledger, _ = host
    work = tmp_path / "owned"
    work.mkdir()
    identity = {"session_id": "owned", "run_id": "run-a"}
    resources.host_execution("admit", target=work, **identity)
    work.rmdir()
    before = ledger.path.read_bytes()
    with pytest.raises(ControllerError, match="identity changed"):
        resources.host_execution("cancel", target=tmp_path / "different", **identity)
    assert ledger.path.read_bytes() == before
