"""Exclusive, FD-relative immutable cleanup action evidence."""

from pathlib import Path
from typing import Any

from . import hardlinks
from . import preservation as p
from .preserved_cleanup import _document
from .store import now_iso


def write(directory: Path, name: str, value: dict[str, Any]) -> dict[str, str]:
    with p._absolute_directory(directory, create=True) as chain:
        digest = p._write_once(chain[-1], name, value)
    return {"path": str(directory / name), "sha256": digest}


def read(path: Path, digest: str | None = None) -> tuple[dict[str, Any], str]:
    from .metadata_relocation import relocated

    path = relocated(path, digest)
    with p._absolute_directory(path.parent) as chain:
        value, actual = _document(chain[-1], path.name)
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
