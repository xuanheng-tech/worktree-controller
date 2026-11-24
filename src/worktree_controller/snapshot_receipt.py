"""Resolve the candidate's evidence through the Snapshot Runner read interface."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .models import ControllerError


def verify_candidate_snapshot(
    reference: str, checkout: Path, branch: str | None, candidate: str, base: str
) -> dict[str, Any]:
    bounded = reference.startswith("snapshot-runner:bounded:")
    digest = reference.removeprefix("snapshot-runner:bounded:" if bounded else "snapshot-runner:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ControllerError("INVALID_SNAPSHOT_RECEIPT", "expected a full Snapshot Runner SHA-256")
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
        )
        result = json.loads(read.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ControllerError(
            "INVALID_SNAPSHOT_RECEIPT", "Snapshot Runner could not verify the candidate review"
        ) from exc
    if (
        not isinstance(result, dict)
        or result.get("evidence_schema_version") != 1
        or result.get("command") != "branch-review"
        or result.get("snapshot_id") != digest
        or result.get("repository") != checkout.name
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
        or (truncated and status != "partial")
    ):
        raise ControllerError(
            "INVALID_SNAPSHOT_RECEIPT", "invalid review coverage from Snapshot Runner"
        )
    if bounded != (status == "partial" or gap):
        raise ControllerError(
            "SNAPSHOT_COVERAGE_MISMATCH",
            "use snapshot-runner:bounded:<sha256> only after reviewing a truncated or incomplete snapshot's gaps",
        )
    return {"snapshot_id": digest, "status": status, "truncated": truncated, "evidence_gap": gap}
