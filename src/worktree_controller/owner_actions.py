"""Structured requests for the recorded task owner; never execution authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT = "checkout-owner-action/v1"


def owner_action(repository_path: str, item: dict[str, Any], *, reason: str,
                 required_actions: tuple[str, ...]) -> dict[str, Any]:
    """Bind a continuation request to the exact allocation, owner, HEAD and CAS.

    A Task consumer must match its existing binding and re-observe these facts
    before restoring its owner. The request itself grants no writer or cleanup.
    """
    request = {
        "contract": CONTRACT,
        "repository_path": repository_path,
        **{key: item.get(key) for key in
           ("allocation_id", "task_key", "authority_key", "owner", "path", "branch", "head")},
        "expected_record_version": item.get("record_version"),
        "reason": reason,
        "required_actions": list(required_actions),
        "automatic_discard": False,
        "execution_authority": False,
    }
    request["action_id"] = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    request["inspect_argv"] = ["worktree-controller", "status", "--repo", repository_path, "--json"]
    return request
