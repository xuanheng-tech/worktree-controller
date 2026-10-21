"""Session closeout observations do not accumulate beyond their reachable holders."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace

from tests.test_controlled import adopt_canonical
from tests.test_shadow import GitRepository
from worktree_controller.git_backend import inventory
from worktree_controller.session import _prune_unreachable_closeouts
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import describe_holder


def _write(store: MetadataStore, holder) -> object:
    identity = asdict(holder)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = store.session_closeouts_dir / f"{key}.json"
    store._atomic_json(
        path, {"contract": "session-closeout/v1", "holder": identity, "allocations": []}
    )
    return path


def test_only_provably_dead_holders_are_pruned() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        live = describe_holder(os.getpid(), client_id="test-client", session_id="live")
        current = _write(store, live)
        other_live = _write(store, replace(live, session_id="other"))
        dead = _write(store, replace(live, start_ticks=live.start_ticks + 1, session_id="dead"))
        rebooted = _write(store, replace(live, boot_id="other-boot", session_id="rebooted"))
        unknown = store.session_closeouts_dir / ("0" * 64 + ".json")
        unknown.write_text("{}", encoding="utf-8")
        with store.lock(exclusive=True):
            _prune_unreachable_closeouts(store, keep=current)
        assert current.exists() and other_live.exists() and unknown.exists()
        assert not dead.exists() and not rebooted.exists()
