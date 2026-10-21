"""Narrow recovery of an archived Codex thread on a still-live shared daemon.

The normal dead-process gate stays unchanged. This opt-in path observes the native
server, never archives a thread, stops a process, or accepts caller-authored status.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import struct
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .environment import runtime_authority_evidence
from .models import ControllerError, WriterHolder
from .writer_lease import holder_is_live

CONTRACT = "archived-codex-binding-recovery/v1"
_MAX_MESSAGE = 2_000_000


def _blocked(reason: str) -> ControllerError:
    return ControllerError("NATIVE_SESSION_RECOVERY_REFUSED", reason)


def _shared_daemon(holder: WriterHolder) -> None:
    if holder.client_id not in {"codex", "codex-cli"} or not holder.session_id:
        raise _blocked("an exact Codex session identity is required")
    if not holder_is_live(holder):
        raise _blocked("use ordinary binding removal for an exited process")
    proc = Path("/proc") / str(holder.pid)
    arguments = (proc / "cmdline").read_bytes().split(b"\0")
    if (
        proc.stat().st_uid != os.getuid()
        or b"app-server" not in arguments
        or b"--managed-daemon" not in arguments
    ):
        raise _blocked("the holder is not the same user's managed shared Codex daemon")


def _native_observation(holder: WriterHolder) -> dict[str, Any]:
    # Lazy import keeps ordinary Controller observations usable without a transport.
    from websockets.sync.client import unix_connect

    path = Path.home() / ".codex/app-server-control/app-server-control.sock"
    if path.lstat().st_uid != os.getuid():
        raise _blocked("native control socket link authority is invalid")
    # Codex's stable entry is a symlink to its private Unix socket. Pin the
    # resolved endpoint, then verify the kernel peer against the recorded holder.
    path = path.resolve(strict=True)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise _blocked("native control socket authority is invalid")
    with unix_connect(
        str(path), uri="ws://localhost/", open_timeout=5, close_timeout=1, max_size=_MAX_MESSAGE
    ) as client:
        pid, uid, _gid = struct.unpack(
            "3i", client.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if pid != holder.pid or uid != os.getuid():
            raise _blocked("native socket peer differs from the recorded holder")

        def call(number: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            client.send(json.dumps({"id": number, "method": method, "params": params}))
            deadline = time.monotonic() + 5
            for _ in range(100):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _blocked("native observation timed out")
                response = json.loads(client.recv(timeout=remaining))
                if not isinstance(response, dict):
                    raise _blocked("native response is not an object")
                if response.get("id") == number:
                    result = response.get("result")
                    if not isinstance(result, dict) or "error" in response:
                        raise _blocked("native observation was refused")
                    return result
            raise _blocked("native observation exceeded its notification bound")

        call(
            1,
            "initialize",
            {"clientInfo": {"name": "worktree_controller_recovery", "version": "1"}},
        )
        client.send(json.dumps({"method": "initialized", "params": {}}))
        thread = call(2, "thread/read", {"threadId": holder.session_id, "includeTurns": False}).get(
            "thread"
        )
        loaded = call(3, "thread/loaded/list", {}).get("data")
        if not isinstance(thread, dict) or not isinstance(loaded, list):
            raise _blocked("native thread observation is incomplete")
        return {"thread": thread, "loaded": loaded, "peer_pid": pid}


def _archived_thread(holder: WriterHolder, observation: dict[str, Any]) -> dict[str, Any]:
    thread, loaded = observation["thread"], observation["loaded"]
    if (
        thread.get("id") != holder.session_id
        or thread.get("status") != {"type": "notLoaded"}
        or holder.session_id in loaded
    ):
        raise _blocked("the exact native thread must be unloaded")
    archive = Path.home() / ".codex/archived_sessions"
    raw_path = thread.get("path")
    if not isinstance(raw_path, str):
        raise _blocked("native archived rollout identity is missing")
    path = Path(raw_path)
    if not path.is_absolute() or not path.is_relative_to(archive):
        raise _blocked("the exact native thread has not been archived")
    if path.resolve(strict=True) != path or path.lstat().st_uid != os.getuid():
        raise _blocked("native archived rollout authority is invalid")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise _blocked("native archived rollout is not a regular file")
    with path.open("rb") as stream:
        first = stream.readline(262_145)
    if len(first) > 262_144:
        raise _blocked("native session metadata exceeds its bound")
    record = json.loads(first)
    if (
        not isinstance(record, dict)
        or record.get("type") != "session_meta"
        or not isinstance(record.get("payload"), dict)
        or record["payload"].get("id") != holder.session_id
    ):
        raise _blocked("native archived session metadata identity differs")
    return {
        "thread_id": holder.session_id,
        "status": "notLoaded",
        "archived_rollout": str(path),
        "session_metadata_sha256": hashlib.sha256(first).hexdigest(),
        "other_loaded_thread_count": len(loaded),
        "peer_pid": observation["peer_pid"],
    }


def observe_archived_session(target: Path, holder: WriterHolder) -> dict[str, Any]:
    """Prove a native terminal boundary and no remaining references to this checkout."""
    try:
        _shared_daemon(holder)
        native = _archived_thread(holder, _native_observation(holder))
        references = runtime_authority_evidence(target)
        if any(
            references[key]
            for key in (
                "managed_references",
                "observed_references",
                "managed_visibility_uncertainties",
                "scan_uncertainties",
            )
        ):
            raise _blocked("checkout use or uncertain runtime references remain")
        _shared_daemon(holder)
        return {
            "contract": CONTRACT,
            "holder": asdict(holder),
            "native": native,
            "runtime_authority": references,
            "processes_stopped": 0,
            "threads_archived": 0,
        }
    except ControllerError:
        raise
    except Exception as exc:
        # No native response, rollout contents, connection details or credentials in errors.
        raise _blocked(f"native recovery observation failed ({type(exc).__name__})") from None
