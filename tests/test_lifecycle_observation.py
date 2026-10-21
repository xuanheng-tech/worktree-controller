"""Advisory lifecycle observations must not change recorded task authority."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle_observation import (
    observe_repository_orphans,
    observe_worktree,
)
from worktree_controller.models import ControllerError, WriterHolder
from worktree_controller.policy import load_policy
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import RUNTIME_BINDING_PREFIX, new_lease


def _case(repository: GitRepository):
    target, acquired = acquire_one(repository)
    state = inventory(repository.repo)
    snapshot = MetadataStore(state.common_dir).load()
    item = next(item for item in state.worktrees if item.path == target)
    allocation_id = acquired["decision"]["allocation_id"]
    return state, snapshot, item, snapshot.records[allocation_id], load_policy(state.canonical_path)


def _lease(repository: GitRepository, allocation_id: str, target, owner: str):
    holder = WriterHolder(
        client_id="test-agent",
        pid=12345,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_ticks=42,
        session_id="session-1",
        run_id="run-1",
    )
    return new_lease(
        repository_id=MetadataStore(repository.repo / ".git").load().repository_id,
        allocation_id=allocation_id,
        worktree_path=str(target),
        owner=owner,
        holder=holder,
        ttl_seconds=None,
    )


def test_expired_lease_and_renewal_proxy_leave_lifecycle_unchanged() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        state, snapshot, item, record, policy = _case(repository)
        observed_at = datetime.now(UTC)
        lease = replace(
            _lease(repository, record.allocation_id, item.path, record.owner),
            renewed_at=(observed_at - timedelta(hours=1)).isoformat(),
            expires_at=(observed_at - timedelta(seconds=1)).isoformat(),
        )
        projected = replace(snapshot, writer_leases={record.allocation_id: lease})

        observation = observe_worktree(
            item,
            record,
            projected,
            policy,
            now_at=observed_at,
            holder_probe=lambda _holder: True,
        )

        assert observation["owner"] == record.owner
        assert observation["lifecycle"] == "ACTIVE"
        assert observation["session"]["session_id"] == "session-1"
        assert observation["lease"]["status"] == "EXPIRED"
        assert observation["status"]["holder"] == "LIVE"
        assert observation["heartbeat"] == {
            "source": "LEASE_RENEWAL",
            "last_at": lease.renewed_at,
            "independent": False,
        }
        assert "LEASE_EXPIRED" in observation["stale_facts"]
        assert observation["advisory_only"] is True
        assert record.lifecycle == "ACTIVE"
        assert MetadataStore(state.common_dir).load().records[record.allocation_id] == record
        assert not MetadataStore(state.common_dir).load().writer_leases


def test_holder_visibility_failures_and_namespace_uncertainty_are_unknown() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _state, snapshot, item, record, policy = _case(repository)
        lease = _lease(repository, record.allocation_id, item.path, record.owner)
        projected = replace(snapshot, writer_leases={record.allocation_id: lease})

        invisible = observe_worktree(
            item, record, projected, policy, holder_probe=lambda _holder: False
        )
        assert invisible["status"]["holder"] == "NOT_VISIBLE"
        assert "HOLDER_NOT_VISIBLE" in invisible["stale_facts"]
        assert invisible["lease"]["status"] == "NO_TTL"

        def unavailable(_holder):
            raise ControllerError("INVALID_HOLDER", "process visibility unavailable")

        unknown = observe_worktree(item, record, projected, policy, holder_probe=unavailable)
        assert unknown["status"]["holder"] == "UNKNOWN"
        assert "HOLDER_NOT_VISIBLE" not in unknown["stale_facts"]

        def must_not_probe(_holder):
            raise AssertionError("namespace uncertainty must skip process probing")

        namespace_unknown = observe_worktree(
            item,
            record,
            projected,
            policy,
            holder_probe=must_not_probe,
            namespace_healthy=False,
        )
        assert namespace_unknown["status"]["holder"] == "UNKNOWN"
        assert namespace_unknown["status"]["host_session"] == "UNKNOWN"
        assert record.lifecycle == "ACTIVE"


def test_runtime_session_and_structural_orphans_remain_advisory() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        state, snapshot, item, record, policy = _case(repository)
        lease = _lease(repository, record.allocation_id, item.path, record.owner)
        binding = RUNTIME_BINDING_PREFIX + json.dumps(
            {"holder": asdict(lease.holder), "lease_id": lease.lease_id, "kind": "test"}
        )
        runtime_record = replace(record, external_bindings=(binding,))
        runtime = observe_worktree(item, runtime_record, snapshot, policy)
        assert runtime["lease"]["present"] is False
        assert runtime["heartbeat"] == {
            "source": "NONE",
            "last_at": None,
            "independent": False,
        }
        assert runtime["runtime_sessions"][0]["session_id"] == "session-1"
        assert runtime["runtime_sessions"][0]["occupancy"] == "RECORDED"
        assert runtime["status"]["host_session"] == "UNKNOWN"

        missing_id = str(uuid4())
        orphan_id = str(uuid4())
        orphan_record = replace(record, allocation_id=orphan_id)
        orphan_lease = replace(lease, allocation_id=missing_id)
        anomalous = replace(
            snapshot,
            bindings={
                **snapshot.bindings,
                "wt_" + "x" * 64: record.allocation_id,
                "wt_" + "y" * 64: missing_id,
            },
            records={**snapshot.records, orphan_id: orphan_record},
            writer_leases={missing_id: orphan_lease},
        )
        codes = {finding["code"] for finding in observe_repository_orphans(state, anomalous)}
        assert codes >= {
            "ORPHAN_BINDING",
            "MISSING_RECORD",
            "ORPHAN_RECORD",
            "DUPLICATE_BINDING",
            "STRANDED_WRITER_LEASE",
        }
        assert observe_repository_orphans(state, anomalous) == observe_repository_orphans(
            state, anomalous
        )
        assert record.lifecycle == "ACTIVE"
        assert MetadataStore(state.common_dir).load().records[record.allocation_id] == record
