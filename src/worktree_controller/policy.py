"""Load and validate repository worktree policy."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .models import CANONICAL_ROLES, ControllerError, ConvergencePolicy, WorktreePolicy

EXPECTED_TOP_LEVEL = {
    "schema_version",
    "limits",
    "canonical",
    "slots",
    "review",
    "authority",
}
EXPECTED_TOP_LEVEL_V2 = EXPECTED_TOP_LEVEL | {"publication"}
EXPECTED_TOP_LEVEL_V3 = EXPECTED_TOP_LEVEL_V2 | {"release"}
EXPECTED_TOP_LEVEL_V4 = EXPECTED_TOP_LEVEL_V3
EXPECTED_TOP_LEVEL_V5 = EXPECTED_TOP_LEVEL_V4
EXPECTED_TOP_LEVEL_V6 = EXPECTED_TOP_LEVEL_V5 | {"reconciler"}
EXPECTED_TOP_LEVEL_V7 = EXPECTED_TOP_LEVEL_V6 | {"admission"}
EXPECTED_TOP_LEVEL_V8 = EXPECTED_TOP_LEVEL_V7 | {"convergence"}
CONVERGENCE_FIELDS = {
    "enabled",
    "grace_minutes",
    "idle_hours_max",
    "idle_hours_min",
    "standing_reserve",
}
# Optional [convergence] switches; an absent switch keeps the conservative default.
CONVERGENCE_OPTIONAL_FIELDS = {"release_unintegrated_handoffs"}


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ControllerError("INVALID_POLICY", f"policy table [{name}] is required")
    return value


def _positive_int(table: dict[str, Any], name: str) -> int:
    value = table.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ControllerError("INVALID_POLICY", f"policy {name} must be a positive integer")
    return value


def _nonempty_string(table: dict[str, Any], name: str) -> str:
    value = table.get(name)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ControllerError("INVALID_POLICY", f"policy {name} must be a non-empty string")
    return value.strip()


def _boolean(table: dict[str, Any], name: str) -> bool:
    value = table.get(name)
    if not isinstance(value, bool):
        raise ControllerError("INVALID_POLICY", f"policy {name} must be boolean")
    return value


def default_policy(canonical_path: Path) -> WorktreePolicy:
    return WorktreePolicy(
        path=canonical_path / ".agents" / "worktree-policy.toml",
        present=False,
        policy_schema_version=1,
        normal_limit=5,
        hard_limit=6,
        canonical_protected=True,
        canonical_allowed_roles=CANONICAL_ROLES,
        integration_candidate_limit=1,
        integration_prepare_limit=1,
        feature_audit_slots=3,
        emergency_slots=1,
        recovery_reserve=0,
        retained_slots=0,
        reconciler_enabled=False,
        reconciler_actor="lifecycle-reconciler",
        reconciler_legacy_adoption=False,
        main_publish_limit=1,
        publication_remote="origin",
        publication_branch="main",
        automatic_feature_audit_release=False,
        automatic_integration_continuation=False,
        preserved_auto_release=False,
        release_intent_ttl_seconds=3600,
        stale_warning_days=7,
        require_adoption=True,
        require_task_key=True,
        require_authority_key=True,
        unique_active_feature_authority=True,
    )


def load_policy(canonical_path: Path) -> WorktreePolicy:
    path = canonical_path / ".agents" / "worktree-policy.toml"
    if not path.exists():
        return default_policy(canonical_path)
    if not path.is_file() or path.is_symlink():
        raise ControllerError("INVALID_POLICY", f"policy is not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ControllerError("INVALID_POLICY", f"cannot read policy: {exc}") from exc
    return parse_policy(canonical_path, data)


def parse_policy(canonical_path: Path, data: dict[str, Any]) -> WorktreePolicy:
    """Validate policy data read from the checkout or a pinned publication tree."""
    path = canonical_path / ".agents" / "worktree-policy.toml"
    schema_version = data.get("schema_version")
    if schema_version == 1:
        if set(data) != EXPECTED_TOP_LEVEL:
            raise ControllerError("INVALID_POLICY", "policy schema v1 fields are invalid")
    elif schema_version == 2:
        if set(data) != EXPECTED_TOP_LEVEL_V2:
            raise ControllerError("INVALID_POLICY", "policy schema v2 fields are invalid")
    elif schema_version == 3:
        if set(data) != EXPECTED_TOP_LEVEL_V3:
            raise ControllerError("INVALID_POLICY", "policy schema v3 fields are invalid")
    elif schema_version == 4:
        if set(data) != EXPECTED_TOP_LEVEL_V4:
            raise ControllerError("INVALID_POLICY", "policy schema v4 fields are invalid")
    elif schema_version == 5:
        if set(data) != EXPECTED_TOP_LEVEL_V5:
            raise ControllerError("INVALID_POLICY", "policy schema v5 fields are invalid")
    elif schema_version == 6:
        if set(data) != EXPECTED_TOP_LEVEL_V6:
            raise ControllerError("INVALID_POLICY", "policy schema v6 fields are invalid")
    elif schema_version == 7:
        if set(data) != EXPECTED_TOP_LEVEL_V7:
            raise ControllerError("INVALID_POLICY", "policy schema v7 fields are invalid")
    elif schema_version == 8:
        if set(data) != EXPECTED_TOP_LEVEL_V8:
            raise ControllerError("INVALID_POLICY", "policy schema v8 fields are invalid")
    else:
        raise ControllerError("INVALID_POLICY", "unsupported policy schema version")

    limits = _table(data, "limits")
    canonical = _table(data, "canonical")
    slots = _table(data, "slots")
    review = _table(data, "review")
    authority = _table(data, "authority")
    if set(limits) != {"normal_limit", "hard_limit"}:
        raise ControllerError("INVALID_POLICY", "policy [limits] fields are invalid")
    if set(canonical) != {"protected", "allowed_roles"}:
        raise ControllerError("INVALID_POLICY", "policy [canonical] fields are invalid")
    expected_slots = (
        {"integration", "feature_or_audit", "emergency"}
        if schema_version == 1
        else {
            ("integration_prepare_limit" if schema_version >= 4 else "integration_candidate_limit"),
            "feature_or_audit",
            "emergency",
            "recovery_reserve",
        }
        | ({"retained"} if schema_version >= 5 else set())
    )
    if set(slots) != expected_slots:
        raise ControllerError("INVALID_POLICY", "policy [slots] fields are invalid")
    if set(review) != {"stale_warning_days"}:
        raise ControllerError("INVALID_POLICY", "policy [review] fields are invalid")
    if set(authority) != {
        "require_adoption",
        "require_task_key",
        "require_authority_key",
        "unique_active_feature_authority",
    }:
        raise ControllerError("INVALID_POLICY", "policy [authority] fields are invalid")

    normal_limit = _positive_int(limits, "normal_limit")
    hard_limit = _positive_int(limits, "hard_limit")
    if normal_limit >= hard_limit:
        raise ControllerError("INVALID_POLICY", "normal_limit must be below hard_limit")
    allowed_roles = canonical.get("allowed_roles")
    if (
        not isinstance(allowed_roles, list)
        or not allowed_roles
        or not all(isinstance(item, str) and item for item in allowed_roles)
    ):
        raise ControllerError("INVALID_POLICY", "canonical allowed_roles must be a string list")
    stale = _positive_int(review, "stale_warning_days")
    integration = _positive_int(
        slots,
        (
            "integration"
            if schema_version == 1
            else "integration_prepare_limit"
            if schema_version >= 4
            else "integration_candidate_limit"
        ),
    )
    feature_audit = _positive_int(slots, "feature_or_audit")
    emergency = _positive_int(slots, "emergency")
    recovery = 0 if schema_version == 1 else _positive_int(slots, "recovery_reserve")
    retained = _positive_int(slots, "retained") if schema_version >= 5 else 0

    # Automatic reconciliation is delegated, never assumed: a repository that has
    # not opted in keeps the pre-lease behaviour exactly, so installing a newer
    # Controller before migrating a policy changes nothing.
    reconciler_enabled = False
    reconciler_actor = "lifecycle-reconciler"
    reconciler_legacy_adoption = False
    if schema_version >= 6:
        reconciler = _table(data, "reconciler")
        if set(reconciler) != {"enabled", "actor", "allow_legacy_adoption"}:
            raise ControllerError("INVALID_POLICY", "policy [reconciler] fields are invalid")
        reconciler_enabled = _boolean(reconciler, "enabled")
        reconciler_actor = _nonempty_string(reconciler, "actor")
        reconciler_legacy_adoption = _boolean(reconciler, "allow_legacy_adoption")
    # Counts are independent observations under resource admission. Requiring
    # their historical slot sums to balance would still turn them into a veto.

    if schema_version == 1:
        main_publish_limit = 1
        publication_remote = "origin"
        publication_branch = "main"
    else:
        publication = _table(data, "publication")
        if set(publication) != {"main_publish_limit", "remote", "branch"}:
            raise ControllerError("INVALID_POLICY", "policy [publication] fields are invalid")
        main_publish_limit = _positive_int(publication, "main_publish_limit")
        if main_publish_limit != 1:
            raise ControllerError(
                "INVALID_POLICY", "this controller version requires main_publish_limit = 1"
            )
        publication_remote = _nonempty_string(publication, "remote")
        publication_branch = _nonempty_string(publication, "branch")

    if schema_version < 3:
        automatic_feature_audit_release = False
        automatic_integration_continuation = False
        preserved_auto_release = False
        release_intent_ttl_seconds = 3600
    else:
        release_policy = _table(data, "release")
        expected_release = {"automatic_feature_audit", "intent_ttl_seconds"}
        if schema_version >= 4:
            expected_release |= {
                "automatic_integration_continuation",
                "preserved_auto_release",
            }
        if set(release_policy) != expected_release:
            raise ControllerError("INVALID_POLICY", "policy [release] fields are invalid")
        automatic_feature_audit_release = _boolean(release_policy, "automatic_feature_audit")
        automatic_integration_continuation = (
            _boolean(release_policy, "automatic_integration_continuation")
            if schema_version >= 4
            else False
        )
        preserved_auto_release = (
            _boolean(release_policy, "preserved_auto_release") if schema_version >= 4 else False
        )
        if preserved_auto_release:
            raise ControllerError(
                "INVALID_POLICY", "this controller version requires preserved_auto_release = false"
            )
        release_intent_ttl_seconds = _positive_int(release_policy, "intent_ttl_seconds")

    growth_bytes, growth_inodes = 2 * 1024**3, 100_000
    if schema_version >= 7:
        admission = _table(data, "admission")
        if set(admission) != {"strategy", "checkout_growth_bytes", "checkout_growth_inodes"} or admission.get("strategy") != "resource-budget-v1":
            raise ControllerError("INVALID_POLICY", "unsupported resource admission policy")
        growth_bytes = _positive_int(admission, "checkout_growth_bytes")
        growth_inodes = _positive_int(admission, "checkout_growth_inodes")

    convergence = None
    if schema_version >= 8:
        table = _table(data, "convergence")
        if not CONVERGENCE_FIELDS <= set(table) <= CONVERGENCE_FIELDS | CONVERGENCE_OPTIONAL_FIELDS:
            raise ControllerError("INVALID_POLICY", "policy [convergence] fields are invalid")
        idle_max = _positive_int(table, "idle_hours_max")
        idle_min = _positive_int(table, "idle_hours_min")
        if idle_min > idle_max:
            raise ControllerError(
                "INVALID_POLICY", "policy idle_hours_min must not exceed idle_hours_max"
            )
        if not reconciler_enabled:
            raise ControllerError(
                "INVALID_POLICY", "retention convergence requires the delegated reconciler"
            )
        convergence = ConvergencePolicy(
            enabled=_boolean(table, "enabled"),
            grace_minutes=_positive_int(table, "grace_minutes"),
            idle_hours_max=idle_max,
            idle_hours_min=idle_min,
            standing_reserve=_positive_int(table, "standing_reserve"),
            release_unintegrated_handoffs=(
                _boolean(table, "release_unintegrated_handoffs")
                if "release_unintegrated_handoffs" in table
                else False
            ),
        )

    return WorktreePolicy(
        path=path,
        present=True,
        policy_schema_version=schema_version,
        normal_limit=normal_limit,
        hard_limit=hard_limit,
        canonical_protected=_boolean(canonical, "protected"),
        canonical_allowed_roles=frozenset(allowed_roles),
        integration_candidate_limit=integration,
        integration_prepare_limit=integration,
        feature_audit_slots=feature_audit,
        emergency_slots=emergency,
        recovery_reserve=recovery,
        retained_slots=retained,
        reconciler_enabled=reconciler_enabled,
        reconciler_actor=reconciler_actor,
        reconciler_legacy_adoption=reconciler_legacy_adoption,
        main_publish_limit=main_publish_limit,
        publication_remote=publication_remote,
        publication_branch=publication_branch,
        automatic_feature_audit_release=automatic_feature_audit_release,
        automatic_integration_continuation=automatic_integration_continuation,
        preserved_auto_release=preserved_auto_release,
        release_intent_ttl_seconds=release_intent_ttl_seconds,
        stale_warning_days=stale,
        require_adoption=_boolean(authority, "require_adoption"),
        require_task_key=_boolean(authority, "require_task_key"),
        require_authority_key=_boolean(authority, "require_authority_key"),
        unique_active_feature_authority=_boolean(authority, "unique_active_feature_authority"),
        resource_admission=True,
        checkout_growth_bytes=growth_bytes,
        checkout_growth_inodes=growth_inodes,
        convergence=convergence,
    )
