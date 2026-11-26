"""Real cross-version read/write and rollback compatibility tests against active 1.36.5 (b677c77)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

from tests.test_auto_release import POLICY_AUTO, _publish_source
from tests.test_controlled import (
    _commit_reproducible_project,
    _configure_remote,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_shadow import GitRepository, git
from tests.test_slim_and_cleanup_retention import POLICY_V8
from worktree_controller import cleanup_evidence, lifecycle
from worktree_controller.git_backend import inventory
from worktree_controller.store import MetadataStore, now_iso

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "b677c77fec638ab75eb4641c2fb481377fa483db"


def _extract_baseline(dest: Path) -> Path:
    """Extract the exact live 1.36.5 baseline source via git archive."""
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE_COMMIT],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    import io

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        tar.extractall(path=dest, filter="data")
    assert (dest / "src/worktree_controller/__init__.py").is_file()
    return dest / "src"


def _run_py(src_dir: Path, code: str, *args: str, state_home: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state_home.parent),
        "XDG_STATE_HOME": str(state_home),
        "PYTHONPATH": str(src_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


def _run_cli(src_dir: Path, cli_args: list[str], *, state_home: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state_home.parent),
        "XDG_STATE_HOME": str(state_home),
        "PYTHONPATH": str(src_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "worktree_controller.cli", *cli_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


def _large_topology(count: int = 700) -> dict:
    groups = {
        f"66306:{idx}": {
            "classification": "single-link",
            "content": {"content_sha256": f"{idx:064x}"},
            "expected_steps": [
                {
                    "deleted_links": [["apps/docs/node_modules", f"pkg-{idx}/index.js"]],
                    "expected_nlink": 0,
                    "resource": "apps/docs/node_modules",
                    "surviving_paths": [],
                }
            ],
            "identity": [66306, idx, 33204, 128, 1, 1, 1000, 1000, 1],
            "members": [["apps/docs/node_modules", f"pkg-{idx}/index.js"]],
        }
        for idx in range(count)
    }
    return {
        "contract": "cleanup-hardlink-topology/v1",
        "resources": ["apps/docs/node_modules"],
        "anchors": {},
        "groups": groups,
    }


def _assert_all_metadata_json_plaintext(meta_root: Path) -> int:
    """Assert every persisted .json under worktree-controller/v1 is UTF-8 JSON, never gzip."""
    count = 0
    for path in sorted(meta_root.rglob("*.json")):
        raw = path.read_bytes()
        assert not raw.startswith(b"\x1f\x8b"), f"gzip persisted file breaks b677c77 rollback: {path}"
        parsed = json.loads(raw.decode("utf-8"))
        assert isinstance(parsed, dict), f"non-object metadata json: {path}"
        count += 1
    assert count > 0
    return count


def test_new_version_writes_are_100pct_compatible_with_b677c77_rollback(tmp_path: Path) -> None:
    """(a) New 1.36.6 writes transactions/receipts/actions/recoveries/records/intents -> b677c77 reads, doctor, status, reconcile."""
    old_src = _extract_baseline(tmp_path / "wc_b677c77")
    state_home = tmp_path / "home" / ".local/state"
    state_home.mkdir(parents=True)

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)

        # Perform full publication + release + second worktree with .venv teardown in 1.36.6.
        _publish_source(repository, source_task="compat-src", integration_task="compat-int")
        wt2, acq2 = acquire_one(repository, task="compat-venv", authority="compat-venv")
        alloc2 = str(acq2["decision"]["allocation_id"])
        _create_reproducible_venv(wt2)
        reg = lifecycle.register_disposable(
            repository.repo,
            target=wt2,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="compat-test",
            teardown_policy="on-release",
        )
        handoff_one(repository, wt2, expected=reg["decision"]["record_version"])
        lifecycle.release(
            repository.repo,
            target=wt2,
            expected_record_version=reg["decision"]["record_version"] + 1,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )

        # Write >200 KiB topology evidence into all 4 persistence categories.
        store = MetadataStore(inventory(repository.repo).common_dir)
        topology = _large_topology(700)
        rid = str(uuid.uuid4())
        tx_id = str(uuid.uuid4())
        rec_tx_id = str(uuid.uuid4())

        action_ref = cleanup_evidence.write(
            store.cleanup_actions_dir / rid,
            "0000-before.json",
            {"phase": "BEFORE", "topology": topology},
        )
        recovery_ref = cleanup_evidence.write(
            store.cleanup_recoveries_dir / rec_tx_id,
            f"{uuid.uuid4()}.json",
            {
                "contract": "cleanup-recovery/v1",
                "transaction_id": rec_tx_id,
                "allocation_id": alloc2,
                "topology": topology,
            },
        )
        tx_payload = {
            "transaction_id": tx_id,
            "allocation_id": alloc2,
            "command": "cleanup-disposable",
            "cleanup_plan": {"hardlink_topology": topology},
        }
        store.write_transaction(tx_id, tx_payload)
        store.write_completion_receipt(
            rid,
            {
                "schema_version": 1,
                "receipt_id": rid,
                "contract": "typed-disposable-resource/v1",
                "result": "CLEANED",
                "plan": {
                    "allocation_id": alloc2,
                    "target_path": str(wt2),
                    "hardlink_topology": topology,
                },
                "created_at": now_iso(),
            },
        )

        # Every single persisted JSON file must be pure UTF-8 plaintext (zero gzip).
        _assert_all_metadata_json_plaintext(store.root)

        # Old b677c77 must read transaction, completion-receipt, cleanup-action, cleanup-recovery, and full snapshot cleanly.
        read_check = _run_py(
            old_src,
            """
