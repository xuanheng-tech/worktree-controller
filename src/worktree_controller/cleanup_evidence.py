"""FD-relative immutable evidence reads and exclusive cleanup action evidence."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

from . import hardlinks
from . import preservation as p
from .models import ControllerError
from .store import MetadataStore, now_iso

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
COMPRESS_THRESHOLD_BYTES = 65536


def _decode_evidence_bytes(raw: bytes) -> dict[str, Any]:
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            uncompressed = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(uncompressed) > MAX_EVIDENCE_BYTES:
            raise ControllerError(
                "PRESERVATION_COVERAGE_INVALID", "preservation evidence exceeds its bound"
            )
        value = json.loads(uncompressed)
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "invalid preservation document")
    return value


def _write_once_bytes(parent: p._Directory, name: str, data: bytes) -> str:
    parent.check()
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent.fd,
    )
    try:
        expected = p._inode(os.fstat(fd))
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(data)
            stream.flush()
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        if p._inode(os.stat(name, dir_fd=parent.fd, follow_symlinks=False)) != expected:
            raise p._fail("PRESERVATION_DESTINATION_CHANGED", "evidence file replaced")
        parent.check()
        os.fsync(parent.fd)
    finally:
        os.close(fd)
    return hashlib.sha256(data).hexdigest()


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
    value = _decode_evidence_bytes(raw)
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
    data = p._json_bytes(value)
    if len(data) > COMPRESS_THRESHOLD_BYTES:
        data = gzip.compress(data, compresslevel=6, mtime=0)
    with p._absolute_directory(directory, create=True) as chain:
        digest = _write_once_bytes(chain[-1], name, data)
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
