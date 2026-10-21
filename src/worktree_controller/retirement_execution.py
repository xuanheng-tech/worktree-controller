"""Provider-neutral vocabulary for authorized retirement execution.

The Controller owns the decision; a consumer owns the ref mutation.  These are the
two documents that cross that boundary: an *authorization* the Controller issues
and a *result* the consumer reports back.  Neither one names a consumer product,
and neither one is trusted for what it claims about Git state — the Controller
re-observes that itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import ControllerError

AUTHORIZATION_SCHEMA_VERSION = 1
AUTHORIZATION_CONTRACT = "branch-retirement-execution/v1"

RESULT_SCHEMA_VERSION = 1
RESULT_CONTRACT = "retirement-execution-result/v1"

#: The plan publishes this recipe so a consumer knows what it may be granted to
#: compare-and-delete without importing any Controller helper.
RECIPE_SCHEMA_VERSION = 1
RECIPE_CONTRACT = "retirement-execution-recipe/v1"

KIND_CAS_DELETE = "CAS_DELETE"
KIND_ATTEST_ABSENT = "ATTEST_ABSENT"
EXECUTION_KINDS = frozenset({KIND_CAS_DELETE, KIND_ATTEST_ABSENT})

REF_REMOVED_AT_EXPECTED_OID = "REF_REMOVED_AT_EXPECTED_OID"
REF_ALREADY_ABSENT = "REF_ALREADY_ABSENT"
REF_STILL_PRESENT = "REF_STILL_PRESENT"
EXECUTION_FAILED = "EXECUTION_FAILED"
OUTCOMES = frozenset(
    {REF_REMOVED_AT_EXPECTED_OID, REF_ALREADY_ABSENT, REF_STILL_PRESENT, EXECUTION_FAILED}
)
SUCCESS_OUTCOMES = frozenset({REF_REMOVED_AT_EXPECTED_OID, REF_ALREADY_ABSENT})

#: The authorization identity is content-addressed over exactly these fields, in
#: this order.  ``token`` is deliberately excluded so the secret capability cannot
#: change the authorization's own address.
AUTHORIZATION_IDENTITY_FIELDS = (
    "schema_version",
    "contract",
    "request_id",
    "plan_id",
    "operation",
    "kind",
    "repository_id",
    "allocation_id",
    "branch",
    "ref",
    "expected_oid",
    "integrated_ref",
    "expected_integrated_oid",
    "tracking_ref",
    "observed",
    "controller_state_digest",
    "holder_sha256",
    "issued_at",
    "expires_at",
)

#: Every result document carries these; they are the values the Controller can
#: check against its own plan and against Git afterwards.
RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "contract",
        "plan_id",
        "operation",
        "kind",
        "outcome",
        "ref",
        "expected_oid",
        "expected_integrated_oid",
        "executor",
    }
)
#: Optional because a released consumer summary predates them; when present they are
#: validated, and the Controller never infers a missing one.
RESULT_OPTIONAL_FIELDS = frozenset({"integrated_ref", "performed_at", "evidence"})

RECIPE_FIELDS = frozenset(
    {"contract", "schema_version", "grant", "settle", "result_contract", "operations"}
)
RECIPE_OPERATION_FIELDS = frozenset(
    {
        "ref",
        "mutation",
        "expected_oid",
        "integrated_ref",
        "expected_integrated_oid",
        "tracking_ref",
        "kind",
    }
)

OBSERVATION_FIELDS = frozenset(
    {"refs", "checked_out_paths", "upstream_ref", "upstream_target", "classification", "blockers"}
)

_HEX = frozenset("0123456789abcdef")


def identity(document: Mapping[str, Any]) -> dict[str, Any]:
    """The authorization fields that must match at settle time."""

    return {field: document.get(field) for field in AUTHORIZATION_IDENTITY_FIELDS}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerError("INVALID_EXECUTION_RESULT", f"{field} must be a non-empty string")
    return value


def _object_id(value: object, field: str) -> str:
    """Accept either Git object ID length; the repository decides which one it uses."""

    text = _text(value, field)
    if len(text) not in {40, 64} or not _HEX.issuperset(text):
        raise ControllerError("INVALID_EXECUTION_RESULT", f"{field} must be a Git object id")
    return text


def _digest(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or not _HEX.issuperset(text):
        raise ControllerError("INVALID_EXECUTION_RESULT", f"{field} must be a sha256 hex digest")
    return text


def normalize_result(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one execution result document and return it with checked types.

    A well-formed document proves nothing about the world: the caller must still
    observe the ref.  This only guarantees every field the observation is compared
    against is present and shaped.
    """

    unknown = sorted(set(document) - RESULT_FIELDS - RESULT_OPTIONAL_FIELDS)
    if unknown:
        raise ControllerError("INVALID_EXECUTION_RESULT", f"unknown result fields: {unknown}")
    if not RESULT_FIELDS.issubset(document):
        raise ControllerError(
            "INVALID_EXECUTION_RESULT",
            f"execution result is missing {sorted(RESULT_FIELDS - set(document))}",
        )
    if document.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ControllerError(
            "INVALID_EXECUTION_RESULT",
            f"unsupported result schema {document.get('schema_version')!r}",
        )
    if document.get("contract") != RESULT_CONTRACT:
        raise ControllerError(
            "INVALID_EXECUTION_RESULT", f"unknown result contract {document.get('contract')!r}"
        )
    operation = _text(document.get("operation"), "operation")
    if operation not in {"local", "remote"}:
        raise ControllerError("INVALID_EXECUTION_RESULT", "operation must be local or remote")
    kind = _text(document.get("kind"), "kind")
    if kind not in EXECUTION_KINDS:
        raise ControllerError("INVALID_EXECUTION_RESULT", f"unknown execution kind {kind!r}")
    outcome = _text(document.get("outcome"), "outcome")
    if outcome not in OUTCOMES:
        raise ControllerError("INVALID_EXECUTION_RESULT", f"unknown outcome {outcome!r}")
    executor = document.get("executor")
    if not isinstance(executor, Mapping):
        raise ControllerError("INVALID_EXECUTION_RESULT", "executor must be an object")
    evidence = document.get("evidence")
    if evidence is not None and not isinstance(evidence, Mapping):
        raise ControllerError("INVALID_EXECUTION_RESULT", "evidence must be an object or null")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract": RESULT_CONTRACT,
        "plan_id": _digest(document.get("plan_id"), "plan_id"),
        "operation": operation,
        "kind": kind,
        "outcome": outcome,
        "ref": _text(document.get("ref"), "ref"),
        "expected_oid": _object_id(document.get("expected_oid"), "expected_oid"),
        "integrated_ref": (
            _text(document["integrated_ref"], "integrated_ref")
            if document.get("integrated_ref") is not None
            else None
        ),
        "expected_integrated_oid": _object_id(
            document.get("expected_integrated_oid"), "expected_integrated_oid"
        ),
        "performed_at": (
            _text(document["performed_at"], "performed_at")
            if document.get("performed_at") is not None
            else None
        ),
        "executor": {
            "name": _text(executor.get("name"), "executor.name"),
            "version": _text(executor.get("version"), "executor.version"),
        },
        "evidence": dict(evidence or {}),
    }


def normalize_observation(value: object) -> dict[str, Any]:
    """Return an observation vector with a stable shape, or fail closed."""

    if not isinstance(value, Mapping) or not OBSERVATION_FIELDS.issubset(value):
        raise ControllerError("INVALID_METADATA", "retirement observation vector is incomplete")
    refs = value.get("refs")
    if not isinstance(refs, Mapping):
        raise ControllerError("INVALID_METADATA", "retirement observation refs must be an object")
    paths = value.get("checked_out_paths")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise ControllerError("INVALID_METADATA", "retirement observation paths must be a list")
    blockers = value.get("blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        raise ControllerError("INVALID_METADATA", "retirement observation blockers must be a list")
    return {
        "refs": {
            key: (oid if oid is None or isinstance(oid, str) else None)
            for key, oid in sorted(refs.items())
        },
        "checked_out_paths": sorted(paths),
        "upstream_ref": value.get("upstream_ref"),
        "upstream_target": value.get("upstream_target"),
        "classification": value.get("classification"),
        "blockers": sorted(blockers),
    }


def succeeded(result: Mapping[str, Any]) -> bool:
    return result.get("outcome") in SUCCESS_OUTCOMES
