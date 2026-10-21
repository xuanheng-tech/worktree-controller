"""CLI measurements must retain real authority, ledger and cleanup checks."""

import json
import runpy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from worktree_controller import __version__, cli, resource_admission
from worktree_controller.models import WriterHolder
from worktree_controller.writer_lease import holder_is_live

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/benchmark-cli"


@pytest.fixture
def benchmark():
    return runpy.run_path(str(SCRIPT))


@pytest.fixture
def controller(tmp_path):
    path = tmp_path / "controller"
    path.write_text(f"#!{sys.executable}\nfrom worktree_controller.cli import main\n"
                    "raise SystemExit(main())\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("case", ("available", "memory-recovery", "identity-recovery"))
def test_fresh_installed_processes_measure_and_verify_each_admission_path(benchmark, controller, tmp_path, case):
    result = benchmark["measure_admission"](
        controller, f"worktree-controller {__version__}", tmp_path / case, 2, case, 30)
    proof = result["verification"]
    assert proof["budget_verified"] and proof["request_idempotent"]
    assert proof["live_writer_unchanged"] and proof["scope_unchanged"] and proof["publication_unchanged"]
    assert proof["finished_allocated"] == (2 if case == "available" else 0)
    assert proof["finished_released"] == {"available": 0, "memory-recovery": 3, "identity-recovery": 2}[case]
    assert proof["post_resume_available"] == (case == "identity-recovery")
    assert result["seconds"] > 0 and result["peak_rss_kib"] > 0
    initial = json.loads((tmp_path / case / "first-result.json").read_text())
    assert initial["exit_code"] == (2 if case == "identity-recovery" else 0)
    rows = json.loads((tmp_path / case / "host-state/worktree-controller/resource-admission/v1/state.json").read_text())["requests"]
    holders = [row["holder"] for row in rows.values() if row.get("holder")]
    assert all(not holder_is_live(WriterHolder(**holder)) for holder in holders)


@pytest.mark.parametrize("drift", ("guard", "scope", "refusal"))
def test_verification_rejects_changed_live_authority_or_scope(benchmark, tmp_path, monkeypatch, capsys, drift):
    state = benchmark["admission_fixture"](tmp_path / "fixture", 2, "available")
    try:
        monkeypatch.setenv("HOME", state["env"]["HOME"])
        monkeypatch.setenv("XDG_STATE_HOME", state["env"]["XDG_STATE_HOME"])
        monkeypatch.setattr(resource_admission, "pressure", lambda: {
            "available_memory_bytes": state["memory"], "observed_at_ns": 1,
            "psi_avg10": {"cpu": 0.0, "io": 0.0, "memory": 0.0}})
        code = cli.main(["execution-admit", "--repo", str(state["repo"]),
            "--path", state["target"]["path"], "--owner", "fixture-owner", "--client-id", "fixture",
            "--session-id", "target", "--run-id", "new", "--json"])
        payload = json.loads(capsys.readouterr().out)
        benchmark["verify_admission"](state, "available", payload, code)
        if drift == "guard":
            with state["ledger"].locked():
                rows = state["ledger"].read()
                rows["requests"][state["guard_key"]]["updated_at_ns"] += 1
                state["ledger"]._save(rows)
        elif drift == "scope":
            record = state["store"].load().records[state["target"]["allocation_id"]]
            state["store"].replace_record(replace(record, scope_paths=("outside.txt",)))
        else:
            (state["repo"].parent / "recovery-refusals.json").write_text(
                json.dumps([{"code": "RUNTIME_OCCUPIED", "message": "fixture visibility refusal"}]))
        with pytest.raises(ValueError, match="changed live writer|changed the scope fence|recovery refused"):
            benchmark["verify_admission"](state, "available", payload, code)
    finally:
        benchmark["stop_processes"](state["processes"])


def test_wrong_runtime_version_fails_and_reaps_fixture_holders(benchmark, controller, tmp_path):
    with pytest.raises(ValueError, match="different Controller version"):
        benchmark["measure_admission"](controller, "worktree-controller 0.0.0", tmp_path / "mismatch", 0, "available", 30)


@pytest.mark.parametrize("case", ("memory-recovery", "identity-recovery"))
def test_execution_profile_retains_initial_recovery_instead_of_untimed_replays(
        benchmark, controller, tmp_path, case):
    root = tmp_path / case
    result = benchmark["measure_admission"](controller, f"worktree-controller {__version__}",
        root, 2, case, 30, repo_count=2, profile=True)
    assert result["profile"]["metadata_snapshot_loads"] > 1
    assert result["profile"] == json.loads((root / "profile.json").read_text())
    assert result["verification"]["request_idempotent"]
    initial = json.loads((root / "first-result.json").read_text())
    assert initial["exit_code"] == (2 if case == "identity-recovery" else 0)


def test_existing_output_is_rejected_without_changes(controller, tmp_path):
    marker = tmp_path / "keep"
    marker.write_text("caller-owned")
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(tmp_path),
        "--mode", "execution-admit", "--controller", f"candidate={controller}"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "must not exist" in result.stderr
    assert marker.read_text() == "caller-owned" and not (tmp_path / "results.json").exists()


def test_query_mode_still_measures_all_original_commands(controller, tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(tmp_path / "queries"),
        "--controller", f"candidate={controller}", "--sizes", "0", "--repeats", "1"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rows = json.loads((tmp_path / "queries/results.json").read_text())["measurements"]
    assert [row["command"] for row in rows] == ["status", "doctor", "lifecycle-history"]
    assert all(len(row["samples"]) == 1 and row["first_call"] for row in rows)


def test_case_selection_finishes_only_the_requested_case(controller, tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(tmp_path / "selection"),
        "--mode", "execution-admit", "--admission-cases", "available", "--controller", f"candidate={controller}",
        "--sizes", "0", "--repeats", "1"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads((tmp_path / "selection/results.json").read_text())
    assert data["status"] == "PASS" and len(data["measurements"]) == 1
    assert data["measurements"][0]["case"] == "available"
    assert set(data["namespaces"]) == {"pid", "mnt", "user"}


def test_timeout_terminates_the_measured_process(benchmark, tmp_path):
    pid_file = tmp_path / "pid"
    program = "import os,time; from pathlib import Path; Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        benchmark["timed_process"]([sys.executable, "-c", program, str(pid_file)], tmp_path / "rss", 0.5)
    process = Path(f"/proc/{int(pid_file.read_text())}/stat")
    try:
        observed = process.read_text()
    except (FileNotFoundError, ProcessLookupError):
        return
    # A namespace init can retain an exited grandchild until the namespace ends.
    assert observed[observed.rfind(")") + 2:].split()[0] in {"Z", "X"}


@pytest.mark.parametrize("case", ("available", "memory-recovery"))
def test_session_admission_with_repository_and_host_history_checks_its_live_holder(
        benchmark, controller, tmp_path, case):
    result = benchmark["measure_admission"](controller, f"worktree-controller {__version__}",
        tmp_path / case, 2, case, 30, repo_count=2, mode="session-admit", profile=True)
    proof = result["verification"]
    assert all(proof[name] for name in ("live_writer_unchanged", "scope_unchanged", "publication_unchanged",
        "repository_history_unchanged", "session_fallback_unchanged", "request_idempotent", "budget_verified",
        "live_holder_bound", "writer_lease_verified", "write_guard_allowed"))
    assert proof["finished_released"] == (0 if case == "available" else 3)
    assert result["profile"]["metadata_snapshot_loads"] > 1
    assert result["profile"]["metadata_decoders"]["release_receipt_from_dict"] > 0
    rows = json.loads((tmp_path / case / "host-state/worktree-controller/resource-admission/v1/state.json").read_text())["requests"]
    assert all(not holder_is_live(WriterHolder(**row["holder"])) for row in rows.values() if row.get("holder"))


def test_session_mode_keeps_the_two_history_dimensions_and_excludes_extra_profile_calls(controller, tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(tmp_path / "session"),
        "--mode", "session-admit", "--admission-cases", "available", "--controller", f"candidate={controller}",
        "--sizes", "1", "--repo-sizes", "0", "2", "--repeats", "1", "--profile"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads((tmp_path / "session/results.json").read_text())
    assert data["status"] == "PASS"
    assert [(r["command"], r["history_size"], r["repo_history_size"]) for r in data["measurements"]] == [
        ("session-admit", 1, 0), ("session-admit", 1, 2)]
    assert all(len(r["samples"]) == 1 and r["profile_diagnostic"]["profile"] for r in data["measurements"])


@pytest.mark.parametrize("extra", (["--repo-sizes", "-1"], ["--admission-cases", "identity-recovery"]))
def test_invalid_session_dimensions_are_rejected_before_fixture_creation(controller, tmp_path, extra):
    root = tmp_path / "invalid"
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "session-admit", "--controller", f"candidate={controller}", *extra],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and not root.exists()


def test_first_session_timeout_preserves_running_evidence_and_reaps_fixture_holders(controller, tmp_path):
    root = tmp_path / "timeout"
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "session-admit", "--controller", f"candidate={controller}",
        "--sizes", "0", "--repeats", "1", "--timeout", "0.001"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    data = json.loads((root / "results.json").read_text())
    assert data["status"] == "RUNNING" and not data["measurements"]
    rows = json.loads(next(root.glob("admit-*/host-state/worktree-controller/resource-admission/v1/state.json")).read_text())["requests"]
    assert all(not holder_is_live(WriterHolder(**row["holder"])) for row in rows.values() if row.get("holder"))


def test_verification_rejects_changed_closed_repository_history(benchmark, tmp_path, monkeypatch, capsys):
    state = benchmark["admission_fixture"](tmp_path / "history-drift", 0, "available", repo_count=2)
    try:
        monkeypatch.setenv("HOME", state["env"]["HOME"])
        monkeypatch.setenv("XDG_STATE_HOME", state["env"]["XDG_STATE_HOME"])
        monkeypatch.setattr(resource_admission, "pressure", lambda: {
            "available_memory_bytes": state["memory"], "observed_at_ns": 1,
            "psi_avg10": {"cpu": 0.0, "io": 0.0, "memory": 0.0}})
        code = cli.main(["execution-admit", "--repo", str(state["repo"]),
            "--path", state["target"]["path"], "--owner", "fixture-owner", "--client-id", "fixture",
            "--session-id", "target", "--run-id", "new", "--json"])
        payload = json.loads(capsys.readouterr().out)
        record = next(iter(state["history_records"].values()))
        state["store"].replace_record(replace(record, module="changed-history"))
        with pytest.raises(ValueError, match="changed closed repository history"):
            benchmark["verify_admission"](state, "available", payload, code)
    finally:
        benchmark["stop_processes"](state["processes"])


@pytest.mark.parametrize("profile", (False, True))
@pytest.mark.parametrize("case", ("active-write", "handoff-read", "frozen-write", "foreign-session"))
@pytest.mark.parametrize("action", ("check", "host-check"))
def test_writer_checks_use_real_holder_and_leave_metadata_unchanged(benchmark, controller, tmp_path, case, action, profile):
    root = tmp_path / "fixture"
    with benchmark["writer_fixture"](root, 2) as state:
        result = benchmark["measure_writer_check"](controller, state, case, action, tmp_path / "call.json", 15, profile=profile)
        proof = result["verification"]
        assert proof["metadata_unchanged"] and proof["write_target_absent"]
        assert proof["exit_code"] == (2 if case in {"frozen-write", "foreign-session"} else 0)
        assert proof["lease_id"] in {lease.lease_id for lease in state["leases"].values()}
        assert result["seconds"] > 0 and result["peak_rss_kib"] > 0
        if profile:
            diagnostic = result["profile"]
            assert diagnostic == json.loads((tmp_path / "call.profile.json").read_text())
            assert diagnostic["metadata_snapshot_loads"] >= 1
            assert diagnostic["metadata_snapshot_seconds"] > 0
            assert diagnostic["metadata_decoders"]["release_receipt_from_dict"] >= 2
            assert diagnostic["top_cumulative"]
        else:
            assert "profile" not in result and not (tmp_path / "call.profile.json").exists()
        assert benchmark["writer_metadata"](state) == json.loads((root / "metadata-before.json").read_text())
    cleanup = json.loads((root / "fixture-cleanup.json").read_text())
    assert cleanup["status"] == "PASS" and len(cleanup["closed"]) == 2
    assert cleanup["writer_leases"] == cleanup["linked_checkouts"] == 0
    assert not state["store"].load().writer_leases
    assert all(not Path(target["path"]).exists() for target in state["targets"])


@pytest.mark.parametrize("drift", ("admission", "code", "allocation", "metadata"))
def test_writer_verification_rejects_wrong_authority_and_metadata_drift(benchmark, controller, tmp_path, drift):
    with benchmark["writer_fixture"](tmp_path / "fixture", 0) as state:
        output = tmp_path / "call.json"
        benchmark["measure_writer_check"](controller, state, "handoff-read", "host-check", output, 15)
        first = json.loads(output.read_text())
        if drift == "admission":
            first["payload"]["decision"]["admission"] = "GRANTED"
        elif drift == "code":
            first["payload"]["decision"]["code"] = "WRITE_ALLOWED"
        elif drift == "allocation":
            first["payload"]["decision"]["allocation_id"] = state["targets"][0]["allocation_id"]
        else:
            path = state["store"].records_dir / f'{state["targets"][1]["allocation_id"]}.json'
            path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(ValueError, match="unexpected authority|changed repository or host metadata"):
            benchmark["verify_writer_check"](state, "handoff-read", first["payload"], first["exit_code"])


def test_writer_mode_records_all_cases_and_keeps_labels_out_of_output_paths(controller, tmp_path):
    root = tmp_path / "writer"
    label = str(tmp_path / "caller-label")
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "writer-check", "--controller", f"{label}={controller}",
        "--sizes", "0", "--repeats", "1", "--profile"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads((root / "results.json").read_text())
    assert data["status"] == "PASS" and len(data["measurements"]) == 8
    assert data["controllers"][label]["writer_contract"]["host_handoff_continuation_version"] >= 2
    for row in data["measurements"]:
        assert row["first_call"] and len(row["samples"]) == 1
        assert "profile" not in row["first_call"] and "profile" not in row["samples"][0]
        assert row["median_seconds"] == row["samples"][0]["seconds"]
        assert row["profile_diagnostic"]["profile"]["metadata_snapshot_loads"] >= 1
        assert row["first_call"]["payload_sha256"] == row["samples"][0]["payload_sha256"] == row["profile_diagnostic"]["payload_sha256"]
        assert Path(row["profile_diagnostic"]["output"]).parent == root / "writer-0/calls"
        assert row["first_call"]["verification"]["metadata_unchanged"]
        assert Path(row["samples"][0]["output"]).parent == root / "writer-0/calls"
    assert data["fixture_cleanup"][0]["status"] == "PASS"
    assert data["fixture_cleanup"][0]["writer_leases"] == data["fixture_cleanup"][0]["linked_checkouts"] == 0
    assert not list(tmp_path.glob("caller-label-*.json"))


def test_writer_mode_refuses_changed_json_and_closes_its_fixtures(controller, tmp_path):
    changed = tmp_path / "changed-controller"
    changed.write_text(f"#!{sys.executable}\nimport contextlib, io, json, sys\n"
        "from worktree_controller.cli import main\n"
        "if sys.argv[1] == 'writer-session':\n"
        "    output = io.StringIO()\n"
        "    with contextlib.redirect_stdout(output):\n        code = main()\n"
        "    payload = json.loads(output.getvalue())\n"
        "    payload['decision']['reason'] = 'changed response'\n"
        "    print(json.dumps(payload))\n    raise SystemExit(code)\n"
        "raise SystemExit(main())\n")
    changed.chmod(0o755)
    root = tmp_path / "changed"
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "writer-check", "--controller", f"before={controller}",
        "--controller", f"after={changed}", "--sizes", "0", "--repeats", "1"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "writer CLI output changed" in result.stderr
    assert json.loads((root / "results.json").read_text())["status"] == "RUNNING"
    cleanup = json.loads((root / "writer-0/fixture-cleanup.json").read_text())
    assert cleanup["status"] == "PASS" and len(cleanup["closed"]) == 2
    assert cleanup["writer_leases"] == cleanup["linked_checkouts"] == 0


def test_writer_timeout_retains_failure_and_releases_fixture_authority(tmp_path):
    controller = tmp_path / "slow-controller"
    controller.write_text(f"#!{sys.executable}\nimport sys, time\n"
        "from worktree_controller.cli import main\n"
        "if sys.argv[1] == 'writer-session':\n    time.sleep(60)\n"
        "raise SystemExit(main())\n")
    controller.chmod(0o755)
    root = tmp_path / "timeout"
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "writer-check", "--controller", f"candidate={controller}",
        "--sizes", "0", "--repeats", "1", "--timeout", "0.1"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "TimeoutExpired" in result.stderr
    data = json.loads((root / "results.json").read_text())
    assert data["status"] == "RUNNING" and not data["measurements"]
    cleanup = json.loads((root / "writer-0/fixture-cleanup.json").read_text())
    assert cleanup["status"] == "PASS" and len(cleanup["closed"]) == 2
    assert cleanup["writer_leases"] == cleanup["linked_checkouts"] == 0


def test_writer_mode_refuses_missing_continuation_contract_before_creating_fixtures(tmp_path):
    controller = tmp_path / "incompatible-controller"
    controller.write_text(f"#!{sys.executable}\nimport json, sys\n"
        "from worktree_controller.cli import main\n"
        "if sys.argv[1] == 'capabilities':\n"
        "    print(json.dumps({'errors': [], 'decision': {'host_handoff_continuation_version': 1}}))\n"
        "else:\n    raise SystemExit(main())\n")
    controller.chmod(0o755)
    root = tmp_path / "incompatible"
    result = subprocess.run([sys.executable, str(SCRIPT), "--work-dir", str(root),
        "--mode", "writer-check", "--controller", f"candidate={controller}",
        "--sizes", "0", "--repeats", "1"], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "host_handoff_continuation_version" in result.stderr
    assert not list(root.glob("writer-*")) and not (root / "results.json").exists()
