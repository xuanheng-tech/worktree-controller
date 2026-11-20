"""Frontier capabilities: non-mutating conflict checks and client hook adapters."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .controller import envelope
from .git_backend import (
    inventory,
    resolve_commit,
    resolve_repository,
    run_git,
    validate_branch_name,
)
from .lifecycle import acquire, complete_no_change, reconcile_session, release
from .models import ControllerError
from .policy import load_policy
from .semantics import managed_record as _managed_record
from .session import end_session
from .store import MetadataStore
from .writer_session import check as writer_check
from .writer_session import host_check as writer_host_check
from .writer_session import host_managed_requested

_MAX_HOOK_STDIN_BYTES = 1_048_576
_SUPPORTED_HOOK_CLIENTS = frozenset({"claude", "codex", "gemini"})


def _parse_merge_tree_conflicts(stdout: bytes) -> tuple[str, list[str]]:
    header, _, _ = stdout.partition(b"\0\0")
    tokens = [token for token in header.split(b"\0") if token]
    if not tokens:
        return "", []
    tree_oid = tokens[0].decode("ascii", "strict")
    conflicts = sorted(
        {os.fsdecode(token.split(b"\t", 1)[1]) for token in tokens[1:] if b"\t" in token}
    )
    return tree_oid, conflicts


def conflict_check(
    repository_path: Path,
    *,
    branches: tuple[str, ...] = (),
    targets: tuple[Path, ...] = (),
    base: str | None = None,
) -> dict[str, Any]:
    """Check cross-branch or cross-worktree merge conflicts without mutating checkouts."""
    repository = inventory(repository_path)
    canonical = repository.canonical_path
    resolved: list[dict[str, str]] = []
    for raw_target in targets:
        expanded = raw_target.expanduser()
        normalized = expanded.parent.resolve(strict=False) / expanded.name
        matched = next(
            (item for item in repository.worktrees if item.path.resolve(strict=False) == normalized),
            None,
        )
        if matched is None:
            raise ControllerError("INVALID_TARGET", f"worktree target is not registered: {raw_target}")
        if not matched.head:
            raise ControllerError("UNBORN_WORKTREE", f"worktree has no HEAD commit: {raw_target}")
        resolved.append({"label": matched.branch or str(matched.path), "oid": matched.head})
    for branch in branches:
        if not branch or branch.startswith("-"):
            raise ControllerError("INVALID_BRANCH", f"branch reference is invalid: {branch}")
        oid = resolve_commit(canonical, branch)
        resolved.append({"label": branch, "oid": oid})
    base_oid: str | None = None
    if base is not None:
        if not base or base.startswith("-"):
            raise ControllerError("INVALID_COMMIT", f"base revision is invalid: {base}")
        base_oid = resolve_commit(canonical, base)
        if len(resolved) == 1:
            resolved.insert(0, {"label": base, "oid": base_oid})
    if len(resolved) < 2:
        raise ControllerError(
            "INVALID_REQUEST",
            "conflict-check requires at least two branches/worktrees (or one target plus --base)",
        )
    pair_results: list[dict[str, Any]] = []
    any_conflict = False
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            args: list[str] = ["merge-tree", "--write-tree", "-z"]
            if base_oid is not None:
                args.append(f"--merge-base={base_oid}")
            args.extend(["--", left["oid"], right["oid"]])
            proc = run_git(canonical, *args, check=False)
            if proc.returncode == 0:
                tree_oid = proc.stdout.split(b"\0", 1)[0].decode("ascii", "strict")
                pair_results.append(
                    {
                        "left": left["label"],
                        "right": right["label"],
                        "clean": True,
                        "tree_oid": tree_oid,
                        "conflicting_paths": [],
                    }
                )
            elif proc.returncode == 1:
                tree_oid, conflicts = _parse_merge_tree_conflicts(proc.stdout)
                any_conflict = True
                pair_results.append(
                    {
                        "left": left["label"],
                        "right": right["label"],
                        "clean": False,
                        "tree_oid": tree_oid,
                        "conflicting_paths": conflicts,
                    }
                )
            else:
                stderr = proc.stderr.decode("utf-8", "replace").strip()
                raise ControllerError("GIT_ERROR", f"git merge-tree failed: {stderr}")
    payload = envelope("conflict-check")
    payload["repository"] = {"canonical_path": str(canonical)}
    payload["decision"] = {
        "code": "CONFLICTS_DETECTED" if any_conflict else "NO_CONFLICTS",
        "conflicts_detected": any_conflict,
        "base": base_oid,
        "targets": resolved,
        "pairs": pair_results,
    }
    return payload


def _read_hook_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_MAX_HOOK_STDIN_BYTES + 1)
    if len(raw) > _MAX_HOOK_STDIN_BYTES:
        raise ControllerError("INVALID_HOOK_EVENT", "hook event payload exceeds maximum size")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("INVALID_HOOK_EVENT", f"invalid hook JSON payload: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ControllerError("INVALID_HOOK_EVENT", "hook event payload must be a JSON object")
    return parsed


def _repo_from_event(repository_path: Path | None, event: dict[str, Any]) -> Path:
    if repository_path is not None:
        return repository_path
    for key in ("repo", "repository_path", "cwd"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value)
    return Path.cwd()


def hook_adapter(
    *,
    client: str,
    repository_path: Path | None = None,
    payload_data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Normalize Claude, Codex, and Gemini hook payloads onto governed Controller verbs."""
    if client not in _SUPPORTED_HOOK_CLIENTS:
        raise ControllerError("INVALID_CLIENT", f"unsupported hook client: {client}")
    event = _read_hook_stdin() if payload_data is None else payload_data
    event_name = str(
        event.get("hook_event_name") or event.get("event") or event.get("hook") or ""
    ).strip()
    if not event_name:
        raise ControllerError("INVALID_HOOK_EVENT", "hook event name is required")

    if event_name == "WorktreeCreate":
        repo_hint = _repo_from_event(repository_path, event)
        canonical, _ = resolve_repository(repo_hint)
        name = str(event.get("name") or event.get("branch") or "").strip()
        if not name:
            raise ControllerError("INVALID_HOOK_EVENT", "WorktreeCreate requires name or branch")
        branch = str(event.get("branch") or name).strip()
        validate_branch_name(canonical, branch)
        base_ref = str(event.get("base_ref") or "HEAD").strip()
        owner = str(event.get("owner") or event.get("session_id") or client).strip()
        slug = name.replace("/", "-")
        target = (
            Path(event["worktree_path"]).expanduser()
            if isinstance(event.get("worktree_path"), str) and event["worktree_path"].strip()
            else canonical.parent / f"{canonical.name}-worktrees" / slug
        )
        task_key = str(event.get("task_key") or f"{client}-{slug}").strip()
        authority_key = str(event.get("authority_key") or f"branch:{branch}").strip()
        module = str(event.get("module") or "repository").strip()
        role = str(event.get("role") or "feature").strip()
        result = acquire(
            canonical,
            target=target,
            task_key=task_key,
            authority_key=authority_key,
            module=module,
            role=role,
            owner=owner,
            base_ref=base_ref,
            branch=branch,
            scope_paths=tuple(event.get("scope_paths") or ()),
        )
        created_path = result["decision"]["path"]
        out = {
            **result,
            "command": "hook-adapter",
            "client": client,
            "hook_event_name": event_name,
            "worktree_path": created_path,
        }
        if client == "claude":
            out["hookSpecificOutput"] = {
                "hookEventName": "WorktreeCreate",
                "worktreePath": created_path,
            }
        return out, 0

    if event_name == "WorktreeRemove":
        raw_target = event.get("worktree_path") or event.get("path")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ControllerError("INVALID_HOOK_EVENT", "WorktreeRemove requires worktree_path")
        target = Path(raw_target).expanduser()
        repo_hint = repository_path or resolve_repository(target)[0]
        repository = inventory(repo_hint)
        policy = load_policy(repository.canonical_path)
        snapshot = MetadataStore(repository.common_dir).load(include_history=False)
        item, record = _managed_record(repository, snapshot, target)
        owner = str(event.get("owner") or record.owner).strip()
        expected_version = record.record_version
        if (
            record.lifecycle == "ACTIVE"
            and policy.retained_slots >= 1
            and item.clean
            and record.base_commit is not None
            and not record.handoff_commit
            and item.head == resolve_commit(item.path, record.base_commit)
        ):
            completed = complete_no_change(
                repository.canonical_path,
                target=target,
                expected_record_version=expected_version,
                owner=owner,
            )
            expected_version = int(completed["decision"]["record_version"])
        result = release(
            repository.canonical_path,
            target=target,
            expected_record_version=expected_version,
            owner_release=owner,
        )
        return {
            **result,
            "command": "hook-adapter",
            "client": client,
            "hook_event_name": event_name,
        }, 0

    if event_name in {"PreToolUse", "write_guard", "Write", "Edit", "MultiEdit"}:
        tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        cwd_raw = event.get("cwd")
        raw_file = (
            event.get("file_path")
            or tool_input.get("file_path")
            or tool_input.get("path")
            or event.get("path")
        )
        check_target = (
            Path(str(raw_file)).expanduser()
            if isinstance(raw_file, str) and raw_file.strip()
            else Path(str(cwd_raw)).expanduser()
            if isinstance(cwd_raw, str) and cwd_raw.strip()
            else (repository_path or Path.cwd())
        )
        probe_path = (
            Path(str(cwd_raw)).expanduser()
            if isinstance(cwd_raw, str) and cwd_raw.strip()
            else check_target
            if check_target.is_dir()
            else check_target.parent
        )
        if host_managed_requested():
            decision = writer_host_check(
                probe_path,
                access="write" if raw_file else None,
                target=check_target if raw_file else None,
            )
        else:
            decision = writer_check(
                probe_path,
                client_id=str(event.get("client_id") or client),
                selector=str(event.get("holder") or event.get("holder_pid") or os.getppid()),
                session_id=event.get("session_id"),
                run_id=event.get("run_id"),
                access="write" if raw_file else None,
                target=check_target if raw_file else None,
            )
        allowed = decision.get("admission") in {"GRANTED", "NOT_APPLICABLE"}
        out: dict[str, Any] = {
            **envelope("hook-adapter"),
            "client": client,
            "hook_event_name": event_name,
            "allowed": allowed,
            "decision": decision,
        }
        if client == "claude":
            out["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                **(
                    {}
                    if allowed
                    else {
                        "permissionDecisionReason": str(
                            decision.get("reason") or decision.get("code") or "write blocked"
                        )
                    }
                ),
            }
        return out, 0 if allowed else 2

    if event_name in {"SessionEnd", "Stop", "task-terminal"}:
        repo_hint = _repo_from_event(repository_path, event)
        raw_session_id = event.get("session_id")
        if isinstance(raw_session_id, str) and raw_session_id.strip():
            result = end_session(
                repo_hint,
                trigger="task-terminal",
                client_id=str(event.get("client_id") or client),
                holder_selector=str(event.get("holder") or event.get("holder_pid") or os.getppid()),
                session_id=raw_session_id.strip(),
                run_id=event.get("run_id"),
            )
        else:
            result = reconcile_session(repo_hint, trigger="task-terminal")
        return {
            **result,
            "command": "hook-adapter",
            "client": client,
            "hook_event_name": event_name,
        }, 0

    raise ControllerError("INVALID_HOOK_EVENT", f"unsupported hook event: {event_name}")
