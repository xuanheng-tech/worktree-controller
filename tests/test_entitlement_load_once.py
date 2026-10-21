"""A per-tool entitlement check decodes repository metadata once."""

from __future__ import annotations

import os

import pytest

from tests.test_handoff_scope_and_check import direct as direct
from tests.test_handoff_scope_and_check import repository as repository
from tests.test_host_managed_writer_session import hosted as hosted
from tests.test_write_admission import task as task
from worktree_controller import writer_session
from worktree_controller.store import MetadataStore


@pytest.fixture
def loads(monkeypatch):
    """Each full metadata decode; ``load()`` decodes through ``_load_unlocked()``."""
    counted = []
    original = MetadataStore._load_unlocked

    def unlocked(self, *args, **kwargs):
        counted.append(self.root)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MetadataStore, "_load_unlocked", unlocked)
    return counted


@pytest.mark.parametrize("access", [None, "read"])
def test_host_check_loads_metadata_once(hosted, loads, access):
    _repo, path, _, _, _ = hosted
    assert writer_session.host_check(path, access=access)["code"] == "WRITE_ALLOWED"
    assert len(loads) == 1, loads


def test_direct_continuation_write_into_held_checkout_reuses_the_snapshot(direct, loads):
    from tests.test_handoff_scope_and_check import _changed_task, _lease

    repository, path, _, identity = direct
    held, _ = _changed_task(repository, task="held", files=())
    _lease(repository, held, "held-session")
    loads.clear()
    assert writer_session.check(path, **identity, access="read")["admission"] == "CONTINUATION_GRANTED"
    baseline = len(loads)
    loads.clear()
    decision = writer_session.check(path, **identity, access="write", target=held / "x")
    assert decision["admission"] == "CONTINUATION_GRANTED"
    # The held checkout reuses the continuation's snapshot: no decode beyond a plain
    # continuation check, however many that check itself needs.
    assert len(loads) == baseline, loads


def test_corrupt_metadata_still_refuses_without_the_early_load(hosted, monkeypatch):
    _repo, path, _, _, _ = hosted

    def corrupt(self, *args, **kwargs):
        from worktree_controller.models import ControllerError
        raise ControllerError("INVALID_METADATA", "injected corrupt metadata")

    monkeypatch.setattr(MetadataStore, "_load_unlocked", corrupt)
    decision = writer_session.host_check(path, access="read")
    assert decision["admission"] == "REFUSED" and decision["code"] == "INVALID_METADATA"
    assert os.environ["CONTROLLER_WRITER_MODE"] == "host-managed-v2"
