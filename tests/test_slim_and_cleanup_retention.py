"""Tests for source-side output slimming and safe terminal cleanup evidence retention."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_convergence import POLICY_V8
from tests.test_shadow import GitRepository, git
from worktree_controller import cleanup_retention
from worktree_controller.cli import main
from worktree_controller.controller import doctor, plan, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    abandon_integration_intent,
    complete_no_change,
    enqueue_integration,
    handoff,
    integration_queue,
    park,
    release,
    reopen,
    resume,
    update_external_binding,
)
from worktree_controller.store import MetadataStore, now_iso


def _version(repository: GitRepository, target: Path) -> int:
    return int(
        next(
            row["record_version"]
            for row in status(repository.repo)["worktrees"]
            if row["path"] == str(target)
        )
    )


def _cli_json(capsys, *argv: str) -> tuple[int, dict, str]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out), out


def _close_and_release(repository: GitRepository, target: Path) -> dict:
    complete_no_change(
        repository.repo,
        target=target,
        expected_record_version=_version(repository, target),
        owner="module-owner",
    )
    return release(
        repository.repo,
        target=target,
        expected_record_version=_version(repository, target),
        owner_release="module-owner",
    )


def test_status_compact_default_and_all_or_full_status(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        first, _ = acquire_one(repository, task="task-a", authority="auth-a")
        second, _ = acquire_one(repository, task="task-b", authority="auth-b")
        _close_and_release(repository, second)

        code, compact_payload, compact_raw = _cli_json(capsys, "status", "--repo", str(repository.repo))
        assert code == 0
        # Compact single-line JSON (no indentation newlines inside JSON object).
        assert len(compact_raw.strip().splitlines()) == 1
        assert compact_payload["decision"] is None
        summary = compact_payload["repository"]["summary"]
        assert summary["records_total"] == 3
        assert summary["records_released"] == 1
        assert summary["worktrees_by_lifecycle"]["ACTIVE"] == 2
        row = next(w for w in compact_payload["worktrees"] if w["path"] == str(first))
        assert "release_note" not in row
        assert "quarantine_expected_commit" not in row
        assert row["external_bindings"] == []
        assert row["lifecycle_observation"]["lease"]["present"] is False
        assert "heartbeat" not in row["lifecycle_observation"]

        code_full, full_payload, _ = _cli_json(
            capsys, "status", "--repo", str(repository.repo), "--full-status"
        )
        assert code_full == 0
        full_row = next(w for w in full_payload["worktrees"] if w["path"] == str(first))
        assert "release_note" in full_row
        assert "quarantine_expected_commit" in full_row
        assert "heartbeat" in full_row["lifecycle_observation"]
        assert len(json.dumps(compact_payload)) < len(json.dumps(full_payload))

        code_all, all_payload, _ = _cli_json(
            capsys, "status", "--repo", str(repository.repo), "--all"
        )
        assert code_all == 0
        assert all_payload["decision"]["code"] == "STATUS_ALL"
        assert all_payload["decision"]["released_records_count"] == 1
        assert all_payload["decision"]["released_records"][0]["task_key"] == "task-b"


def test_routine_commands_return_affected_row_or_empty_by_default(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        first, acq1 = acquire_one(repository, task="task-1", authority="auth-1")
        second, acq2 = acquire_one(repository, task="task-2", authority="auth-2")
        # acquire returns only the newly acquired worktree row by default.
        assert [row["path"] for row in acq1["worktrees"]] == [str(first)]
        assert [row["path"] for row in acq2["worktrees"]] == [str(second)]
        assert "mount_diagnostics" not in acq2

        # plan returns [] when CREATE_ALLOWED, and [matched] when REUSE.
        create_plan = plan(
            repository.repo, task_key="task-3", authority_key="auth-3", module="m", role="feature"
        )
        assert create_plan["decision"]["code"] == "CREATE_ALLOWED"
        assert create_plan["worktrees"] == []
        reuse_plan = plan(
            repository.repo, task_key="task-1", authority_key="auth-1", module="m", role="feature"
        )
        assert reuse_plan["decision"]["code"] == "REUSE"
        assert [row["path"] for row in reuse_plan["worktrees"]] == [str(first)]

        # park / resume / binding / handoff / reopen return only the affected worktree.
        parked = park(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            owner="module-owner",
        )
        assert [row["path"] for row in parked["worktrees"]] == [str(first)]
        resumed = resume(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            owner="module-owner",
        )
        assert [row["path"] for row in resumed["worktrees"]] == [str(first)]
        bound = update_external_binding(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            owner="module-owner",
            binding="external:test",
            action="add",
        )
        assert [row["path"] for row in bound["worktrees"]] == [str(first)]
        update_external_binding(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            owner="module-owner",
            binding="external:test",
            action="remove",
        )
        (first / "note.txt").write_text("work\n", encoding="utf-8")
        git(first, "add", "note.txt")
        git(first, "commit", "-m", "work")
        head = git(first, "rev-parse", "HEAD")
        handed = handoff(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            kind="preserved",
            commit=head,
            finalizer_record_id="fin-1",
        )
        assert [row["path"] for row in handed["worktrees"]] == [str(first)]
        reopened = reopen(
            repository.repo,
            target=first,
            expected_record_version=_version(repository, first),
            owner="module-owner",
        )
        assert [row["path"] for row in reopened["worktrees"]] == [str(first)]

        # --full-status restores all registered worktrees on any routine command.
        code, full_park, _ = _cli_json(
            capsys,
            "park",
            "--repo",
            str(repository.repo),
            "--path",
            str(first),
            "--expected-record-version",
            str(_version(repository, first)),
            "--owner",
            "module-owner",
            "--full-status",
        )
        assert code == 0
        assert len(full_park["worktrees"]) == 3
        assert "mount_diagnostics" in full_park

        # release returns worktrees == [] since target is gone.
        rel = _close_and_release(repository, second)
        assert rel["worktrees"] == []


def test_integration_queue_filters_terminal_intents_by_default(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        src1, _ = acquire_one(
            repository, task="src-1", authority="auth-1", scope_paths=("src1.txt",)
        )
        src2, _ = acquire_one(
            repository, task="src-2", authority="auth-2", scope_paths=("src2.txt",)
        )
        for src, fname in ((src1, "src1.txt"), (src2, "src2.txt")):
            (src / fname).write_text("change\n", encoding="utf-8")
            git(src, "add", fname)
            git(src, "commit", "-m", f"change {fname}")
            handoff(
                repository.repo,
                target=src,
                expected_record_version=_version(repository, src),
                kind="integration-ready",
                commit=git(src, "rev-parse", "HEAD"),
                finalizer_record_id=f"fin-{fname}",
            )
        st = status(repository.repo)
        alloc1 = next(w["allocation_id"] for w in st["worktrees"] if w["path"] == str(src1))
        alloc2 = next(w["allocation_id"] for w in st["worktrees"] if w["path"] == str(src2))

        enq1 = enqueue_integration(
            repository.repo,
            task_key="int-1",
            authority_key="int-1",
            module="m",
            owner="int-owner",
            source_allocations=(alloc1,),
        )
        assert enq1["worktrees"] == []
        intent1 = enq1["decision"]["intent_id"]
        enq2 = enqueue_integration(
            repository.repo,
            task_key="int-2",
            authority_key="int-2",
            module="m",
            owner="int-owner",
            source_allocations=(alloc2,),
        )
        intent2 = enq2["decision"]["intent_id"]
        abandon_integration_intent(
            repository.repo, intent_id=intent1, owner="int-owner", reason="no longer needed"
        )

        # Default omits terminal ABANDONED intent and reports summary counts.
        code, default_q, _ = _cli_json(
            capsys, "integration-queue", "--repo", str(repository.repo)
        )
        assert code == 0
        assert default_q["worktrees"] == []
        dec = default_q["decision"]
        assert [row["intent_id"] for row in dec["intents"]] == [intent2]
        assert dec["total_intents"] == 2
        assert dec["active_intents"] == 1
        assert dec["terminal_intents"] == 1
        assert dec["omitted_terminal_intents"] == 1
        assert dec["include_terminal"] is False
        assert dec["effective_state_counts"] == {"ABANDONED": 1, "QUEUED": 1}

        # --all includes terminal history.
        code_all, all_q, _ = _cli_json(
            capsys, "integration-queue", "--repo", str(repository.repo), "--all"
        )
        assert code_all == 0
        dec_all = all_q["decision"]
        assert {row["intent_id"] for row in dec_all["intents"]} == {intent1, intent2}
        assert dec_all["omitted_terminal_intents"] == 0
        assert dec_all["include_terminal"] is True

        # --full-status embeds all worktrees.
        code_full, full_q, _ = _cli_json(
            capsys, "integration-queue", "--repo", str(repository.repo), "--full-status"
        )
        assert code_full == 0
        assert len(full_q["worktrees"]) == 3


def _write_fake_cleanup_evidence(
    store: MetadataStore,
    *,
    receipt_id: str,
    allocation_id: str,
    target_path: Path,
    contract: str = "typed-disposable-resource/v1",
    payload_padding: int = 2048,
) -> tuple[Path, Path]:
    store.completion_receipts_dir.mkdir(parents=True, exist_ok=True)
    action_dir = store.cleanup_actions_dir / receipt_id
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "0000-before.json").write_text(
        json.dumps({"padding": "b" * payload_padding}), encoding="utf-8"
    )
    (action_dir / "0000-after.json").write_text(
        json.dumps({"padding": "a" * payload_padding}), encoding="utf-8"
    )
    receipt_path = store.completion_receipts_dir / f"{receipt_id}.json"
    store.write_completion_receipt(
        receipt_id,
        {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "contract": contract,
            "result": "CLEANED",
            "plan": {
                "allocation_id": allocation_id,
                "target_path": str(target_path),
                "hardlink_topology": {"padding": "p" * payload_padding},
            },
            "created_at": now_iso(),
        },
    )
    return receipt_path, action_dir


def test_reconcile_cleanup_evidence_prunes_only_verified_released_and_preserves_protected(
    capsys,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        active_wt, acq_active = acquire_one(repository, task="live", authority="live")
        rel_wt1, acq_rel1 = acquire_one(repository, task="rel-1", authority="rel-1")
        rel_wt2, acq_rel2 = acquire_one(repository, task="rel-2", authority="rel-2")
        active_alloc = acq_active["decision"]["allocation_id"]
        rel_alloc1 = acq_rel1["decision"]["allocation_id"]
        rel_alloc2 = acq_rel2["decision"]["allocation_id"]

        _close_and_release(repository, rel_wt1)
        _close_and_release(repository, rel_wt2)

        store = MetadataStore(inventory(repository.repo).common_dir)
        # 1. Active worktree cleanup evidence -> MUST BE KEPT
        active_rid = str(uuid.uuid4())
        active_cr, active_ca = _write_fake_cleanup_evidence(
            store, receipt_id=active_rid, allocation_id=active_alloc, target_path=active_wt
        )
        # 2. Released worktree #1 clean evidence -> ELIGIBLE FOR PRUNE
        rel1_rid = str(uuid.uuid4())
        rel1_cr, rel1_ca = _write_fake_cleanup_evidence(
            store, receipt_id=rel1_rid, allocation_id=rel_alloc1, target_path=rel_wt1
        )
        # 3. Released worktree #2 evidence referenced by a retained recovery -> MUST BE KEPT
        rel2_rid = str(uuid.uuid4())
        rel2_cr, rel2_ca = _write_fake_cleanup_evidence(
            store, receipt_id=rel2_rid, allocation_id=rel_alloc2, target_path=rel_wt2
        )
        rec_tx = str(uuid.uuid4())
        rec_dir = store.cleanup_recoveries_dir / rec_tx / str(uuid.uuid4())
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / "complete.json").write_text(
            json.dumps({"completion_receipt_id": rel2_rid}), encoding="utf-8"
        )
        # 4. Non-prunable contract (no_change_completion / integration_abort) -> MUST BE KEPT
        abort_rid = str(uuid.uuid4())
        store.write_completion_receipt(
            abort_rid,
            {
                "schema_version": 1,
                "receipt_id": abort_rid,
                "kind": "integration_abort",
                "allocation_id": rel_alloc1,
                "created_at": now_iso(),
            },
        )
        abort_cr = store.completion_receipts_dir / f"{abort_rid}.json"

        # Verify write_completion_receipt writes compact JSON (no 2-space indent).
        assert "\n  " not in rel1_cr.read_text(encoding="utf-8")

        # Dry-run plan reports exact eligible entry and mutates nothing.
        code_plan, planned, _ = _cli_json(
            capsys, "reconcile", "cleanup-evidence", "--repo", str(repository.repo)
        )
        assert code_plan == 0
        dec = planned["decision"]
        assert dec["code"] == "CLEANUP_EVIDENCE_PLAN"
        assert dec["dry_run"] is True
        assert dec["eligible_receipts"] == 1
        assert dec["eligible_action_dirs"] == 1
        assert dec["eligible_action_files"] == 2
        assert dec["eligible_total_bytes"] > 6000
        assert rel1_cr.exists() and rel1_ca.exists()

        # Apply prunes ONLY rel1 and leaves active, recovery-referenced, and abort receipts intact.
        code_apply, applied, _ = _cli_json(
            capsys, "reconcile", "cleanup-evidence", "--repo", str(repository.repo), "--apply"
        )
        assert code_apply == 0
        app_dec = applied["decision"]
        assert app_dec["code"] == "CLEANUP_EVIDENCE_PRUNED"
        assert app_dec["result"]["pruned_receipts"] == 1
        assert app_dec["result"]["pruned_action_dirs"] == 1
        assert app_dec["result"]["pruned_action_files"] == 2
        assert app_dec["result"]["bytes_reclaimed"] == dec["eligible_total_bytes"]
        assert not rel1_cr.exists() and not rel1_ca.exists()
        assert active_cr.exists() and active_ca.exists()
        assert rel2_cr.exists() and rel2_ca.exists()
        assert abort_cr.exists()
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_release_auto_bounds_historical_terminal_cleanup_evidence(monkeypatch) -> None:
    monkeypatch.setattr(cleanup_retention, "DEFAULT_AUTO_KEEP_RECENT", 1)
    monkeypatch.setattr(cleanup_retention, "DEFAULT_AUTO_MAX_BYTES", 4096)
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        wt1, acq1 = acquire_one(repository, task="auto-1", authority="auto-1")
        wt2, acq2 = acquire_one(repository, task="auto-2", authority="auto-2")
        alloc1 = acq1["decision"]["allocation_id"]
        store = MetadataStore(inventory(repository.repo).common_dir)

        _close_and_release(repository, wt1)
        rid1 = str(uuid.uuid4())
        cr1, ca1 = _write_fake_cleanup_evidence(
            store, receipt_id=rid1, allocation_id=alloc1, target_path=wt1, payload_padding=3000
        )
        assert cr1.exists() and ca1.exists()

        # Releasing wt2 triggers auto_prune_terminal_cleanup_locked, pruning oversized older
        # RELEASED cleanup evidence for wt1 while keeping wt2's own completion receipt intact.
        _close_and_release(repository, wt2)
        assert not cr1.exists() and not ca1.exists()
        assert integration_queue(repository.repo)["worktrees"] == []


def test_large_topology_evidence_stays_compact_plaintext_json_and_prunable(capsys) -> None:
    from worktree_controller import cleanup_evidence

    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        wt, acq = acquire_one(repository, task="plain-json-task", authority="plain-json-auth")
        alloc = acq["decision"]["allocation_id"]
        _close_and_release(repository, wt)
        store = MetadataStore(inventory(repository.repo).common_dir)

        # Build a synthetic ~250 KiB hardlink_topology payload resembling node_modules cleanup.
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
            for idx in range(700)
        }
        topology = {"contract": "cleanup-hardlink-topology/v1", "resources": ["apps/docs/node_modules"], "anchors": {}, "groups": groups}
        pretty_len = len(json.dumps(topology, indent=2, sort_keys=True).encode("utf-8"))
        compact_len = len(json.dumps(topology, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        assert compact_len > 200_000 and compact_len < pretty_len

        rid = str(uuid.uuid4())
        action_dir = store.cleanup_actions_dir / rid
        before_ref = cleanup_evidence.write(
            action_dir,
            "0000-before.json",
            {"phase": "BEFORE", "topology": topology},
        )
        before_path = Path(before_ref["path"])
        before_raw = before_path.read_bytes()
        assert before_raw.startswith(b"{") and not before_raw.startswith(b"\x1f\x8b")
        assert json.loads(before_raw.decode("utf-8"))["topology"] == topology

        # Exact SHA-256 verification and plaintext roundtrip.
        decoded, actual_sha = cleanup_evidence.read(before_path, before_ref["sha256"])
        assert actual_sha == before_ref["sha256"]
        assert decoded["topology"] == topology

        # Large transaction journal and completion receipt stay compact UTF-8 plaintext JSON.
        tx_id = str(uuid.uuid4())
        tx_payload = {"transaction_id": tx_id, "allocation_id": alloc, "cleanup_plan": {"hardlink_topology": topology}}
        store.write_transaction(tx_id, tx_payload)
        tx_path = store.transactions_dir / f"{tx_id}.json"
        tx_raw = tx_path.read_bytes()
        assert tx_raw.startswith(b"{") and not tx_raw.startswith(b"\x1f\x8b")
        assert len(tx_raw) < pretty_len
        assert json.loads(tx_raw.decode("utf-8")) == tx_payload
        assert store._read_json(tx_path) == tx_payload
        store.remove_transaction(tx_id)

        store.write_completion_receipt(
            rid,
            {
                "schema_version": 1,
                "receipt_id": rid,
                "contract": "typed-disposable-resource/v1",
                "result": "CLEANED",
                "plan": {
                    "allocation_id": alloc,
                    "target_path": str(wt),
                    "hardlink_topology": topology,
                },
                "created_at": now_iso(),
            },
        )
        receipt_path = store.completion_receipts_dir / f"{rid}.json"
        receipt_raw = receipt_path.read_bytes()
        assert receipt_raw.startswith(b"{") and not receipt_raw.startswith(b"\x1f\x8b")
        assert len(receipt_raw) < pretty_len
        assert json.loads(receipt_raw.decode("utf-8"))["plan"]["hardlink_topology"] == topology
        assert cleanup_evidence.read(receipt_path)[0]["plan"]["hardlink_topology"] == topology

        # reconcile cleanup-evidence reads and prunes plaintext RELEASED evidence cleanly.
        code, applied, _ = _cli_json(
            capsys, "reconcile", "cleanup-evidence", "--repo", str(repository.repo), "--apply"
        )
        assert code == 0
        assert applied["decision"]["result"]["pruned_receipts"] == 1
        assert applied["decision"]["result"]["pruned_action_dirs"] == 1
        assert not receipt_path.exists() and not action_dir.exists()


def test_store_load_include_receipts_false_skips_receipt_dirs() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        wt, _ = acquire_one(repository, task="fast-load", authority="fast-load")
        _close_and_release(repository, wt)
        store = MetadataStore(inventory(repository.repo).common_dir)
        full = store.load()
        light = store.load(include_receipts=False)
        assert len(full.release_receipts) >= 1
        assert light.release_receipts == {}
        assert light.release_plans == {}
        assert light.records == full.records
        assert light.bindings == full.bindings
