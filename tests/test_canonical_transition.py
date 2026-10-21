from __future__ import annotations

import pytest

from tests.test_shadow import POLICY, POLICY_V2, GitRepository, git
from worktree_controller import canonical_transition
from worktree_controller.canonical_transition import transition_canonical
from worktree_controller.controller import adopt, doctor, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import handoff
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def frozen_canonical(
    repository: GitRepository, *, target_policy: str = POLICY_V2, main_branch: str = "main"
) -> dict[str, str]:
    if main_branch != "main":
        git(repository.repo, "branch", "-m", main_branch)
    target_policy = target_policy.replace('branch = "main"', f'branch = "{main_branch}"')
    initial = git(repository.repo, "rev-parse", "HEAD")
    git(repository.repo, "switch", "-c", "docs/frozen")
    (repository.repo / "README.md").write_text("frozen handoff\n", encoding="utf-8")
    git(repository.repo, "add", "README.md")
    git(repository.repo, "commit", "-m", "freeze canonical handoff")
    old_head = git(repository.repo, "rev-parse", "HEAD")
    adopt(
        repository.repo,
        target=repository.repo,
        task_key="old-task",
        authority_key="old-authority",
        module="repository",
        role="control-plane",
        owner="old-owner",
        lifecycle="ACTIVE",
    )
    handed = handoff(
        repository.repo,
        target=repository.repo,
        expected_record_version=1,
        kind="integration-ready",
        commit=old_head,
        finalizer_record_id="test-frozen-finalizer",
    )
    git(repository.repo, "switch", main_branch)
    git(repository.repo, "merge", "--ff-only", "docs/frozen")
    (repository.repo / ".agents" / "worktree-policy.toml").write_text(
        target_policy, encoding="utf-8"
    )
    git(repository.repo, "add", ".agents/worktree-policy.toml")
    git(repository.repo, "commit", "--allow-empty", "-m", "publish target policy")
    published = git(repository.repo, "rev-parse", "HEAD")
    remote = repository.root / "origin.git"
    git(repository.root, "init", "--bare", str(remote))
    git(repository.repo, "remote", "add", "origin", str(remote))
    git(repository.repo, "push", "origin", main_branch)
    git(repository.repo, "switch", "docs/frozen")
    git(repository.repo, "branch", "-f", main_branch, initial)
    return {
        "old_head": old_head,
        "initial_main": initial,
        "published": published,
        "record_version": handed["decision"]["record_version"],
    }


def transition(repository: GitRepository, facts: dict[str, str]) -> dict:
    return transition_canonical(
        repository.repo,
        target=repository.repo,
        expected_record_version=int(facts["record_version"]),
        owner="old-owner",
        expected_branch="docs/frozen",
        expected_head=facts["old_head"],
        expected_main_head=facts["initial_main"],
        expected_remote_head=facts["published"],
        new_owner="new-owner",
        new_task_key="repository-control",
        new_authority_key="repository-control",
    )


def initial_policy(main_branch: str) -> str:
    return (
        POLICY
        if main_branch == "main"
        else POLICY_V2.replace('branch = "main"', f'branch = "{main_branch}"')
    )


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_fast_forwards_main_and_preserves_frozen_branch(
    main_branch: str,
) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(repository, main_branch=main_branch)
        result = transition(repository, facts)

        assert result["decision"]["code"] == "CANONICAL_TRANSITIONED"
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == main_branch
        assert git(repository.repo, "rev-parse", "HEAD") == facts["published"]
        assert git(repository.repo, "rev-parse", "docs/frozen") == facts["old_head"]
        canonical = status(repository.repo)["worktrees"][0]
        assert canonical["lifecycle"] == "ACTIVE"
        assert canonical["owner"] == "new-owner"
        assert canonical["handoff_commit"] is None
        assert canonical["scope_paths"] == []
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


