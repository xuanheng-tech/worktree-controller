"""Optional permission acceptance through a real, versioned Controller entry point."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_host_handoff_continuation import frozen as frozen
from tests.test_host_managed_writer_session import metadata
from tests.test_integration_pipeline import POLICY_THREE_CANDIDATES
from tests.test_shadow import GitRepository
from worktree_controller import writer_lease


@pytest.fixture(scope="module")
def installed():
    configured = os.environ.get("WORKTREE_CONTROLLER_ACCEPTANCE")
    if not configured:
        pytest.skip("set WORKTREE_CONTROLLER_ACCEPTANCE to a versioned installed Controller")
    entry = Path(configured)
    assert entry.is_absolute() and entry.is_file() and os.access(entry, os.X_OK)
    return entry


def check(installed, frozen, mode, *, access=None, target=None, session_id=None):
    repository, path, _, holder = frozen
    environment = dict(os.environ)
    # The installed interpreter must not import this checkout's implementation.
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(key, None)
    if mode == "direct":
        environment.pop("CONTROLLER_WRITER_MODE", None)
        environment.pop("CONTROLLER_WRITER_CONTEXT", None)
        arguments = ["check", holder.client_id, str(path), str(holder.pid),
                     "--session-id", session_id or holder.session_id]
    else:
        assert mode == "hosted"
        arguments = ["host-check", str(path)]
        if session_id:
            context = json.loads(environment["CONTROLLER_WRITER_CONTEXT"])
            environment["CONTROLLER_WRITER_CONTEXT"] = json.dumps(
                {**context, "session_id": session_id}
            )
    if access is not None:
        arguments += ["--access", access]
    if target is not None:
        arguments += ["--target", str(target)]
    before = metadata(repository)
    result = subprocess.run(
        [str(installed), "writer-session", *arguments], cwd=path,
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    assert metadata(repository) == before
    assert result.returncode in (0, 2), result.stderr
    payload = json.loads(result.stdout)
    assert not payload.get("errors"), payload
    decision = payload["decision"]
    assert result.returncode == (0 if decision["admission"] == "CONTINUATION_GRANTED" else 2)
    return decision


def test_installed_direct_holder_continues_after_handoff(installed, frozen, tmp_path):
    _, _, allocation, _ = frozen
    for access in ("read", "exec"):
        decision = check(installed, frozen, "direct", access=access)
        assert decision["admission"] == "CONTINUATION_GRANTED"
        assert decision["allocation_id"] == allocation
        assert decision["lifecycle"] == "HANDOFF_READY"
    outside = tmp_path / "notes/new.txt"
    assert check(installed, frozen, "direct", access="write", target=outside)[
        "admission"
    ] == "CONTINUATION_GRANTED"
    assert not outside.exists()
    assert check(installed, frozen, "direct")["admission"] == "REFUSED"
    assert check(installed, frozen, "direct", access="read", session_id="wrong-session")[
        "admission"
    ] == "REFUSED"


def test_installed_hosted_holder_continues_after_handoff(installed, frozen, tmp_path):
    _, _, allocation, _ = frozen
    for access in ("read", "exec"):
        decision = check(installed, frozen, "hosted", access=access)
        assert decision["admission"] == "CONTINUATION_GRANTED"
        assert decision["allocation_id"] == allocation
        assert decision["lifecycle"] == "HANDOFF_READY"
    outside = tmp_path / "notes/new.txt"
    assert check(installed, frozen, "hosted", access="write", target=outside)[
        "admission"
    ] == "CONTINUATION_GRANTED"
    assert not outside.exists()
    assert check(installed, frozen, "hosted")["admission"] == "REFUSED"
    assert check(installed, frozen, "hosted", access="read", session_id="wrong-session")[
        "admission"
    ] == "REFUSED"


def leased_target(repository):
    path, payload = acquire_one(
        repository, task="held", authority="held", scope_paths=("held.txt",)
    )
    holder = writer_lease.resolve_holder(
        str(os.getpid()), client_id="another-client", session_id="held-session", run_id=None
    )
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id=holder.client_id,
        holder_pid=holder.pid, session_id=holder.session_id, run_id=None, holder=holder,
    )
    return path / "held.txt", payload["decision"]["allocation_id"]


def test_installed_same_holder_can_write_other_leased_checkouts(installed, frozen):
    repository, _, _, _ = frozen
    local_target, local_allocation = leased_target(repository)
    unheld, _ = acquire_one(
        repository, task="unheld", authority="unheld", scope_paths=("unheld.txt",)
    )
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as other:
        adopt_canonical(other)
        _configure_remote(other)
        cross_target, cross_allocation = leased_target(other)
        before = metadata(other)
        for mode in ("direct", "hosted"):
            for target, allocation in (
                (local_target, local_allocation), (cross_target, cross_allocation),
            ):
                decision = check(installed, frozen, mode, access="write", target=target)
                assert decision["admission"] == "CONTINUATION_GRANTED"
                assert decision["target_allocation_id"] == allocation
                assert not target.exists()
            assert check(installed, frozen, mode, access="write", target=unheld / "unheld.txt")[
                "admission"
            ] == "REFUSED"
            assert metadata(other) == before


def test_installed_frozen_checkout_still_refuses_writes(installed, frozen, tmp_path):
    _, path, _, _ = frozen
    alias = tmp_path / "frozen-alias"
    alias.symlink_to(path / "hosted.txt")
    for mode in ("direct", "hosted"):
        for target in (path, path / "hosted.txt", path / "new/file.txt", alias):
            decision = check(installed, frozen, mode, access="write", target=target)
            assert decision["admission"] == "REFUSED"
            assert decision["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"
