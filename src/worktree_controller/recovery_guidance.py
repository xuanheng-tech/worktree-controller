"""Read-only, state-specific directions through the existing lifecycle contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .controller import envelope, status
from .git_backend import inventory
from .lifecycle_history import lifecycle_history
from .models import ControllerError
from .store import MetadataStore

CONTRACT = "recovery-guidance/v1"
REASON = "Preserve the failed candidate and reprepare the original frozen source"


def guide_argv(repository: str, path: str) -> list[str]:
    return ["worktree-controller", "recovery-guide", "--repo", repository,
            "--path", path, "--json"]


def recovery_guide(repository_path: Path, *, target: Path | None = None,
                   intent_id: str | None = None) -> dict[str, Any]:
    """Observe one checkout or original intent; never authorize or execute a step.

    Commands bind only the current observation. Later transitions must be observed
    again; in particular, no future CAS or consumer-generated digest is emitted.
    """
    if (target is None) == (intent_id is None):
        raise ControllerError("INVALID_SELECTOR", "select exactly one path or integration intent")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        snapshot = store._load_unlocked()
        observed = status(repository_path)
        canonical = str(repository.canonical_path)
        intent = None
        item = None
        if intent_id is not None:
            intent = snapshot.integration_intents.get(intent_id)
            if intent is None:
                raise ControllerError("UNKNOWN_INTENT", "integration intent is not registered")
            item = next((row for row in observed["worktrees"]
                         if row["allocation_id"] == intent.allocation_id), None)
            if intent.allocation_id is not None and item is None and intent.state != "RELEASED":
                raise ControllerError("UNKNOWN_TASK", "intent allocation has no registered checkout")
        else:
            resolved = str(target.expanduser().resolve(strict=True))
            item = next((row for row in observed["worktrees"] if row["path"] == resolved), None)
            if item is None or item["metadata_missing"]:
                raise ControllerError("UNKNOWN_TASK", "path has no governed allocation")
            intent = snapshot.integration_intents.get(item["integration_intent_id"])

        result = envelope("recovery-guide")
        result["repository"] = {"canonical_path": canonical,
                                "repository_id": snapshot.repository_id}
        result["warnings"] = observed["warnings"]
        decision: dict[str, Any] = {
            "code": "RECOVERY_GUIDANCE", "contract": CONTRACT,
            "execution_authority": False, "execution_guards_checked": False,
            "mutation_performed": False, "task_completed": False,
            "reobserve_after_each_step": True, "blockers": [], "next_steps": [],
            "reason": "Inspect the current state before choosing a recovery action",
        }
        result["decision"] = decision
        if intent is not None:
            decision["intent"] = {field: getattr(intent, field) for field in
                                  ("intent_id", "owner", "state", "blocked_reason", "attempt",
                                   "allocation_id", "queue_sequence", "scope_paths", "source_handoffs")}

        def step(action: str, message: str, argv: list[str] | None = None) -> None:
            value: dict[str, Any] = {"action": action, "message": message}
            if argv is not None:
                value["argv"] = argv
            decision["next_steps"].append(value)

        if item is None:
            if intent.state == "RELEASED" and intent.allocation_id is not None:
                history = lifecycle_history(repository_path, allocation_id=intent.allocation_id,
                                            limit=1)["decision"]
                closed = history["closed_releases"]
                evidence = closed[0] if len(closed) == 1 else {}
                publications = [delivery for delivery in evidence.get("deliveries", [])
                    if delivery["relationship"] == "candidate"
                    and delivery["integration_intent_id"] == intent.intent_id
                    and delivery["candidate_oid"] == intent.candidate_oid
                    and delivery["publication_status"] == "REMOTE_VERIFIED"]
                exact = (not history["live_allocations"] and not history["incomplete_history"]
                    and not history["next_after"] and evidence.get("proof_status") == "COMPLETE"
                    and evidence.get("release_status") == "RELEASED"
                    and evidence.get("role") == "integration"
                    and all(evidence.get(key) == getattr(intent, key) for key in
                            ("allocation_id", "owner", "task_key", "authority_key"))
                    and evidence.get("released_head") == intent.candidate_oid and bool(publications))
                if exact:
                    decision["terminal_status"] = "PUBLISHED_RELEASED"
                    decision["terminal_evidence"] = {
                        "allocation_id": intent.allocation_id,
                        "candidate_oid": intent.candidate_oid,
                        "release_receipt_id": evidence["release_receipt_id"],
                        "publication_receipt_ids": sorted({p["publication_receipt_id"] for p in publications}),
                    }
                    decision["reason"] = "This integration was published and its checkout released; recovery is unnecessary"
                else:
                    decision["blockers"].append("CLOSED_LIFECYCLE_EVIDENCE_INCOMPLETE")
                    decision["reason"] = "The released intent needs complete matching publication and release evidence"
                step("inspect_history", "Inspect immutable lifecycle evidence; do not retry an already published integration",
                     ["worktree-controller", "lifecycle-history", "--repo", canonical,
                      "--allocation-id", intent.allocation_id, "--json"])
                return result
            decision["reason"] = "The original intent has no live candidate; inspect its retry plan"
            if intent.state == "ATTENTION_REQUIRED":
                step("inspect_retry", "Require complete abort/release proof and unchanged source; "
                     "apply only the returned public plan_id with the identical request",
                     ["worktree-controller", "integration-retry", "--repo", canonical,
                      "--intent-id", intent.intent_id, "--owner", intent.owner,
                      "--reason", REASON, "--dry-run", "--json"])
                step("readmit", "After retry, claim the same intent through ordinary admission; "
                     "resolve conflicts explicitly and run fresh validation")
            else:
                step("inspect_queue", "Keep the original intent, source, scope and queue position",
                     ["worktree-controller", "integration-queue", "--repo", canonical, "--json"])
            return result

        decision["identity"] = {field: item[field] for field in
                                ("allocation_id", "owner", "task_key", "authority_key", "path",
                                 "branch", "head", "record_version", "lifecycle", "role")}
        decision["inspect_argv"] = guide_argv(canonical, item["path"])
        blockers = decision["blockers"]
        for field, code in (("canonical", "CANONICAL_PROTECTED"),
                            ("metadata_missing", "MISSING_METADATA"),
                            ("dirty", "DIRTY_WORKTREE"), ("detached", "DETACHED_HEAD"),
                            ("locked", "WORKTREE_LOCKED"), ("prunable", "WORKTREE_PRUNABLE"),
                            ("external_bindings", "EXTERNAL_BINDINGS")):
            if item.get(field):
                blockers.append(code)
        if item["allocation_id"] in snapshot.writer_leases:
            blockers.append("WRITER_LEASE_PRESENT")
        lease = snapshot.publication_lease
        if lease is not None and lease.allocation_id == item["allocation_id"]:
            blockers.append("PUBLICATION_LEASE_HELD")
        from .transaction_scope import conflicts

        if conflicts(store, allocation=item["allocation_id"], target=Path(item["path"])):
            blockers.append("BLOCK_TRANSACTION")
        if blockers:
            decision["reason"] = "Preserve files and resolve the observed blockers with their recorded owner"
            step("inspect_blockers", "End only the exact owned writer and preserve unknown files; "
                 "doctor checks integrity, not integration or publication eligibility",
                 ["worktree-controller", "doctor", "--repo", canonical, "--json"])
            return result

        common = ["--repo", canonical, "--path", item["path"], "--owner", item["owner"],
                  "--expected-record-version", str(item["record_version"])]
        lifecycle = item["lifecycle"]
        if item["role"] == "integration" and lifecycle in {"BLOCKED", "QUARANTINED"}:
            if intent is not None:
                from .integration_retry import _sources

                try:
                    _sources(repository, snapshot, store, intent, require_idle=False)
                except ControllerError as error:
                    blockers.append(error.code)
                    decision["reason"] = str(error)
                    step("restore_source_evidence", "The recorded source and scope must be restored "
                         "by their owner; do not replace the original request")
                    return result
            if lifecycle == "BLOCKED":
                expected = item["task_commit"] or item["handoff_commit"] or item["base_commit"]
                if expected != item["head"]:
                    blockers.append("CANDIDATE_HEAD_DRIFT")
                    decision["reason"] = "Unexpected commits require explicit quarantine and lineage recovery"
                    step("inspect_lineage", "Preserve the extra commits; use quarantine/recovery/externalize "
                         "after reviewing the latest legitimate commit")
                    return result
                decision["reason"] = "BLOCKED cannot abort directly; first record quarantine of this exact candidate"
                step("quarantine", "Equal expected and observed HEAD is valid; no synthetic commit is needed",
                     ["worktree-controller", "quarantine", *common, "--expected-commit", expected,
                      "--reason", REASON, "--json"])
                step("abort_then_release", "Reinspect QUARANTINED, record formal abort, then inspect "
                     "release guards; preserve unclassified artifacts before cleanup")
            else:
                decision["reason"] = "Quarantine preserves failure identity; formal abort is the next lifecycle step"
                step("abort", "This records the failed preparation and preserves the original source",
                     ["worktree-controller", "integration-abort", *common,
                      "--reason", REASON, "--json"])
        elif lifecycle == "INTEGRATION_ABORTED":
            decision["reason"] = "The failed attempt is recorded; inspect guarded release before retry"
            step("inspect_release", "Unknown generated files need exact preservation and typed cleanup; "
                 "release retains the branch and must produce complete lifecycle evidence",
                 ["worktree-controller", "release", "--repo", canonical, "--path", item["path"],
                  "--owner-release", item["owner"], "--expected-record-version",
                  str(item["record_version"]), "--dry-run", "--json"])
            step("retry_original_intent", "After complete release, select the original intent with "
                 "recovery-guide --intent-id; retry requires its public plan and fresh admission")
        elif lifecycle == "HANDOFF_READY":
            decision["reason"] = "A frozen handoff needs exact Finalizer identity before session closeout"
            step("check_session", "Run worktree-session-end --check with the real client/session/holder/run "
                 "and expected allocation IDs; read every row and summary.terminal_complete")
        else:
            decision["reason"] = "Unfinished work needs its existing Task checkpoint or a verified handoff"
            step("preserve_task", "Finalize deliverable files through Git Finalizer, or save the existing "
                 "Task checkpoint and park with exact owner, HEAD and record version; preserve unknown files")
        return result