@pytest.mark.parametrize("checkout_main", [False, True])
@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_recovers_exact_partial_git_progress(
    checkout_main: bool, main_branch: str
) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(repository, main_branch=main_branch)
        git(
            repository.repo,
            "update-ref",
            f"refs/heads/{main_branch}",
            facts["published"],
            facts["initial_main"],
        )
        if checkout_main:
            git(repository.repo, "checkout", main_branch)

        result = transition(repository, facts)
        assert result["decision"]["code"] == "CANONICAL_TRANSITIONED"
        assert git(repository.repo, "rev-parse", "HEAD") == facts["published"]


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_rejects_dirty_checkout_before_ref_change(main_branch: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(repository, main_branch=main_branch)
        (repository.repo / "README.md").write_text("unfinished work\n", encoding="utf-8")

        with pytest.raises(ControllerError, match="not clean"):
            transition(repository, facts)
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == "docs/frozen"
        assert git(repository.repo, "rev-parse", main_branch) == facts["initial_main"]


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_rejects_remote_drift_before_ref_change(main_branch: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(repository, main_branch=main_branch)
        facts["published"] = facts["old_head"]

        with pytest.raises(ControllerError) as failure:
            transition(repository, facts)
        assert failure.value.code == "REMOTE_DRIFT"
        assert git(repository.repo, "rev-parse", main_branch) == facts["initial_main"]


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_restores_frozen_checkout_on_bad_target_policy(
    main_branch: str,
) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(
            repository,
            target_policy=POLICY_V2.replace("schema_version = 2", "schema_version = 99"),
            main_branch=main_branch,
        )

        with pytest.raises(ControllerError) as failure:
            transition(repository, facts)
        assert failure.value.code == "INVALID_POLICY"
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == "docs/frozen"
        assert git(repository.repo, "rev-parse", main_branch) == facts["published"]
        assert status(repository.repo)["worktrees"][0]["lifecycle"] == "HANDOFF_READY"


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_canonical_transition_refuses_occupied_main_branch(main_branch: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_canonical(repository, main_branch=main_branch)
        other = repository.root / "other"
        git(repository.repo, "worktree", "add", str(other), main_branch)

        with pytest.raises(ControllerError) as failure:
            transition(repository, facts)
        assert failure.value.code == "BRANCH_OCCUPIED"
        assert git(repository.repo, "rev-parse", main_branch) == facts["initial_main"]


def test_canonical_transition_refuses_other_publication_branches() -> None:
    with GitRepository(linked=0, policy_text=initial_policy("develop")) as repository:
        facts = frozen_canonical(repository, main_branch="develop")
        with pytest.raises(ControllerError) as failure:
            transition(repository, facts)
        assert failure.value.code == "INVALID_POLICY"
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == "docs/frozen"
        assert git(repository.repo, "rev-parse", "develop") == facts["initial_main"]


def frozen_main(
    repository: GitRepository,
    *,
    main_branch: str = "main",
    target_policy: str = POLICY_V2,
    track_target_policy: bool = True,
) -> dict[str, str]:
    if main_branch != "main":
        git(repository.repo, "branch", "-m", main_branch)
    (repository.repo / "README.md").write_text("frozen main handoff\n", encoding="utf-8")
    git(repository.repo, "add", "README.md")
    git(repository.repo, "commit", "-m", "freeze main handoff")
    old_head = git(repository.repo, "rev-parse", "HEAD")
    adopt(
        repository.repo,
        target=repository.repo,
        task_key="old-task",
        authority_key="old-authority",
        module="repository",
        role="control-plane",
        owner="old-owner",
        lifecycle="ACTIVE",
    )
    handed = handoff(
        repository.repo,
        target=repository.repo,
        expected_record_version=1,
        kind="integration-ready",
        commit=old_head,
        finalizer_record_id="test-frozen-main-finalizer",
    )
    if track_target_policy:
        (repository.repo / ".agents/worktree-policy.toml").write_text(
            target_policy.replace('branch = "main"', f'branch = "{main_branch}"'),
            encoding="utf-8",
        )
        git(repository.repo, "add", ".agents/worktree-policy.toml")
    git(repository.repo, "commit", "--allow-empty", "-m", "publish main target")
    published = git(repository.repo, "rev-parse", "HEAD")
    remote = repository.root / "origin.git"
    git(repository.root, "init", "--bare", "--initial-branch", main_branch, str(remote))
    git(repository.repo, "remote", "add", "origin", str(remote))
    git(repository.repo, "push", "origin", main_branch)
    # Only this isolated fixture rewinds its checked-out branch.
    git(repository.repo, "reset", "--hard", old_head)
    return {
        "old_head": old_head,
        "initial_main": old_head,
        "published": published,
        "main_branch": main_branch,
        "record_version": str(handed["decision"]["record_version"]),
    }


def refresh(repository: GitRepository, facts: dict[str, str], **overrides) -> dict:
    arguments = {
        "target": repository.repo,
        "expected_record_version": int(facts["record_version"]),
        "owner": "old-owner",
        "expected_branch": facts["main_branch"],
        "expected_head": facts["old_head"],
        "expected_main_head": facts["initial_main"],
        "expected_remote_head": facts["published"],
        "new_owner": "new-owner",
        "new_task_key": "repository-control",
        "new_authority_key": "repository-control",
        **overrides,
    }
    return transition_canonical(repository.repo, **arguments)


def canonical_record(repository: GitRepository):
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    return next(iter(snapshot.records.values()))


@pytest.mark.parametrize("main_branch", ["main", "master"])
def test_frozen_main_refresh_preserves_history_without_an_extra_branch(main_branch: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy(main_branch)) as repository:
        facts = frozen_main(repository, main_branch=main_branch)
        before = canonical_record(repository)
        branches = git(repository.repo, "for-each-ref", "--format=%(refname)", "refs/heads")

        result = refresh(repository, facts)

        assert result["decision"]["code"] == "CANONICAL_MAIN_REFRESHED"
        assert git(repository.repo, "rev-parse", "HEAD") == facts["published"]
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == main_branch
        assert git(repository.repo, "for-each-ref", "--format=%(refname)", "refs/heads") == branches
        after = canonical_record(repository)
        assert after.history[:-1] == before.history
        assert after.history[-1]["event"] == "CANONICAL_MAIN_REFRESHED"
        assert after.history[-1]["old_finalizer_record_id"] == before.finalizer_record_id
        assert after.history[-1]["old_head"] == facts["old_head"]
        assert after.history[-1]["main_after"] == facts["published"]
        assert after.owner == "new-owner"
        assert after.lifecycle == "ACTIVE"
        assert after.handoff_commit is None
        assert doctor(repository.repo)["decision"]["code"] == "PASS"
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts)
        assert failure.value.code == "CAS_MISMATCH"
        assert canonical_record(repository) == after


def test_frozen_main_refresh_recovers_interrupted_metadata_without_rewinding(monkeypatch) -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        before = canonical_record(repository)
        with monkeypatch.context() as patch:
            def interrupted(*_args, **_kwargs):
                raise OSError("injected metadata interruption")
            patch.setattr(MetadataStore, "replace_record", interrupted)
            with pytest.raises(OSError, match="metadata interruption"):
                refresh(repository, facts)
        assert git(repository.repo, "rev-parse", "HEAD") == facts["published"]
        assert git(repository.repo, "status", "--porcelain") == ""
        assert canonical_record(repository) == before
        assert refresh(repository, facts)["decision"]["code"] == "CANONICAL_MAIN_REFRESHED"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"owner": "foreign-owner"}, "OWNER_MISMATCH"),
        ({"expected_record_version": 1}, "CAS_MISMATCH"),
        ({"expected_head": "published"}, "HANDOFF_IDENTITY_MISMATCH"),
        ({"expected_main_head": "published"}, "HEAD_MISMATCH"),
        ({"expected_remote_head": "old_head"}, "REMOTE_DRIFT"),
    ],
)
def test_frozen_main_refresh_requires_exact_identities(override: dict, code: str) -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        before = canonical_record(repository)
        arguments = {key: facts.get(value, value) for key, value in override.items()}
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts, **arguments)
        assert failure.value.code == code
        assert git(repository.repo, "rev-parse", "HEAD") == facts["old_head"]
        assert canonical_record(repository) == before


