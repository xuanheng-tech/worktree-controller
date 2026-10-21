from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _publish,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller.git_backend import inventory
from worktree_controller.keyed_document_scope import (
    PROGRESS_PATH,
    _changed_keys,
    independent_progress_scopes,
)
from worktree_controller.lifecycle import (
    _blocking_scope_intent,
    acquire_publication_lease,
    refresh_integration,
)
from worktree_controller.store import MetadataStore


def _document() -> dict:
    return {
        "schema_version": 2,
        "repo_id": "quant-platform",
        "note": "synthetic fixture",
        "tasks": [
            {"authority_key": "left", "current_step": "before"},
            {"authority_key": "right", "current_step": "before"},
        ],
        "development_lines": [],
        "modules": [],
        "decisions": [],
    }


def _raw(value: dict) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def _seed(repository: GitRepository) -> dict:
    doc = _document()
    path = repository.repo / PROGRESS_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(_raw(doc))
    (repository.repo / ".gitattributes").write_text(f"{PROGRESS_PATH} merge=qp-handbook-progress\n")
    git(repository.repo, "add", PROGRESS_PATH, ".gitattributes")
    git(repository.repo, "commit", "-m", "synthetic keyed document")
    adopt_canonical(repository)
    _configure_remote(repository)
    return doc


def _sources(repository: GitRepository, *, same_key: bool = False):
    doc = _seed(repository)
    left, right = copy.deepcopy(doc), copy.deepcopy(doc)
    left["tasks"][0]["current_step"] = "left changed"
    right["tasks"][0 if same_key else 1]["current_step"] = "right changed"
    return [
        _source(repository, task=task, relative_path=PROGRESS_PATH, content=_raw(value).decode())
        for task, value in (("left", left), ("right", right))
    ]


def test_diff_uses_whole_records_and_includes_additions_and_deletions() -> None:
    before, after = _document(), _document()
    after["tasks"].pop(0)
    after["tasks"].append({"authority_key": "new", "goal": "new goal"})
    after["tasks"][0]["evidence"] = ["doc:synthetic.md"]
    assert _changed_keys(_raw(before), _raw(after)) == {
        ("tasks", "left"),
        ("tasks", "right"),
        ("tasks", "new"),
    }


def test_json_boolean_and_number_are_distinct_record_changes() -> None:
    before, after = _document(), _document()
    before["tasks"][0]["value"] = True
    after["tasks"][0]["value"] = 1
    assert _changed_keys(_raw(before), _raw(after)) == {("tasks", "left")}


def test_json_boolean_and_number_metadata_change_retains_file_fence() -> None:
    before, after = _document(), _document()
    before["note"] = {"verified": True}
    after["note"] = {"verified": 1}
    with pytest.raises(ValueError, match="document metadata changed"):
        _changed_keys(_raw(before), _raw(after))


@pytest.mark.parametrize(
    "mutation",
    ["metadata", "schema", "duplicate_identity", "duplicate_key", "reorder", "unknown", "nan"],
)
def test_unsupported_or_ambiguous_documents_fail_closed(mutation: str) -> None:
    doc = _document()
    if mutation == "metadata":
        doc["note"] = "changed contract"
    elif mutation == "schema":
        doc["schema_version"] = 2.0
    elif mutation == "duplicate_identity":
        doc["tasks"].append(doc["tasks"][0])
    elif mutation == "reorder":
        doc["tasks"].reverse()
    elif mutation == "unknown":
        doc["new_collection"] = []
    raw = _raw(doc)
    if mutation == "duplicate_key":
        raw = raw.replace(b'"repo_id":', b'"repo_id": "first", "repo_id":')
    elif mutation == "nan":
        raw = raw.replace(b'"before"', b"NaN")
    with pytest.raises(ValueError):
        _changed_keys(_raw(_document()), raw)


def test_frozen_objects_prove_disjointness_without_trusting_worktree() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repo:
        (left_path, _left_id, left), (_right_path, _right_id, right) = _sources(repo)
        (left_path / PROGRESS_PATH).write_text("invalid mutable contents")
        assert independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (left,), (right,)
        )
        for left_scopes, right_scopes in (
            ((), (PROGRESS_PATH,)),
            (("docs",), (PROGRESS_PATH,)),
            ((PROGRESS_PATH, "code.py"), (PROGRESS_PATH, "code.py")),
        ):
            assert not independent_progress_scopes(
                repo.repo, left_scopes, right_scopes, (left,), (right,)
            )
        assert not independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (left,), ("0" * 40,)
        )
        assert not independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (left,), (left,)
        )
        parent = git(left_path, "rev-parse", f"{left}^").strip()
        assert not independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (parent,), (left,)
        )
        assert not independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (left,), (right, left)
        )


