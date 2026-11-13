"""Fast-forward canonical without treating passive root cwd as content occupancy."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical_transition import _check_target_policy, _published_policy
from .controller import envelope
from .git_backend import fetch_remote_branch, git_read, git_write, inventory, is_ancestor
from .models import ControllerError
from .policy import load_policy
from .semantics import record_for as _record_for
from .store import MetadataStore
from .transaction_scope import conflicts
from .writer_lease import ProcessPathScan, own_ancestry

CANONICAL_REFRESH_VERSION = 1
_PROC_ROOT = Path("/proc")
_PROCESS_LIMIT = 5
_PATH_LIMIT = 3
_PATH_LENGTH = 512


def _process_metadata(pid: int) -> dict[str, Any]:
    """Best-effort kernel identity, separate from the scan that already blocked refresh."""
    unknown = {"name": "unknown", "parent_pid": None, "metadata_status": "unknown"}
    try:
        with (_PROC_ROOT / str(pid) / "stat").open(encoding="utf-8", errors="replace") as stream:
            raw = stream.read(4097)
        prefix, separator, suffix = raw.rpartition(")")
        name = prefix.partition("(")[2]
        if len(raw) > 4096 or not separator or not name:
            return unknown
        parent = int(suffix.split()[1])
        if parent < 0:
            return unknown
        return {"name": name[:64], "parent_pid": parent, "metadata_status": "observed"}
    except (OSError, ValueError, IndexError):
        return unknown


class CanonicalProcessOccupancyError(ControllerError):
    """The ordinary refusal with bounded advisory metadata and a guarded retry hint."""

    def __init__(self, target: Path, used: list[tuple[int, list[str]]]) -> None:
        shown = sorted(used)[:_PROCESS_LIMIT]
        super().__init__(
            "CANONICAL_IN_USE",
            "live processes use the canonical checkout: "
            + ", ".join(str(pid) for pid, _paths in shown),
        )
        processes = []
        for pid, paths in shown:
            metadata = _process_metadata(pid)
            parent = metadata["parent_pid"]
            parent_name = _process_metadata(parent)["name"] if parent is not None else "unknown"
            unique_paths = list(dict.fromkeys(paths))
            processes.append({
                "pid": pid,
                **metadata,
                "parent_name": parent_name,
                "matched_paths": [path[:_PATH_LENGTH] for path in unique_paths[:_PATH_LIMIT]],
                "paths_truncated": len(unique_paths) > _PATH_LIMIT
                or any(len(path) > _PATH_LENGTH for path in unique_paths[:_PATH_LIMIT]),
            })
        self.diagnostics = {
            "process_count": len(used),
            "processes": processes,
            "processes_omitted": len(used) - len(shown),
            "recovery_steps": [
                "Verify process ownership and the exact owning task/session; this metadata is advisory.",
                "For tools owned by a shared daemon, use its native session inventory to check active work. "
                "End only the exact completed task-owned session through its normal lifecycle; "
                "closing the current CLI may not release those tools.",
                "After references clear, run retry_argv. If CANONICAL_REFRESH_READY is returned, "
                "repeat without --dry-run. Unknown metadata keeps the original refusal.",
            ],
            "retry_argv": ["worktree-controller", "canonical-refresh", "--repo", str(target),
                           "--dry-run", "--json"],
        }


def _published_paths(root: Path, before: str, target: str,
                     paths: tuple[str, ...]) -> tuple[list[dict[str, str]], str]:
    """Only explicitly selected tracked bytes already published may enter the index.

    No checkout/reset/stash or content conversion is used. An interrupted add may
    leave the same published blobs staged; the identical request can be retried.
    """
    if not paths or len(set(paths)) != len(paths):
        raise ControllerError("CANONICAL_DIRTY", "unique published paths are required")
    changed = set(git_read(root, ("diff", "HEAD", "--name-only", "-z")).stdout.split(b"\0")) - {b""}
    changed |= set(git_read(root, ("diff", "--cached", "HEAD", "--name-only", "-z")).stdout.split(b"\0")) - {b""}
    untracked = git_read(root, ("ls-files", "--others", "--exclude-standard", "-z")).stdout
    if untracked or changed - {p.encode() for p in paths}:
        raise ControllerError("CANONICAL_DIRTY", "unselected or untracked canonical changes remain")
    proofs = []
    for name in sorted(paths):
        relative = PurePosixPath(name)
        if relative.is_absolute() or relative.as_posix() != name or any(p in {"", ".", "..", ".git"} for p in relative.parts):
            raise ControllerError("CANONICAL_DIRTY", "invalid published path")
        path = root / name
        if path.resolve() != path or not path.is_file() or path.is_symlink():
            raise ControllerError("CANONICAL_DIRTY", "published path must be a regular unaliased file")
        entries = []
        for revision in ("HEAD", target):
            entry = git_read(root, ("ls-tree", "-z", revision, "--", name)).stdout
            if not entry.endswith(b"\0") or entry.count(b"\0") != 1:
                raise ControllerError("CANONICAL_DIRTY", "published path must exist in both commits")
            metadata, actual_name = entry[:-1].split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if actual_name != name.encode() or mode not in {"100644", "100755"} or kind != "blob":
                raise ControllerError("CANONICAL_DIRTY", "published path is not a regular tracked blob")
            entries.append((mode, oid))
        mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        oid = git_read(root, ("hash-object", "--no-filters", "--", name)).stdout.decode().strip()
        index = git_read(root, ("ls-files", "--stage", "-z", "--", name)).stdout
        permitted = {f"{m} {o} 0\t{name}\0".encode() for m, o in (entries[0], (mode, oid))}
        if index not in permitted:
            raise ControllerError("CANONICAL_DIRTY", "index contains independent or unmerged changes")
        # Filters could stage bytes different from those reviewed above.
        attributes = git_read(root, ("check-attr", "-z", "filter", "working-tree-encoding", "--", name)).stdout.split(b"\0")
        if any(value not in {b"unspecified", b"unset"} for value in attributes[2::3]):
            raise ControllerError("CANONICAL_DIRTY", "published reconciliation refuses content filters")
        proofs.append({"path": name, "mode": mode, "blob": oid})
    expected = b"".join(f"{p['mode']} blob {p['blob']}\t{p['path']}\0".encode() for p in proofs)
    revisions = git_read(root, ("rev-list", "--first-parent", f"{before}..{target}", "--", *paths)).stdout.decode().splitlines()
    for baseline in dict.fromkeys((target, *revisions, before)):
        actual = git_read(root, ("ls-tree", "-z", baseline, "--", *sorted(paths))).stdout
        if actual == expected and is_ancestor(root, before, baseline):
            return proofs, baseline
    raise ControllerError("CANONICAL_DIRTY", "local bytes and modes have no matching published main baseline")


def refresh_canonical(repository_path: Path, *, dry_run: bool = False,
                      reconcile_published_paths: tuple[str, ...] = (),
                      expected_target: str | None = None) -> dict[str, Any]:
    """Move canonical main forward to the fetched publication branch, or say why not.

    Integrations publish from candidates, so the canonical checkout falls behind
    published main. This only ever fast-forwards a clean checkout that is ACTIVE,
    on the publication branch, holds no writer lease or binding and has no live
    content references. Root cwd and shared Git metadata references survive a
    fast-forward; unlike worktree removal, refresh does not delete those nodes.
    A frozen canonical keeps the exact ``canonical-transition`` flow.
    """

    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        policy = load_policy(repository.canonical_path)
        if not policy.present or not policy.canonical_protected or policy.policy_schema_version < 2:
            raise ControllerError("INVALID_POLICY", "canonical refresh requires a governed publication branch")
        item = next((worktree for worktree in repository.worktrees if worktree.canonical), None)
        record = _record_for(item, snapshot) if item is not None else None
        if item is None or record is None:
            raise ControllerError("UNMANAGED_CANONICAL", "canonical checkout has no Controller record")
        if record.lifecycle != "ACTIVE":
            raise ControllerError(
                "CANONICAL_NOT_ACTIVE",
                f"canonical lifecycle {record.lifecycle}; a frozen canonical uses canonical-transition",
            )
        if item.detached or item.branch != policy.publication_branch:
            raise ControllerError("CANONICAL_BRANCH", "canonical is not on the publication branch")
        if (not item.clean and not reconcile_published_paths) or item.locked or item.prunable:
            raise ControllerError("CANONICAL_DIRTY", "canonical checkout is not clean")
        if record.allocation_id in snapshot.writer_leases or record.external_bindings:
            raise ControllerError("CANONICAL_IN_USE", "canonical holds a writer lease or binding")
        if conflicts(store, allocation=record.allocation_id, target=item.path, assume_serialized=True):
            raise ControllerError("INTERRUPTED_TRANSACTION", "canonical refresh conflicts with a transaction")
        ancestry = own_ancestry()
        observed = [entry for entry in ProcessPathScan().referenced_paths_in_use(item.path)
                    if entry[0] not in ancestry]
        used = []
        for pid, paths in observed:
            content_paths = [path for path in paths if Path(path) != item.path
                             and not Path(path).is_relative_to(repository.common_dir)]
            if content_paths:
                used.append((pid, content_paths))
        if used:
            raise CanonicalProcessOccupancyError(item.path, used)
        before = item.head
        target_oid = fetch_remote_branch(item.path, policy.publication_remote, policy.publication_branch)
        if reconcile_published_paths and expected_target != target_oid:
            raise ControllerError("PUBLISHED_TARGET_CHANGED", "published path reconciliation requires the exact fetched target")
        if not is_ancestor(item.path, before, target_oid):
            raise ControllerError("NON_FAST_FORWARD", "canonical main is not an ancestor of published main")
        proofs, baseline = (_published_paths(item.path, before, target_oid, reconcile_published_paths)
                            if reconcile_published_paths else ([], target_oid))
        decision: dict[str, Any] = {
            "path": str(item.path),
            "branch": item.branch,
            "before": before,
            "target": target_oid,
            "dry_run": dry_run,
            "reconciled_published_paths": proofs,
            "reconciliation_baseline": baseline,
            "non_content_process_count": len(observed) - len(used),
        }
        if before == target_oid:
            decision.update(code="CANONICAL_CURRENT", reason="canonical already equals published main")
        elif dry_run:
            decision.update(code="CANONICAL_REFRESH_READY", reason="canonical can fast-forward")
        else:
            # A published main that this Controller could not govern is refused first.
            _check_target_policy(_published_policy(repository, target_oid, policy),
                                 policy.publication_branch, policy.publication_remote)
            if proofs:
                git_write(item.path, ("add", "--", *reconcile_published_paths))
                for proof in proofs:
                    if git_read(item.path, ("ls-files", "--stage", "-z", "--", proof["path"])).stdout != (
                        f"{proof['mode']} {proof['blob']} 0\t{proof['path']}\0".encode()
                    ):
                        raise ControllerError("CANONICAL_DIRTY", "staged bytes differ; preserved index requires inspection")
                if baseline != target_oid and baseline != before:
                    git_write(item.path, ("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", baseline))
            git_write(item.path, ("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", target_oid))
            after = next(worktree for worktree in inventory(item.path).worktrees if worktree.canonical)
            if after.head != target_oid or not after.clean or after.branch != policy.publication_branch:
                raise ControllerError("REFRESH_INCOMPLETE", "canonical did not reach exact published main")
            decision.update(code="CANONICAL_REFRESHED", reason="canonical fast-forwarded to published main")
    payload = envelope("canonical-refresh")
    payload["decision"] = decision
    return payload
