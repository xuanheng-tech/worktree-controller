"""Command-line interface for worktree-controller."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .branch_retirement import (
    authorize_branch_retirement,
    branch_retirement_executions,
    branch_retirement_status,
    complete_branch_retirement,
    plan_branch_retirement,
    record_branch_retirement,
    verify_branch_retirement,
)
from .controller import adopt, doctor, envelope, guard_write, plan, status
from .delivered_closeout import archive_retained, close_delivered
from .frozen_recovery import recover_frozen_checkout
from .governance import gc_apply, gc_plan
from .integration_conflict import resolve_integration
from .integration_retry import retry_integration
from .lifecycle import (
    abandon_integration_intent,
    abort_integration,
    abort_publication_lease,
    acknowledge_lifecycle_event,
    acquire,
    acquire_across,
    acquire_publication_lease,
    acquire_when_free,
    adopt_delivered,
    adopt_recovery,
    claim_integration,
    cleanup_disposable,
    cleanup_typed_disposable,
    close_preserved,
    complete_no_change,
    complete_publication_lease,
    create_release_intent,
    enqueue_integration,
    externalize,
    handoff,
    integration_queue,
    integration_recovery_matrix,
    lifecycle_events,
    park,
    quarantine,
    reconcile_integration_intents,
    reconcile_publication_receipts,
    reconcile_release_intents,
    reconcile_release_plans,
    reconcile_session,
    record_equivalent_replay,
    refresh_integration,
    register_disposable,
    release,
    reopen,
    reopen_integration,
    resume,
    review,
    update_external_binding,
    validate_integration,
)
from .models import (
    LIFECYCLE_STATES,
    ORDINARY_ROLES,
    WORKTREE_ROLES,
    ControllerError,
    WriterHolder,
)
from .namespace_migration import (
    LEGACY_ROOT_NAME,
    migrate_namespace,
    namespace_migration_receipts,
    plan_namespace_migration,
    verify_namespace,
)
from .reconciler import reconcile_writers
from .rendering import render
from .retirement_execution import KIND_CAS_DELETE
from .writer_lease import acquire_writer_lease, release_writer_lease, resolve_holder


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="path inside the managed Git repository"
    )
    parser.add_argument("--json", action="store_true", help="emit the stable JSON contract")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worktree-controller",
        description="Govern worktree budget, authority, ownership, and lifecycle",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()

    from .task_workflow import add_parser as add_task_parser

    add_task_parser(commands, common)

    status_parser = commands.add_parser("status", parents=[common], help="show Git and controller state")
    status_parser.add_argument("--namespace-pid", type=int,
                               help="compare mounts with an existing writer process")
    history_parser = commands.add_parser(
        "lifecycle-history", parents=[common],
        help="query live allocation and durable closed lifecycle evidence without mutation",
    )
    selector = history_parser.add_mutually_exclusive_group(required=True)
    for name in ("allocation-id", "authority-key", "task-key", "integration-intent-id", "release-receipt-id"):
        selector.add_argument(f"--{name}")
    history_parser.add_argument("--limit", type=int, default=100)
    history_parser.add_argument("--after", help="exclusive release receipt id cursor")
    inspect_parser = commands.add_parser(
        "release-recovery-inspect", parents=[common],
        help="read the exact original release request without recovery or authorization",
    )
    inspect_parser.add_argument("--transaction-id", required=True)
    guide_parser = commands.add_parser(
        "recovery-guide", parents=[common], help="explain a blocked task and its next formal step without mutation",
    )
    guide_selector = guide_parser.add_mutually_exclusive_group(required=True)
    guide_selector.add_argument("--path", type=Path)
    guide_selector.add_argument("--intent-id")
    admission_parser = commands.add_parser("admission-queue", parents=[common],
                                          help="inspect or resume durable resource admission requests")
    admission_parser.add_argument("--apply", action="store_true")
    admission_parser.add_argument("--all-repositories", action="store_true")
    host_parser = commands.add_parser("host-execution", parents=[common],
                                     help="budget and track a process without a repository writer lease")
    host_parser.add_argument("--action",
                             choices=("admit", "bind", "transfer", "clear", "cancel", "inspect"),
                             required=True)
    host_parser.add_argument("--path", type=Path)
    host_parser.add_argument("--request-id")
    host_parser.add_argument("--client-id", default="agent-workspace")
    host_parser.add_argument("--session-id")
    host_parser.add_argument("--run-id")
    host_parser.add_argument("--holder-pid", type=int)
    host_parser.add_argument("--memory-bytes", type=int, default=2 * 1024**3)
    host_parser.add_argument("--recover-dead-holder", action="store_true",
                             help="typed release of an ALLOCATED reservation whose own recorded holder "
                                  "is provably dead; needs that holder digest and terminal evidence")
    host_parser.add_argument("--expected-holder-sha256",
                             help="sha256 of the reservation's recorded holder, presented by the caller")
    host_parser.add_argument("--terminal-evidence",
                             help="canonical JSON terminal-state record for the reservation's session/run")
    for execution_action in ("admit", "cancel"):
        execution_parser = commands.add_parser("execution-" + execution_action, parents=[common],
                                              help="reserve or cancel resources for a never-launched execution")
        execution_parser.add_argument("--path", type=Path, required=True)
        execution_parser.add_argument("--owner", required=True)
        execution_parser.add_argument("--client-id", required=True)
        execution_parser.add_argument("--session-id", required=True)
        execution_parser.add_argument("--run-id")
        execution_parser.add_argument("--memory-bytes", type=int, default=2 * 1024**3)
    admit_session_parser = commands.add_parser(
        "session-admit", parents=[common],
        help="acquire a new feature allocation and admit its execution in one process",
    )
    admit_session_parser.add_argument("--task-key", required=True)
    admit_session_parser.add_argument("--authority-key", required=True)
    admit_session_parser.add_argument("--module", required=True)
    admit_session_parser.add_argument("--owner", required=True)
    admit_session_parser.add_argument("--branch", required=True)
    admit_session_parser.add_argument("--base-ref", required=True)
    admit_session_parser.add_argument("--client-id", required=True)
    admit_session_parser.add_argument("--session-id", required=True)
    admit_session_parser.add_argument("--run-id")
    bind_session_parser = commands.add_parser(
        "session-bind", parents=[common],
        help="grant, check and register one live executor's write entitlement in one process",
    )
    bind_session_parser.add_argument("--path", type=Path, required=True)
    bind_session_parser.add_argument("--task-key", required=True)
    bind_session_parser.add_argument("--authority-key", required=True)
    bind_session_parser.add_argument("--owner", required=True)
    bind_session_parser.add_argument("--client-id", required=True)
    bind_session_parser.add_argument("--holder-pid", type=int, required=True)
    bind_session_parser.add_argument("--session-id", required=True)
    bind_session_parser.add_argument("--run-id")
    bind_session_parser.add_argument("--kind", choices=["agent", "command", "test"], default="agent")
    commands.add_parser(
        "capabilities", parents=[common], help="read caller compatibility capabilities"
    )

    refresh_parser = commands.add_parser(
        "canonical-refresh", parents=[common],
        help="fast-forward an ACTIVE idle canonical checkout to published main",
    )
    refresh_parser.add_argument("--dry-run", action="store_true")

    canonical_parser = commands.add_parser(
        "canonical-transition", parents=[common],
        help="advance a frozen canonical checkout to exact published main",
    )
    canonical_parser.add_argument("--path", type=Path, required=True)
    canonical_parser.add_argument("--expected-record-version", type=int, required=True)
    canonical_parser.add_argument("--owner", required=True)
    canonical_parser.add_argument("--expected-branch", required=True)
    canonical_parser.add_argument("--expected-head", required=True)
    canonical_parser.add_argument("--expected-main-head", required=True)
    canonical_parser.add_argument("--expected-remote-head", required=True)
    canonical_parser.add_argument("--new-owner", required=True)
    canonical_parser.add_argument("--new-task-key", required=True)
    canonical_parser.add_argument("--new-authority-key", required=True)

    plan_parser = commands.add_parser(
        "plan", parents=[common], help="make a read-only allocation decision"
    )
    plan_parser.add_argument("--task-key", required=True)
    plan_parser.add_argument("--authority-key")
    plan_parser.add_argument("--module", required=True)
    plan_parser.add_argument("--role", required=True, choices=sorted(WORKTREE_ROLES))
    plan_parser.add_argument("--target", type=Path)

    wait_parser = commands.add_parser(
        "acquire-when-free",
        parents=[common],
        help="acquire, waiting a bounded time for capacity that may free",
    )
    wait_parser.add_argument("--task-key", required=True)
    wait_parser.add_argument("--authority-key", required=True)
    wait_parser.add_argument("--module", required=True)
    wait_parser.add_argument("--role", required=True, choices=sorted(ORDINARY_ROLES))
    wait_parser.add_argument("--owner", required=True)
    wait_parser.add_argument("--branch", required=True)
    wait_parser.add_argument("--base-ref", required=True)
    wait_parser.add_argument("--path", type=Path)
    wait_parser.add_argument("--scope", action="append", default=[])
    wait_parser.add_argument("--deadline-seconds", type=int, required=True)
    wait_parser.add_argument("--poll-seconds", type=float, default=2.0)

    across_parser = commands.add_parser(
        "acquire-across",
        parents=[common],
        help="admit one task in every required repository, or in none",
    )
    across_parser.add_argument(
        "--request",
        action="append",
        required=True,
        help="JSON object naming repository, task_key, authority_key, module, role, "
        "owner, branch, base_ref and optional target/scope_paths",
    )

    guard_parser = commands.add_parser(
        "write-guard", parents=[common], help="authorize a declared task write before editing"
    )
    guard_parser.add_argument("--path", type=Path, required=True)
    guard_parser.add_argument("--task-key", required=True)
    guard_parser.add_argument("--authority-key", required=True)
    guard_parser.add_argument("--owner", required=True)
    guard_parser.add_argument("--holder", help="pid or ancestor:<program> of the lease holder")
    guard_parser.add_argument("--client-id", default="manual")
    guard_parser.add_argument("--session-id")
    guard_parser.add_argument("--run-id")
    guard_parser.add_argument(
        "--operation",
        required=True,
        choices=["feature-write", "integration-write", "recovery", "finalization"],
    )

    adopt_parser = commands.add_parser(
        "adopt", parents=[common], help="adopt a registered worktree into metadata"
    )
    adopt_parser.add_argument("--path", type=Path, required=True)
    adopt_parser.add_argument("--task-key", required=True)
    adopt_parser.add_argument("--authority-key", required=True)
    adopt_parser.add_argument("--module", required=True)
    adopt_parser.add_argument("--role", required=True, choices=sorted(WORKTREE_ROLES))
    adopt_parser.add_argument("--owner", required=True)
    adopt_parser.add_argument("--lifecycle", required=True, choices=sorted(LIFECYCLE_STATES))
    adopt_parser.add_argument("--base-commit")
    adopt_parser.add_argument("--task-commit")
    adopt_parser.add_argument("--scope", action="append", default=[])
    adopt_parser.add_argument("--source-allocation", action="append", default=[])
    adopt_parser.add_argument("--external-binding", action="append", default=[])

    acquire_parser = commands.add_parser(
        "acquire", parents=[common], help="create one policy-approved linked worktree"
    )
    acquire_parser.add_argument("--task-key", required=True)
    acquire_parser.add_argument("--authority-key", required=True)
    acquire_parser.add_argument("--module", required=True)
    acquire_parser.add_argument("--role", required=True, choices=sorted(WORKTREE_ROLES))
    acquire_parser.add_argument("--owner", required=True)
    acquire_parser.add_argument("--branch", required=True)
    acquire_parser.add_argument("--base-ref", required=True)
    acquire_parser.add_argument("--path", type=Path)
    acquire_parser.add_argument("--scope", action="append", default=[])
    acquire_parser.add_argument("--source-allocation", action="append", default=[])
    acquire_parser.add_argument("--source-handoff", action="append", default=[])
    acquire_parser.add_argument("--external-binding", action="append", default=[])

    commands.add_parser(
        "integration-queue", parents=[common], help="show frozen integration intent queue"
    )

    enqueue_parser = commands.add_parser(
        "integration-enqueue",
        parents=[common],
        help="queue a frozen source handoff without occupying a checkout",
    )
    enqueue_parser.add_argument("--task-key", required=True)
    enqueue_parser.add_argument("--authority-key", required=True)
    enqueue_parser.add_argument("--module", required=True)
    enqueue_parser.add_argument("--owner", required=True)
    enqueue_parser.add_argument("--source-allocation", action="append", default=[])
    enqueue_parser.add_argument("--source-handoff", action="append", default=[])
    enqueue_parser.add_argument("--source-snapshot", action="append", default=[])

    claim_parser = commands.add_parser(
        "integration-claim",
        parents=[common],
        help="claim the oldest queued intent into an available candidate slot",
    )
    claim_parser.add_argument("--owner", required=True)
    claim_parser.add_argument("--intent-id")
    claim_parser.add_argument("--path", type=Path)
    claim_parser.add_argument("--branch")
    claim_parser.add_argument("--resolve-conflicts", action="store_true")

    resolve_parser = commands.add_parser(
        "integration-resolve", parents=[common],
        help="bind an owner-resolved two-parent merge to its pending conflict claim",
    )
    resolve_parser.add_argument("--path", type=Path, required=True)
    resolve_parser.add_argument("--expected-record-version", type=int, required=True)
    resolve_parser.add_argument("--owner", required=True)
    resolve_parser.add_argument("--expected-head", required=True)
    resolve_parser.add_argument("--finalizer-record-id", required=True)

    refresh_parser = commands.add_parser(
        "integration-refresh",
        parents=[common],
        help="rebuild a validated candidate from frozen handoffs and latest main",
    )
    refresh_parser.add_argument("--path", type=Path, required=True)
    refresh_parser.add_argument("--expected-record-version", type=int, required=True)
    refresh_parser.add_argument("--owner", required=True)

    validate_parser = commands.add_parser(
        "integration-validate",
        parents=[common],
        help="bind validation evidence to the exact candidate revision",
    )
    validate_parser.add_argument("--path", type=Path, required=True)
    validate_parser.add_argument("--expected-record-version", type=int, required=True)
    validate_parser.add_argument("--owner", required=True)
    validate_parser.add_argument("--validation-evidence-id", required=True)
    validate_parser.add_argument("--tests-receipt-id")
    validate_parser.add_argument("--snapshot-receipt-id")

    integration_reopen_parser = commands.add_parser(
        "integration-reopen",
        parents=[common],
        help="invalidate prepared evidence before changing an integration candidate",
    )
    integration_reopen_parser.add_argument("--path", type=Path, required=True)
    integration_reopen_parser.add_argument("--expected-record-version", type=int, required=True)
    integration_reopen_parser.add_argument("--owner", required=True)

    events_parser = commands.add_parser(
        "lifecycle-events",
        parents=[common],
        help="list durable integration lifecycle events not acknowledged by a consumer",
    )
    events_parser.add_argument("--consumer", required=True)

    event_ack_parser = commands.add_parser(
        "lifecycle-event-ack",
        parents=[common],
        help="idempotently acknowledge one durable integration lifecycle event",
    )
    event_ack_parser.add_argument("--consumer", required=True)
    event_ack_parser.add_argument("--event-id", required=True)

    commands.add_parser(
        "integration-recovery-matrix",
        parents=[common],
        help="show the supported integration transaction recovery contract",
    )

    equivalent_parser = commands.add_parser(
        "integration-equivalent",
        parents=[common],
        help="bind explicit equivalent-replay evidence before integration validation",
    )
    equivalent_parser.add_argument("--path", type=Path, required=True)
    equivalent_parser.add_argument("--expected-record-version", type=int, required=True)
    equivalent_parser.add_argument("--owner", required=True)
    equivalent_parser.add_argument("--source-allocation", required=True)
    equivalent_parser.add_argument("--source-handoff", required=True)
    equivalent_parser.add_argument("--replay-commit", required=True)
    equivalent_parser.add_argument("--evidence-id", required=True)

    no_change_parser = commands.add_parser(
        "complete-no-change",
        parents=[common],
        help="record a genuine no-change completion for a task that produced no commit",
    )
    no_change_parser.add_argument("--path", type=Path, required=True)
    no_change_parser.add_argument("--expected-record-version", type=int, required=True)
    no_change_parser.add_argument("--owner", required=True)
    delivered_parser = commands.add_parser(
        "close-delivered", parents=[common],
        help="close an owner-reviewed retained or legacy preserved checkout already published",
    )
    delivered_parser.add_argument("--path", type=Path, required=True)
    delivered_parser.add_argument("--expected-record-version", type=int, required=True)
    delivered_parser.add_argument("--owner-release", required=True)
    delivered_parser.add_argument("--actor", required=True)
    delivered_parser.add_argument("--dry-run", action="store_true")
    delivered_parser.add_argument("--plan-id")
    archive_parser = commands.add_parser(
        "archive-retained", parents=[common],
        help="preserve an unfinished clean retained checkout on its branch before release",
    )
    archive_parser.add_argument("--path", type=Path, required=True)
    archive_parser.add_argument("--expected-record-version", type=int, required=True)
    archive_parser.add_argument("--owner-release", required=True)
    archive_parser.add_argument("--actor", required=True)
    archive_parser.add_argument("--dry-run", action="store_true")
    archive_parser.add_argument("--plan-id")
    preserved_parser = commands.add_parser("close-preserved", parents=[common],
        help="close exactly preserved source work without declaring business completion")
    preserved_parser.add_argument("--path", type=Path, required=True)
    preserved_parser.add_argument("--expected-record-version", type=int, required=True)
    preserved_parser.add_argument("--owner", required=True)
    abandon_parser = commands.add_parser(
        "integration-abandon",
        parents=[common],
        help="terminate an integration intent whose preparation is already gone",
    )
    abandon_parser.add_argument("--intent-id", required=True)
    abandon_parser.add_argument("--owner", required=True)
    abandon_parser.add_argument("--reason", required=True)
    abort_parser = commands.add_parser(
        "integration-abort",
        parents=[common],
        help="terminate a failed or owner-aborted integration preparation",
    )
    abort_parser.add_argument("--path", type=Path, required=True)
    abort_parser.add_argument("--expected-record-version", type=int, required=True)
    abort_parser.add_argument("--owner", required=True)
    abort_parser.add_argument("--reason", required=True)
    retry_parser = commands.add_parser(
        "integration-retry", parents=[common],
        help="retry an aborted released candidate under its original frozen intent",
    )
    retry_parser.add_argument("--intent-id", required=True)
    retry_parser.add_argument("--owner", required=True)
    retry_parser.add_argument("--reason", required=True)
    retry_parser.add_argument("--prerequisite-intent")
    retry_parser.add_argument("--dry-run", action="store_true")
    retry_parser.add_argument("--expected-plan-id")
    session_parser = commands.add_parser(
        "reconcile-session",
        parents=[common],
        help="terminal reconciliation when a task session stops",
    )
    session_parser.add_argument("--path", type=Path, required=True)
    session_parser.add_argument("--expected-record-version", type=int, required=True)
    session_parser.add_argument("--owner", required=True)
    session_parser.add_argument(
        "--trigger", required=True, choices=["task-terminal", "session-archive", "manual"]
    )
    lease_acquire = commands.add_parser(
        "writer-lease-acquire",
        parents=[common],
        help="bind write entitlement to a verifiable live process",
    )
    lease_acquire.add_argument("--path", type=Path, required=True)
    lease_acquire.add_argument("--owner")
    lease_acquire.add_argument("--client-id", required=True)
    lease_acquire.add_argument("--holder-pid", type=int, required=True)
    lease_acquire.add_argument("--session-id")
    lease_acquire.add_argument("--run-id")
    lease_acquire.add_argument("--ttl-seconds", type=int)
    lease_acquire.add_argument("--expected-record-version", type=int)
    lease_release = commands.add_parser(
        "writer-lease-release",
        parents=[common],
        help="give up write entitlement without asserting the task finished",
    )
    lease_release.add_argument("--path", type=Path, required=True)
    lease_release.add_argument("--holder-pid", type=int)
    lease_release.add_argument("--holder-boot-id")
    lease_release.add_argument("--holder-start-ticks", type=int)
    lease_release.add_argument("--client-id", default="manual")
    lease_release.add_argument("--session-id")
    lease_release.add_argument("--run-id")
    lease_release.add_argument("--lease-id")
    commands.add_parser("writer-session", help="strict executor admission and post-exit cleanup")
    reconcile_writers_parser = commands.add_parser(
        "reconcile-writers",
        parents=[common],
        help="report, and optionally converge, provably inactive writer entitlement",
    )
    reconcile_writers_parser.add_argument("--actor", default="lifecycle-reconciler")
    reconcile_writers_parser.add_argument(
        "--trigger", choices=("manual", "admission", "background"), default="manual"
    )
    reconcile_writers_parser.add_argument(
        "--apply",
        action="store_true",
        help="converge instead of reporting; without it the command is read-only",
    )
    reconcile_writers_parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TASK_KEY",
        help="converge only these allocations; everything else is reported but untouched",
    )
    reconcile_writers_parser.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="one-time classification of allocations created before writer leases",
    )
    park_parser = commands.add_parser(
        "park", parents=[common], help="retain an unfinished checkout and free its writer slot"
    )
    park_parser.add_argument("--path", type=Path, required=True)
    park_parser.add_argument("--expected-record-version", type=int, required=True)
    park_parser.add_argument("--owner", required=True)
    park_parser.add_argument("--checkpoint-id", help="content-addressed checkpoint retained by the existing Task")
    park_parser.add_argument("--checkpoint-head", help="exact HEAD saved in that checkpoint")
    park_parser.add_argument("--waiting-on", help="external dependency kept by the Task, not a reason to retain a directory")
    resume_parser = commands.add_parser(
        "resume", parents=[common], help="return a retained checkout to writer status"
    )
    resume_parser.add_argument("--path", type=Path, required=True)
    resume_parser.add_argument("--expected-record-version", type=int, required=True)
    resume_parser.add_argument("--owner", required=True)
    adopt_parser = commands.add_parser(
        "adopt-delivered",
        parents=[common],
        help="adopt an allocation whose deliverable is already in the publication branch",
    )
    adopt_parser.add_argument("--path", type=Path, required=True)
    adopt_parser.add_argument("--expected-record-version", type=int, required=True)
    adopt_parser.add_argument("--new-owner", required=True)
    adopt_parser.add_argument("--actor", required=True)
    review_parser = commands.add_parser(
        "review", parents=[common], help="record owner review or explicit owner transfer"
    )
    review_parser.add_argument("--path", type=Path, required=True)
    review_parser.add_argument("--expected-record-version", type=int, required=True)
    review_parser.add_argument("--owner", required=True)
    review_parser.add_argument("--new-owner")
    review_parser.add_argument("--add-scope", action="append", default=[])
    review_parser.add_argument("--expected-head")
    review_parser.add_argument("--scope-reason")

    handoff_parser = commands.add_parser(
        "handoff", parents=[common], help="record an integration-ready or preserved handoff"
    )
    handoff_parser.add_argument("--path", type=Path, required=True)
    handoff_parser.add_argument("--expected-record-version", type=int, required=True)
    handoff_parser.add_argument("--kind", required=True, choices=["integration-ready", "preserved"])
    handoff_parser.add_argument("--commit", required=True)
    handoff_parser.add_argument("--finalizer-record-id", required=True)
    handoff_parser.add_argument("--finalizer-commit")
    handoff_parser.add_argument("--scope", action="append", default=[])
    handoff_parser.add_argument(
        "--scope-changed", action="store_true",
        help="record the exact paths the handoff commit changed as the task scope",
    )
    handoff_parser.add_argument("--mainline-commit")

    quarantine_parser = commands.add_parser(
        "quarantine", parents=[common], help="isolate a drifted integration candidate"
    )
    quarantine_parser.add_argument("--path", type=Path, required=True)
    quarantine_parser.add_argument("--expected-record-version", type=int, required=True)
    quarantine_parser.add_argument("--owner", required=True)
    quarantine_parser.add_argument("--reason", required=True)
    quarantine_parser.add_argument("--expected-commit")

    recovery_adopt_parser = commands.add_parser(
        "recovery-adopt",
        parents=[common],
        help="move an existing checkout under governance recovery capacity",
    )
    recovery_adopt_parser.add_argument("--source-path", type=Path, required=True)
    recovery_adopt_parser.add_argument("--recovery-path", type=Path, required=True)
    recovery_adopt_parser.add_argument("--expected-source-record-version", type=int, required=True)
    recovery_adopt_parser.add_argument(
        "--expected-recovery-record-version", type=int, required=True
    )
    recovery_adopt_parser.add_argument("--source-owner", required=True)
    recovery_adopt_parser.add_argument("--recovery-owner", required=True)
    recovery_adopt_parser.add_argument("--scope", action="append", default=[])

    externalize_parser = commands.add_parser(
        "externalize", parents=[common], help="bind quarantined lineage to a recovery allocation"
    )
    externalize_parser.add_argument("--source-path", type=Path, required=True)
    externalize_parser.add_argument("--recovery-path", type=Path, required=True)
    externalize_parser.add_argument("--expected-source-record-version", type=int, required=True)
    externalize_parser.add_argument("--expected-recovery-record-version", type=int, required=True)
    externalize_parser.add_argument("--owner", required=True)

    build_register = commands.add_parser("build-resource-register", parents=[common],
                                        help="register exact owner-attested frozen Node build outputs")
    build_register.add_argument("--path", type=Path, required=True)
    build_register.add_argument("--owner", required=True)
    build_register.add_argument("--expected-record-version", type=int, required=True)
    build_register.add_argument("--expected-head", required=True)
    build_register.add_argument("--project", required=True)
    build_register.add_argument("--recipe", required=True)
    build_register.add_argument("--resource-path", action="append", required=True)
    build_register.add_argument("--attest-rebuildable", action="store_true", required=True)
    build_register.add_argument("--dry-run", action="store_true")
    build_register.add_argument("--expected-plan-id")

    resource_register_parser = commands.add_parser(
        "resource-register", parents=[common], help="register a rebuildable task-local resource"
    )
    resource_register_parser.add_argument("--path", type=Path, required=True)
    resource_register_parser.add_argument("--expected-record-version", type=int, required=True)
    resource_register_parser.add_argument("--owner", required=True)
    resource_register_parser.add_argument("--resource-path", required=True)
    resource_register_parser.add_argument("--kind", required=True, choices=["python-venv"])
    resource_register_parser.add_argument(
        "--reproducible", action=argparse.BooleanOptionalAction, required=True
    )
    resource_register_parser.add_argument("--creator", required=True)
    resource_register_parser.add_argument(
        "--teardown-policy", required=True, choices=["manual", "on-release"]
    )

    resource_clean_parser = commands.add_parser(
        "resource-clean", parents=[common], help="remove a registered rebuildable resource"
    )
    resource_clean_parser.add_argument("--path", type=Path, required=True)
    resource_clean_parser.add_argument("--expected-record-version", type=int, required=True)
    resource_clean_parser.add_argument("--owner", required=True)
    resource_mode = resource_clean_parser.add_mutually_exclusive_group(required=True)
    resource_mode.add_argument("--resource-path")
    resource_mode.add_argument(
        "--typed", action="store_true", help="use the narrow typed cleanup contract"
    )
    resource_clean_parser.add_argument("--dry-run", action="store_true")
    resource_clean_parser.add_argument("--expected-plan-id")
    resource_clean_parser.add_argument("--preservation-receipt", type=Path)
    resource_clean_parser.add_argument("--preservation-receipt-sha256")
    resource_clean_parser.add_argument(
        "--additional-preservation-receipt", type=Path, action="append", default=[]
    )
    resource_clean_parser.add_argument(
        "--additional-preservation-receipt-sha256", action="append", default=[]
    )

    recovery_parser = commands.add_parser(
        "resource-recover",
        parents=[common],
        help="append a re-preserved cleanup baseline or resume its remaining actions",
    )
    recovery_parser.add_argument(
        "--stage",
        required=True,
        choices=["prepare", "prepare-multiple", "resume", "reconcile-attempt", "reconcile-typed", "rollout-check"],
    )
    recovery_parser.add_argument("--path", type=Path)
    recovery_parser.add_argument("--owner")
    recovery_parser.add_argument("--expected-record-version", type=int)
    recovery_parser.add_argument("--transaction-id")
    recovery_parser.add_argument("--transaction-sha256")
    recovery_parser.add_argument("--cleanup-receipt-sha256")
    recovery_parser.add_argument("--preservation-receipt", type=Path)
    recovery_parser.add_argument("--preservation-receipt-sha256")
    recovery_parser.add_argument("--expected-plan-id")
    recovery_parser.add_argument("--recovery-receipt", type=Path)
    recovery_parser.add_argument("--recovery-receipt-sha256")
    recovery_parser.add_argument("--dry-run", action="store_true")

    lease_acquire_parser = commands.add_parser(
        "lease-acquire", parents=[common], help="acquire serialized main publication authority"
    )
    lease_acquire_parser.add_argument("--path", type=Path, required=True)
    lease_acquire_parser.add_argument("--expected-record-version", type=int, required=True)
    lease_acquire_parser.add_argument("--owner", required=True)
    lease_acquire_parser.add_argument("--run-id")
    lease_acquire_parser.add_argument("--ttl-seconds", type=int, default=900)

    lease_complete_parser = commands.add_parser(
        "lease-complete", parents=[common], help="verify publication and release its lease"
    )
    lease_complete_parser.add_argument("--path", type=Path, required=True)
    lease_complete_parser.add_argument("--expected-record-version", type=int, required=True)
    lease_complete_parser.add_argument("--lease-id", required=True)
    lease_complete_parser.add_argument("--published-commit", required=True)
    lease_complete_parser.add_argument("--finalizer-record-id", required=True)
    lease_complete_parser.add_argument("--run-id", required=True)
    lease_complete_parser.add_argument(
        "--auto-release",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="guarded release after remote verification (default: enabled)",
    )

    publication_session_parser = commands.add_parser(
        "publication-execution-session", parents=[common],
        help="fence a consumer's exact publication through the public stdio protocol",
    )
    publication_session_parser.add_argument("--candidate-oid", required=True)
    publication_session_parser.add_argument("--lease-id", required=True)
    publication_session_parser.add_argument("--run-id", required=True)

    lease_abort_parser = commands.add_parser(
        "lease-abort", parents=[common], help="release unchanged publication authority"
    )
    lease_abort_parser.add_argument("--path", type=Path, required=True)
    lease_abort_parser.add_argument("--expected-record-version", type=int, required=True)
    lease_abort_parser.add_argument("--lease-id")
    lease_abort_parser.add_argument("--owner", required=True)
    lease_abort_parser.add_argument("--run-id", required=True)

    release_parser = commands.add_parser(
        "release", parents=[common], help="remove an evidence-cleared linked checkout"
    )
    release_parser.add_argument("--path", type=Path, required=True)
    release_parser.add_argument("--expected-record-version", type=int, required=True)
    release_parser.add_argument("--owner-release")
    release_parser.add_argument("--intent-id")
    release_parser.add_argument("--publication-receipt-id")
    release_parser.add_argument("--teardown-reproducible-venv", action="store_true")
    release_parser.add_argument("--dry-run", action="store_true")

    gc_plan_parser = commands.add_parser(
        "gc-plan", parents=[common], help="classify worktrees and preflight a selected release"
    )
    gc_plan_parser.add_argument("--path", type=Path)
    gc_plan_parser.add_argument("--owner-release")
    gc_plan_parser.add_argument("--intent-id")
    gc_plan_parser.add_argument("--namespace-pid", type=int,
                                help="compare mounts with an existing writer process")

    gc_apply_parser = commands.add_parser(
        "gc-apply", parents=[common], help="apply a reviewed plan through guarded release"
    )
    gc_apply_parser.add_argument("--path", type=Path, required=True)
    gc_apply_parser.add_argument("--plan-id", required=True)
    gc_apply_parser.add_argument("--owner-release")
    gc_apply_parser.add_argument("--intent-id")

    release_intent_parser = commands.add_parser(
        "release-intent", parents=[common], help="record a short-lived feature release intent"
    )
    release_intent_parser.add_argument("--path", type=Path, required=True)
    release_intent_parser.add_argument("--expected-record-version", type=int, required=True)
    release_intent_parser.add_argument("--owner", required=True)
    release_intent_parser.add_argument(
        "--trigger",
        required=True,
        choices=[
            "integration-remote-verified",
            "task-terminal",
            "session-archive",
            "manual",
            "capacity-pressure",
            "binding-cleared",
        ],
    )
    release_intent_parser.add_argument("--integration-intent-id")

    retirement_plan_parser = commands.add_parser(
        "branch-retirement-plan",
        parents=[common],
        help="classify a released branch and persist an OID-bound retirement plan",
    )
    retirement_target = retirement_plan_parser.add_mutually_exclusive_group(required=True)
    retirement_target.add_argument("--allocation-id")
    retirement_target.add_argument("--branch")
    retirement_plan_parser.add_argument("--remote", required=True)
    retirement_plan_parser.add_argument("--integrated-into", required=True)
    retirement_plan_parser.add_argument("--dry-run", action="store_true")

    retirement_status_parser = commands.add_parser(
        "branch-retirement-status",
        parents=[common],
        help="revalidate a retirement plan and show independent local/remote progress",
    )
    retirement_status_parser.add_argument("--plan-id", required=True)

    retirement_verify_parser = commands.add_parser(
        "branch-retirement-verify",
        parents=[common],
        help="verify one retirement operation immediately before mutation and return the CAS fields",
    )
    retirement_verify_parser.add_argument("--plan-id", required=True)
    retirement_verify_parser.add_argument("--operation", choices=("local", "remote"), required=True)

    retirement_authorize_parser = commands.add_parser(
        "branch-retirement-authorize",
        parents=[common],
        help="grant one single-use execution authorization for one retirement operation",
    )
    retirement_authorize_parser.add_argument("--plan-id", required=True)
    retirement_authorize_parser.add_argument("--operation", choices=("local", "remote"), required=True)
    retirement_authorize_parser.add_argument("--request-id", required=True)
    retirement_authorize_parser.add_argument(
        "--holder",
        default="self",
        help="self, or ancestor:<program> of the process performing the mutation",
    )
    retirement_authorize_parser.add_argument("--client-id", default="retirement-executor")
    retirement_authorize_parser.add_argument("--session-id")
    retirement_authorize_parser.add_argument("--run-id")
    retirement_authorize_parser.add_argument(
        "--kind", choices=("cas-delete",), default="cas-delete", help="the only grantable kind"
    )
    retirement_authorize_parser.add_argument("--ttl-seconds", type=int, default=900)

    retirement_complete_parser = commands.add_parser(
        "branch-retirement-complete",
        parents=[common],
        help="close an authorized retirement using the Controller's own ref observation",
    )
    retirement_complete_parser.add_argument("--authorization-id", required=True)
    retirement_complete_parser.add_argument("--token", required=True)
    retirement_complete_parser.add_argument("--result-file", type=Path, required=True)

    retirement_executions_parser = commands.add_parser(
        "branch-retirement-executions",
        parents=[common],
        help="list, inspect or abandon outstanding retirement execution authorizations",
    )
    retirement_executions_parser.add_argument(
        "--action", choices=("list", "inspect", "abandon"), required=True
    )
    retirement_executions_parser.add_argument("--authorization-id")
    retirement_executions_parser.add_argument("--token")
    retirement_executions_parser.add_argument("--reason")

    retirement_record_parser = commands.add_parser(
        "branch-retirement-record",
        parents=[common],
        help="record one consumer-reported retirement execution result",
    )
    retirement_record_parser.add_argument("--plan-id", required=True)
    retirement_record_parser.add_argument("--operation", choices=("local", "remote"), required=True)
    summary_target = retirement_record_parser.add_mutually_exclusive_group(required=True)
    summary_target.add_argument(
        "--summary-file",
        type=Path,
        help="legacy consumer summary; translated by the compatibility adapter",
    )
    summary_target.add_argument(
        "--result-file", type=Path, help="provider-neutral retirement-execution-result/v1 document"
    )

    namespace_plan_parser = commands.add_parser(
        "namespace-migration-plan",
        parents=[common],
        help="report the legacy namespace move for one repository without writing",
    )
    namespace_plan_parser.add_argument("--legacy-namespace", default=LEGACY_ROOT_NAME)
    namespace_plan_parser.add_argument("--policy-from", type=Path)
    namespace_migrate_parser = commands.add_parser(
        "namespace-migrate",
        parents=[common],
        help="idempotently move the legacy metadata namespace under the repository lock",
    )
    namespace_migrate_parser.add_argument("--legacy-namespace", default=LEGACY_ROOT_NAME)
    namespace_migrate_parser.add_argument("--policy-from", type=Path)
    namespace_migrate_parser.add_argument("--actor", required=True)
    namespace_migrate_parser.add_argument("--expected-plan-id", required=True)
    namespace_migrate_parser.add_argument("--dry-run", action="store_true")
    commands.add_parser(
        "namespace-migration-receipts",
        parents=[common],
        help="list the immutable namespace migration receipts of one repository",
    )
    commands.add_parser(
        "namespace-migration-verify",
        parents=[common],
        help="re-derive a completed namespace migration from immutable evidence",
    )

    reopen_parser = commands.add_parser(
        "reopen", parents=[common], help="invalidate a frozen handoff before continuing work"
    )
    reopen_parser.add_argument("--path", type=Path, required=True)
    reopen_parser.add_argument("--expected-record-version", type=int, required=True)
    reopen_parser.add_argument("--owner", required=True)
    reopen_parser.add_argument(
        "--drifted-head",
        help="exact observed HEAD of a frozen checkout that gained commits after its handoff",
    )

    for recovery_command in ("recover-handoff", "recover-published"):
        recovery_parser = commands.add_parser(
            recovery_command, parents=[common], help="recover a published frozen checkout with immutable evidence"
        )
        recovery_parser.add_argument("--path", type=Path, required=True)
        recovery_parser.add_argument("--expected-record-version", type=int, required=True)
        recovery_parser.add_argument("--expected-head", required=True)
        recovery_parser.add_argument("--owner", required=True)
        if recovery_command == "recover-published":
            recovery_parser.add_argument("--branch", required=True)

    for binding_action in ("add", "remove", "transfer"):
        binding_parser = commands.add_parser(
            f"binding-{binding_action}",
            parents=[common],
            help=f"{binding_action} an explicit runtime or workspace binding",
        )
        binding_parser.add_argument("--path", type=Path, required=True)
        binding_parser.add_argument("--expected-record-version", type=int, required=True)
        binding_parser.add_argument("--owner", required=True)
        binding_parser.add_argument("--binding", required=True)
        if binding_action == "transfer":
            binding_parser.add_argument("--holder-pid", type=int, required=True)
        if binding_action == "remove":
            binding_parser.add_argument("--recover-archived-codex-session", action="store_true")
            binding_parser.add_argument("--dry-run", action="store_true")

    reconcile_parser = commands.add_parser(
        "reconcile", help="reconcile immutable receipts or existing release intents"
    )
    reconcile_commands = reconcile_parser.add_subparsers(dest="reconcile_command", required=True)
    receipts_parser = reconcile_commands.add_parser("receipts", parents=[common])
    receipts_parser.add_argument("--actor", required=True)
    receipts_parser.add_argument("--receipt-id", action="append", default=[])
    receipts_parser.add_argument("--dry-run", action="store_true")
    no_change_parser = reconcile_commands.add_parser(
        "no-change-completion", parents=[common],
        help="bind exact legacy no-change completion to its successful cleanup evidence",
    )
    no_change_parser.add_argument("--path", type=Path, required=True)
    no_change_parser.add_argument("--owner", required=True)
    no_change_parser.add_argument("--expected-record-version", type=int, required=True)
    no_change_parser.add_argument("--completion-receipt-id", required=True)
    no_change_parser.add_argument("--completion-receipt-sha256", required=True)
    no_change_parser.add_argument("--cleanup-receipt-id", action="append", required=True)
    no_change_parser.add_argument("--cleanup-receipt-sha256", action="append", required=True)
    no_change_parser.add_argument("--dry-run", action="store_true")
    no_change_parser.add_argument("--expected-plan-id")
    intents_parser = reconcile_commands.add_parser("release-intents", parents=[common])
    intents_parser.add_argument("--actor", required=True)
    intents_parser.add_argument(
        "--trigger",
        required=True,
        choices=[
            "integration-remote-verified",
            "task-terminal",
            "session-archive",
            "binding-cleared",
            "manual",
            "capacity-pressure",
        ],
    )
    intents_parser.add_argument("--intent-id", action="append", default=[])
    intents_parser.add_argument("--dry-run", action="store_true")
    release_plans_parser = reconcile_commands.add_parser("release-plans", parents=[common])
    release_plans_parser.add_argument("--actor", required=True)
    release_plans_parser.add_argument("--dry-run", action="store_true")
    retention_parser = reconcile_commands.add_parser(
        "retention",
        parents=[common],
        help="close idle clean checkouts through guarded release (dry-run unless --apply)",
    )
    retention_parser.add_argument("--actor", required=True)
    retention_parser.add_argument(
        "--trigger", choices=("manual", "admission", "background"), default="manual"
    )
    retention_parser.add_argument("--apply", action="store_true")
    registrations_parser = reconcile_commands.add_parser(
        "binding-registrations", parents=[common],
        help="tie legacy linked bindings to their current Git registration (dry-run first)",
    )
    registrations_parser.add_argument("--dry-run", action="store_true")
    registrations_parser.add_argument("--expected-plan-id")
    integration_intents_parser = reconcile_commands.add_parser(
        "integration-intents", parents=[common]
    )
    integration_intents_parser.add_argument("--actor", required=True)
    integration_intents_parser.add_argument("--intent-id", action="append", default=[])
    integration_intents_parser.add_argument("--dry-run", action="store_true")
    integration_intents_parser.add_argument("--dispatch", action="store_true")

    doctor_parser = commands.add_parser("doctor", parents=[common], help="diagnose controller drift")
    doctor_parser.add_argument("--namespace-pid", type=int,
                               help="compare mounts with an existing writer process")
    return parser


def _print(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(payload))


def main(argv: list[str] | None = None) -> int:
    from .store import decode_cache

    arguments = list(sys.argv[1:] if argv is None else argv)
    # Queries and execution's first attempt need one snapshot. Recovery opts into reuse below.
    if arguments and arguments[0] in {
        "status", "doctor", "lifecycle-history", "release-recovery-inspect", "recovery-guide",
        "execution-admit", "execution-cancel"
    }:
        return _main(arguments)
    # Writer checks load each repository once; retain the CLI reader without its caches.
    reuse = arguments[:2] not in (
        ["writer-session", "check"], ["writer-session", "host-check"]
    )
    with decode_cache(reuse=reuse):
        return _main(arguments)


def _main(argv: list[str] | None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "writer-session":
        from .writer_session import main as writer_session_main

        return writer_session_main(arguments[1:])
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    try:
        if parsed.command == "capabilities":
            from .writer_session import (
                HOST_HANDOFF_CONTINUATION_VERSION,
                HOST_MANAGED_WRITER_SESSION_VERSION,
            )

            payload = envelope("capabilities")
            payload["decision"] = {
                "code": "CAPABILITIES",
                "contract_version": 61,
                "task_workflow_version": 1,
                "exited_holder_release_version": 1,
                "canonical_refresh_version": 1,
                "host_handoff_continuation_version": HOST_HANDOFF_CONTINUATION_VERSION,
                "release_recovery_inspection_version": 1,
                "rollout_transaction_diagnostics_version": 1,
                "no_change_cleanup_closeout_version": 1,
                "canonical_transition_version": 1,
                "canonical_main_refresh_version": 1,
                "integration_lineage_admission_version": 1,
                "integration_keyed_document_scope_version": 1,
                "canonical_transition_retirement_version": 1,
                "closed_lifecycle_query_version": 1,
                "source_release_summary_version": 1,
                "repository_lock_admission_version": 1,
                "tracked_source_progress_recovery_version": 1,
                "published_checkout_recovery_version": 1,
                "published_handoff_resource_registration_version": 1,
                "registered_build_resources_version": 1,
                "build_preservation_recovery_version": 1,
                "retention_release_preflight_version": 1,
                "multiple_preservation_cleanup_version": 1,
                "multiple_preservation_recovery_version": 1,
                "preservation_supersession_version": 1,
                "retained_typed_cleanup_version": 1,
                "aborted_integration_preserved_cleanup_version": 2,
                "aborted_integration_branch_retirement_version": 1,
                "drifted_handoff_reopen_version": 1,
                "terminal_preserved_cleanup_version": 1,
                "preservation_leaf_version": 1,
                "integration_publication_execution_version": 1,
                "integration_retry_version": 1,
                "integration_conflict_resolution_version": 2,
                "session_admission_version": 1,
                "capabilities_version": 1,
                "storage_layout_version": 5,
                "writer_session_version": 2,
                "holder_resolver_version": 1,
                "host_managed_writer_session_version": HOST_MANAGED_WRITER_SESSION_VERSION,
                "resource_admission_version": 1,
                "acquire_no_effect_recovery_version": 1,
                "execution_admission_version": 1,
                "host_execution_admission_version": 2,
                "runtime_binding_version": 1,
                "archived_codex_binding_recovery_version": 1,
                "branch_retirement_version": 1,
                "branch_retirement_verify_version": 1,
                "branch_retirement_execution_version": 1,
                "namespace_migration_version": 1,
                "governance_version": 2,
                "delivered_closeout_version": 1,
                "retained_archive_version": 1,
                "retention_convergence_version": 2,
                "session_end_selector_version": 1,
                "session_closeout_report_version": 1,
                "session_closeout_expectations_version": 1,
                "recovery_guidance_version": 1,
                "hosted_session_closeout_check_version": 1,
                "checkout_owner_action_version": 1,
                "task_checkpoint_park_version": 1,
                "binding_registration_backfill_version": 1,
                "gc_apply_replay_version": 1,
                "doctor_mount_version": 2,
                "cleanup_hardlink_topology_version": 2,
                "typed_cleanup_no_action_version": 1,
                "typed_cleanup_no_action_receipt_version": 1,
                "typed_cleanup_verify_boundary_version": 1,
                "supported_writer_session_versions": [1, 2],
                "writer_lease_schema_version": 2,
            }
        elif parsed.command == "publication-execution-session":
            from .publication_execution import run_session

            return run_session(
                parsed.repo, candidate_oid=parsed.candidate_oid,
                lease_id=parsed.lease_id, run_id=parsed.run_id,
            )
        elif parsed.command == "status":
            payload = status(parsed.repo, namespace_pid=parsed.namespace_pid)
        elif parsed.command == "canonical-refresh":
            from .canonical_refresh import refresh_canonical

            payload = refresh_canonical(parsed.repo, dry_run=parsed.dry_run)
        elif parsed.command == "canonical-transition":
            from .canonical_transition import transition_canonical

            payload = transition_canonical(
                parsed.repo, target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner, expected_branch=parsed.expected_branch,
                expected_head=parsed.expected_head,
                expected_main_head=parsed.expected_main_head,
                expected_remote_head=parsed.expected_remote_head,
                new_owner=parsed.new_owner, new_task_key=parsed.new_task_key,
                new_authority_key=parsed.new_authority_key,
            )
        elif parsed.command == "lifecycle-history":
            from .lifecycle_history import lifecycle_history

            payload = lifecycle_history(
                parsed.repo, allocation_id=parsed.allocation_id,
                authority_key=parsed.authority_key, task_key=parsed.task_key,
                integration_intent_id=parsed.integration_intent_id,
                release_receipt_id=parsed.release_receipt_id,
                limit=parsed.limit, after=parsed.after,
            )
        elif parsed.command == "task":
            from .task_workflow import dispatch as dispatch_task

            payload = dispatch_task(parsed)
        elif parsed.command == "recovery-guide":
            from .recovery_guidance import recovery_guide

            payload = recovery_guide(parsed.repo, target=parsed.path, intent_id=parsed.intent_id)
        elif parsed.command == "release-recovery-inspect":
            from . import cleanup_recovery

            payload = cleanup_recovery.inspect_release(parsed.repo, transaction_id=parsed.transaction_id)
        elif parsed.command == "admission-queue":
            from .resource_admission import reconcile as reconcile_admission

            payload = reconcile_admission(repository_path=None if parsed.all_repositories else parsed.repo,
                                          apply=parsed.apply)
        elif parsed.command == "host-execution":
            from .resource_admission import host_execution

            evidence = None
            if parsed.terminal_evidence is not None:
                try:
                    evidence = json.loads(parsed.terminal_evidence)
                except ValueError:
                    raise ControllerError("INVALID_REQUEST", "terminal evidence must be canonical JSON") from None
            payload = host_execution(parsed.action, target=parsed.path, request_id=parsed.request_id,
                client_id=parsed.client_id, session_id=parsed.session_id, run_id=parsed.run_id,
                holder_pid=parsed.holder_pid, memory_bytes=parsed.memory_bytes,
                recover_dead_holder=parsed.recover_dead_holder,
                expected_holder_sha256=parsed.expected_holder_sha256,
                terminal_evidence=evidence)
        elif parsed.command == "session-admit":
            from .convergence import admission_convergence
            from .session_admission import admit_session

            payload = admit_session(
                parsed.repo, task_key=parsed.task_key, authority_key=parsed.authority_key,
                module=parsed.module, owner=parsed.owner, branch=parsed.branch,
                base_ref=parsed.base_ref, client_id=parsed.client_id,
                session_id=parsed.session_id, run_id=parsed.run_id,
            )
            if payload["decision"].get("code") in {"SESSION_ADMITTED", "RESOURCE_WAIT"}:
                # Started after admission, so the pass never holds the lock this
                # admission needs; it still runs on every admission that changed state.
                convergence = admission_convergence(parsed.repo)
                if convergence is not None:
                    payload["retention_convergence"] = convergence
        elif parsed.command == "session-bind":
            from .session_admission import bind_session

            payload = bind_session(
                parsed.repo, target=parsed.path, task_key=parsed.task_key,
                authority_key=parsed.authority_key, owner=parsed.owner,
                client_id=parsed.client_id, holder_pid=parsed.holder_pid,
                session_id=parsed.session_id, run_id=parsed.run_id, kind=parsed.kind,
            )
        elif parsed.command in {"execution-admit", "execution-cancel"}:
            from .resource_admission import admit_execution

            payload = admit_execution(parsed.repo, target=parsed.path, owner=parsed.owner,
                                      client_id=parsed.client_id, session_id=parsed.session_id,
                                      run_id=parsed.run_id, memory_bytes=parsed.memory_bytes,
                                      cancel=parsed.command == "execution-cancel", _cache_recovery=True)
        elif parsed.command == "plan":
            payload = plan(
                parsed.repo,
                task_key=parsed.task_key,
                authority_key=parsed.authority_key,
                module=parsed.module,
                role=parsed.role,
                target=parsed.target,
            )
        elif parsed.command == "write-guard":
            payload = guard_write(
                parsed.repo,
                target=parsed.path,
                task_key=parsed.task_key,
                authority_key=parsed.authority_key,
                owner=parsed.owner,
                operation=parsed.operation,
                holder=(
                    resolve_holder(
                        parsed.holder,
                        client_id=parsed.client_id,
                        session_id=parsed.session_id,
                        run_id=parsed.run_id,
                    )
                    if parsed.holder
                    else None
                ),
            )
        elif parsed.command == "adopt":
            payload = adopt(
                parsed.repo,
                target=parsed.path,
                task_key=parsed.task_key,
                authority_key=parsed.authority_key,
                module=parsed.module,
                role=parsed.role,
                owner=parsed.owner,
                lifecycle=parsed.lifecycle,
                base_commit=parsed.base_commit,
                task_commit=parsed.task_commit,
                scope_paths=tuple(parsed.scope),
                source_allocations=tuple(parsed.source_allocation),
                external_bindings=tuple(parsed.external_binding),
            )
        elif parsed.command == "acquire":
            from .convergence import admission_convergence

            try:
                payload = acquire(
                    parsed.repo,
                    task_key=parsed.task_key,
                    authority_key=parsed.authority_key,
                    module=parsed.module,
                    role=parsed.role,
                    owner=parsed.owner,
                    branch=parsed.branch,
                    base_ref=parsed.base_ref,
                    target=parsed.path,
                    scope_paths=tuple(parsed.scope),
                    source_allocations=tuple(parsed.source_allocation),
                    source_handoffs=tuple(parsed.source_handoff),
                    external_bindings=tuple(parsed.external_binding),
                )
            finally:
                # After the acquisition, including a refused one, so the detached
                # pass never queues this admission behind its repository lock.
                convergence = admission_convergence(parsed.repo)
            if convergence is not None:
                payload["retention_convergence"] = convergence
        elif parsed.command == "integration-queue":
            payload = integration_queue(parsed.repo)
        elif parsed.command == "integration-enqueue":
            payload = enqueue_integration(
                parsed.repo,
                task_key=parsed.task_key,
                authority_key=parsed.authority_key,
                module=parsed.module,
                owner=parsed.owner,
                source_allocations=tuple(parsed.source_allocation),
                source_handoffs=tuple(parsed.source_handoff),
                source_snapshot_ids=tuple(parsed.source_snapshot),
            )
        elif parsed.command == "integration-claim":
            payload = claim_integration(
                parsed.repo,
                owner=parsed.owner,
                intent_id=parsed.intent_id,
                target=parsed.path,
                branch=parsed.branch,
                resolve_conflicts=parsed.resolve_conflicts,
            )
        elif parsed.command == "integration-resolve":
            payload = resolve_integration(
                parsed.repo, target=parsed.path,
                expected_record_version=parsed.expected_record_version, owner=parsed.owner,
                expected_head=parsed.expected_head, finalizer_record_id=parsed.finalizer_record_id,
            )
        elif parsed.command == "integration-refresh":
            payload = refresh_integration(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
            )
        elif parsed.command == "integration-validate":
            payload = validate_integration(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                validation_evidence_id=parsed.validation_evidence_id,
                tests_receipt_id=parsed.tests_receipt_id,
                snapshot_receipt_id=parsed.snapshot_receipt_id,
            )
        elif parsed.command == "integration-reopen":
            payload = reopen_integration(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
            )
        elif parsed.command == "lifecycle-events":
            payload = lifecycle_events(parsed.repo, consumer=parsed.consumer)
        elif parsed.command == "lifecycle-event-ack":
            payload = acknowledge_lifecycle_event(
                parsed.repo,
                consumer=parsed.consumer,
                event_id=parsed.event_id,
            )
        elif parsed.command == "integration-recovery-matrix":
            payload = integration_recovery_matrix(parsed.repo)
        elif parsed.command == "integration-equivalent":
            payload = record_equivalent_replay(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                source_allocation_id=parsed.source_allocation,
                source_handoff=parsed.source_handoff,
                replay_commit=parsed.replay_commit,
                evidence_id=parsed.evidence_id,
            )
        elif parsed.command == "close-delivered":
            payload = close_delivered(
                parsed.repo, target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner_release=parsed.owner_release, actor=parsed.actor,
                dry_run=parsed.dry_run, plan_id=parsed.plan_id,
            )
        elif parsed.command == "archive-retained":
            payload = archive_retained(
                parsed.repo, target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner_release=parsed.owner_release, actor=parsed.actor,
                dry_run=parsed.dry_run, plan_id=parsed.plan_id,
            )
        elif parsed.command in {"complete-no-change", "close-preserved"}:
            close = close_preserved if parsed.command == "close-preserved" else complete_no_change
            payload = close(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
            )
        elif parsed.command == "integration-abandon":
            payload = abandon_integration_intent(
                parsed.repo,
                intent_id=parsed.intent_id,
                owner=parsed.owner,
                reason=parsed.reason,
            )
        elif parsed.command == "integration-abort":
            payload = abort_integration(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                reason=parsed.reason,
            )
        elif parsed.command == "integration-retry":
            payload = retry_integration(
                parsed.repo, intent_id=parsed.intent_id, owner=parsed.owner,
                reason=parsed.reason, prerequisite_intent_id=parsed.prerequisite_intent,
                dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id,
            )
        elif parsed.command == "reconcile-session":
            payload = reconcile_session(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                trigger=parsed.trigger,
            )
        elif parsed.command == "writer-lease-acquire":
            payload = acquire_writer_lease(
                parsed.repo,
                target=parsed.path,
                owner=parsed.owner,
                client_id=parsed.client_id,
                holder_pid=parsed.holder_pid,
                session_id=parsed.session_id,
                run_id=parsed.run_id,
                ttl_seconds=parsed.ttl_seconds,
                expected_record_version=parsed.expected_record_version,
            )
        elif parsed.command == "writer-lease-release":
            payload = release_writer_lease(
                parsed.repo,
                target=parsed.path,
                holder_pid=parsed.holder_pid,
                lease_id=parsed.lease_id,
                holder=(
                    WriterHolder(
                        client_id=parsed.client_id,
                        pid=parsed.holder_pid,
                        boot_id=parsed.holder_boot_id,
                        start_ticks=parsed.holder_start_ticks,
                        session_id=parsed.session_id,
                        run_id=parsed.run_id,
                    )
                    if all(
                        value is not None
                        for value in (
                            parsed.holder_pid,
                            parsed.holder_boot_id,
                            parsed.holder_start_ticks,
                        )
                    )
                    else None
                ),
            )
        elif parsed.command == "reconcile-writers":
            payload = reconcile_writers(
                parsed.repo,
                actor=parsed.actor,
                trigger=parsed.trigger,
                dry_run=not parsed.apply,
                adopt_legacy=parsed.adopt_legacy,
                only=tuple(parsed.only),
            )
        elif parsed.command == "acquire-when-free":
            payload = acquire_when_free(
                parsed.repo,
                deadline_seconds=parsed.deadline_seconds,
                poll_seconds=parsed.poll_seconds,
                task_key=parsed.task_key,
                authority_key=parsed.authority_key,
                module=parsed.module,
                role=parsed.role,
                owner=parsed.owner,
                branch=parsed.branch,
                base_ref=parsed.base_ref,
                target=parsed.path,
                scope_paths=tuple(parsed.scope),
            )
        elif parsed.command == "acquire-across":
            payload = acquire_across([json.loads(item) for item in parsed.request])
        elif parsed.command == "park":
            payload = park(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                checkpoint_id=parsed.checkpoint_id,
                checkpoint_head=parsed.checkpoint_head,
                waiting_on=parsed.waiting_on,
            )
        elif parsed.command == "resume":
            payload = resume(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
            )
        elif parsed.command == "adopt-delivered":
            payload = adopt_delivered(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                new_owner=parsed.new_owner,
                actor=parsed.actor,
            )
        elif parsed.command == "review":
            payload = review(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                new_owner=parsed.new_owner,
                add_scope_paths=tuple(parsed.add_scope),
                expected_head=parsed.expected_head,
                scope_reason=parsed.scope_reason,
            )
        elif parsed.command == "handoff":
            payload = handoff(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                kind=parsed.kind,
                commit=parsed.commit,
                finalizer_record_id=parsed.finalizer_record_id,
                finalizer_commit=parsed.finalizer_commit,
                scope_paths=tuple(parsed.scope),
                mainline_commit=parsed.mainline_commit,
                scope_changed=parsed.scope_changed,
            )
        elif parsed.command == "quarantine":
            payload = quarantine(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                reason=parsed.reason,
                expected_commit=parsed.expected_commit,
            )
        elif parsed.command == "externalize":
            payload = externalize(
                parsed.repo,
                source_target=parsed.source_path,
                recovery_target=parsed.recovery_path,
                expected_source_record_version=parsed.expected_source_record_version,
                expected_recovery_record_version=parsed.expected_recovery_record_version,
                owner=parsed.owner,
            )
        elif parsed.command == "recovery-adopt":
            payload = adopt_recovery(
                parsed.repo,
                source_target=parsed.source_path,
                recovery_target=parsed.recovery_path,
                expected_source_record_version=parsed.expected_source_record_version,
                expected_recovery_record_version=parsed.expected_recovery_record_version,
                source_owner=parsed.source_owner,
                recovery_owner=parsed.recovery_owner,
                scope_paths=tuple(parsed.scope),
            )
        elif parsed.command == "build-resource-register":
            from .build_resources import register

            payload = register(parsed.repo, target=parsed.path, owner=parsed.owner,
                               expected_record_version=parsed.expected_record_version,
                               expected_head=parsed.expected_head, project=parsed.project,
                               recipe=parsed.recipe, paths=tuple(parsed.resource_path),
                               attest_rebuildable=parsed.attest_rebuildable,
                               dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id)
        elif parsed.command == "resource-register":
            payload = register_disposable(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                resource_path=parsed.resource_path,
                kind=parsed.kind,
                reproducible=parsed.reproducible,
                creator=parsed.creator,
                teardown_policy=parsed.teardown_policy,
            )
        elif parsed.command == "resource-clean":
            if parsed.typed:
                payload = cleanup_typed_disposable(
                    parsed.repo,
                    target=parsed.path,
                    expected_record_version=parsed.expected_record_version,
                    owner=parsed.owner,
                    dry_run=parsed.dry_run,
                    expected_plan_id=parsed.expected_plan_id,
                    preservation_receipt=parsed.preservation_receipt,
                    preservation_receipt_sha256=parsed.preservation_receipt_sha256,
                    additional_preservation_receipts=tuple(parsed.additional_preservation_receipt),
                    additional_preservation_receipt_sha256=tuple(
                        parsed.additional_preservation_receipt_sha256
                    ),
                )
            else:
                if (
                    parsed.dry_run
                    or parsed.expected_plan_id
                    or parsed.preservation_receipt
                    or parsed.preservation_receipt_sha256
                    or parsed.additional_preservation_receipt
                    or parsed.additional_preservation_receipt_sha256
                ):
                    raise ControllerError("INVALID_RESOURCE", "dry-run and plan id require --typed")
                payload = cleanup_disposable(
                    parsed.repo,
                    target=parsed.path,
                    expected_record_version=parsed.expected_record_version,
                    owner=parsed.owner,
                    resource_path=parsed.resource_path,
                )
        elif parsed.command == "resource-recover":
            from . import cleanup_recovery

            if parsed.stage == "rollout-check":
                if not parsed.dry_run:
                    raise ControllerError("INVALID_RECOVERY", "rollout check requires --dry-run")
                payload = cleanup_recovery.rollout_check(
                    parsed.repo,
                    receipt_path=parsed.preservation_receipt,
                    receipt_sha=parsed.preservation_receipt_sha256,
                )
            elif parsed.stage == "reconcile-typed":
                if not parsed.transaction_id or not parsed.transaction_sha256:
                    raise ControllerError("INVALID_RECOVERY", "exact typed transaction and digest required")
                from .cleanup_attempts import reconcile_typed

                payload = reconcile_typed(parsed.repo, parsed.transaction_id, parsed.transaction_sha256,
                    dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id)
            elif parsed.stage == "prepare-multiple":
                if not all([
                    parsed.path, parsed.owner, parsed.expected_record_version,
                    parsed.transaction_id, parsed.transaction_sha256,
                    parsed.cleanup_receipt_sha256,
                ]):
                    raise ControllerError(
                        "INVALID_RECOVERY", "exact original multiple cleanup bindings required"
                    )
                from . import multiple_cleanup_recovery

                payload = multiple_cleanup_recovery.prepare(
                    parsed.repo, target=parsed.path, owner=parsed.owner,
                    version=parsed.expected_record_version,
                    recovery=multiple_cleanup_recovery.context(
                        parsed.transaction_id, parsed.transaction_sha256,
                        parsed.cleanup_receipt_sha256,
                    ),
                    dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id,
                )
            elif parsed.stage == "prepare":
                if not all(
                    [
                        parsed.path,
                        parsed.owner,
                        parsed.expected_record_version,
                        parsed.transaction_id,
                        parsed.transaction_sha256,
                        parsed.cleanup_receipt_sha256,
                        parsed.preservation_receipt,
                        parsed.preservation_receipt_sha256,
                    ]
                ):
                    raise ControllerError(
                        "INVALID_RECOVERY", "exact original and new preservation bindings required"
                    )
                payload = cleanup_recovery.prepare(
                    parsed.repo,
                    target=parsed.path,
                    owner=parsed.owner,
                    version=parsed.expected_record_version,
                    recovery=cleanup_recovery.context(
                        parsed.transaction_id,
                        parsed.transaction_sha256,
                        parsed.cleanup_receipt_sha256,
                    ),
                    receipt_path=parsed.preservation_receipt,
                    receipt_sha=parsed.preservation_receipt_sha256,
                    dry_run=parsed.dry_run,
                    expected_plan_id=parsed.expected_plan_id,
                )
            else:
                if not parsed.recovery_receipt or not parsed.recovery_receipt_sha256:
                    raise ControllerError(
                        "INVALID_RECOVERY", "immutable recovery record and trusted digest required"
                    )
                recovery_options = dict(
                    recovery_path=parsed.recovery_receipt,
                    recovery_sha=parsed.recovery_receipt_sha256,
                    dry_run=parsed.dry_run,
                )
                if parsed.stage == "reconcile-attempt":
                    payload = cleanup_recovery.reconcile_attempt(
                        parsed.repo, **recovery_options, expected_plan_id=parsed.expected_plan_id
                    )
                else:
                    payload = cleanup_recovery.resume(parsed.repo, **recovery_options)
        elif parsed.command == "lease-acquire":
            payload = acquire_publication_lease(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                run_id=parsed.run_id,
                ttl_seconds=parsed.ttl_seconds,
            )
        elif parsed.command == "lease-complete":
            payload = complete_publication_lease(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                lease_id=parsed.lease_id,
                published_commit=parsed.published_commit,
                finalizer_record_id=parsed.finalizer_record_id,
                run_id=parsed.run_id,
                auto_release=parsed.auto_release,
            )
        elif parsed.command == "lease-abort":
            payload = abort_publication_lease(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                lease_id=parsed.lease_id,
                owner=parsed.owner,
                run_id=parsed.run_id,
            )
        elif parsed.command == "release":
            payload = release(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner_release=parsed.owner_release,
                teardown_reproducible_venv=parsed.teardown_reproducible_venv,
                dry_run=parsed.dry_run,
                release_intent_id=parsed.intent_id,
                publication_receipt_id=parsed.publication_receipt_id,
            )
        elif parsed.command == "gc-plan":
            payload = gc_plan(
                parsed.repo,
                target=parsed.path,
                owner_release=parsed.owner_release,
                release_intent_id=parsed.intent_id,
                namespace_pid=parsed.namespace_pid,
            )
        elif parsed.command == "gc-apply":
            payload = gc_apply(
                parsed.repo,
                target=parsed.path,
                expected_gc_plan_id=parsed.plan_id,
                owner_release=parsed.owner_release,
                release_intent_id=parsed.intent_id,
            )
        elif parsed.command == "release-intent":
            payload = create_release_intent(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                trigger=parsed.trigger,
                integration_intent_id=parsed.integration_intent_id,
            )
        elif parsed.command == "branch-retirement-plan":
            payload = plan_branch_retirement(
                parsed.repo,
                allocation_id=parsed.allocation_id,
                branch=parsed.branch,
                remote=parsed.remote,
                integrated_into=parsed.integrated_into,
                dry_run=parsed.dry_run,
            )
        elif parsed.command == "branch-retirement-status":
            payload = branch_retirement_status(parsed.repo, plan_id=parsed.plan_id)
        elif parsed.command == "branch-retirement-verify":
            payload = verify_branch_retirement(
                parsed.repo,
                plan_id=parsed.plan_id,
                operation=parsed.operation,
            )
        elif parsed.command == "branch-retirement-authorize":
            payload = authorize_branch_retirement(
                parsed.repo,
                plan_id=parsed.plan_id,
                operation=parsed.operation,
                request_id=parsed.request_id,
                holder=parsed.holder,
                client_id=parsed.client_id,
                session_id=parsed.session_id,
                run_id=parsed.run_id,
                kind=KIND_CAS_DELETE,
                ttl_seconds=parsed.ttl_seconds,
            )
        elif parsed.command == "branch-retirement-complete":
            payload = complete_branch_retirement(
                parsed.repo,
                authorization_id=parsed.authorization_id,
                token=parsed.token,
                result_path=parsed.result_file,
            )
        elif parsed.command == "branch-retirement-executions":
            payload = branch_retirement_executions(
                parsed.repo,
                action=parsed.action,
                authorization_id=parsed.authorization_id,
                token=parsed.token,
                reason=parsed.reason,
            )
        elif parsed.command == "branch-retirement-record":
            payload = record_branch_retirement(
                parsed.repo,
                plan_id=parsed.plan_id,
                operation=parsed.operation,
                summary_path=parsed.summary_file,
                result_path=parsed.result_file,
            )
        elif parsed.command == "namespace-migration-plan":
            payload = plan_namespace_migration(
                parsed.repo,
                legacy_namespace=parsed.legacy_namespace,
                policy_from=parsed.policy_from,
            )
        elif parsed.command == "namespace-migrate":
            payload = migrate_namespace(
                parsed.repo,
                legacy_namespace=parsed.legacy_namespace,
                policy_from=parsed.policy_from,
                actor=parsed.actor,
                expected_plan_id=parsed.expected_plan_id,
                dry_run=parsed.dry_run,
            )
        elif parsed.command == "namespace-migration-receipts":
            payload = namespace_migration_receipts(parsed.repo)
        elif parsed.command == "namespace-migration-verify":
            payload = verify_namespace(parsed.repo)
        elif parsed.command in {"recover-handoff", "recover-published"}:
            payload = recover_frozen_checkout(
                parsed.repo, target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                expected_head=parsed.expected_head, owner=parsed.owner,
                branch=getattr(parsed, "branch", None),
            )
        elif parsed.command == "reopen":
            payload = reopen(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                drifted_head=parsed.drifted_head,
            )
        elif parsed.command in {"binding-add", "binding-remove", "binding-transfer"}:
            payload = update_external_binding(
                parsed.repo,
                target=parsed.path,
                expected_record_version=parsed.expected_record_version,
                owner=parsed.owner,
                binding=parsed.binding,
                holder_pid=getattr(parsed, "holder_pid", None),
                action=parsed.command.removeprefix("binding-"),
                recover_archived_codex_session=getattr(
                    parsed, "recover_archived_codex_session", False),
                dry_run=getattr(parsed, "dry_run", False),
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "no-change-completion":
            from .delivered_closeout import reconcile_no_change_completion

            payload = reconcile_no_change_completion(
                parsed.repo, target=parsed.path, owner=parsed.owner,
                expected_record_version=parsed.expected_record_version,
                completion_id=parsed.completion_receipt_id,
                completion_sha256=parsed.completion_receipt_sha256,
                cleanup_ids=tuple(parsed.cleanup_receipt_id),
                cleanup_sha256=tuple(parsed.cleanup_receipt_sha256),
                dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id,
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "receipts":
            payload = reconcile_publication_receipts(
                parsed.repo,
                actor=parsed.actor,
                receipt_ids=tuple(parsed.receipt_id),
                dry_run=parsed.dry_run,
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "release-intents":
            payload = reconcile_release_intents(
                parsed.repo,
                actor=parsed.actor,
                trigger=parsed.trigger,
                intent_ids=tuple(parsed.intent_id),
                dry_run=parsed.dry_run,
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "release-plans":
            payload = reconcile_release_plans(
                parsed.repo, actor=parsed.actor, dry_run=parsed.dry_run
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "retention":
            from .convergence import converge_retention

            payload = converge_retention(
                parsed.repo,
                actor=parsed.actor,
                trigger=parsed.trigger,
                dry_run=not parsed.apply,
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "binding-registrations":
            from .registration_backfill import reconcile_binding_registrations

            payload = reconcile_binding_registrations(
                parsed.repo, dry_run=parsed.dry_run, expected_plan_id=parsed.expected_plan_id
            )
        elif parsed.command == "reconcile" and parsed.reconcile_command == "integration-intents":
            payload = reconcile_integration_intents(
                parsed.repo,
                actor=parsed.actor,
                intent_ids=tuple(parsed.intent_id),
                dry_run=parsed.dry_run,
                dispatch=parsed.dispatch,
            )
        elif parsed.command == "doctor":
            payload = doctor(parsed.repo, namespace_pid=parsed.namespace_pid)
        else:
            parser.error("unknown command")
    except (ControllerError, OSError) as exc:
        payload = envelope(parsed.command)
        code = exc.code if isinstance(exc, ControllerError) else "FILESYSTEM_ERROR"
        payload["errors"] = [{"code": code, "message": str(exc)}]
        payload["decision"] = {"code": "ERROR", "reason": str(exc)}
        if parsed.command == "canonical-refresh":
            from .canonical_refresh import CanonicalProcessOccupancyError

            if isinstance(exc, CanonicalProcessOccupancyError):
                payload["decision"]["process_diagnostics"] = exc.diagnostics
        if (
            parsed.command == "resource-recover" and parsed.stage == "rollout-check"
            and isinstance(exc, cleanup_recovery.RolloutTransactionError)
        ):
            payload["decision"]["transaction_diagnostics"] = [exc.transaction_diagnostic]
        if parsed.command == "writer-lease-acquire":
            payload["decision"]["admission"] = "REFUSED"
        if parsed.command == "session-bind" and isinstance(getattr(exc, "details", None), dict):
            payload["decision"]["lease_outcome"] = exc.details
        _print(payload, parsed.json)
        return 2
    _print(payload, parsed.json)
    if parsed.command == "task" and (
        payload.get("errors") or payload.get("decision", {}).get("operation_complete") is False
    ):
        return 2
    if (payload.get("decision") or {}).get("code") in {
        "RESOURCE_CLEANUP_BLOCKED",
        "RECOVERY_ATTEMPT_INCOMPLETE",
    }:
        return 1
    if parsed.command == "doctor" and payload.get("decision", {}).get("code") == "FAIL":
        return 1
    if (
        parsed.command == "write-guard"
        and payload.get("decision", {}).get("code") != "WRITE_ALLOWED"
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
