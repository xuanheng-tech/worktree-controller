"""Prove independent Handbook records from frozen Git objects, never working files.

This refines only the known keyed-document overlap. It does not choose merge results,
change declared ownership, or replace candidate validation and main publication CAS.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .git_backend import _git
from .models import ControllerError

PROGRESS_PATH = "docs/handbook/business-progress.json"
_ATTRIBUTE = f"{PROGRESS_PATH} merge=qp-handbook-progress"
_COLLECTIONS = {
    "tasks": "authority_key",
    "development_lines": "id",
    "modules": "id",
    "decisions": "id",
}
_MAX_BYTES = 2 * 1024 * 1024
_OID = re.compile(r"[0-9a-f]{40}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def _document(raw: bytes) -> dict[str, object]:
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or value.get("repo_id") != "quant-platform"
        or set(value) - {*_COLLECTIONS, "schema_version", "repo_id", "note"}
    ):
        raise ValueError("unsupported keyed document")
    return value


def _rows(value: object, identity_key: str) -> dict[str, object]:
    if not isinstance(value, list):
        raise ValueError("expected keyed array")
    rows: dict[str, object] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("expected keyed object")
        identity = row.get(identity_key)
        if not isinstance(identity, str) or not identity.strip() or identity in rows:
            raise ValueError("missing or duplicate identity")
        rows[identity] = row
    return rows


def _changed_keys(before: bytes, after: bytes) -> set[tuple[str, str]]:
    old, new = _document(before), _document(after)
    if json.dumps(
        {k: v for k, v in old.items() if k not in _COLLECTIONS}, sort_keys=True
    ) != json.dumps({k: v for k, v in new.items() if k not in _COLLECTIONS}, sort_keys=True):
        raise ValueError("document metadata changed")
    touched: set[tuple[str, str]] = set()
    for collection, identity_key in _COLLECTIONS.items():
        left = _rows(old.get(collection, []), identity_key)
        right = _rows(new.get(collection, []), identity_key)
        if [key for key in left if key in right] != [key for key in right if key in left]:
            raise ValueError("existing records reordered")
        touched.update(
            (collection, key)
            for key in left.keys() | right.keys()
            if json.dumps(left.get(key), sort_keys=True)
            != json.dumps(right.get(key), sort_keys=True)
        )
    return touched


def _blob(repo: Path, commit: str, path: str, *, limit: int = _MAX_BYTES) -> bytes:
    entry = _git(repo, "ls-tree", "-z", commit, "--", path).stdout
    # Only a regular, tracked blob at the exact path can declare this protocol.
    mode, kind, remainder = entry.split(b" ", 2)
    oid, actual_path = remainder.split(b"\t", 1)
    if mode != b"100644" or kind != b"blob" or actual_path != path.encode() + b"\0":
        raise ValueError("unsupported Git document entry")
    name = oid.decode("ascii")
    if not _OID.fullmatch(name):
        raise ValueError("invalid blob identity")
    size = int(_git(repo, "cat-file", "-s", name).stdout)
    if size < 0 or size > limit:
        raise ValueError("document exceeds scope inspection limit")
    raw = _git(repo, "cat-file", "blob", name).stdout
    if len(raw) != size:
        raise ValueError("blob size changed")
    return raw


def _progress(repo: Path, commit: str) -> tuple[str, bytes]:
    attributes = _blob(repo, commit, ".gitattributes", limit=64 * 1024).decode("utf-8")
    if _ATTRIBUTE not in attributes.splitlines():
        raise ValueError("keyed merge protocol is not declared")
    return attributes, _blob(repo, commit, PROGRESS_PATH)


def independent_progress_scopes(
    repo: Path,
    left_paths: tuple[str, ...],
    right_paths: tuple[str, ...],
    left_sources: tuple[str, ...],
    right_sources: tuple[str, ...],
) -> bool:
    """Unknown, broad or conflicting evidence retains the ordinary file fence.

    The immutable source commits already belong to the existing integration intents.
    Unique merge bases prove what each side actually changes, including source ancestry.
    Old queued intents can use this proof without replacing their frozen evidence.
    """
    if not left_paths or not right_paths or not left_sources or not right_sources:
        return False
    overlaps = {
        (left, right)
        for left in left_paths
        for right in right_paths
        if left == right or left.startswith(right + "/") or right.startswith(left + "/")
    }
    if overlaps != {(PROGRESS_PATH, PROGRESS_PATH)}:
        return False
    if len(left_sources) > 8 or len(right_sources) > 8:
        return False
    if any(not _OID.fullmatch(source) for source in (*left_sources, *right_sources)):
        return False
    try:
        for left in left_sources:
            for right in right_sources:
                bases = _git(repo, "merge-base", "--all", left, right).stdout.splitlines()
                if len(bases) != 1:
                    return False
                base = bases[0].decode("ascii")
                if not _OID.fullmatch(base) or base in (left, right):
                    return False
                attributes, original = _progress(repo, base)
                left_attributes, left_document = _progress(repo, left)
                right_attributes, right_document = _progress(repo, right)
                if attributes != left_attributes or attributes != right_attributes:
                    return False
                left_keys = _changed_keys(original, left_document)
                right_keys = _changed_keys(original, right_document)
                if left_keys & right_keys:
                    return False
        return True
    except (ControllerError, OSError, ValueError, RecursionError):
        return False
