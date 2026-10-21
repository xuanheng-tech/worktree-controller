"""Legacy linked bindings gain registration identity only on matching live evidence."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_binding_registration import _codes, _item, _row, _store
from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller.cli import main
from worktree_controller.controller import status
from worktree_controller.models import ControllerError
from worktree_controller.registration_backfill import (
    REGISTRATION_WINDOW_SECONDS,
    reconcile_binding_registrations,
)
from worktree_controller.store import REGISTRATION_MARKER


def _legacy(repository: GitRepository, target: Path) -> None:
    """Strip the registration exactly as a pre-1.26 runtime would have left it."""
    item = _item(repository, target)
    (_store(repository).binding_registrations_dir / f"{item.key}.json").unlink()
    (item.git_dir / REGISTRATION_MARKER).unlink()


def _age_registration(repository: GitRepository, target: Path, seconds: int) -> None:
    item = _item(repository, target)
    record = _store(repository).load().records[_row(status(repository.repo), target)["allocation_id"]]
    moment = datetime.fromisoformat(record.created_at).timestamp() + seconds
    os.utime(item.git_dir / "commondir", (moment, moment))


def test_dry_run_then_apply_registers_a_legacy_binding() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        allocation = acquired["decision"]["allocation_id"]
        _legacy(repository, target)
        before = _store(repository).load()
        planned = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert planned["code"] == "BINDING_REGISTRATION_BACKFILL_READY"
        assert [c["allocation_id"] for c in planned["candidates"]] == [allocation]
        assert planned["refused"] == []
        item = _item(repository, target)
        assert not (item.git_dir / REGISTRATION_MARKER).exists()
        applied = reconcile_binding_registrations(
            repository.repo, dry_run=False, expected_plan_id=planned["plan_id"]
        )["decision"]
        assert applied["code"] == "BINDING_REGISTRATIONS_BACKFILLED"
        assert applied["written"] == 1
        document = json.loads((_store(repository).binding_registrations_dir / f"{item.key}.json").read_text())
        marker = json.loads((item.git_dir / REGISTRATION_MARKER).read_text())
        assert document["token"] == marker["token"]
        assert document["allocation_id"] == marker["allocation_id"] == allocation
        after = _store(repository).load()
        assert after.records == before.records
        assert after.bindings == before.bindings
        again = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert again["code"] == "BINDING_REGISTRATION_BACKFILL_NOTHING_TO_DO"
        assert again["already_registered"] == 1


def test_backfilled_binding_then_detects_a_reused_registration_name() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository, task="alpha", owner="owner-a")
        _legacy(repository, target)
        plan = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]["plan_id"]
        reconcile_binding_registrations(repository.repo, dry_run=False, expected_plan_id=plan)
        git(repository.repo, "worktree", "remove", "--force", str(target))
        git(repository.repo, "worktree", "add", "-b", "unrelated/other", str(target), "HEAD")
        observed = status(repository.repo)
        assert _row(observed, target)["allocation_id"] is None
        assert "BINDING_IDENTITY_DRIFT" in _codes(observed)


def test_registration_newer_than_the_allocation_is_refused() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        _legacy(repository, target)
        _age_registration(repository, target, REGISTRATION_WINDOW_SECONDS + 60)
        decision = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert decision["code"] == "BINDING_REGISTRATION_BACKFILL_NOTHING_TO_DO"
        assert decision["refused"][0]["blockers"] == ["REGISTRATION_NEWER_THAN_ALLOCATION"]
        # Refused bindings keep their legacy meaning.
        assert _row(status(repository.repo), target)["allocation_id"] == acquired["decision"]["allocation_id"]


def test_registration_inside_the_acquire_window_is_accepted() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        _legacy(repository, target)
        _age_registration(repository, target, REGISTRATION_WINDOW_SECONDS - 60)
        decision = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert decision["code"] == "BINDING_REGISTRATION_BACKFILL_READY"


def test_stale_marker_and_unrelated_history_are_refused() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        item = _item(repository, target)
        (_store(repository).binding_registrations_dir / f"{item.key}.json").unlink()
        decision = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert decision["refused"][0]["blockers"] == ["STALE_REGISTRATION_MARKER"]
        (item.git_dir / REGISTRATION_MARKER).unlink()
        git(target, "checkout", "-q", "--orphan", "unrelated")
        git(target, "commit", "-q", "--allow-empty", "-m", "unrelated root")
        decision = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert decision["refused"][0]["blockers"] == ["BASE_NOT_IN_HISTORY"]


def test_apply_requires_the_exact_current_plan() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        _legacy(repository, target)
        with pytest.raises(ControllerError) as refused:
            reconcile_binding_registrations(repository.repo, dry_run=False, expected_plan_id="0" * 64)
        assert refused.value.code == "RESOURCE_PLAN_MISMATCH"
        assert not (_item(repository, target).git_dir / REGISTRATION_MARKER).exists()


def test_canonical_and_registered_bindings_are_untouched() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        decision = reconcile_binding_registrations(repository.repo, dry_run=True)["decision"]
        assert decision["code"] == "BINDING_REGISTRATION_BACKFILL_NOTHING_TO_DO"
        assert decision["candidates"] == decision["refused"] == []
        assert decision["already_registered"] == 1
        canonical = _item(repository, repository.repo)
        assert not (canonical.git_dir / REGISTRATION_MARKER).exists()


def test_cli_surface(capsys) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        _legacy(repository, target)
        argv = ["reconcile", "binding-registrations", "--repo", str(repository.repo), "--json"]
        assert main([*argv, "--dry-run"]) == 0
        planned = json.loads(capsys.readouterr().out)
        assert planned["command"] == "reconcile"
        plan_id = planned["decision"]["plan_id"]
        assert main([*argv, "--expected-plan-id", "f" * 64]) == 2
        assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "RESOURCE_PLAN_MISMATCH"
        assert main([*argv, "--expected-plan-id", plan_id]) == 0
        assert json.loads(capsys.readouterr().out)["decision"]["written"] == 1