import json, sys
from pathlib import Path
from worktree_controller import __version__, cleanup_evidence
from worktree_controller.git_backend import inventory
from worktree_controller.store import MetadataStore

repo, tx_id, rid, action_path, action_sha, rec_path, rec_sha = sys.argv[1:]
assert __version__ == "1.36.5", __version__
store = MetadataStore(inventory(Path(repo)).common_dir)
snap = store.load()
tx = store._read_json(store.transactions_dir / f"{tx_id}.json")
action_doc, action_actual = cleanup_evidence.read(Path(action_path), action_sha)
rec_doc, rec_actual = cleanup_evidence.read(Path(rec_path), rec_sha)
comp_doc, _ = cleanup_evidence.read(store.completion_receipts_dir / f"{rid}.json")
print(json.dumps({
    "version": __version__,
    "records": len(snap.records),
    "intents": len(snap.integration_intents),
    "pub_receipts": len(snap.publication_receipts),
    "release_receipts": len(snap.release_receipts),
    "tx_groups": len(tx["cleanup_plan"]["hardlink_topology"]["groups"]),
    "action_sha_ok": action_actual == action_sha and len(action_doc["topology"]["groups"]) == 700,
    "recovery_sha_ok": rec_actual == rec_sha and len(rec_doc["topology"]["groups"]) == 700,
    "comp_groups": len(comp_doc["plan"]["hardlink_topology"]["groups"]),
}))
""",
            str(repository.repo),
            tx_id,
            rid,
            action_ref["path"],
            action_ref["sha256"],
            recovery_ref["path"],
            recovery_ref["sha256"],
            state_home=state_home,
        )
        assert read_check["version"] == "1.36.5"
        assert read_check["records"] >= 3
        assert read_check["intents"] >= 1
        assert read_check["pub_receipts"] >= 1
        assert read_check["release_receipts"] >= 2
        assert read_check["tx_groups"] == 700
        assert read_check["action_sha_ok"] is True
        assert read_check["recovery_sha_ok"] is True
        assert read_check["comp_groups"] == 700

        # Remove synthetic unfinished transaction so doctor & reconcile cleanup-evidence test cleanly.
        store.remove_transaction(tx_id)

        old_status = _run_cli(
            old_src, ["status", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert old_status["tool_version"] == "1.36.5"
        assert old_status["errors"] == []

        old_doctor = _run_cli(
            old_src, ["doctor", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert old_doctor["tool_version"] == "1.36.5"
        assert old_doctor["errors"] == []
        assert old_doctor["decision"]["code"] == "PASS", old_doctor

        old_reconcile = _run_cli(
            old_src,
            ["reconcile", "cleanup-evidence", "--repo", str(repository.repo), "--apply", "--json"],
            state_home=state_home,
        )
        assert old_reconcile["tool_version"] == "1.36.5"
        assert old_reconcile["errors"] == []
        assert old_reconcile["decision"]["result"]["pruned_receipts"] == 1
        assert old_reconcile["decision"]["result"]["pruned_action_dirs"] == 1


def test_old_b677c77_upgrade_to_new_and_rollback_to_b677c77_roundtrip(tmp_path: Path) -> None:
    """(b) b677c77 writes -> 1.36.6 reads/writes -> rollback to b677c77 stays 100% lossless."""
    old_src = _extract_baseline(tmp_path / "wc_b677c77")
    new_src = ROOT / "src"
    state_home = tmp_path / "home" / ".local/state"
    state_home.mkdir(parents=True)

    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        _commit_reproducible_project(repository)
        _configure_remote(repository)

        # Phase 1: Old b677c77 initializes governance and runs a live task.
        old_adopt = _run_cli(
            old_src,
            [
                "adopt", "--repo", str(repository.repo),
                "--path", str(repository.repo),
                "--task-key", "canonical",
                "--authority-key", "canonical",
                "--module", "repository",
                "--role", "canonical",
                "--owner", "repo-owner",
                "--lifecycle", "ACTIVE",
                "--json",
            ],
            state_home=state_home,
        )
        assert old_adopt["tool_version"] == "1.36.5"
        old_wt = repository.root / "wt-old"
        old_acq = _run_cli(
            old_src,
            [
                "acquire", "--repo", str(repository.repo),
                "--path", str(old_wt),
                "--task-key", "old-task",
                "--authority-key", "old-auth",
                "--module", "mod",
                "--role", "feature",
                "--owner", "old-owner",
                "--branch", "feat/old-task",
                "--base-ref", "main",
                "--scope", "old.txt",
                "--json",
            ],
            state_home=state_home,
        )
        assert old_acq["tool_version"] == "1.36.5"
        (old_wt / "old.txt").write_text("from b677c77\n")
        git(old_wt, "add", "old.txt")
        git(old_wt, "commit", "-m", "commit under b677c77")
        old_park = _run_cli(
            old_src,
            [
                "park", "--repo", str(repository.repo),
                "--path", str(old_wt),
                "--expected-record-version", "1",
                "--owner", "old-owner",
                "--json",
            ],
            state_home=state_home,
        )
        assert old_park["decision"]["code"] == "RETAINED"

        # Phase 2: Upgrade to 1.36.6 — read old state, resume/handoff/release old worktree,
        # and acquire + park a new worktree under 1.36.6.
        new_doc = _run_cli(
            new_src, ["doctor", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert new_doc["tool_version"] == "1.36.6"
        assert new_doc["decision"]["code"] == "PASS"

        resumed = _run_cli(
            new_src,
            [
                "resume", "--repo", str(repository.repo),
                "--path", str(old_wt),
                "--expected-record-version", "2",
                "--owner", "old-owner",
                "--json",
            ],
            state_home=state_home,
        )
        assert resumed["decision"]["code"] == "RESUMED"
        old_head = git(old_wt, "rev-parse", "HEAD")
        handed = _run_cli(
            new_src,
            [
                "handoff", "--repo", str(repository.repo),
                "--path", str(old_wt),
                "--expected-record-version", "3",
                "--kind", "integration-ready",
                "--commit", old_head,
                "--finalizer-record-id", "fin-old-task",
                "--json",
            ],
            state_home=state_home,
        )
        assert handed["decision"]["code"] == "HANDOFF_READY"
        released = _run_cli(
            new_src,
            [
                "release", "--repo", str(repository.repo),
                "--path", str(old_wt),
                "--expected-record-version", "4",
                "--owner-release", "old-owner",
                "--json",
            ],
            state_home=state_home,
        )
        assert released["decision"]["code"] == "RELEASED"

        new_wt = repository.root / "wt-new"
        new_acq = _run_cli(
            new_src,
            [
                "acquire", "--repo", str(repository.repo),
                "--path", str(new_wt),
                "--task-key", "new-task",
                "--authority-key", "new-auth",
                "--module", "mod",
                "--role", "feature",
                "--owner", "new-owner",
                "--branch", "feat/new-task",
                "--base-ref", "main",
                "--scope", "new.txt",
                "--json",
            ],
            state_home=state_home,
        )
        assert new_acq["tool_version"] == "1.36.6"
        (new_wt / "new.txt").write_text("from 1.36.6\n")
        git(new_wt, "add", "new.txt")
        git(new_wt, "commit", "-m", "commit under 1.36.6")
        new_head = git(new_wt, "rev-parse", "HEAD")
        _run_cli(
            new_src,
            [
                "handoff", "--repo", str(repository.repo),
                "--path", str(new_wt),
                "--expected-record-version", "1",
                "--kind", "integration-ready",
                "--commit", new_head,
                "--finalizer-record-id", "fin-new-task",
                "--json",
            ],
            state_home=state_home,
        )

        # Verify all persisted metadata files remain plaintext JSON.
        store = MetadataStore(inventory(repository.repo).common_dir)
        _assert_all_metadata_json_plaintext(store.root)

        # Phase 3: Roll back to b677c77 — status, doctor, history, and releasing the 1.36.6 checkout must all pass cleanly.
        rb_status = _run_cli(
            old_src, ["status", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert rb_status["tool_version"] == "1.36.5"
        assert rb_status["errors"] == []

        rb_doctor = _run_cli(
            old_src, ["doctor", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert rb_doctor["tool_version"] == "1.36.5"
        assert rb_doctor["decision"]["code"] == "PASS", rb_doctor

        rb_hist = _run_cli(
            old_src,
            ["lifecycle-history", "--repo", str(repository.repo), "--task-key", "old-task", "--json"],
            state_home=state_home,
        )
        assert rb_hist["errors"] == []
        assert len(rb_hist["decision"]["closed_releases"]) == 1

        rb_release = _run_cli(
            old_src,
            [
                "release", "--repo", str(repository.repo),
                "--path", str(new_wt),
                "--expected-record-version", "2",
                "--owner-release", "new-owner",
                "--json",
            ],
            state_home=state_home,
        )
        assert rb_release["tool_version"] == "1.36.5"
        assert rb_release["decision"]["code"] == "RELEASED"
        assert not new_wt.exists()
        assert git(repository.repo, "rev-parse", "refs/heads/feat/new-task") == new_head

        final_doc = _run_cli(
            old_src, ["doctor", "--repo", str(repository.repo), "--json"], state_home=state_home
        )
        assert final_doc["decision"]["code"] == "PASS", final_doc
