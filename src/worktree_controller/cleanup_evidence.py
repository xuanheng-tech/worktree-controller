"""FD-relative immutable evidence reads and exclusive cleanup action evidence."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import hardlinks
from . import preservation as p
from .models import ControllerError
from .store import MetadataStore, now_iso

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def read_document(directory: p._Directory, name: str) -> tuple[dict[str, Any], str]:
    """Read exact FD-bound bytes, retaining the existing preservation refusals."""
    metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    if metadata.st_size > MAX_EVIDENCE_BYTES:
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "preservation evidence exceeds its bound")
    with (
        p._regular_fd(directory.fd, name, p._identity(metadata)) as fd,
        os.fdopen(os.dup(fd), "rb") as stream,
    ):
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "preservation evidence exceeds its bound")
    directory.check()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "invalid preservation document")
    return value, hashlib.sha256(raw).hexdigest()


def completion_for_allocation(
    store: MetadataStore, path: Path, allocation: str,
) -> tuple[dict[str, Any], str] | None:
    """Reuse only unrelated scope, never a receipt or a validation verdict.

    Cleanup rechecks its plan between actions. Large unrelated completions need
    not be decoded on each check while the same repository flock is held. Pin
    and recheck their complete inode identity even on a negative scope hit;
    relevant bytes and every preservation generation still receive full checks.
    """
    with p._absolute_directory(path.parent) as chain:
        directory = chain[-1]
        identity = p._identity(os.stat(path.name, dir_fd=directory.fd, follow_symlinks=False))
        scopes = store._completion_scopes
        previous = None if scopes is None else scopes.get(path)
        if previous is not None and previous[0] == tuple(identity) and previous[1] != allocation:
            with p._regular_fd(directory.fd, path.name, identity):
                return None
        receipt, raw_sha = read_document(directory, path.name)
        if scopes is not None:
            scope = receipt.get("plan", {}).get("allocation_id")
            # Allocation IDs are canonical UUIDs. Unknown larger/nonstring values
            # cannot match one; do not retain arbitrary payloads in this small map.
            scopes[path] = (
                tuple(identity), scope if isinstance(scope, str) and len(scope) == 36 else None
            )
        return receipt, raw_sha


def write(directory: Path, name: str, value: dict[str, Any]) -> dict[str, str]:
    with p._absolute_directory(directory, create=True) as chain:
        digest = p._write_once(chain[-1], name, value)
    return {"path": str(directory / name), "sha256": digest}


def read(path: Path, digest: str | None = None) -> tuple[dict[str, Any], str]:
    from .metadata_relocation import relocated

    path = relocated(path, digest)
    with p._absolute_directory(path.parent) as chain:
        value, actual = read_document(chain[-1], path.name)
    if digest is not None and actual != digest:
        raise hardlinks._blocked("immutable cleanup evidence bytes changed")
    return value, actual


def matches_reference(ref: dict, expected: Path) -> bool:
    from .metadata_relocation import relocated

    return (
        bool(ref.get("sha256")) and relocated(Path(ref.get("path", "")), ref["sha256"]) == expected
    )


def before(directory: Path, step: int, resource: str, topology: dict[str, Any]) -> dict[str, str]:
    return write(
        directory,
        f"{step:04d}-before.json",
        {
            "contract": hardlinks.mutation_contract(topology),
            "phase": "BEFORE",
            "execution_state": "ACTION_STARTED",
            "resource": resource,
            "topology": topology,
            "timestamp": now_iso(),
        },
    )


def after(
    directory: Path,
    step: int,
    resource: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    before_receipt: dict[str, str],
    external_survivors: dict[str, Any] | None = None,
) -> dict[str, str]:
    mutation = hardlinks.transition(previous, current, resource, external_survivors)
    return write(
        directory,
        f"{step:04d}-after.json",
        {
            "contract": mutation["contract"],
            "phase": "COMPLETED",
            "execution_state": "ACTION_COMPLETED",
            "resource": resource,
            "topology": current,
            "before": before_receipt,
            "mutation": mutation,
            "timestamp": now_iso(),
        },
    )
