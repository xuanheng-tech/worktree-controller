"""Prove independent keyed-document records from frozen Git objects, never working files.

Rules are declared per repository in ``.agents/worktree-policy.toml`` under
``[keyed_documents]`` (disabled by default, preserving the ordinary file fence).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .git_backend import _git
from .models import ControllerError, KeyedDocumentRule
from .policy import load_policy

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


def _document(raw: bytes, rule: KeyedDocumentRule) -> dict[str, object]:
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    collections = dict(rule.collections)
    allowed = {*collections, *rule.extra_fields, "note"}
    if rule.schema_version is not None:
        allowed.add("schema_version")
        if type(value.get("schema_version")) is not int or value.get("schema_version") != rule.schema_version:
            raise ValueError("unsupported keyed document")
    else:
        allowed.add("schema_version")
    if rule.repo_id is not None:
        allowed.add("repo_id")
        if value.get("repo_id") != rule.repo_id:
            raise ValueError("unsupported keyed document")
    else:
        allowed.add("repo_id")
    if not isinstance(value, dict) or set(value) - allowed:
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


def _changed_keys(before: bytes, after: bytes, rule: KeyedDocumentRule) -> set[tuple[str, str]]:
    old, new = _document(before, rule), _document(after, rule)
    collections = dict(rule.collections)
    if json.dumps(
        {k: v for k, v in old.items() if k not in collections}, sort_keys=True
    ) != json.dumps({k: v for k, v in new.items() if k not in collections}, sort_keys=True):
        raise ValueError("document metadata changed")
    touched: set[tuple[str, str]] = set()
    for collection, identity_key in collections.items():
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


def _progress(repo: Path, commit: str, rule: KeyedDocumentRule) -> tuple[str, bytes]:
    attributes = _blob(repo, commit, ".gitattributes", limit=64 * 1024).decode("utf-8")
    if f"{rule.path} merge={rule.merge_driver}" not in attributes.splitlines():
        raise ValueError("keyed merge protocol is not declared")
    return attributes, _blob(repo, commit, rule.path)


def independent_progress_scopes(
    repo: Path,
    left_paths: tuple[str, ...],
    right_paths: tuple[str, ...],
    left_sources: tuple[str, ...],
    right_sources: tuple[str, ...],
    *,
    rules: tuple[KeyedDocumentRule, ...] | None = None,
) -> bool:
    """Unknown, broad or conflicting evidence retains the ordinary file fence.

    The immutable source commits already belong to the existing integration intents.
    Unique merge bases prove what each side actually changes, including source ancestry.
    """
    active_rules = load_policy(repo).keyed_documents if rules is None else rules
    if not active_rules:
        return False
    rule_by_path = {rule.path: rule for rule in active_rules}
    if not left_paths or not right_paths or not left_sources or not right_sources:
        return False
    overlaps = {
        (left, right)
        for left in left_paths
        for right in right_paths
        if left == right or left.startswith(right + "/") or right.startswith(left + "/")
    }
    if len(overlaps) != 1:
        return False
    path, other = next(iter(overlaps))
    if path != other or path not in rule_by_path:
        return False
    rule = rule_by_path[path]
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
                attributes, original = _progress(repo, base, rule)
                left_attributes, left_document = _progress(repo, left, rule)
                right_attributes, right_document = _progress(repo, right, rule)
                if attributes != left_attributes or attributes != right_attributes:
                    return False
                left_keys = _changed_keys(original, left_document, rule)
                right_keys = _changed_keys(original, right_document, rule)
                if left_keys & right_keys:
                    return False
        return True
    except (ControllerError, OSError, ValueError, RecursionError):
        return False
