"""Resolve the candidate's evidence through the Snapshot Runner read interface."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .models import ControllerError

_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_REPOSITORY_NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,127}")
_SNAPSHOT_READ_TIMEOUT_SECONDS = 60


def repository_display_name(name: str) -> str:
    """Mirror Snapshot Runner's portable repository display name sanitization."""
    if name not in {".", ".."} and _REPOSITORY_NAME_RE.fullmatch(name) is not None:
        return name
    if (
        not name
        or name in {".", ".."}
        or any(
            ord(character) < 32 or ord(character) == 127 or character in {"/", "\ufeff"}
            for character in name
        )
    ):
        raise ControllerError(
            "SNAPSHOT_CANDIDATE_MISMATCH", "checkout directory name cannot match Snapshot Runner"
        )
    try:
        digest = hashlib.sha256(name.encode("utf-8", errors="strict")).hexdigest()[:16]
    except UnicodeEncodeError as exc:
        raise ControllerError(
            "SNAPSHOT_CANDIDATE_MISMATCH", "checkout directory name cannot match Snapshot Runner"
        ) from exc
    prefix = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")[:64] or "repository"
    return f"{prefix}-{digest}"


def verify_candidate_snapshot(
    reference: str, checkout: Path, branch: str | None, candidate: str, base: str
) -> dict[str, Any]:
    bounded = reference.startswith("snapshot-runner:bounded:")
    digest = reference.removeprefix("snapshot-runner:bounded:" if bounded else "snapshot-runner:")
    if not _HEX64_RE.fullmatch(digest):
        raise ControllerError("INVALID_SNAPSHOT_RECEIPT", "expected a full Snapshot Runner SHA-256")
    if (
        not isinstance(branch, str)
        or not branch.strip()
        or branch.strip() != branch
        or "\x00" in branch
        or branch.startswith("refs/")
        or not isinstance(candidate, str)
        or not _HEX40_RE.fullmatch(candidate)
        or not isinstance(base, str)
        or not _HEX40_RE.fullmatch(base)
    ):
        raise ControllerError(
            "SNAPSHOT_CANDIDATE_MISMATCH",
            "candidate branch and exact commit identities are required for Snapshot verification",
        )
    resolved_checkout = checkout.resolve(strict=False)
    expected_repository = repository_display_name(resolved_checkout.name)
    fields = {
        "target_ref": f"refs/heads/{branch}",
        "target_head": candidate,
        "base_commit": base,
        "merge_base_commit": base,
    }
    try:
        read = subprocess.run(
            [
                "snapshot-runner",
                "read",
                digest,
                "--repo",
                str(checkout),
                "--fields",
                ",".join(fields),
            ],
            cwd="/",
            capture_output=True,
            check=True,
            timeout=_SNAPSHOT_READ_TIMEOUT_SECONDS,
        )
        result = json.loads(read.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ControllerError(
            "INVALID_SNAPSHOT_RECEIPT", "Snapshot Runner could not verify the candidate review"
        ) from exc
    if (
        not isinstance(result, dict)
        or result.get("evidence_schema_version") != 1
        or result.get("command") != "branch-review"
        or result.get("snapshot_id") != digest
        or result.get("repository") != expected_repository
        or result.get("evidence") != {"fields": fields}
    ):
        raise ControllerError(
            "SNAPSHOT_CANDIDATE_MISMATCH",
            "the review must cover this checkout, branch and exact candidate range",
        )
    truncated = result.get("truncated")
    gap = result.get("evidence_gap")
    status = result.get("status")
    if (
        type(truncated) is not bool
        or type(gap) is not bool
        or status not in {"partial", "complete"}
        or ((truncated or gap) and status != "partial")
    ):
        raise ControllerError(
            "INVALID_SNAPSHOT_RECEIPT", "invalid review coverage from Snapshot Runner"
        )
    incomplete = status == "partial" or truncated or gap
    if bounded != incomplete:
        raise ControllerError(
            "SNAPSHOT_COVERAGE_MISMATCH",
            "use snapshot-runner:bounded:<sha256> only after reviewing a truncated or incomplete snapshot's gaps",
        )
    return {"snapshot_id": digest, "status": status, "truncated": truncated, "evidence_gap": gap}
