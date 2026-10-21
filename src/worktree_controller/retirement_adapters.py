"""Compatibility adapters between consumer wire formats and the neutral schema.

Core governance speaks `retirement_execution` only.  These adapters translate the
released 1.5.0/1.6.0 consumer vocabulary so existing integrations keep working
while nothing in the governance path depends on one consumer product.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import retirement_execution as execution
from .models import ControllerError

GIT_FINALIZER_CONTRACT = "git-finalizer-summary/v1"

_SUMMARY_OUTCOMES = {
    "LOCAL_BRANCH_RETIRED_VERIFIED": (
        execution.KIND_CAS_DELETE,
        execution.REF_REMOVED_AT_EXPECTED_OID,
    ),
    "REMOTE_BRANCH_RETIRED_VERIFIED": (
        execution.KIND_CAS_DELETE,
        execution.REF_REMOVED_AT_EXPECTED_OID,
    ),
    "ALREADY_ABSENT_VERIFIED": (execution.KIND_ATTEST_ABSENT, execution.REF_ALREADY_ABSENT),
}


def _ref(operation: str, result: Mapping[str, Any]) -> str | None:
    branch = result.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    if operation == "local":
        return f"refs/heads/{branch}"
    remote = result.get("remote")
    return f"refs/remotes/{remote}/{branch}" if isinstance(remote, str) and remote else None


def load_summary(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ControllerError("INVALID_FINALIZER_SUMMARY", f"cannot read summary: {exc}") from exc
    import json

    try:
        summary = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControllerError("INVALID_FINALIZER_SUMMARY", f"cannot read summary: {exc}") from exc
    if not isinstance(summary, dict):
        raise ControllerError("INVALID_FINALIZER_SUMMARY", "summary must be a JSON object")
    return raw, summary


def from_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one released consumer summary into a neutral execution result.

    Unrecognised shapes fail with the released error code so 1.5.0/1.6.0 callers
    keep their existing failure contract.
    """

    status = summary.get("status")
    mode = summary.get("mode")
    result = summary.get("mode_result")
    if status != "success" or not isinstance(mode, str) or not isinstance(result, Mapping):
        raise ControllerError(
            "FINALIZER_RESULT_MISMATCH", "Finalizer summary does not prove this plan"
        )
    operation = {"retire_local_branch": "local", "retire_remote_branch": "remote"}.get(mode)
    if operation is None:
        raise ControllerError(
            "FINALIZER_RESULT_MISMATCH", "Finalizer summary does not prove this plan"
        )
    reported = result.get("result")
    if reported not in _SUMMARY_OUTCOMES:
        raise ControllerError(
            "FINALIZER_RESULT_MISMATCH", "Finalizer summary does not prove this plan"
        )
    kind, outcome = _SUMMARY_OUTCOMES[reported]
    executor_version = summary.get("finalizer_version")
    return {
        "schema_version": execution.RESULT_SCHEMA_VERSION,
        "contract": execution.RESULT_CONTRACT,
        "plan_id": result.get("retirement_plan_id"),
        "operation": operation,
        "kind": kind,
        "outcome": outcome,
        "ref": _ref(operation, result),
        "expected_oid": result.get("expected_oid"),
        "integrated_ref": result.get("integrated_ref"),
        "expected_integrated_oid": result.get("integrated_into_oid"),
        "performed_at": summary.get("finished_at") or summary.get("started_at"),
        "executor": {
            "name": GIT_FINALIZER_CONTRACT,
            "version": executor_version if isinstance(executor_version, str) else "unknown",
        },
        "evidence": {"remote": result.get("remote")},
    }


def arguments(plan: Mapping[str, Any], operation: str, *, repository_root: Path) -> list[str]:
    """The released consumer invocation for one authorized retirement operation."""

    expected_flag = "--expected-local-oid" if operation == "local" else "--expected-remote-oid"
    return [
        "git-finalize",
        f"--retire-{operation}-branch",
        str(plan["branch"]),
        "--remote",
        str(plan["remote"]),
        "--integrated-into",
        str(plan["integrated_into"]),
        expected_flag,
        str(plan["expected_oid"]),
        "--expected-integrated-oid",
        str(plan["expected_integrated_oid"]),
        "--retirement-plan-id",
        str(plan["plan_id"]),
        "--repo",
        str(repository_root),
        "--summary",
    ]


def summary_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