@pytest.mark.parametrize("mutation", ["missing", "symlink", "oversized", "profile"])
def test_unproved_committed_protocol_keeps_file_fence(mutation: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repo:
        (left_path, _left_id, _left), (_right_path, _right_id, right) = _sources(repo)
        path = left_path / PROGRESS_PATH
        if mutation in ("missing", "symlink"):
            path.unlink()
            if mutation == "symlink":
                path.symlink_to("../../.gitattributes")
        elif mutation == "oversized":
            path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        else:
            (left_path / ".gitattributes").write_text(
                f"{PROGRESS_PATH} merge=qp-handbook-progress\n* -merge\n"
            )
        git(left_path, "add", PROGRESS_PATH, ".gitattributes")
        git(left_path, "commit", "-m", "synthetic unproved protocol")
        left = git(left_path, "rev-parse", "HEAD").strip()
        assert not independent_progress_scopes(
            repo.repo, (PROGRESS_PATH,), (PROGRESS_PATH,), (left,), (right,)
        )


def test_existing_scope_wait_clears_without_replacing_frozen_evidence(monkeypatch) -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repo:
        (_lp, left, _lh), (_rp, right, _rh) = _sources(repo)
        _enqueue(repo, task="left-integration", source_allocation=left)
        with monkeypatch.context() as old_runtime:
            old_runtime.setattr(
                "worktree_controller.lifecycle.independent_progress_scopes",
                lambda *args: False,
            )
            second = _enqueue(repo, task="right-integration", source_allocation=right)
        assert second["decision"]["state"] == "WAITING_SCOPE"
        intent_id = second["decision"]["intent_id"]
        store = MetadataStore(inventory(repo.repo).common_dir)
        before = store.load().integration_intents[intent_id]
        _, result = _claim(repo, task="right-integration", intent_id=intent_id)
        assert result["decision"]["state"] == "PREPARING_INTEGRATION"
        after = store.load().integration_intents[intent_id]
        assert after.source_handoffs == before.source_handoffs
        assert after.scope_paths == before.scope_paths
        assert after.scope_digest == before.scope_digest
        assert after.queue_sequence == before.queue_sequence


def test_same_record_still_waits_in_the_actual_queue() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repo:
        (_lp, left, _lh), (_rp, right, _rh) = _sources(repo, same_key=True)
        first = _enqueue(repo, task="left-integration", source_allocation=left)
        second = _enqueue(repo, task="right-integration", source_allocation=right)
        assert first["decision"]["state"] == "QUEUED"
        assert second["decision"]["state"] == "WAITING_SCOPE"
        _, result = _claim(
            repo, task="right-integration", intent_id=second["decision"]["intent_id"]
        )
        assert result["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"


def test_independent_records_prepare_but_stale_main_still_requires_revalidation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repo:
        (_lp, left, _lh), (_rp, right, _rh) = _sources(repo)
        first = _enqueue(repo, task="left-integration", source_allocation=left)
        second = _enqueue(repo, task="right-integration", source_allocation=right)
        assert first["decision"]["state"] == second["decision"]["state"] == "QUEUED"
        first_path, _ = _claim(
            repo, task="left-integration", intent_id=first["decision"]["intent_id"]
        )
        second_path, _ = _claim(
            repo, task="right-integration", intent_id=second["decision"]["intent_id"]
        )
        _validate(repo, first_path, "left-keyed")
        _validate(repo, second_path, "right-keyed")
        store = MetadataStore(inventory(repo.repo).common_dir)
        snapshot = store.load()
        earlier = snapshot.integration_intents[first["decision"]["intent_id"]]
        later = snapshot.integration_intents[second["decision"]["intent_id"]]
        assert _blocking_scope_intent(snapshot, later, repository_path=repo.repo) is None
        for state in ("RECOVERY_REQUIRED", "ATTENTION_REQUIRED", "PUBLISHING"):
            modified = replace(
                snapshot,
                integration_intents={
                    **snapshot.integration_intents,
                    earlier.intent_id: replace(earlier, state=state),
                },
            )
            assert _blocking_scope_intent(modified, later, repository_path=repo.repo) is not None
        _publish(repo, first_path)
        stale = acquire_publication_lease(
            repo.repo,
            target=second_path,
            expected_record_version=2,
            owner="integration-owner",
            run_id="stale-right-keyed",
        )
        assert stale["decision"]["code"] == "NEEDS_REPREPARE"
        refreshed = refresh_integration(
            repo.repo, target=second_path, expected_record_version=3, owner="integration-owner"
        )
        assert refreshed["decision"]["state"] == "PREPARING_INTEGRATION"
        merged = json.loads((second_path / PROGRESS_PATH).read_text())
        assert [row["current_step"] for row in merged["tasks"]] == ["left changed", "right changed"]
        _validate(repo, second_path, "right-keyed-refreshed", expected_record_version=4)
        _publish(repo, second_path, expected_record_version=5)