@pytest.mark.parametrize(
    "target_policy",
    [
        POLICY_V2.replace("schema_version = 2", "schema_version = 99"),
        POLICY_V2.replace("protected = true", "protected = false"),
        POLICY_V2.replace('remote = "origin"', 'remote = "other"'),
        POLICY_V2.replace('"control-plane", ', ""),
        "malformed TOML [",
    ],
)
def test_frozen_main_refresh_validates_target_policy_before_git_moves(target_policy: str) -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository, target_policy=target_policy)
        before = canonical_record(repository)
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts)
        assert failure.value.code in {"INVALID_POLICY", "INVALID_TARGET_POLICY"}
        assert git(repository.repo, "rev-parse", "HEAD") == facts["old_head"]
        assert git(repository.repo, "status", "--porcelain") == ""
        assert canonical_record(repository) == before


@pytest.mark.parametrize("shape", ["deleted", "symlink", "invalid-utf8", "ignored-collision"])
def test_frozen_main_refresh_preserves_checkout_when_published_files_are_unsafe(shape: str) -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        git(repository.repo, "switch", "--detach", facts["published"])
        policy = repository.repo / ".agents/worktree-policy.toml"
        if shape == "deleted":
            policy.unlink()
        elif shape == "symlink":
            policy.unlink()
            policy.symlink_to("../README.md")
        elif shape == "invalid-utf8":
            policy.write_bytes(b"\xff\xfe")
        else:
            (repository.repo / "local-evidence.txt").write_text("published\n")
        git(repository.repo, "add", "--", ".agents/worktree-policy.toml", "README.md")
        if shape == "ignored-collision":
            git(repository.repo, "add", "local-evidence.txt")
        git(repository.repo, "commit", "-m", "publish unsafe fixture")
        facts["published"] = git(repository.repo, "rev-parse", "HEAD")
        git(repository.repo, "push", "origin", "HEAD:main")
        git(repository.repo, "switch", "main")
        if shape == "ignored-collision":
            (repository.repo / ".git/info/exclude").write_text("local-evidence.txt\n")
            (repository.repo / "local-evidence.txt").write_text("valuable local evidence\n")
        before = canonical_record(repository)
        with pytest.raises(ControllerError):
            refresh(repository, facts)
        assert git(repository.repo, "rev-parse", "HEAD") == facts["old_head"]
        assert canonical_record(repository) == before
        if shape == "ignored-collision":
            assert (repository.repo / "local-evidence.txt").read_text() == "valuable local evidence\n"


