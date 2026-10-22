"""Small task-oriented entrypoints over the existing Controller authorities.

No task database, inferred owner, writer entitlement or new lifecycle is introduced.
Mutations resolve one live allocation and pass its identity into the locked verb.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .controller import envelope, status
from .git_backend import resolve_commit
from .lifecycle import acquire, complete_no_change, handoff, park, release, resume
from .lifecycle_history import lifecycle_history
from .models import ControllerError
from .policy import load_policy
from .recovery_guidance import recovery_guide

CONTRACT = "task-workflow/v1"
ACTIONS = ("list", "show", "start", "resume", "finish", "recover")


def add_parser(commands: Any, common: argparse.ArgumentParser) -> None:
    task = commands.add_parser("task", help="start, inspect, resume and settle a named task")
    actions = task.add_subparsers(dest="task_action", required=True)
    for action in ACTIONS:
        parser = actions.add_parser(action, parents=[common])
        if action == "list":
            parser.add_argument("--owner", help="show only this recorded owner")
            continue
        if action == "start":
            parser.add_argument("--task-key", required=True)
            parser.add_argument("--owner", required=True)
            parser.add_argument("--scope", action="append", required=True)
            parser.add_argument("--authority-key", help="defaults to the explicit task key")
            parser.add_argument("--module", default="task")
            parser.add_argument("--branch", help="defaults to task/<task-key>")
            parser.add_argument("--base-ref", help="defaults to policy publication remote/branch")
            parser.add_argument("--path", type=Path)
            continue
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--task-key")
        selector.add_argument("--path", type=Path)
        if action in {"show", "recover"}:
            selector.add_argument("--intent-id", help="original integration identity, also after release")
        else:
            parser.add_argument("--owner", required=True)
        if action == "finish":
            parser.add_argument("--outcome", choices=("no-change", "checkpoint", "handoff"), required=True)
            parser.add_argument("--checkpoint-id")
            parser.add_argument("--checkpoint-head", help="HEAD bound by the existing Task checkpoint")
            parser.add_argument("--waiting-on")
            parser.add_argument("--finalizer-record-id")
            parser.add_argument("--commit", help="exact commit bound by the Finalizer record")


def _row(observed: dict[str, Any], *, task_key: str | None, target: Path | None) -> dict[str, Any]:
    if (task_key is None) == (target is None):
        raise ControllerError("INVALID_SELECTOR", "select exactly one task key or checkout path")
    resolved = str(target.expanduser().resolve(strict=False)) if target is not None else None
    matches = [row for row in observed["worktrees"]
               if (row["task_key"] == task_key if task_key is not None else row["path"] == resolved)]
    if not matches:
        raise ControllerError("UNKNOWN_TASK", "no live allocation matches; use task show for retained history")
    if len(matches) != 1:
        raise ControllerError("AMBIGUOUS_TASK", "multiple live allocations share this task key; select --path explicitly")
    row = matches[0]
    if row["metadata_missing"]:
        raise ControllerError("MISSING_METADATA", "checkout has no recorded task authority")
    return row


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in (
        "allocation_id", "task_key", "authority_key", "owner", "path", "branch", "head",
        "record_version", "lifecycle", "role", "scope_paths", "wait_checkpoint",
    )}


def _response(observed: dict[str, Any], action: str, code: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload = envelope("task")
    payload["repository"] = observed["repository"]
    payload["warnings"] = observed["warnings"]
    payload["decision"] = {
        "code": code, "contract": CONTRACT, "action": action, "reason": reason,
        "business_task_completed": False, **extra,
    }
    return payload


def inspect_task(repository_path: Path, *, task_key: str | None = None,
                 target: Path | None = None, intent_id: str | None = None) -> dict[str, Any]:
    """Keep original recovery commands/evidence; never turn observation into success."""
    if sum(value is not None for value in (task_key, target, intent_id)) != 1:
        raise ControllerError("INVALID_SELECTOR", "select exactly one task, path or original intent")
    observed = status(repository_path)
    if intent_id is not None:
        guide = recovery_guide(repository_path, intent_id=intent_id)
    else:
        try:
            row = _row(observed, task_key=task_key, target=target)
        except ControllerError as error:
            if error.code != "UNKNOWN_TASK" or task_key is None:
                raise
            history = lifecycle_history(repository_path, task_key=task_key)
            decision = history["decision"]
            if not (decision["closed_releases"] or decision["incomplete_history"]):
                raise error
            return _response(observed, "show", "TASK_HISTORY", "No live checkout; inspect the original release evidence",
                             mutation_performed=False, history=decision)
        guide = recovery_guide(repository_path, target=Path(row["path"]))
    return _response(observed, "show", "TASK_GUIDANCE", guide["decision"]["reason"],
                     mutation_performed=False, guidance=guide["decision"])


def start_task(repository_path: Path, *, task_key: str, owner: str, scope_paths: tuple[str, ...],
               authority_key: str | None = None, module: str = "task", branch: str | None = None,
               base_ref: str | None = None, target: Path | None = None) -> dict[str, Any]:
    if not scope_paths or any(not value.strip() for value in scope_paths):
        raise ControllerError("MISSING_TASK_SCOPE", "declare the task's repository-relative file scope")
    observed = status(repository_path)
    if any(row["task_key"] == task_key for row in observed["worktrees"]):
        raise ControllerError("TASK_ALREADY_EXISTS", "inspect or resume the existing task; no second checkout was requested")
    policy = load_policy(Path(observed["repository"]["canonical_path"]))
    base = f"{policy.publication_remote}/{policy.publication_branch}" if base_ref is None else base_ref
    if base_ref is None:
        try:
            resolve_commit(Path(observed["repository"]["canonical_path"]), base)
        except ControllerError as error:
            if error.code != "INVALID_COMMIT":
                raise
            raise ControllerError("TASK_BASE_UNAVAILABLE", f"policy base {base} is unavailable locally; "
                                  "fetch through your Git workflow or select --base-ref explicitly") from error
    operation = acquire(repository_path, task_key=task_key,
                        authority_key=task_key if authority_key is None else authority_key,
                        module=module, role="feature", owner=owner,
                        branch=f"task/{task_key}" if branch is None else branch,
                        base_ref=base,
                        target=target, scope_paths=scope_paths)
    complete = operation["decision"]["code"] == "ACQUIRED"
    return _response(operation, "start", "TASK_STARTED" if complete else "TASK_WAITING",
                     "Checkout admitted; obtain a live writer lease before editing" if complete else
                     "Admission did not create a checkout; keep the original request and inspect its decision",
                     operation_complete=complete, operation=operation["decision"],
                     writer_entitlement_granted=False)


def _owned(repository_path: Path, task_key: str | None, target: Path | None,
           owner: str) -> tuple[dict[str, Any], dict[str, Any]]:
    observed = status(repository_path)
    row = _row(observed, task_key=task_key, target=target)
    if row["owner"] != owner:
        raise ControllerError("OWNER_MISMATCH", "task owner does not match the recorded owner")
    if row["canonical"] or row["role"] not in {"feature", "audit"}:
        raise ControllerError("INVALID_ROLE", "this entry settles only feature/audit tasks; inspect recovery guidance for other roles")
    return observed, row


def resume_task(repository_path: Path, *, owner: str, task_key: str | None = None,
                target: Path | None = None) -> dict[str, Any]:
    _observed, row = _owned(repository_path, task_key, target, owner)
    operation = resume(repository_path, target=Path(row["path"]), owner=owner,
                       expected_record_version=row["record_version"],
                       expected_allocation_id=row["allocation_id"], require_no_writer_lease=True)
    return _response(operation, "resume", "TASK_RESUMED", "Original checkout resumed; obtain a live writer lease before editing",
                     task=_identity(_row(operation, task_key=None, target=Path(row["path"]))),
                     operation_complete=True, operation=operation["decision"],
                     writer_entitlement_granted=False)


def finish_task(repository_path: Path, *, owner: str, outcome: str, task_key: str | None = None,
                target: Path | None = None, checkpoint_id: str | None = None,
                checkpoint_head: str | None = None, waiting_on: str | None = None,
                finalizer_record_id: str | None = None, commit: str | None = None) -> dict[str, Any]:
    if outcome not in {"no-change", "checkpoint", "handoff"}:
        raise ControllerError("INVALID_OUTCOME", "select no-change, checkpoint or handoff explicitly")
    if (outcome != "checkpoint" and any(value is not None for value in (checkpoint_id, checkpoint_head, waiting_on))
            or outcome != "handoff" and any(value is not None for value in (finalizer_record_id, commit))):
        raise ControllerError("INVALID_OUTCOME", "evidence options must belong to the selected outcome")
    if outcome == "checkpoint" and (not checkpoint_id or not checkpoint_head):
        raise ControllerError("INVALID_CHECKPOINT", "save the existing Task checkpoint first and provide its ID and bound HEAD")
    if outcome == "handoff" and (not finalizer_record_id or not commit):
        raise ControllerError("MISSING_FINALIZER_RECORD", "provide the actual Finalizer record and its exact commit")
    observed, row = _owned(repository_path, task_key, target, owner)
    arguments = dict(target=Path(row["path"]), expected_record_version=row["record_version"],
                     expected_allocation_id=row["allocation_id"], require_no_writer_lease=True)
    if outcome == "checkpoint":
        operation = park(repository_path, owner=owner, checkpoint_id=checkpoint_id,
                         checkpoint_head=checkpoint_head, waiting_on=waiting_on, **arguments)
        return _response(operation, "finish", "TASK_CHECKPOINT_RETAINED",
                         "Checkpoint retained with its original checkout and branch; work remains unfinished",
                         operation_complete=True, operation=operation["decision"],
                         task=_identity(_row(operation, task_key=None, target=Path(row["path"]))), checkout_released=False)
    if outcome == "handoff":
        operation = handoff(repository_path, kind="integration-ready", commit=commit,
                            finalizer_commit=commit, finalizer_record_id=finalizer_record_id,
                            expected_owner=owner, **arguments)
        return _response(operation, "finish", "TASK_HANDED_OFF",
                         "Exact commit handed off; integration and publication are still separate steps",
                         operation_complete=True, operation=operation["decision"],
                         task=_identity(_row(operation, task_key=None, target=Path(row["path"]))), checkout_released=False)
    operations = []
    if row["lifecycle"] != "NO_CHANGE_COMPLETED":
        operation = complete_no_change(repository_path, owner=owner, **arguments)
        operations.append(operation["decision"])
        row = {**row, "record_version": operation["decision"]["record_version"],
               "lifecycle": "NO_CHANGE_COMPLETED"}
    try:
        operation = release(repository_path, target=Path(row["path"]), owner_release=owner,
                            expected_record_version=row["record_version"],
                            expected_allocation_id=row["allocation_id"])
    except (ControllerError, OSError) as error:
        # Completion and release have distinct durable boundaries. Preserve and
        # report the completed prefix; a cleanup refusal must never read as PASS.
        code = getattr(error, "code", "FILESYSTEM_ERROR")
        next_step = {
            "message": "Inspect release guards; preserve unknown content before cleanup",
            "argv": ["worktree-controller", "release", "--repo",
                     observed["repository"]["canonical_path"], "--path", row["path"],
                     "--owner-release", owner, "--expected-record-version",
                     str(row["record_version"]), "--dry-run", "--json"],
        }
        if code in {"IDENTITY_MISMATCH", "TASK_CHECKOUT_MISSING", "CAS_MISMATCH", "BINDING_IDENTITY_DRIFT"}:
            next_step = {
                "message": "Inspect the original allocation; do not act on a replacement checkout",
                "argv": ["worktree-controller", "lifecycle-history", "--repo",
                         observed["repository"]["canonical_path"], "--allocation-id",
                         row["allocation_id"], "--json"],
            }
        payload = _response(observed, "finish", "TASK_RELEASE_BLOCKED",
                            "No-change completion recorded; checkout release still needs owner action",
                            operation_complete=False, operations=operations, task=_identity(row),
                            checkout_released=False, next_steps=[next_step])
        payload["errors"] = [{"code": code, "message": str(error)}]
        return payload
    operations.append(operation["decision"])
    return _response(operation, "finish", "TASK_CHECKOUT_RELEASED", "No-change completion recorded and checkout released; branch retained",
                     operation_complete=True, operations=operations, task=_identity(row),
                     checkout_released=True)


def dispatch(parsed: argparse.Namespace) -> dict[str, Any]:
    action = parsed.task_action
    if action == "list":
        observed = status(parsed.repo)
        rows = [row for row in observed["worktrees"] if not row["canonical"]
                and (parsed.owner is None or row["owner"] == parsed.owner)]
        return _response(observed, action, "TASK_LIST", "Live checkouts only; clean or idle does not mean completed",
                         mutation_performed=False, tasks=[{
                             **{key: row[key] for key in ("task_key", "owner", "path", "lifecycle", "clean", "metadata_missing")},
                             "writer": (row.get("lifecycle_observation") or {}).get("lease", {}).get("status", "UNKNOWN"),
                             "waiting_on": (row.get("wait_checkpoint") or {}).get("waiting_on"),
                         } for row in rows])
    if action == "start":
        return start_task(parsed.repo, task_key=parsed.task_key, owner=parsed.owner,
                          scope_paths=tuple(parsed.scope), authority_key=parsed.authority_key,
                          module=parsed.module, branch=parsed.branch, base_ref=parsed.base_ref, target=parsed.path)
    selector = {"task_key": parsed.task_key, "target": parsed.path}
    if action in {"show", "recover"}:
        result = inspect_task(parsed.repo, **selector, intent_id=parsed.intent_id)
        result["decision"]["action"] = action
        return result
    if action == "resume":
        return resume_task(parsed.repo, owner=parsed.owner, **selector)
    return finish_task(parsed.repo, owner=parsed.owner, outcome=parsed.outcome, **selector,
                       checkpoint_id=parsed.checkpoint_id, checkpoint_head=parsed.checkpoint_head,
                       waiting_on=parsed.waiting_on, finalizer_record_id=parsed.finalizer_record_id, commit=parsed.commit)
