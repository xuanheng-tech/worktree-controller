"""Formal publication lineage regressions; all Git graphs are disposable fixtures."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import _record, _version
from tests.test_shadow import GitRepository, git
from tests.test_writer_lease import POLICY_V6
from worktree_controller.controller import doctor
from worktree_controller.git_backend import changed_paths, inventory, is_ancestor
from worktree_controller.lifecycle import handoff
from worktree_controller.models import ControllerError
from worktree_controller.policy import load_policy
from worktree_controller.semantics import attribution_base, path_scope_issues, publication_ref

FORMAL_REF = "refs/remotes/origin/main"


def _commit(path, filename, content, message="fixture work"):
    target = path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-m", message)
    return git(path, "rev-parse", "HEAD")


def _publish(repository, oid):
    # Simulate the configured remote-tracking update, including Git's reflog lineage.
    git(repository.repo, "update-ref", FORMAL_REF, oid)


def _task(repository, name="mine", scopes=("mine.txt",), base_ref="HEAD"):
    path, payload = acquire_one(
        repository,
        task=name,
        authority=name,
        owner="review-owner",
        scope_paths=scopes,
        base_ref=base_ref,
    )
    allocation = payload["decision"]["allocation_id"]
    return path, allocation, _record(repository, allocation)


def _preserve(repository, path, allocation, head):
    return handoff(
        repository.repo,
        target=path,
        expected_record_version=_version(repository, allocation),
        kind="preserved",
        commit=head,
        finalizer_commit=head,
        finalizer_record_id=f"synthetic-test-only:{head}",
    )


@pytest.mark.parametrize(
    "case",
    [
        "ordinary",
        "main_advanced",
        "sync_main",
        "partial_published",
        "publication_ahead",
        "missing",
        "rewritten_behind",
        "unrelated",
        "octopus",
    ],
)
def test_dag_endpoint_only_advances_along_frozen_base_to_head(case):
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        root = git(repository.repo, "rev-parse", "HEAD")
        base = _commit(repository.repo, "seed.txt", "seed")
        path, _, record = _task(repository, scopes=("mine.txt", "extra.txt"))
        publication = base
        if case == "partial_published":
            publication = _commit(path, "done.txt", "published portion")
        head = _commit(path, "mine.txt", "unpublished task")
        expected = {"mine.txt"}
        if case in {"main_advanced", "sync_main", "octopus"}:
            publication = _commit(repository.repo, "upstream.txt", "unrelated publication")
        if case == "sync_main":
            git(path, "merge", "--no-edit", publication)
            head = _commit(path, "mine.txt", "continued work")
        if case == "octopus":
            extra, _, _ = _task(repository, "extra", scopes=("extra.txt",), base_ref=base)
            extra_head = _commit(extra, "extra.txt", "other unpublished task")
            git(path, "merge", "--no-edit", publication, extra_head)
            head = git(path, "rev-parse", "HEAD")
            assert len(git(path, "show", "-s", "--format=%P", head).split()) == 3
            expected.add("extra.txt")
        if case == "publication_ahead":
            git(repository.repo, "merge", "--ff-only", head)
            publication = _commit(repository.repo, "after.txt", "later publication")
            expected = set()
        if case == "rewritten_behind":
            publication = root
        if case == "unrelated":
            tree = git(repository.repo, "rev-parse", f"{root}^{{tree}}")
            publication = git(repository.repo, "commit-tree", tree, "-m", "unrelated root")
        if case != "missing":
            _publish(repository, publication)
        ref = publication_ref(path, load_policy(repository.repo))
        endpoint = attribution_base(path, base, head, ref)
        assert is_ancestor(path, base, endpoint)
        assert is_ancestor(path, endpoint, head)
        assert set(changed_paths(path, endpoint, head)) == expected
        if case in {"ordinary", "main_advanced", "missing", "rewritten_behind", "unrelated"}:
            assert endpoint == base
        if case == "publication_ahead":
            assert endpoint == head
        assert record.base_commit == base


def test_criss_cross_multiple_best_bases_keep_frozen_endpoint():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, _, record = _task(repository)
        left = _commit(repository.repo, "published-left.txt", "left")
        right = _commit(path, "published-right.txt", "right")
        git(repository.repo, "merge", "--no-edit", right)
        publication = git(repository.repo, "rev-parse", "HEAD")
        git(path, "merge", "--no-edit", left)
        head = _commit(path, "mine.txt", "task")
        assert len(git(path, "merge-base", "--all", head, publication).splitlines()) == 2
        _publish(repository, publication)
        endpoint = attribution_base(path, record.base_commit, head, FORMAL_REF)
        assert endpoint == record.base_commit
        assert set(changed_paths(path, endpoint, head)) == {
            "published-left.txt",
            "published-right.txt",
            "mine.txt",
        }
        assert (
            path_scope_issues(path, record, head, publication_ref=FORMAL_REF)[0]["code"]
            == "PATH_SCOPE_VIOLATION"
        )


def test_local_unpublished_main_never_exempts_undeclared_uv_lock():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, allocation, record = _task(
            repository, "quant-console-workspace-member", scopes=("apps/quant_console",)
        )
        head = _commit(path, "uv.lock", "genuine undeclared dependency change")
        git(repository.repo, "merge", "--ff-only", head)
        assert publication_ref(path, load_policy(repository.repo)) is None
        for untrusted in (None, "main", "refs/heads/main", head, "refs/remotes/unconfigured/main"):
            issues = path_scope_issues(path, record, head, publication_ref=untrusted)
            assert issues[0]["code"] == "PATH_SCOPE_VIOLATION"
            assert issues[0]["path"] == "uv.lock"
        with pytest.raises(ControllerError) as error:
            _preserve(repository, path, allocation, head)
        assert error.value.code == "PATH_SCOPE_VIOLATION"


def test_validation_runtime_contract_accepts_only_published_background_changes():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, allocation, record = _task(
            repository, "validation-runtime-contract", scopes=("validation",)
        )
        publication = _commit(repository.repo, "uv.lock", "published lockfile from another task")
        _commit(path, "validation/runtime.py", "contract = True\n")
        git(path, "merge", "--no-edit", publication)
        head = git(path, "rev-parse", "HEAD")
        assert path_scope_issues(path, record, head)[0]["path"] == "uv.lock"
        _publish(repository, publication)
        assert attribution_base(path, record.base_commit, head, FORMAL_REF) == publication
        assert not path_scope_issues(path, record, head, publication_ref=FORMAL_REF)
        assert _preserve(repository, path, allocation, head)["decision"]["code"] == "PRESERVED"
        assert not [
            warning
            for warning in doctor(repository.repo)["warnings"]
            if warning.get("allocation_id") == allocation
            and warning["code"] == "PATH_SCOPE_VIOLATION"
        ]


@pytest.mark.parametrize(
    "corruption",
    [
        "rewrite",
        "graft",
        "replace",
        "shallow",
        "missing_log",
        "truncated_log",
        "symbolic",
        "head_rewrite",
    ],
)
@pytest.mark.parametrize("share_edges", [False, True])
def test_untrusted_history_cannot_advance_attribution(corruption, share_edges):
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, _, record = _task(repository)
        base = record.base_commit
        publication = _commit(repository.repo, "outside.txt", "published")
        _publish(repository, publication)
        _commit(path, "mine.txt", "task")
        git(path, "merge", "--no-edit", publication)
        head = git(path, "rev-parse", "HEAD")
        edges = set() if share_edges else None
        assert attribution_base(path, base, head, FORMAL_REF, verified_edges=edges) == publication
        common = inventory(path).common_dir
        if corruption == "rewrite":
            _publish(repository, base)
            _publish(repository, publication)
        elif corruption == "graft":
            (common / "info/grafts").write_text(f"{head}\n")
        elif corruption == "replace":
            git(path, "replace", head, base)
        elif corruption == "shallow":
            (common / "shallow").write_text(f"{base}\n")
        elif corruption in {"missing_log", "truncated_log"}:
            (common / "logs" / FORMAL_REF).write_text(
                "" if corruption == "missing_log" else f"{publication} {publication} test\n"
            )
        elif corruption == "symbolic":
            git(path, "symbolic-ref", FORMAL_REF, "refs/heads/main")
        else:
            head = git(
                path,
                "commit-tree",
                git(path, "rev-parse", f"{head}^{{tree}}"),
                "-m",
                "rewritten feature root",
            )
        assert attribution_base(path, base, head, FORMAL_REF, verified_edges=edges) == base
        assert "outside.txt" in changed_paths(path, base, head)


def test_doctor_reuses_publication_edges_only_within_one_observation(monkeypatch):
    from worktree_controller import git_backend

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        tasks = [_task(repository, name=name) for name in ("first", "second")]
        _publish(repository, tasks[0][2].base_commit)
        first = _commit(repository.repo, "outside.txt", "first publication")
        _publish(repository, first)
        second = _commit(repository.repo, "outside.txt", "second publication")
        _publish(repository, second)
        for path, allocation, _ in tasks:
            _commit(path, "mine.txt", "task")
            git(path, "merge", "--no-edit", second)
            _preserve(repository, path, allocation, git(path, "rev-parse", "HEAD"))

        calls = []
        original = git_backend._git

        def observed(repo, *args, **kwargs):
            if args == ("--no-replace-objects", "merge-base", "--is-ancestor", first, second):
                calls.append(args)
            return original(repo, *args, **kwargs)

        monkeypatch.setattr(git_backend, "_git", observed)
        for expected in (1, 2):
            result = doctor(repository.repo)
            assert not any(w["code"] == "PATH_SCOPE_VIOLATION" for w in result["warnings"])
            assert len(calls) == expected


def test_rewritten_base_and_undeclared_octopus_parent_are_not_forgiven():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, _, record = _task(repository)
        publication = _commit(repository.repo, "outside.txt", "published")
        _publish(repository, publication)
        extra, _, _ = _task(repository, "extra", scopes=("uv.lock",), base_ref=record.base_commit)
        extra_head = _commit(extra, "uv.lock", "unpublished parent")
        _commit(path, "mine.txt", "task")
        git(path, "merge", "--no-edit", publication, extra_head)
        head = git(path, "rev-parse", "HEAD")
        issues = path_scope_issues(path, record, head, publication_ref=FORMAL_REF)
        assert issues[0]["path"] == "uv.lock"
        alien = git(path, "commit-tree", git(path, "rev-parse", f"{head}^{{tree}}"), "-m", "alien")
        assert attribution_base(path, alien, head, FORMAL_REF) == alien
        # A frozen base is never rewritten in allocation metadata by attribution.
        assert replace(record, base_commit=alien).base_commit == alien


@pytest.mark.parametrize("anchor_as", ["old", "new"])
def test_retained_publication_history_covering_task_base_allows_scoped_handoff(anchor_as):
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        earlier = _commit(repository.repo, "earlier.txt", "earlier publication")
        _publish(repository, earlier)
        base = _commit(repository.repo, "seed.txt", "task baseline")
        _publish(repository, base)
        path, allocation, record = _task(repository)
        publication = _commit(repository.repo, "outside.txt", "another owner's publication")
        _publish(repository, publication)
        _commit(path, "mine.txt", "task")
        git(path, "merge", "--no-edit", publication)
        head = git(path, "rev-parse", "HEAD")
        log = inventory(path).common_dir / "logs" / FORMAL_REF
        entries = log.read_text().splitlines()
        position = 0 if anchor_as == "old" else 1
        start = next(i for i, line in enumerate(entries) if line.split()[position] == base)
        # Disposable fixture only: simulate expiry of pre-task reflog entries.
        retained = "\n".join(entries[start:]) + "\n"
        log.write_text(retained)
        assert retained.split()[0] != "0" * 40

        assert attribution_base(path, base, head, FORMAL_REF) == publication
        assert not path_scope_issues(path, record, head, publication_ref=FORMAL_REF)
        assert _preserve(repository, path, allocation, head)["decision"]["code"] == "PRESERVED"
        assert _record(repository, allocation).base_commit == base
        assert log.read_text() == retained


@pytest.mark.parametrize("fault", ["missing_base", "broken_chain", "rewind", "stale_tip"])
def test_retained_publication_history_still_requires_base_and_forward_chain(fault):
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        base = _commit(repository.repo, "seed.txt", "task baseline")
        _publish(repository, base)
        path, _, record = _task(repository)
        middle = _commit(repository.repo, "outside.txt", "published middle")
        _publish(repository, middle)
        publication = _commit(repository.repo, "outside.txt", "published tip")
        _publish(repository, publication)
        _commit(path, "mine.txt", "task")
        git(path, "merge", "--no-edit", publication)
        head = git(path, "rev-parse", "HEAD")
        log = inventory(path).common_dir / "logs" / FORMAL_REF
        entries = log.read_text().splitlines()
        start = next(i for i, line in enumerate(entries) if line.split()[0] == base)
        retained = entries[start:]
        if fault == "missing_base":
            retained = retained[1:]
        elif fault == "broken_chain":
            retained[1] = retained[1].replace(middle, base, 1)
        elif fault == "rewind":
            retained.extend([f"{publication} {base} fixture", f"{base} {publication} fixture"])
        else:
            retained = retained[:-1]
        log.write_text("\n".join(retained) + "\n")

        assert attribution_base(path, base, head, FORMAL_REF) == base
        issues = path_scope_issues(path, record, head, publication_ref=FORMAL_REF)
        assert issues[0]["path"] == "outside.txt"
