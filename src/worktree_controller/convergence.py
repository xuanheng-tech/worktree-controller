"""Retention convergence: close checkouts nobody is using, without losing a byte.

Admission is resource based and never counts checkouts, so the population can only
stay bounded if unused checkouts leave again. Before this engine, every exit from
``RETAINED`` was a manual, per-target command signed with an owner identity that
usually belonged to an agent session which no longer existed; retained checkouts
therefore accumulated without limit.

The engine runs under the repository's delegated reconciler identity and only
composes the existing guarded verbs: ``close-delivered``, ``complete-no-change``,
``archive-retained`` and ``release``. It never touches a dirty, detached, bound,
leased or in-use checkout, never deletes anything that is not either reachable from
a retained branch or a registered reproducible ``.venv``, and never claims task
completion. Every physical removal still passes the unchanged release preflight and
writes the ordinary release receipt.

A frozen handoff normally leaves once its commit is published. When the policy opts
in with ``release_unintegrated_handoffs``, an integration-ready feature/audit/hotfix handoff
whose exact HEAD is retained by both its local and remote-tracking branch may leave
before integration; the integration queue then replays it from the retained branch.

Idle thresholds are dynamic. With ``live`` checkouts in use, the pressure reference
is ``live + standing_reserve``; while the registered population stays at or below
it, an unfinished checkout may idle for ``idle_hours_max``. Above it the threshold
shrinks with the square of the overshoot down to ``idle_hours_min``. Published and
no-change checkouts only wait ``grace_minutes``. Nothing here refuses admission.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .controller import envelope
from .git_backend import inventory, is_ancestor, resolve_commit
from .models import (
    TERMINAL_INTEGRATION_STATES,
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    WorktreeGitState,
)
from .owner_actions import owner_action
from .policy import load_policy
from .semantics import record_for as _record_for
from .store import MetadataStore
from .writer_lease import _referenced_paths, holder_is_live, process_group_is_live

TRIGGERS = ("manual", "admission", "background")

CLOSED_STATES = frozenset({"RELEASE_READY", "ARCHIVED", "NO_CHANGE_COMPLETED"})
CANDIDATE_STATES = frozenset({"ACTIVE", "RETAINED", "HANDOFF_READY"}) | CLOSED_STATES
CONVERGED_ROLES = frozenset({"feature", "audit"})

# Outcomes
LIVE = "IN_USE"
ATTENTION = "ATTENTION_REQUIRED"
WAITING = "IDLE_BELOW_THRESHOLD"
BLOCKED = "BLOCKED"
PLANNED = "WOULD_RELEASE"
RELEASED = "RELEASED"
SKIPPED = "OUT_OF_SCOPE"

# Summary order and wording; the reason names what actually kept checkouts on disk.
OUTCOME_LABELS = (
    (PLANNED, "would be released"),
    (LIVE, "in use"),
    (ATTENTION, "need owner action"),
    (BLOCKED, "blocked"),
    (WAITING, "idle below threshold"),
    (SKIPPED, "out of scope"),
)


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _admin_dir(item: WorktreeGitState) -> Path | None:
    marker = Path(item.path) / ".git"
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text.split(":", 1)[1].strip())
    return admin if admin.is_absolute() else (Path(item.path) / admin).resolve()


def last_activity(item: WorktreeGitState, record: MetadataRecord) -> float:
    """Latest observable touch of the checkout: Git admin files or a lifecycle event.

    Only clean checkouts are ever acted on, so every change that matters has passed
    through the index, HEAD or its reflog.
    """

    stamps: list[float] = []
    admin = _admin_dir(item)
    if admin is not None:
        for name in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD", "FETCH_HEAD"):
            try:
                stamps.append((admin / name).stat().st_mtime)
            except OSError:
                continue
    for value in (
        record.created_at,
        record.last_reviewed_at,
        *(event.get("at") for event in record.history if isinstance(event, dict)),
    ):
        parsed = _parse_time(value if isinstance(value, str) else None)
        if parsed is not None:
            stamps.append(parsed)
    return max(stamps) if stamps else 0.0


def idle_threshold_hours(policy_convergence: Any, *, registered: int, live: int) -> float:
    reference = live + policy_convergence.standing_reserve
    pressure = registered / reference if reference else float(registered)
    if pressure <= 1:
        return float(policy_convergence.idle_hours_max)
    return max(
        float(policy_convergence.idle_hours_min),
        policy_convergence.idle_hours_max / (pressure * pressure),
    )


def busy_paths(targets: set[str]) -> set[str]:
    """Targets that any live process references, from one pass over ``/proc``.

    Same evidence as ``writer_lease.paths_in_use`` (cwd, open files, mappings), but a
    single scan serves every checkout instead of one full scan per checkout.
    """

    found: set[str] = set()
    own = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == own:
            continue
        for candidate in _referenced_paths(int(entry)):
            current = candidate
            while current and current != os.sep:
                if current in targets:
                    found.add(current)
                    break
                current = os.path.dirname(current)
    return found


def _in_use(
    item: WorktreeGitState,
    record: MetadataRecord,
    snapshot: MetadataSnapshot,
    busy: set[str],
) -> str | None:
    lease = snapshot.writer_leases.get(record.allocation_id)
    if lease is not None:
        if holder_is_live(lease.holder) or process_group_is_live(lease.holder):
            return "writer lease holder is alive"
        return "writer lease is recorded; writer reconciliation must retire it first"
    if str(item.path) in busy:
        return "a live process is using this checkout"
    if record.external_bindings:
        return "registered external or runtime bindings"
    if (
        snapshot.publication_lease
        and snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        return "a publication lease is in progress"
    return None


def _is_candidate(record: MetadataRecord) -> bool:
    """feature/audit in any candidate state, plus a frozen hotfix handoff.

    A frozen hotfix handoff follows the same handoff routes as feature/audit; every
    other hotfix state keeps its own flow.
    """

    if record.role in CONVERGED_ROLES:
        return record.lifecycle in CANDIDATE_STATES
    return record.role == "hotfix" and record.lifecycle == "HANDOFF_READY"


def _integration_bound(snapshot: MetadataSnapshot, record: MetadataRecord) -> bool:
    from .semantics import effective_intent_state

    return any(
        intent.allocation_id == record.allocation_id
        or record.allocation_id in intent.source_allocations
        for intent in snapshot.integration_intents.values()
        if effective_intent_state(snapshot, intent) not in TERMINAL_INTEGRATION_STATES
    ) or any(
        other.lifecycle != "RELEASED" and record.allocation_id in other.source_allocations
        for other in snapshot.records.values()
    )


def _published_oid(canonical: Path, remote: str, branch: str) -> str | None:
    for ref in (f"refs/remotes/{remote}/{branch}", f"refs/heads/{branch}"):
        try:
            return resolve_commit(canonical, ref)
        except ControllerError:
            continue
    return None


def _ref_oid(canonical: Path, ref: str) -> str | None:
    try:
        return resolve_commit(canonical, ref)
    except ControllerError:
        return None


def _unintegrated_handoff_hold(
    canonical: Path, item: WorktreeGitState, record: MetadataRecord, remote: str
) -> str | None:
    """Why an unpublished frozen handoff must keep its checkout, or None if it may leave.

    Leaving early is only safe when nothing but the checkout directory is lost: the
    frozen identities all equal HEAD, the handoff is integration-ready (so the queue
    can replay it from its branch), and both the local branch and the remote-tracking
    branch retain exactly that HEAD.
    """

    if not any(event.get("event") == "HANDOFF_INTEGRATION_READY" for event in record.history):
        return "handoff is not integration-ready"
    identities = (record.task_commit, record.handoff_commit, record.finalizer_commit)
    if not record.finalizer_record_id or any(identity != item.head for identity in identities):
        return "task, handoff and Finalizer identities do not all equal HEAD"
    if any(
        (
            record.quarantine_reason,
            record.quarantine_observed_commit,
            record.recovery_ref,
            record.recovery_allocation_id,
        )
    ):
        return "recovery lineage keeps its own flow"
    if item.branch is None or _ref_oid(canonical, f"refs/heads/{item.branch}") != item.head:
        return "local branch does not retain the frozen HEAD"
    if _ref_oid(canonical, f"refs/remotes/{remote}/{item.branch}") != item.head:
        return f"{remote}/{item.branch} does not retain the frozen HEAD; push it with Git Finalizer"
    return None


def _route(
    canonical: Path,
    item: WorktreeGitState,
    record: MetadataRecord,
    published: str | None,
    *,
    remote: str,
    release_unintegrated: bool,
) -> tuple[str, str | None]:
    """Pick the existing guarded verb that fits the recorded facts.

    Returns the route and, for ``hold-handoff``, the reason the handoff stays.
    """

    if record.lifecycle in CLOSED_STATES:
        return "release", None
    if record.lifecycle == "HANDOFF_READY":
        if (
            published
            and record.handoff_commit
            and record.handoff_commit == item.head
            and is_ancestor(canonical, record.handoff_commit, published)
        ):
            return "release-published-handoff", None
        if not release_unintegrated:
            return "hold-handoff", (
                "frozen handoff is not integrated and no integration intent is queued; "
                "integration-enqueue it, or enable release_unintegrated_handoffs"
            )
        hold = _unintegrated_handoff_hold(canonical, item, record, remote)
        if hold is not None:
            return "hold-handoff", f"unintegrated handoff keeps its checkout: {hold}"
        return "release-retained-handoff", None
    if record.handoff_commit or record.finalizer_record_id or record.finalizer_commit:
        return "hold-handoff", "handoff evidence exists outside HANDOFF_READY"
    if published and all(
        is_ancestor(canonical, oid, published)
        for oid in (item.head, record.base_commit, record.task_commit)
        if oid
    ):
        return "close-delivered", None
    if record.base_commit and item.head == resolve_commit(canonical, record.base_commit):
        return "complete-no-change", None
    return "archive-retained", None


def _require_intent_renewal_due(
    repository_path: Path, item: WorktreeGitState, record: MetadataRecord
) -> None:
    """Refuse a new release intent while an unchanged handoff backs off.

    Each pass otherwise creates a fresh intent for a checkout whose last one went
    stale for a reason the pass cannot change, one intent per pass for as long as it
    stays blocked. The same rule as stale-intent renewal applies: a changed record or
    HEAD, or a first expiry, proceeds at once.
    """
    from .release_intents import release_intent_renewal_due, release_intents_for_allocation

    canonical = inventory(repository_path)
    snapshot = MetadataStore(canonical.common_dir).load()
    intents = release_intents_for_allocation(snapshot, record.allocation_id)
    if any(intent.status in {"PENDING", "BLOCKED_RETRYABLE"} for intent in intents):
        return  # create_release_intent reuses the live intent
    stale = [intent for intent in intents if intent.status == "STALE"]
    if not stale:
        return
    # The backoff runs from the most recent time an intent of this allocation went stale.
    latest = max(stale, key=lambda intent: (intent.updated_at, intent.created_at, intent.intent_id))
    ttl = load_policy(canonical.canonical_path).release_intent_ttl_seconds
    if not release_intent_renewal_due(snapshot, latest, record, item.head, datetime.now(UTC), ttl):
        raise ControllerError(
            "RENEWAL_BACKOFF",
            f"unchanged handoff release intent is backing off after {latest.blocker or 'expiry'}",
        )


def _register_venv(
    repository_path: Path, item: WorktreeGitState, record: MetadataRecord
) -> MetadataRecord:
    """Classify an unregistered root ``.venv`` so the release preflight can decide on it."""

    from .lifecycle import create_release_intent, register_disposable

    venv = Path(item.path) / ".venv"
    if (
        not venv.is_dir()
        or venv.is_symlink()
        or any(resource["path"] == ".venv" for resource in record.disposable_resources)
    ):
        return record
    if record.role == "hotfix":
        # A hotfix environment is classified by its owner's attestation, which the
        # reconciler must not sign on the owner's behalf.
        raise ControllerError(
            "RESOURCE_UNCLASSIFIED",
            "hotfix handoff has an unregistered .venv; its owner classifies it with "
            f"resource-register --creator handoff-owner-attestation:{record.owner}",
        )
    if record.lifecycle == "HANDOFF_READY":
        # A frozen handoff only accepts resource classification behind a pending release
        # intent, which binds its exact handoff/Finalizer identity; nothing else changes.
        _require_intent_renewal_due(repository_path, item, record)
        create_release_intent(
            repository_path,
            target=Path(item.path),
            expected_record_version=record.record_version,
            owner=record.owner,
            trigger="capacity-pressure",
        )
        record = _reload(repository_path, item)
    register_disposable(
        repository_path,
        target=Path(item.path),
        expected_record_version=record.record_version,
        owner=record.owner,
        resource_path=".venv",
        kind="python-venv",
        reproducible=True,
        creator="retention-convergence",
        teardown_policy="on-release",
    )
    return _reload(repository_path, item)


def _reload(repository_path: Path, item: WorktreeGitState) -> MetadataRecord:
    repository = inventory(repository_path)
    snapshot = MetadataStore(repository.common_dir).load()
    current = next(found for found in repository.worktrees if found.path == item.path)
    record = _record_for(current, snapshot)
    if record is None:
        raise ControllerError("METADATA_MISSING", "allocation disappeared during convergence")
    return record


def _close(
    repository_path: Path, item: WorktreeGitState, record: MetadataRecord, route: str, actor: str
) -> MetadataRecord:
    from .delivered_closeout import archive_retained, close_delivered
    from .lifecycle import complete_no_change

    target = Path(item.path)
    if route in {"release", "release-published-handoff", "release-retained-handoff"}:
        return record
    if route == "complete-no-change":
        complete_no_change(
            repository_path,
            target=target,
            expected_record_version=record.record_version,
            owner=record.owner,
        )
        return _reload(repository_path, item)
    verb = close_delivered if route == "close-delivered" else archive_retained
    common = dict(
        target=target,
        expected_record_version=record.record_version,
        owner_release=record.owner,
        actor=actor,
    )
    planned = verb(repository_path, **common, dry_run=True)
    verb(repository_path, **common, plan_id=planned["decision"]["plan_id"])
    return _reload(repository_path, item)


def _pass_lock_path(common_dir: Path) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    digest = hashlib.sha256(str(common_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    return state / "worktree-controller" / "convergence" / f"{digest}.lock"


def _pass_report_path(lock_path: Path) -> Path:
    return lock_path.with_name(f"{lock_path.stem}.last-pass.json")


def _write_pass_report(lock_path: Path, report: dict[str, Any]) -> bool:
    """Atomically replace the latest applied-pass report; False if it cannot be written.

    The report is an operational observation for callers of detached passes. Release
    receipts stay the only release evidence, so a missing report never implies success.
    """

    target = _pass_report_path(lock_path)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def last_applied_pass(common_dir: Path) -> dict[str, Any] | None:
    """The latest applied pass; a RUNNING report whose pass lock is free is INTERRUPTED."""

    lock_path = _pass_lock_path(common_dir)
    try:
        report = json.loads(_pass_report_path(lock_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    if report.get("state") == "RUNNING":
        try:
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return report
        except OSError:
            return report
        report = {**report, "state": "INTERRUPTED"}
    return report


def _pass_outcome(result: dict[str, Any]) -> dict[str, Any]:
    decision = result["decision"]
    return {
        "code": decision["code"],
        "reason": decision["reason"],
        "outcome_counts": decision.get("outcome_counts", {}),
        "released": [
            {
                "allocation_id": row["allocation_id"],
                "receipt_id": row.get("receipt_id"),
                "path": row["path"],
            }
            for row in decision.get("findings", [])
            if row.get("outcome") == RELEASED
        ],
    }


def converge_retention(
    repository_path: Path,
    *,
    actor: str,
    trigger: str = "manual",
    dry_run: bool = True,
    now_at: float | None = None,
) -> dict[str, Any]:
    """Report, and optionally apply, retention convergence for one repository.

    Applying passes are serialized per repository by a non-blocking host lock; a pass
    that finds another one running returns ``CONVERGENCE_BUSY`` instead of racing it.
    Every applying pass records RUNNING, then COMPLETED or FAILED, beside that lock so a
    caller of a detached pass can see how it ended; a dry run reports the latest one.
    """

    common_dir = inventory(repository_path).common_dir
    if dry_run:
        result = _converge(
            repository_path, actor=actor, trigger=trigger, dry_run=True, now_at=now_at
        )
        result["decision"]["last_applied_pass"] = last_applied_pass(common_dir)
        return result
    lock_path = _pass_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = envelope("reconcile retention")
            result["decision"] = {
                "code": "CONVERGENCE_BUSY",
                "reason": "another convergence pass is running for this repository",
                "trigger": trigger,
            }
            return result
        report: dict[str, Any] = {
            "state": "RUNNING",
            "trigger": trigger,
            "actor": actor,
            "pid": os.getpid(),
            "started_at": _now_iso(),
        }
        recorded = _write_pass_report(lock_path, report)
        try:
            result = _converge(
                repository_path, actor=actor, trigger=trigger, dry_run=False, now_at=now_at
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, ControllerError) else type(exc).__name__
            _write_pass_report(
                lock_path,
                {
                    **report,
                    "state": "FAILED",
                    "finished_at": _now_iso(),
                    "code": code,
                    "reason": str(exc),
                },
            )
            raise
        recorded = (
            _write_pass_report(
                lock_path,
                {
                    **report,
                    "state": "COMPLETED",
                    "finished_at": _now_iso(),
                    **_pass_outcome(result),
                },
            )
            and recorded
        )
        if not recorded:
            result["warnings"].append(
                {
                    "code": "PASS_REPORT_UNAVAILABLE",
                    "message": "the applied-pass report could not be written; receipts remain authoritative",
                }
            )
        return result


def _converge(
    repository_path: Path,
    *,
    actor: str,
    trigger: str,
    dry_run: bool,
    now_at: float | None,
) -> dict[str, Any]:
    from .lifecycle import release
    from .reconciler import reconcile_writers

    if trigger not in TRIGGERS:
        raise ControllerError("INVALID_TRIGGER", f"unsupported convergence trigger: {trigger}")
    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "a convergence actor is required")
    repository = inventory(repository_path)
    policy = load_policy(repository.canonical_path)
    settings = policy.convergence
    if settings is None:
        raise ControllerError(
            "CONVERGENCE_UNSUPPORTED", "repository policy does not configure [convergence]"
        )
    if not dry_run:
        if not settings.enabled:
            raise ControllerError(
                "CONVERGENCE_DISABLED", "repository policy does not enable retention convergence"
            )
        if actor != policy.reconciler_actor:
            raise ControllerError(
                "RECONCILER_ACTOR_MISMATCH",
                "actor is not the delegated reconciler for this repository",
            )
        # Retire provably dead writers first so their checkouts can be evaluated now.
        reconcile_writers(repository_path, actor=actor, trigger=trigger, dry_run=False)
        repository = inventory(repository_path)

    now_ts = now_at if now_at is not None else datetime.now(UTC).timestamp()
    snapshot = MetadataStore(repository.common_dir).load()
    published = _published_oid(
        repository.canonical_path, policy.publication_remote, policy.publication_branch
    )
    linked = [item for item in repository.worktrees if not item.canonical]
    busy_set = busy_paths({str(item.path) for item in linked})
    rows: list[tuple[WorktreeGitState, MetadataRecord | None, str | None]] = []
    live = 0
    for item in linked:
        record = _record_for(item, snapshot)
        busy = _in_use(item, record, snapshot, busy_set) if record is not None else None
        if (
            busy is None
            and record is not None
            and record.role not in CONVERGED_ROLES
            and record.lifecycle == "ACTIVE"
        ):
            busy = f"ACTIVE {record.role} allocation"
        live += busy is not None
        rows.append((item, record, busy))
    threshold = idle_threshold_hours(settings, registered=len(linked), live=live)
    grace = settings.grace_minutes / 60.0

    findings: list[dict[str, Any]] = []
    released = 0
    for item, record, busy in rows:
        row: dict[str, Any] = {
            "path": str(item.path),
            "branch": item.branch,
            "allocation_id": record.allocation_id if record else None,
            "owner": record.owner if record else None,
            "lifecycle": record.lifecycle if record else None,
        }
        findings.append(row)
        if record is None:
            row.update(
                outcome=ATTENTION,
                reason="registered worktree has no controller record",
                owner_action="run doctor, then adopt or recover the checkout through Controller",
            )
            continue
        if busy is not None:
            row.update(outcome=LIVE, reason=busy)
            continue
        if not _is_candidate(record):
            row.update(
                outcome=SKIPPED,
                reason=f"role {record.role} / lifecycle {record.lifecycle} has its own flow",
            )
            continue
        if item.prunable or item.locked or item.detached:
            row.update(outcome=ATTENTION, reason="detached, locked or missing checkout")
            continue
        idle_hours = max(0.0, (now_ts - last_activity(item, record)) / 3600.0)
        row["idle_hours"] = round(idle_hours, 2)
        if not item.clean:
            row.update(
                outcome=ATTENTION,
                reason="uncommitted work is never closed automatically; owner action required",
                changes={
                    "staged": item.staged,
                    "modified": item.modified,
                    "untracked": item.untracked,
                },
                owner_action=_dirty_owner_action(item, record),
                owner_action_request=owner_action(str(repository.canonical_path), {
                    "path": str(item.path), "head": item.head, "branch": item.branch,
                    **{key: getattr(record, key) for key in
                       ("allocation_id", "task_key", "authority_key", "owner", "record_version")},
                }, reason="DIRTY_WORKTREE", required_actions=(
                    "review_task_files", "finalize_or_checkpoint", "preserve_unknown_files", "park_or_handoff")),
            )
            continue
        if _integration_bound(snapshot, record):
            row.update(outcome=BLOCKED, reason="integration lineage still references this checkout")
            continue
        route, hold = _route(
            repository.canonical_path,
            item,
            record,
            published,
            remote=policy.publication_remote,
            release_unintegrated=settings.release_unintegrated_handoffs,
        )
        row["route"] = route
        if route == "hold-handoff":
            row.update(outcome=BLOCKED, reason=hold)
            continue
        # An ACTIVE allocation with no lease, process, binding or Git activity is a
        # session that ended without telling anyone (common for clients that do not
        # use writer leases). It waits the full dynamic threshold, then is parked
        # exactly as a finished session would have parked itself.
        dormant = record.lifecycle == "ACTIVE"
        # Population pressure only shortens how long already-parked work may linger.
        # An ACTIVE lane may be a person or client resuming tomorrow without a lease, so
        # it always gets the full idle_hours_max before it is parked.
        if dormant:
            required = float(settings.idle_hours_max)
        elif route in {"archive-retained", "release-retained-handoff"}:
            required = threshold
        else:
            required = grace
        row["required_idle_hours"] = round(required, 2)
        if idle_hours < required:
            row.update(outcome=WAITING, reason="idle time is below the current threshold")
            continue
        if dry_run:
            row.update(outcome=PLANNED, reason=f"{route} then guarded release")
            continue
        try:
            current = record
            if dormant:
                from .lifecycle import park

                park(
                    repository_path,
                    target=Path(item.path),
                    expected_record_version=current.record_version,
                    owner=current.owner,
                )
                current = _reload(repository_path, item)
                row["parked"] = True
            current = _register_venv(repository_path, item, current)
            current = _close(repository_path, item, current, route, actor)
            receipt = release(
                repository_path,
                target=Path(item.path),
                expected_record_version=current.record_version,
                owner_release=current.owner,
                teardown_reproducible_venv=any(
                    resource["state"] == "registered" for resource in current.disposable_resources
                ),
            )
        except ControllerError as exc:
            row.update(outcome=BLOCKED, reason=str(exc), code=exc.code)
            continue
        released += 1
        row.update(
            outcome=RELEASED,
            reason=f"{route} then guarded release",
            receipt_id=receipt["decision"].get("release_receipt_id"),
            release_plan_id=receipt["decision"].get("release_plan_id"),
        )

    result = envelope("reconcile retention")
    counts = Counter(str(row["outcome"]) for row in findings)
    attention = counts[ATTENTION]
    result["decision"] = {
        "code": "RETENTION_CONVERGED" if released else "NO_RETENTION_ACTION",
        "reason": _summary(counts, released),
        "outcome_counts": dict(sorted(counts.items())),
        "actor": actor,
        "trigger": trigger,
        "dry_run": dry_run,
        "registered": len(linked),
        "live": live,
        "pressure_reference": live + settings.standing_reserve,
        "idle_threshold_hours": round(threshold, 2),
        "grace_hours": round(grace, 2),
        "released": released,
        "would_release": sum(row["outcome"] == PLANNED for row in findings),
        "attention_required": attention,
        "findings": findings,
    }
    return result


def _summary(counts: Counter[str], released: int) -> str:
    """Name what actually happened instead of blaming every protection on idle time."""

    if not sum(counts.values()):
        return "no linked checkout is registered"
    head = (
        f"{released} idle checkout(s) closed through guarded release"
        if released
        else "no checkout was released"
    )
    parts = [f"{counts[outcome]} {label}" for outcome, label in OUTCOME_LABELS if counts[outcome]]
    return f"{head}; {', '.join(parts)}" if parts else head


def _dirty_owner_action(item: WorktreeGitState, record: MetadataRecord) -> str:
    return (
        f"owner {record.owner} (task {record.task_key}) reviews `git -C {item.path} status`, "
        "then commits it through Git Finalizer or discards it explicitly, and ends with "
        "park or handoff; the next pass closes the clean checkout"
    )


ADMISSION_CONVERGENCE_INTERVAL_SECONDS = 300


def _recent_pass(common_dir: Path) -> dict[str, Any] | None:
    """A running or recently started pass that an admission need not repeat."""
    report = last_applied_pass(common_dir)
    if not report:
        return None
    if report.get("state") == "RUNNING":
        return {
            "code": "CONVERGENCE_RUNNING",
            "pid": report.get("pid"),
            "started_at": report.get("started_at"),
        }
    try:
        started = datetime.fromisoformat(str(report.get("started_at")))
    except ValueError:
        return None
    if started.tzinfo is None:
        return None
    age = (datetime.now(UTC) - started).total_seconds()
    if 0 <= age < ADMISSION_CONVERGENCE_INTERVAL_SECONDS:
        return {
            "code": "CONVERGENCE_RECENT",
            "started_at": report.get("started_at"),
            "interval_seconds": ADMISSION_CONVERGENCE_INTERVAL_SECONDS,
        }
    return None


def _spawn_detached(arguments: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def admission_convergence(repository_path: Path) -> dict[str, Any] | None:
    """Start a detached convergence pass after an admission; never block admission.

    The pass runs in its own session so neither the caller's latency nor a caller that
    is killed mid-way (tool timeouts) can interrupt a guarded release transaction.
    A pass that is still running, or one started within the admission interval, is
    not started again: back-to-back admissions would otherwise queue behind repeated
    passes over the same unchanged checkouts.
    """

    try:
        repository = inventory(repository_path)
        policy = load_policy(repository.canonical_path)
        if policy.convergence is None or not policy.convergence.enabled:
            return None
        recent = _recent_pass(repository.common_dir)
        if recent is not None:
            return recent
        child = _spawn_detached(
            [
                sys.executable,
                "-m",
                "worktree_controller.cli",
                "reconcile",
                "retention",
                "--repo",
                str(repository.canonical_path),
                "--actor",
                policy.reconciler_actor,
                "--trigger",
                "admission",
                "--apply",
                "--json",
            ]
        )
    except (ControllerError, OSError) as exc:
        code = exc.code if isinstance(exc, ControllerError) else "OS_ERROR"
        return {"code": "CONVERGENCE_SKIPPED", "reason": str(exc), "error": code}
    # Starting is not finishing: the pass records its own RUNNING/COMPLETED/FAILED
    # report, which `reconcile retention` (dry run) returns as last_applied_pass.
    return {
        "code": "CONVERGENCE_SCHEDULED",
        "pid": child.pid,
        "scheduled_at": _now_iso(),
        "result": "reconcile retention --json (dry run) decision.last_applied_pass",
    }
