"""Human-readable output; the JSON contract is rendered by the CLI."""

from __future__ import annotations

import shlex
from typing import Any


def render(payload: dict[str, Any]) -> str:
    command = payload.get("command")
    repository = payload.get("repository") or {}
    lines: list[str] = []
    if repository:
        lines.append(f"Repository: {repository.get('canonical_path', 'unknown')}")
        if "worktree_count" in repository:
            lines.append(
                "Budget: "
                f"{repository['worktree_count']}/{repository.get('normal_limit', '?')} normal, "
                f"hard limit {repository.get('hard_limit', '?')}"
            )
        if "integration_prepare_limit" in repository:
            lines.append(
                "Governance: "
                f"integration preparation {repository['integration_prepare_limit']}, "
                f"main publish {repository.get('main_publish_limit', '?')}, "
                f"recovery reserve {repository.get('recovery_reserve', '?')}"
            )
        lease = repository.get("publication_lease")
        if isinstance(lease, dict):
            lines.append(
                "Publication lease: "
                f"{lease.get('lease_id')} | {lease.get('allocation_id')} | "
                f"{str(lease.get('candidate_head', ''))[:12]}"
            )
    decision = payload.get("decision")
    if command != "status" and isinstance(decision, dict):
        lines.append(f"Decision: {decision.get('code', 'UNKNOWN')}")
        if decision.get("primary_diagnosis"):
            lines.append(f"Primary diagnosis: {decision['primary_diagnosis']}")
        if decision.get("reason"):
            lines.append(f"Reason: {decision['reason']}")
        if command == "recovery-guide":
            for blocker in decision.get("blockers", []):
                lines.append(f"Blocker: {blocker}")
            for number, step in enumerate(decision.get("next_steps", []), 1):
                lines.append(f"{number}. {step['message']}")
                if step.get("argv"):
                    lines.append(f"   {shlex.join(step['argv'])}")
            lines.append("Read-only guidance; recheck identity and guards after every step.")
        if command == "canonical-refresh" and (diagnostics := decision.get("process_diagnostics")):
            lines.append(f"Process occupancy (advisory): {diagnostics['process_count']} processes")
            for row in diagnostics["processes"]:
                parent = row["parent_pid"] if row["parent_pid"] is not None else "unknown"
                lines.append(
                    f"  pid={row['pid']} name={row['name']!r} parent_pid={parent} "
                    f"parent_name={row['parent_name']!r} metadata={row['metadata_status']}"
                )
                for path in row["matched_paths"]:
                    lines.append(f"    matched_path={path!r}")
                if row["paths_truncated"]:
                    lines.append("    matched paths truncated")
            if diagnostics["processes_omitted"]:
                lines.append(f"  Processes omitted: {diagnostics['processes_omitted']}")
            lines.extend(f"Recovery: {step}" for step in diagnostics["recovery_steps"])
            lines.append(f"Retry after clearance: {shlex.join(diagnostics['retry_argv'])}")
        if decision.get("gc_plan_id"):
            lines.append(f"GC plan: {decision['gc_plan_id']}")
        cleanup_plan = decision.get("plan") or {}
        if cleanup_plan.get("contract") in {
            "typed-disposable-resource/v1",
            "legacy-preserved-state-cleanup/v1",
        }:
            lines.append(f"Cleanup plan: {cleanup_plan['plan_id']}")
            for row in cleanup_plan["resources"]:
                lines.append(
                    f"  resource_type={row['resource_type']} path={row['path']} "
                    f"classification={row['classification']} cleanup_action={row['cleanup_action']} "
                    f"blocker={row['blocker'] or 'none'}"
                )
            for blocker in cleanup_plan["blockers"]:
                lines.append(f"  Blocker: {blocker['code']}: {blocker['message']}")
            for warning in cleanup_plan.get("warnings", []):
                lines.append(
                    f"  Warning: {warning['code']} pids={warning['pids']}: {warning['message']}"
                )
    if payload.get("worktrees"):
        lines.append("Worktrees:")
        for item in payload["worktrees"]:
            branch = item.get("branch") or "detached"
            cleanliness = "clean" if item.get("clean") else "dirty"
            metadata = (
                f"{item.get('task_key')}/{item.get('role')}/{item.get('owner')}"
                if not item.get("metadata_missing")
                else "metadata-missing"
            )
            flags = []
            if item.get("locked"):
                flags.append("locked")
            if item.get("prunable"):
                flags.append("prunable")
            suffix = f" [{' '.join(flags)}]" if flags else ""
            lines.append(
                f"  {item.get('kind')}: {item.get('path')} | {branch} | "
                f"{str(item.get('head', ''))[:12]} | {cleanliness} | {metadata}{suffix}"
            )
            if item.get("lifecycle"):
                lines.append(
                    f"    lifecycle={item['lifecycle']} authority={item.get('authority_key')}"
                )
            if observation := item.get("lifecycle_observation"):
                session = observation["session"]
                lease = observation["lease"]
                lines.append(
                    f"    session={session['session_id'] or 'unknown'} "
                    f"lease={lease['status']} holder={observation['status']['holder']} "
                    f"review={observation['status']['review']}"
                )
    if (
        command == "lifecycle-history"
        and isinstance(decision, dict)
        and decision.get("code") == "LIFECYCLE_HISTORY"
    ):
        for row in decision["live_allocations"]:
            lines.append(
                f"Live: {row['allocation_id']} | {row['authority_key']} | "
                f"{row['owner']} | {row['lifecycle']}"
            )
        for row in decision["closed_releases"]:
            lines.append(
                f"Closed: {row['release_receipt_id']} | {row['allocation_id']} | "
                f"{row['authority_key']} | {row['owner']} | {row['release_status']} | "
                f"{row['branch']} | {row['worktree_path']}"
            )
        for row in decision["incomplete_history"]:
            lines.append(f"Incomplete: {row['allocation_id']} | {row['reason']}")
        if decision["next_after"]:
            lines.append(f"Next after: {decision['next_after']}")
    if command == "gc-plan":
        lines.append("GC candidates:")
        for row in payload.get("candidates", []):
            lease = row["lease"]
            lease_ids = [entry["lease_id"] for entry in lease.values() if entry is not None]
            lines.append(
                f"  {row['allocation_id'] or 'unmanaged'} | {row['path']} | "
                f"{row['branch'] or 'detached'} | {row['lifecycle'] or 'unknown'} | "
                f"owner={row['owner'] or 'unknown'} dirty={row['dirty']} "
                f"session={row['session']['session_id'] or 'unknown'} "
                f"lease={','.join(lease_ids) or 'none'} | "
                f"{row['classification']} reason={row['candidate_reason']} | "
                f"blocking={','.join(row['blocking_facts']) or 'none'} | "
                f"filesystem={row['filesystem_health']['code']} "
                f"namespace={row['namespace_health']['comparison']}"
            )
            if row["stale_facts"] or row["orphan_facts"]:
                lines.append(
                    f"    stale={','.join(row['stale_facts']) or 'none'} "
                    f"orphan={','.join(row['orphan_facts']) or 'none'}"
                )
    if command in {"doctor", "status", "gc-plan"} and (mounts := payload.get("mount_diagnostics")):
        current = mounts["current"]
        lines.append(
            "Mounts: "
            f"repo={current['repo']} .git={current['git_entry']} "
            f"common={current['git_common_dir']} lock={current['lock_mount']}/"
            f"{current['lock_writable']} parent={current['lock_parent_mount']} "
            f"namespace={mounts['namespace']['comparison']} "
            f"diagnosis={mounts['environment']['code']}"
        )
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  {warning.get('code')}: {warning.get('message')}")
    errors = payload.get("errors") or []
    if errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  {error.get('code')}: {error.get('message')}")
    return "\n".join(lines)
