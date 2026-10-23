"""A public, provider-neutral publication session under the producer's lock.

The consumer performs the Git push.  The Controller owns all metadata validation
and keeps its existing repository lock until it independently observes completion
or the bounded stdio session ends.  No private path or digest recipe crosses this
boundary, and the existing lease-complete command persists the lifecycle receipt.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .git_backend import _git, inventory, is_ancestor, validate_branch_name
from .lifecycle import _validate_prepared_publication_receipt
from .models import ControllerError
from .policy import load_policy
from .semantics import intent_for_allocation
from .semantics import managed_record as _managed_record
from .store import MetadataStore, now_iso

VERSION = 1
CONTRACT = "integration-publication-session/v1"
MAX_FRAME_BYTES = 65536
IDLE_TIMEOUT_SECONDS = 300
OID_PATTERN = re.compile(r"[0-9a-f]{40}")
REMOTE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _text(value: object, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ControllerError("INVALID_PUBLICATION_REQUEST", f"invalid {field}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerError("INVALID_PUBLICATION_REQUEST", "duplicate JSON key")
        result[key] = value
    return result


class SessionInput:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.pending = bytearray()

    def read(self) -> dict[str, Any]:
        deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(self.descriptor, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ControllerError(
                        "PUBLICATION_SESSION_TIMEOUT", "publication session timed out"
                    )
                chunk = os.read(self.descriptor, min(4096, MAX_FRAME_BYTES + 1 - len(self.pending)))
                if not chunk:
                    raise ControllerError("PUBLICATION_SESSION_CLOSED", "consumer disconnected")
                self.pending.extend(chunk)
                if len(self.pending) > MAX_FRAME_BYTES:
                    raise ControllerError(
                        "INVALID_PUBLICATION_REQUEST", "publication frame is too large"
                    )
        line, _, pending = self.pending.partition(b"\n")
        self.pending = bytearray(pending)
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ControllerError(
                "INVALID_PUBLICATION_REQUEST", "publication frame must be an object"
            )
        return value


def _emit(stream: TextIO, phase: str, **fields: Any) -> None:
    stream.write(
        json.dumps(
            {"schema_version": VERSION, "contract": CONTRACT, "phase": phase, **fields},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    stream.flush()


class PublicationSession:
    def __init__(self, repository: Path, candidate_oid: str, lease_id: str, run_id: str) -> None:
        if OID_PATTERN.fullmatch(candidate_oid) is None:
            raise ControllerError(
                "INVALID_PUBLICATION_REQUEST", "candidate must be a full commit OID"
            )
        try:
            if str(uuid.UUID(lease_id)) != lease_id:
                raise ValueError
        except ValueError as exc:
            raise ControllerError(
                "INVALID_PUBLICATION_REQUEST", "lease must be a canonical UUID"
            ) from exc
        self.repository = repository.expanduser().resolve(strict=True)
        self.candidate_oid = candidate_oid
        self.lease_id = lease_id
        self.run_id = _text(run_id, "run_id", maximum=256)
        initial = inventory(self.repository)
        self.store = MetadataStore(initial.common_dir)
        self.identity: dict[str, Any] | None = None

    def _guard(self) -> tuple[dict[str, Any], str]:
        if not self.store.flock_held:
            raise ControllerError(
                "PUBLICATION_SESSION_UNLOCKED", "publication session requires its lock"
            )
        repository = inventory(self.repository)
        policy = load_policy(repository.canonical_path)
        snapshot = self.store._load_unlocked()
        lease = snapshot.publication_lease
        if (
            lease is None
            or lease.lease_id != self.lease_id
            or lease.run_id != self.run_id
            or lease.candidate_oid != self.candidate_oid
            or lease.repository_id != snapshot.repository_id
        ):
            raise ControllerError("LEASE_MISMATCH", "publication lease identity differs")
        if (lease.remote, lease.branch) != (policy.publication_remote, policy.publication_branch):
            raise ControllerError(
                "PUBLICATION_POLICY_DRIFT", "publication target differs from policy"
            )
        item, record = _managed_record(repository, snapshot, self.repository)
        if (
            item.path.resolve(strict=True) != self.repository
            or self.repository == repository.canonical_path.resolve(strict=True)
            or item.detached
            or item.branch == lease.target_ref
            or not item.clean
            or item.head != lease.candidate_oid
            or record.allocation_id != lease.allocation_id
            or record.owner != lease.holder
            or record.role != "integration"
            or record.lifecycle != "INTEGRATING"
            or record.task_commit != lease.candidate_oid
            or record.handoff_commit != lease.candidate_oid
            or record.finalizer_commit != lease.candidate_oid
            or record.finalizer_record_id != lease.validation_evidence_id
        ):
            raise ControllerError(
                "PUBLICATION_EVIDENCE_MISMATCH", "leased candidate evidence differs"
            )
        intent = intent_for_allocation(snapshot, record.allocation_id)
        if intent is None or (
            intent.state != "PUBLISHING"
            or intent.publication_identity != lease.lease_id
            or intent.candidate_oid != lease.candidate_oid
            or intent.validated_oid != lease.candidate_oid
            or intent.validation_evidence_id != lease.validation_evidence_id
            or intent.base_main_oid != lease.expected_main_oid
        ):
            raise ControllerError(
                "PUBLICATION_EVIDENCE_MISMATCH", "publishing intent differs from lease"
            )
        _validate_prepared_publication_receipt(
            snapshot,
            intent,
            record,
            item,
            required=policy.policy_schema_version >= 4,
        )
        if not is_ancestor(item.path, lease.expected_main_oid, lease.candidate_oid):
            raise ControllerError(
                "PUBLICATION_EVIDENCE_MISMATCH", "candidate does not contain leased main"
            )
        if REMOTE_PATTERN.fullmatch(lease.remote) is None:
            raise ControllerError("INVALID_REMOTE", "publication remote is unsafe")
        validate_branch_name(item.path, lease.branch)
        if lease.target_ref != f"refs/heads/{lease.branch}":
            raise ControllerError("LEASE_MISMATCH", "publication target ref differs")
        fetch = _git(item.path, "remote", "get-url", "--all", lease.remote, check=False)
        push = _git(item.path, "remote", "get-url", "--push", "--all", lease.remote, check=False)
        if (
            fetch.returncode
            or push.returncode
            or not fetch.stdout.strip()
            or b"\n" in fetch.stdout.strip()
            or fetch.stdout.strip() != push.stdout.strip()
        ):
            raise ControllerError(
                "INVALID_REMOTE", "publication requires one identical fetch/push endpoint"
            )
        identity = {
            "repository_id": lease.repository_id,
            "allocation_id": lease.allocation_id,
            "lease_id": lease.lease_id,
            "run_id": lease.run_id,
            "remote": lease.remote,
            "target_ref": lease.target_ref,
            "candidate_oid": lease.candidate_oid,
            "expected_main_oid": lease.expected_main_oid,
            "validation_evidence_id": lease.validation_evidence_id,
            "record_version": record.record_version,
            "expires_at": lease.expires_at,
        }
        if self.identity is not None and identity != self.identity:
            raise ControllerError(
                "PUBLICATION_SESSION_DRIFT", "publication identity changed during session"
            )
        return identity, self._remote_oid(identity)

    def _remote_oid(self, identity: dict[str, Any]) -> str:
        result = _git(
            self.repository,
            "ls-remote",
            "--exit-code",
            "--refs",
            identity["remote"],
            identity["target_ref"],
            check=False,
        )
        lines = result.stdout.decode("utf-8", "strict").splitlines()
        if result.returncode or len(lines) != 1:
            raise ControllerError(
                "PUBLICATION_REMOTE_UNAVAILABLE", "live publication ref was not verified"
            )
        parts = lines[0].split("\t")
        if (
            len(parts) != 2
            or OID_PATTERN.fullmatch(parts[0]) is None
            or parts[1] != identity["target_ref"]
        ):
            raise ControllerError(
                "PUBLICATION_REMOTE_UNAVAILABLE", "live publication response is invalid"
            )
        return parts[0]

    def _action(self, identity: dict[str, Any], observed: str) -> str:
        if observed == self.candidate_oid:
            return "RECOVER_ALREADY_PUBLISHED"
        if datetime.fromisoformat(identity["expires_at"]) <= datetime.fromisoformat(now_iso()):
            raise ControllerError(
                "PUBLICATION_LEASE_EXPIRED", "publication lease expired before mutation"
            )
        if observed != identity["expected_main_oid"]:
            raise ControllerError(
                "PUBLICATION_REMOTE_DRIFT", "live main differs from leased expected OID"
            )
        return "PUSH_CANDIDATE"

    def verify(self) -> dict[str, Any]:
        identity, observed = self._guard()
        self.identity = identity
        return {
            "identity": identity,
            "action": self._action(identity, observed),
            "remote_oid": observed,
        }

    def authorize(self) -> dict[str, Any]:
        identity, observed = self._guard()
        return {
            "identity": identity,
            "action": self._action(identity, observed),
            "remote_oid": observed,
        }

    def complete(self, result: object) -> dict[str, Any]:
        if not isinstance(result, dict) or set(result) != {
            "record_id",
            "executor",
            "push_executed",
            "remote_oid",
        }:
            raise ControllerError("INVALID_PUBLICATION_REQUEST", "execution result fields differ")
        executor = result["executor"]
        if not isinstance(executor, dict) or set(executor) != {"name", "version"}:
            raise ControllerError("INVALID_PUBLICATION_REQUEST", "executor identity is required")
        _text(executor["name"], "executor.name", maximum=128)
        _text(executor["version"], "executor.version", maximum=128)
        record_id = _text(result["record_id"], "record_id")
        if type(result["push_executed"]) is not bool or result["remote_oid"] != self.candidate_oid:
            raise ControllerError(
                "INVALID_PUBLICATION_REQUEST", "execution result identity differs"
            )
        identity, observed = self._guard()
        if observed != self.candidate_oid:
            raise ControllerError(
                "PUBLICATION_NOT_OBSERVED", "remote does not equal the exact leased candidate"
            )
        return {
            "identity": identity,
            "record_id": record_id,
            "remote_verify": {"status": "verified", "oid": observed},
            "lifecycle_completion_required": True,
        }


def _request(document: dict[str, Any], action: str, identity: dict[str, Any]) -> None:
    fields = {"schema_version", "contract", "action", "identity"}
    if action == "complete":
        fields.add("result")
    supplied_identity = document.get("identity")
    same_identity = (
        isinstance(supplied_identity, dict)
        and set(supplied_identity) == set(identity)
        and all(
            type(supplied_identity[field]) is type(expected)
            and supplied_identity[field] == expected
            for field, expected in identity.items()
        )
    )
    if (
        set(document) != fields
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != VERSION
        or document["contract"] != CONTRACT
        or document["action"] != action
        or not same_identity
    ):
        raise ControllerError(
            "INVALID_PUBLICATION_REQUEST", "publication request identity or phase differs"
        )


def run_session(repository: Path, *, candidate_oid: str, lease_id: str, run_id: str) -> int:
    try:
        session = PublicationSession(repository, candidate_oid, lease_id, run_id)
        channel = SessionInput(sys.stdin.fileno())
        with session.store.lock(exclusive=True):
            verified = session.verify()
            _emit(sys.stdout, "verified", **verified)
            request = channel.read()
            _request(request, "authorize", verified["identity"])
            _emit(sys.stdout, "authorized", **session.authorize())
            request = channel.read()
            _request(request, "complete", verified["identity"])
            _emit(sys.stdout, "completed", **session.complete(request["result"]))
        return 0
    except (ControllerError, OSError, UnicodeError, ValueError) as exc:
        with suppress(BrokenPipeError):
            _emit(
                sys.stdout,
                "blocked",
                code=getattr(exc, "code", "PUBLICATION_SESSION_FAILED"),
                reason=str(exc)[:512],
            )
        return 1
