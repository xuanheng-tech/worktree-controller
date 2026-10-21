"""A topology refusal is a provable no-op, before and after the 1.28.0 boundary move."""

from __future__ import annotations

import json
import os

import pytest

from tests.test_typed_disposable import target as target
from worktree_controller import cleanup_attempts as a
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as r
from worktree_controller import hardlinks, lifecycle, transaction_scope
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _refuse(target, tmp_path, monkeypatch, *, legacy: bool):
    repository, worktree, _ = target
    alias = tmp_path / "external-build"
    os.link(worktree / "frontend/dist/assets/index.js", alias)
    store = MetadataStore(inventory(repository.repo).common_dir)
    options = dict(target=worktree, owner="module-owner", expected_record_version=2)
    plan = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)["decision"]["plan"]

    def refused(*args, **kwargs):
        raise ControllerError("HARDLINK_TOPOLOGY_BLOCKED", "topology changed since the last recorded baseline")

    with monkeypatch.context() as patch:
        if legacy:
            # Before 1.28.0 the BEFORE receipt was written and then verification refused.
            original = e.before

            def before_then_refuse(*args, **kwargs):
                original(*args, **kwargs)
                refused()

            patch.setattr(e, "before", before_then_refuse)
        else:
            patch.setattr(hardlinks, "verify", refused)
        with pytest.raises(ControllerError) as failure:
            lifecycle.cleanup_typed_disposable(
                repository.repo, **options, dry_run=False, expected_plan_id=plan["plan_id"]
            )
    assert failure.value.code == "HARDLINK_TOPOLOGY_BLOCKED"
    journal = next(store.transactions_dir.glob("*.json"))
    _, digest = e.read(journal)
    return repository, worktree, store, journal, digest, options, alias


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("external", [None, "link", "ctime"])
def test_topology_refusal_reconciles_and_cleanup_resumes(target, tmp_path, monkeypatch, legacy, external):
    repository, worktree, store, journal, digest, options, alias = _refuse(
        target, tmp_path, monkeypatch, legacy=legacy
    )
    value = json.loads(journal.read_text())
    action_dir = store.cleanup_actions_dir / journal.stem
    if legacy:
        assert value["actions"][0]["status"] == "FAILED_OR_PARTIAL"
        assert sorted(p.name for p in action_dir.iterdir()) == ["0000-before.json"]
    else:
        # The refusal now precedes the action boundary and leaves no action evidence.
        assert [row["status"] for row in value["actions"]] == ["PENDING"] * len(value["actions"])
        assert not action_dir.exists() or not list(action_dir.iterdir())
    if external == "link":
        os.link(alias, tmp_path / "second-external-build")
    elif external == "ctime":
        # External churn that nets out still changes the shared inode's ctime.
        os.link(alias, tmp_path / "transient")
        os.unlink(tmp_path / "transient")
    compatible = r.rollout_check(repository.repo)["decision"]
    assert [row["state"] for row in compatible["transactions"]] == ["TYPED_NO_ACTION_RECOVERABLE"]
    planned = a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=True)["decision"]["plan"]
    assert ("verify_boundary_receipt" in planned) is legacy
    result = a.reconcile_typed(
        repository.repo, journal.stem, digest, dry_run=False, expected_plan_id=planned["plan_id"]
    )["decision"]
    assert result["code"] == "TYPED_ATTEMPT_SUPERSEDED"
    with store.lock(exclusive=False):
        assert transaction_scope.terminal(store, journal.stem)
    fresh = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)["decision"]["plan"]
    done = lifecycle.cleanup_typed_disposable(
        repository.repo, **options, dry_run=False, expected_plan_id=fresh["plan_id"]
    )
    assert done["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=False)["decision"] == result


@pytest.mark.parametrize("drift", ["extra-evidence", "other-failure", "deleted", "before-topology", "in-scope-ctime"])
def test_legacy_topology_refusal_rejects_uncertainty(target, tmp_path, monkeypatch, drift):
    repository, worktree, store, journal, digest, _, _ = _refuse(target, tmp_path, monkeypatch, legacy=True)
    action_dir = store.cleanup_actions_dir / journal.stem
    if drift == "extra-evidence":
        e.write(action_dir, "0000-after.json", {})
    elif drift == "other-failure":
        value = json.loads(journal.read_text())
        value["failure"]["code"] = "RESOURCE_CLEANUP_FAILED"
        journal.write_text(json.dumps(value))
        _, digest = e.read(journal)
    elif drift == "deleted":
        (worktree / "backend/.venv/CACHEDIR.TAG").unlink()
    elif drift == "before-topology":
        # Test-owned evidence only: a BEFORE receipt for another topology.
        before = action_dir / "0000-before.json"
        value = json.loads(before.read_text())
        value["topology"] = hardlinks.inventory(worktree, ["frontend/dist"])
        before.chmod(0o600)
        before.write_text(json.dumps(value))
    else:
        member = worktree / "frontend/dist/index.html"
        member.chmod(member.stat().st_mode)
    with pytest.raises(ControllerError):
        a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=True)
    assert journal.exists()
    assert not (store.cleanup_recoveries_dir / journal.stem / "typed-no-action.json").exists()