def test_frozen_main_refresh_preserves_an_untracked_local_policy() -> None:
    with GitRepository(linked=0, policy=False) as repository:
        policy = repository.repo / ".agents/worktree-policy.toml"
        policy.parent.mkdir()
        policy.write_text(POLICY_V2)
        (repository.repo / ".git/info/exclude").write_text(".agents/\n")
        facts = frozen_main(repository, track_target_policy=False)
        before = policy.read_bytes()
        assert refresh(repository, facts)["decision"]["code"] == "CANONICAL_MAIN_REFRESHED"
        assert policy.read_bytes() == before


def test_frozen_main_refresh_refuses_a_dirty_or_unpublished_main() -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        before = canonical_record(repository)
        (repository.repo / "README.md").write_text("unfinished local work\n")
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts)
        assert failure.value.code == "DIRTY_WORKTREE"
        assert (repository.repo / "README.md").read_text() == "unfinished local work\n"
        git(repository.repo, "add", "README.md")
        git(repository.repo, "commit", "-m", "unpublished local work")
        observed = git(repository.repo, "rev-parse", "HEAD")
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts, expected_main_head=observed)
        assert failure.value.code == "NON_FAST_FORWARD"
        assert git(repository.repo, "rev-parse", "HEAD") == observed
        assert canonical_record(repository) == before


def test_frozen_main_refresh_recovers_remote_advance_after_git_progress(monkeypatch) -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        before = canonical_record(repository)
        publisher = repository.root / "publisher"
        git(repository.root, "clone", str(repository.root / "origin.git"), str(publisher))
        git(publisher, "config", "user.name", "Fixture Publisher")
        git(publisher, "config", "user.email", "publisher@example.invalid")
        git(publisher, "commit", "--allow-empty", "-m", "concurrent published advance")
        newer = git(publisher, "rev-parse", "HEAD")
        original_fetch = canonical_transition.fetch_remote_branch
        calls = 0

        def fetch_with_advance(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                git(publisher, "push", "origin", "main")
            return original_fetch(*args)

        with monkeypatch.context() as patch:
            patch.setattr(canonical_transition, "fetch_remote_branch", fetch_with_advance)
            with pytest.raises(ControllerError) as failure:
                refresh(repository, facts)
            assert failure.value.code == "REMOTE_DRIFT"
        assert git(repository.repo, "rev-parse", "HEAD") == facts["published"]
        assert canonical_record(repository) == before
        facts["initial_main"] = facts["published"]
        facts["published"] = newer
        assert refresh(repository, facts)["decision"]["code"] == "CANONICAL_MAIN_REFRESHED"
        assert git(repository.repo, "rev-parse", "HEAD") == newer


def test_frozen_main_refresh_refuses_duplicate_branch_occupancy() -> None:
    with GitRepository(linked=0) as repository:
        facts = frozen_main(repository)
        other = repository.root / "other"
        git(repository.repo, "worktree", "add", "--force", str(other), "main")
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts)
        assert failure.value.code == "BRANCH_OCCUPIED"
        assert git(repository.repo, "rev-parse", "HEAD") == facts["old_head"]


def test_frozen_main_refresh_refuses_a_published_symlink_policy_parent() -> None:
    with GitRepository(linked=0, policy=False) as repository:
        policy = repository.repo / ".agents/worktree-policy.toml"
        policy.parent.mkdir()
        policy.write_text(POLICY_V2)
        (repository.repo / ".git/info/exclude").write_text(".agents/\n")
        facts = frozen_main(repository, track_target_policy=False)
        publisher = repository.root / "publisher"
        git(repository.root, "clone", str(repository.root / "origin.git"), str(publisher))
        git(publisher, "config", "user.name", "Fixture Publisher")
        git(publisher, "config", "user.email", "publisher@example.invalid")
        (publisher / ".agents").symlink_to("elsewhere")
        git(publisher, "add", ".agents")
        git(publisher, "commit", "-m", "publish symlink parent")
        git(publisher, "push", "origin", "main")
        facts["published"] = git(publisher, "rev-parse", "HEAD")
        before = policy.read_bytes()
        with pytest.raises(ControllerError) as failure:
            refresh(repository, facts)
        assert failure.value.code == "INVALID_POLICY"
        assert git(repository.repo, "rev-parse", "HEAD") == facts["old_head"]
        assert policy.read_bytes() == before
