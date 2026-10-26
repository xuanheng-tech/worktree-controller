"""Unrelated completion scope can be reused only during the same held flock."""

import json
import os
from uuid import uuid4

import pytest

from tests.test_controlled import adopt_canonical
from tests.test_shadow import GitRepository
from worktree_controller import cleanup_evidence
from worktree_controller import delivered_closeout as dc
from worktree_controller import preserved_cleanup as pc
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture
def completions(monkeypatch):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        observed = inventory(repository.repo)
        item = observed.worktrees[0]
        store = MetadataStore(observed.common_dir)
        snapshot = store.load()
        record = snapshot.records[snapshot.bindings[item.key]]
        paths = []
        for _ in range(3):
            path = store.completion_receipts_dir / f"{uuid4()}.json"
            store._atomic_json(path, {
                "contract": "legacy-preserved-state-cleanup/v1", "result": "CLEANED",
                "plan": {"allocation_id": str(uuid4())}, "opaque": "x" * 65536,
            })
            paths.append(path)
        reads = []
        original = pc.read_document

        def counted(directory, name):
            reads.append(name)
            return original(directory, name)

        monkeypatch.setattr(pc, "read_document", counted)
        monkeypatch.setattr(cleanup_evidence, "read_document", counted)

        def observe(reader=store):
            return pc.cleanup_receipts(reader, item, record, snapshot.repository_id)

        yield store, paths, reads, observe, record


def test_unrelated_payloads_are_decoded_once_within_a_lock(completions):
    store, paths, reads, observe, _ = completions
    with store.lock(exclusive=False):
        assert observe() == observe() == []
        assert len(reads) == len(paths)
    with store.lock(exclusive=False):
        assert observe() == []
        assert len(reads) == 2 * len(paths), "scope must not survive the prior flock"


@pytest.mark.parametrize("replacement", ["atomic", "in-place-restored-mtime"])
def test_changed_scope_is_revalidated_even_with_restored_mtime(completions, replacement):
    store, paths, reads, observe, record = completions
    path = paths[0]
    with store.lock(exclusive=False):
        assert observe() == []
        value = json.loads(path.read_text())
        value["plan"]["allocation_id"] = record.allocation_id
        if replacement == "atomic":
            store._atomic_json(path, value)
        else:
            metadata = path.stat()
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            assert path.stat().st_size == metadata.st_size
        with pytest.raises(ControllerError):
            observe()
        assert reads.count(path.name) == 2


def test_relevant_completion_is_never_served_from_a_scope_hit(completions):
    store, paths, reads, observe, record = completions
    path = paths[0]
    value = json.loads(path.read_text())
    value["plan"]["allocation_id"] = record.allocation_id
    # Unknown completion contracts are ignored by the existing consumer, but
    # still must be read every time when their allocation matches.
    value["contract"] = "unrecognized-fixture/v1"
    store._atomic_json(path, value)
    with store.lock(exclusive=False):
        assert observe() == observe() == []
        assert reads.count(path.name) == 2


def test_replaced_unrelated_payload_cannot_hide_a_symlink(completions):
    store, paths, _, observe, _ = completions
    with store.lock(exclusive=False):
        assert observe() == []
        paths[0].unlink()
        paths[0].symlink_to(paths[1])
        with pytest.raises(ControllerError):
            observe()


@pytest.mark.parametrize("exclusive", [False, True])
def test_same_thread_store_instances_reuse_the_actual_held_flock(completions, exclusive):
    store, paths, reads, observe, _ = completions
    nested = MetadataStore(store.root.parents[1])
    with store.lock(exclusive=exclusive):
        assert observe() == observe(nested) == []
        assert len(reads) == len(paths), "a new Store discarded the held snapshot's scope"
    # The second Store never owned that flock and cannot keep its view afterward.
    assert observe(nested) == observe(nested) == []
    assert len(reads) == 3 * len(paths)


def test_no_change_and_preserved_cleanup_share_only_unrelated_scope(completions):
    store, paths, reads, observe, record = completions
    nested = MetadataStore(store.root.parents[1])
    with store.lock(exclusive=False):
        assert observe() == []
        assert not dc._has_pre_completion_cleanup(store, record)
        assert not dc._has_pre_completion_cleanup(nested, record)
        assert len(reads) == len(paths)
    with store.lock(exclusive=False):
        assert not dc._has_pre_completion_cleanup(store, record)
        assert len(reads) == 2 * len(paths)
    assert not dc._has_pre_completion_cleanup(nested, record)
    assert len(reads) == 3 * len(paths)


@pytest.mark.parametrize("replacement", ["atomic", "in-place-restored-mtime"])
def test_no_change_detects_new_pre_completion_cleanup(completions, replacement):
    store, paths, reads, _, record = completions
    path = paths[0]
    with store.lock(exclusive=False):
        assert not dc._has_pre_completion_cleanup(store, record)
        value = json.loads(path.read_text())
        value["plan"]["allocation_id"] = record.allocation_id
        if replacement == "atomic":
            store._atomic_json(path, value)
        else:
            metadata = path.stat()
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            assert path.stat().st_size == metadata.st_size
        assert dc._has_pre_completion_cleanup(store, record)
        assert reads.count(path.name) == 2


def test_no_change_relevant_payload_is_rechecked_on_each_lookup(completions):
    store, paths, reads, _, record = completions
    path = paths[0]
    value = json.loads(path.read_text())
    value["plan"].update(allocation_id=record.allocation_id, terminal_evidence={"kind": "no_change"})
    store._atomic_json(path, value)
    with store.lock(exclusive=False):
        assert not dc._has_pre_completion_cleanup(store, record)
        assert not dc._has_pre_completion_cleanup(store, record)
        assert reads.count(path.name) == 2


@pytest.mark.parametrize("fault", ["symlink", "malformed"])
def test_no_change_rejects_changed_unrelated_payload(completions, fault):
    store, paths, _, _, record = completions
    with store.lock(exclusive=False):
        assert not dc._has_pre_completion_cleanup(store, record)
        if fault == "symlink":
            paths[0].unlink()
            paths[0].symlink_to(paths[1])
        else:
            paths[0].write_text("not JSON")
        with pytest.raises((ControllerError, ValueError)):
            dc._has_pre_completion_cleanup(store, record)
