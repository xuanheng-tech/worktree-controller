"""Narrow Git inventory and mutation primitives."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from .models import ControllerError, RepositoryInventory, WorktreeGitState

REMOTE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# One Git command may never hold the repository lock indefinitely: a slow
# remote, an authentication prompt or a wedged helper would block every
# Controller caller and the systemd reconcile window alike.
GIT_COMMAND_TIMEOUT_SECONDS = 120

# Per-worktree observations are independent read-only Git processes. Running a
# bounded number at once keeps Controller latency flat as checkouts accumulate.
WORKTREE_OBSERVATION_WORKERS = 8

# Variables that select a repository, object store, index or ref namespace other
# than the one named by ``-C``. A caller inside a Git hook (or any process that
# exported them) would otherwise make every observation describe another
# repository while the Controller attributes it to the requested one.
REPOSITORY_SELECTION_ENVIRONMENT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_GRAFT_FILE",
        "GIT_REPLACE_REF_BASE",
    }
)


def git_environment() -> dict[str, str]:
    """The caller's environment without inherited repository selection."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in REPOSITORY_SELECTION_ENVIRONMENT
    }
    environment["LC_ALL"] = "C"
    return environment


def _git(
    repo: Path,
    *arguments: str,
    check: bool = True,
    readonly: bool = True,
    timeout: float = GIT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    environment = git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "/bin/false"
    environment["SSH_ASKPASS"] = "/bin/false"
    # Authority, integration and retirement evidence must share the stored DAG.
    # Repository-local replacement refs cannot redefine commit ancestry or trees.
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    if readonly:
        environment["GIT_OPTIONAL_LOCKS"] = "0"
    command = ["git"]
    if readonly:
        command.append("--no-optional-locks")
    command.extend(("-C", os.fspath(repo), *arguments))
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ControllerError(
            "GIT_TIMEOUT",
            f"git {arguments[0] if arguments else ''} exceeded {timeout}s and was terminated",
        ) from exc
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip() or "Git command failed"
        raise ControllerError("GIT_ERROR", message)
    return result


def _text(repo: Path, *arguments: str) -> str:
    return _git(repo, *arguments).stdout.decode("utf-8", "strict").strip()


def resolve_repository(path: Path) -> tuple[Path, Path]:
    requested = path.expanduser().resolve(strict=True)
    root = Path(_text(requested, "rev-parse", "--show-toplevel")).resolve(strict=True)
    common = Path(_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(
        strict=True
    )
    return root, common


def commit_tree_oid(repo: Path, commit: str) -> str:
    """Resolve the immutable tree identity for an already validated commit OID."""

    oid = _text(repo, "rev-parse", "--verify", f"{commit}^{{tree}}")
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise ControllerError("GIT_PARSE_ERROR", "Git returned an invalid tree OID")
    return oid


def _parse_porcelain(payload: bytes) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for raw in payload.split(b"\0"):
        if raw == b"":
            if current:
                records.append(current)
                current = {}
            continue
        token = os.fsdecode(raw)
        if token.startswith("worktree "):
            if current:
                records.append(current)
            current = {"path": token.removeprefix("worktree ")}
        elif token.startswith("HEAD "):
            current["head"] = token.removeprefix("HEAD ")
        elif token.startswith("branch "):
            current["branch"] = token.removeprefix("branch ")
        elif token == "detached":
            current["detached"] = True
        elif token.startswith("locked"):
            current["locked"] = True
            current["lock_reason"] = token.removeprefix("locked").strip()
        elif token.startswith("prunable"):
            current["prunable"] = True
            current["prunable_reason"] = token.removeprefix("prunable").strip()
        elif token == "bare":
            current["bare"] = True
        else:
            raise ControllerError("GIT_PARSE_ERROR", f"unknown worktree porcelain field: {token}")
    if current:
        records.append(current)
    if not records:
        raise ControllerError("GIT_PARSE_ERROR", "Git returned no registered worktrees")
    return records


def _linked_git_dir(common: Path, path: Path) -> Path:
    if path.exists():
        return Path(_text(path, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    admin_root = common / "worktrees"
    if admin_root.is_dir():
        expected = os.fspath(path / ".git")
        for candidate in sorted(admin_root.iterdir()):
            pointer = candidate / "gitdir"
            if not pointer.is_file():
                continue
            try:
                target = pointer.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if target == expected:
                return candidate.resolve(strict=True)
    raise ControllerError("GIT_DRIFT", f"cannot resolve linked worktree admin dir: {path}")


def worktree_key(common: Path, git_dir: Path, canonical: bool) -> str:
    identity = "canonical" if canonical else os.fspath(git_dir.relative_to(common))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"wt_{digest}"


def _status_counts(path: Path) -> tuple[bool, int, int, int]:
    payload = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    staged = 0
    modified = 0
    untracked = 0
    entries = iter(payload.split(b"\0"))
    for raw in entries:
        if not raw:
            continue
        if raw.startswith(b"??"):
            untracked += 1
            continue
        if len(raw) < 4 or raw[2:3] != b" ":
            raise ControllerError("GIT_PARSE_ERROR", "invalid status porcelain record")
        x = chr(raw[0])
        y = chr(raw[1])
        # Porcelain v1 -z emits destination NUL source NUL for renames/copies.
        # The source is a path, even when its bytes look like a status row.
        if (x in {"R", "C"} or y in {"R", "C"}) and not next(entries, b""):
            raise ControllerError("GIT_PARSE_ERROR", "missing rename/copy source path")
        if x not in {" ", "?", "!"}:
            staged += 1
        if y not in {" ", "?", "!"}:
            modified += 1
    return not payload, staged, modified, untracked


def _observe_each[T](function: Callable[[Path], T], paths: Sequence[Path]) -> list[Future[T]]:
    """Run independent per-worktree observations concurrently, in input order.

    Every observation completes before this returns. Callers consume the results in
    order, so `Future.result()` re-raises the same first failure a sequential loop would.
    """
    if not paths:
        return []
    with ThreadPoolExecutor(max_workers=min(WORKTREE_OBSERVATION_WORKERS, len(paths))) as pool:
        return [pool.submit(function, item) for item in paths]


def _registered_worktrees(requested_root: Path, common: Path) -> list[dict[str, str | bool]]:
    raw = _git(requested_root, "worktree", "list", "--porcelain", "-z").stdout
    parsed = _parse_porcelain(raw)
    # Git's main entry can name the common directory itself after
    # --separate-git-dir. Resolve the canonical checkout from Git's explicit
    # core.worktree mapping, or from the requested main checkout's Git identity.
    if Path(str(parsed[0]["path"])).resolve() == common and not parsed[0].get("bare"):
        configured = _git(requested_root, "config", "--path", "--get", "core.worktree", check=False)
        if configured.returncode == 0:
            canonical = Path(os.fsdecode(configured.stdout.rstrip(b"\n")))
            if not canonical.is_absolute():
                canonical = common / canonical
        elif Path(_text(requested_root, "rev-parse", "--absolute-git-dir")).resolve() == common:
            canonical = requested_root
        else:
            raise ControllerError(
                "GIT_DRIFT", "separate Git directory requires a core.worktree canonical mapping"
            )
        canonical = canonical.resolve(strict=True)
        if (
            Path(_text(canonical, "rev-parse", "--absolute-git-dir")).resolve() != common
            or Path(_text(canonical, "rev-parse", "--show-toplevel")).resolve() != canonical
        ):
            raise ControllerError("GIT_DRIFT", "separate Git directory canonical mapping differs")
        parsed[0]["path"] = os.fspath(canonical)
    return parsed


def canonical_checkout(path: Path) -> Path:
    """Resolve Git's canonical checkout without observing live worktree contents."""
    requested_root, common = resolve_repository(path)
    parsed = _registered_worktrees(requested_root, common)
    if parsed[0].get("bare") is True:
        raise ControllerError("GIT_ERROR", "bare worktrees are unsupported")
    return Path(str(parsed[0]["path"])).resolve(strict=True)


def inventory(path: Path) -> RepositoryInventory:
    requested_root, common = resolve_repository(path)
    parsed = _registered_worktrees(requested_root, common)
    canonical_path = Path(str(parsed[0]["path"])).resolve(strict=True)
    present = [
        record.get("bare") is not True and Path(str(record["path"])).exists() for record in parsed
    ]
    observed = iter(
        _observe_each(
            _status_counts,
            [
                Path(str(record["path"]))
                for record, exists in zip(parsed, present, strict=True)
                if exists
            ],
        )
    )
    worktrees: list[WorktreeGitState] = []
    for index, record in enumerate(parsed):
        if record.get("bare") is True:
            raise ControllerError("GIT_ERROR", "bare worktrees are unsupported")
        item_path = Path(str(record["path"]))
        canonical = index == 0
        git_dir = common if canonical else _linked_git_dir(common, item_path)
        prunable = bool(record.get("prunable", False))
        if present[index]:
            clean, staged, modified, untracked = next(observed).result()
        elif prunable:
            clean, staged, modified, untracked = False, 0, 0, 0
        else:
            raise ControllerError("GIT_DRIFT", f"registered worktree path is missing: {item_path}")
        branch_ref = record.get("branch")
        branch = None
        if isinstance(branch_ref, str):
            branch = branch_ref.removeprefix("refs/heads/")
        worktrees.append(
            WorktreeGitState(
                path=item_path,
                git_dir=git_dir,
                key=worktree_key(common, git_dir, canonical),
                canonical=canonical,
                head=str(record.get("head", "")),
                branch=branch,
                detached=bool(record.get("detached", False)),
                locked=bool(record.get("locked", False)),
                lock_reason=str(record.get("lock_reason") or "") or None,
                prunable=prunable,
                prunable_reason=str(record.get("prunable_reason") or "") or None,
                clean=clean,
                staged=staged,
                modified=modified,
                untracked=untracked,
            )
        )
    return RepositoryInventory(
        requested_path=requested_root,
        canonical_path=canonical_path,
        common_dir=common,
        worktrees=tuple(worktrees),
    )


def resolve_commit(repo: Path, value: str) -> str:
    if not value:
        raise ControllerError("INVALID_COMMIT", "commit value is empty")
    result = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    if result.returncode != 0:
        raise ControllerError("INVALID_COMMIT", f"commit is not resolvable: {value}")
    oid = result.stdout.decode("ascii", "strict").strip()
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise ControllerError("INVALID_COMMIT", "Git returned a non-SHA-1 commit identity")
    return oid


def validate_branch_name(repo: Path, branch: str) -> None:
    if not branch or branch.startswith("-"):
        raise ControllerError("INVALID_BRANCH", "branch name is empty or unsafe")
    result = _git(repo, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise ControllerError("INVALID_BRANCH", f"invalid branch name: {branch}")


def branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(
            repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    )


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    ancestor_oid = resolve_commit(repo, ancestor)
    descendant_oid = resolve_commit(repo, descendant)
    return (
        _git(
            repo,
            "merge-base",
            "--is-ancestor",
            ancestor_oid,
            descendant_oid,
            check=False,
        ).returncode
        == 0
    )


def changed_paths(repo: Path, base: str, head: str) -> tuple[str, ...]:
    base_oid = resolve_commit(repo, base)
    head_oid = resolve_commit(repo, head)
    payload = _git(
        repo, "--no-replace-objects", "diff", "--name-only", "-z", base_oid, head_oid
    ).stdout
    return tuple(sorted(os.fsdecode(item) for item in payload.split(b"\0") if item))


def trusted_publication_base(
    repo: Path, base: str, head: str, ref: str, *,
    verified_edges: set[tuple[Path, str, str]] | None = None,
) -> str:
    """Advance only through one unmodified DAG and an observed forward-only formal ref.

    A retained history suffix must include the frozen task base. History that no
    longer covers that base, replacement graphs and rewritten refs cannot supply
    attribution evidence. The frozen allocation base is never changed.
    """
    try:
        _root, common = resolve_repository(repo)
        if _text(repo, "rev-parse", "--is-shallow-repository") != "false":
            return base
        if (common / "info" / "grafts").exists():
            return base
        if _text(repo, "for-each-ref", "--format=%(refname)", "refs/replace/"):
            return base
        if os.environ.get("GIT_REPLACE_REF_BASE") or os.environ.get("GIT_SHALLOW_FILE"):
            return base
        if _git(repo, "symbolic-ref", "--quiet", ref, check=False).returncode != 1:
            return base
        publication = resolve_commit(repo, ref)
        history = (common / "logs" / ref).read_text(encoding="utf-8").splitlines()
        if not history:
            return base

        def ancestor(left: str, right: str) -> bool:
            return (
                _git(
                    repo,
                    "--no-replace-objects",
                    "merge-base",
                    "--is-ancestor",
                    left,
                    right,
                    check=False,
                ).returncode
                == 0
            )

        previous = None
        begins_at_creation = False
        observed_base = False
        for line in history:
            old, new = line.split(" ", 2)[:2]
            if any(
                len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid)
                for oid in (old, new)
            ):
                return base
            if previous is None:
                begins_at_creation = old == "0" * 40
            if previous is not None and old != previous:
                return base
            if old != "0" * 40:
                edge = (common, old, new)
                # Only positive immutable commit edges are shared within one doctor
                # observation. Ref/reflog and graph overrides are checked on every call.
                if verified_edges is None or edge not in verified_edges:
                    if not ancestor(old, new):
                        return base
                    if verified_edges is not None:
                        verified_edges.add(edge)
            observed_base = observed_base or base in (old, new)
            previous = new
        # Git may expire entries from before this task. Retain a positive formal
        # ref observation of its exact base instead of requiring the ref's birth.
        if not begins_at_creation and not observed_base:
            return base
        if previous != publication or not ancestor(base, publication) or not ancestor(base, head):
            return base
        result = _git(
            repo, "--no-replace-objects", "merge-base", "--all", head, publication, check=False
        )
        candidates = result.stdout.decode("ascii").splitlines()
        if result.returncode != 0 or len(candidates) != 1:
            return base
        candidate = candidates[0]
        if not ancestor(base, candidate) or not ancestor(candidate, head):
            return base
        # The formal ref may have moved during these read-only checks.
        if resolve_commit(repo, ref) != publication:
            return base
        return candidate
    except (ControllerError, OSError, ValueError):
        return base


def commits_with_parents(repo: Path, base: str, head: str) -> tuple[tuple[str, ...], ...]:
    base_oid = resolve_commit(repo, base)
    head_oid = resolve_commit(repo, head)
    payload = _text(repo, "rev-list", "--parents", f"{base_oid}..{head_oid}")
    if not payload:
        return ()
    commits: list[tuple[str, ...]] = []
    for line in payload.splitlines():
        fields = tuple(line.split())
        if not fields or any(
            len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid)
            for oid in fields
        ):
            raise ControllerError("GIT_PARSE_ERROR", "invalid commit topology")
        commits.append(fields)
    return tuple(commits)


def build_integration_candidate(
    repo: Path,
    base: str,
    source_handoffs: tuple[str, ...],
    *,
    task_key: str,
) -> str:
    """Construct a candidate from immutable commits without touching a checkout or ref."""

    candidate = resolve_commit(repo, base)
    if not source_handoffs:
        raise ControllerError("MISSING_INTEGRATION_SOURCE", "candidate requires frozen handoffs")
    for source_value in source_handoffs:
        source = resolve_commit(repo, source_value)
        if is_ancestor(repo, source, candidate):
            continue
        if is_ancestor(repo, candidate, source):
            candidate = source
            continue
        result = _git(
            repo,
            "merge-tree",
            "--write-tree",
            "--messages",
            candidate,
            source,
            check=False,
            readonly=False,
        )
        if result.returncode == 1:
            raise ControllerError(
                "INTEGRATION_CONFLICT",
                f"frozen handoff {source} conflicts with the selected main base",
            )
        if result.returncode != 0:
            raise ControllerError("GIT_ERROR", "git merge-tree failed for integration candidate")
        lines = result.stdout.decode("utf-8", "replace").splitlines()
        tree = lines[0].strip() if lines else ""
        if len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
            raise ControllerError("GIT_PARSE_ERROR", "git merge-tree returned an invalid tree")
        commit = _git(
            repo,
            "commit-tree",
            tree,
            "-p",
            candidate,
            "-p",
            source,
            "-m",
            f"Integrate frozen handoff for {task_key}",
            check=False,
            readonly=False,
        )
        if commit.returncode != 0:
            raise ControllerError("GIT_ERROR", "git commit-tree failed for integration candidate")
        candidate = commit.stdout.decode("ascii", "strict").strip()
        candidate = resolve_commit(repo, candidate)
    return candidate


def checkout_detached(repo: Path, commit: str) -> None:
    oid = resolve_commit(repo, commit)
    git_write(repo, ("checkout", "--detach", oid))


def update_branch_ref(repo: Path, branch: str, *, expected: str, new: str) -> None:
    validate_branch_name(repo, branch)
    expected_oid = resolve_commit(repo, expected)
    new_oid = resolve_commit(repo, new)
    git_write(repo, ("update-ref", f"refs/heads/{branch}", new_oid, expected_oid))


def checkout_branch(repo: Path, branch: str) -> None:
    validate_branch_name(repo, branch)
    git_write(repo, ("checkout", branch))


def fetch_remote_branch(repo: Path, remote: str, branch: str) -> str:
    if REMOTE_NAME_PATTERN.fullmatch(remote) is None:
        raise ControllerError("INVALID_REMOTE", f"remote name is unsafe: {remote}")
    validate_branch_name(repo, branch)
    if _git(repo, "remote", "get-url", remote, check=False).returncode != 0:
        raise ControllerError("INVALID_REMOTE", f"remote is not configured: {remote}")
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    result = _git(
        repo,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        remote,
        f"+refs/heads/{branch}:{tracking_ref}",
        check=False,
        readonly=False,
    )
    if result.returncode != 0:
        raise ControllerError(
            "PUBLICATION_FETCH_FAILED", "configured publication branch fetch failed"
        )
    return resolve_commit(repo, tracking_ref)


def path_is_ignored(repo: Path, relative_path: str) -> bool:
    return _git(repo, "check-ignore", "--quiet", "--", relative_path, check=False).returncode == 0


def tracked_paths(repo: Path, relative_path: str) -> tuple[str, ...]:
    payload = _git(repo, "ls-files", "-z", "--", relative_path).stdout
    return tuple(os.fsdecode(item) for item in payload.split(b"\0") if item)


def head_paths(repo: Path, relative_path: str) -> tuple[str, ...]:
    """Include committed paths even when their deletion has already been staged."""
    payload = _git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", relative_path).stdout
    return tuple(os.fsdecode(item) for item in payload.split(b"\0") if item)


def ignored_paths(path: Path) -> tuple[str, ...]:
    payload = _git(
        path,
        "status",
        "--porcelain=v1",
        "-z",
        "--ignored=matching",
        "--untracked-files=all",
    ).stdout
    ignored: list[str] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        if raw.startswith(b"!! "):
            ignored.append(os.fsdecode(raw[3:]))
    return tuple(sorted(ignored))


def git_state_fingerprint(repository: RepositoryInventory) -> str:
    digest = hashlib.sha256()
    digest.update(
        _git(
            repository.canonical_path,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)",
        ).stdout
    )
    digest.update(_git(repository.canonical_path, "worktree", "list", "--porcelain", "-z").stdout)
    present = [item.path.exists() for item in repository.worktrees]
    observed = iter(
        _observe_each(
            _index_and_status,
            [
                item.path
                for item, exists in zip(repository.worktrees, present, strict=True)
                if exists
            ],
        )
    )
    for item, exists in zip(repository.worktrees, present, strict=True):
        digest.update(os.fsencode(item.path))
        digest.update(item.head.encode("ascii"))
        digest.update((item.branch or "DETACHED").encode("utf-8"))
        if not exists:
            continue
        index_bytes, status = next(observed).result()
        digest.update(index_bytes)
        digest.update(status)
    return digest.hexdigest()


def _index_and_status(path: Path) -> tuple[bytes, bytes]:
    index_path = Path(_text(path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    index_bytes = index_path.read_bytes()
    status = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    return index_bytes, status


def public_refs(repo: Path) -> dict[str, str]:
    payload = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads",
        "refs/tags",
        "refs/remotes",
    ).stdout
    refs: dict[str, str] = {}
    for raw in payload.splitlines():
        if not raw:
            continue
        try:
            raw_name, raw_oid = raw.split(b"\0", 1)
            name, oid = os.fsdecode(raw_name), raw_oid.decode("ascii", "strict")
        except (UnicodeError, ValueError) as exc:
            raise ControllerError("GIT_PARSE_ERROR", "invalid ref inventory") from exc
        if not name or len(oid) != 40 or any(char not in "0123456789abcdef" for char in oid):
            raise ControllerError("GIT_PARSE_ERROR", "invalid ref inventory")
        refs[name] = oid
    return refs


def worktree_content_fingerprint(
    repository: RepositoryInventory, *, exclude_keys: frozenset[str] = frozenset()
) -> str:
    digest = hashlib.sha256()
    for item in repository.worktrees:
        if item.key in exclude_keys:
            continue
        digest.update(item.key.encode("ascii"))
        digest.update(os.fsencode(item.path))
        digest.update(item.head.encode("ascii"))
        digest.update((item.branch or "DETACHED").encode("utf-8"))
        digest.update(b"1" if item.locked else b"0")
        digest.update(b"1" if item.prunable else b"0")
        if not item.path.exists():
            continue
        index_path = Path(
            _text(item.path, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        digest.update(index_path.read_bytes())
        digest.update(
            _git(item.path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        )
    return digest.hexdigest()


def git_write(repo: Path, arguments: Iterable[str]) -> subprocess.CompletedProcess[bytes]:
    return _git(repo, *tuple(arguments), readonly=False)
