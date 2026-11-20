"""Load and validate repository worktree policy."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    CANONICAL_ROLES,
    ControllerError,
    ConvergencePolicy,
    KeyedDocumentRule,
    WorktreePolicy,
)

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
OPTIONAL_TOP_LEVEL = frozenset({"keyed_documents", "disposable_resources"})
DISPOSABLE_KINDS = frozenset({"python-venv", "node-modules", "frontend-build-output", "build-output"})
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


def _parse_relative_path(raw: str, *, field: str) -> str:
    if not raw or "\x00" in raw or raw.startswith("/"):
        raise ControllerError("INVALID_POLICY", f"invalid {field} path: {raw}")
    posix = PurePosixPath(raw)
    if posix.as_posix() != raw or any(part in {"", ".", "..", ".git"} for part in posix.parts):
        raise ControllerError("INVALID_POLICY", f"invalid {field} path: {raw}")
    return raw


def _parse_keyed_documents(data: dict[str, Any]) -> tuple[KeyedDocumentRule, ...]:
    if "keyed_documents" not in data:
        return ()
    raw_table = _table(data, "keyed_documents")
    rules: list[KeyedDocumentRule] = []
    for rel_path, spec in sorted(raw_table.items()):
        path = _parse_relative_path(rel_path, field="keyed_documents")
        if not isinstance(spec, dict):
            raise ControllerError("INVALID_POLICY", f"keyed_documents rule [{rel_path}] must be a table")
        allowed_rule_fields = {"merge_driver", "collections", "schema_version", "repo_id", "extra_fields"}
        if set(spec) - allowed_rule_fields:
            raise ControllerError("INVALID_POLICY", f"unknown fields in keyed_documents[{rel_path}]")
        merge_driver = _nonempty_string(spec, "merge_driver")
        collections_raw = spec.get("collections")
        if not isinstance(collections_raw, dict) or not collections_raw:
            raise ControllerError("INVALID_POLICY", f"keyed_documents[{rel_path}].collections must be non-empty")
        collections: list[tuple[str, str]] = []
        for col_name, id_field in sorted(collections_raw.items()):
            if (
                not isinstance(col_name, str)
                or not col_name.strip()
                or not isinstance(id_field, str)
                or not id_field.strip()
            ):
                raise ControllerError("INVALID_POLICY", f"invalid collection key in keyed_documents[{rel_path}]")
            collections.append((col_name, id_field))
        schema_version = spec.get("schema_version")
        if schema_version is not None and (type(schema_version) is not int or schema_version < 1):
            raise ControllerError("INVALID_POLICY", f"invalid schema_version in keyed_documents[{rel_path}]")
        repo_id = spec.get("repo_id")
        if repo_id is not None and (not isinstance(repo_id, str) or not repo_id.strip()):
            raise ControllerError("INVALID_POLICY", f"invalid repo_id in keyed_documents[{rel_path}]")
        extra_raw = spec.get("extra_fields", [])
        if not isinstance(extra_raw, list) or not all(isinstance(v, str) and v.strip() for v in extra_raw):
            raise ControllerError("INVALID_POLICY", f"invalid extra_fields in keyed_documents[{rel_path}]")
        rules.append(
            KeyedDocumentRule(
                path=path,
                merge_driver=merge_driver,
                collections=tuple(collections),
                schema_version=schema_version,
                repo_id=repo_id,
                extra_fields=tuple(extra_raw),
            )
        )
    return tuple(rules)


def _parse_disposable_resources(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    if "disposable_resources" not in data:
        return ()
    raw_table = _table(data, "disposable_resources")
    if set(raw_table) != {"paths"}:
        raise ControllerError("INVALID_POLICY", "policy [disposable_resources] supports only 'paths'")
    mapping = raw_table.get("paths")
    if not isinstance(mapping, dict):
        raise ControllerError("INVALID_POLICY", "policy [disposable_resources].paths must be a table")
    parsed: list[tuple[str, str]] = []
    for raw_path, kind in sorted(mapping.items()):
        path = _parse_relative_path(raw_path, field="disposable_resources.paths")
        if not isinstance(kind, str) or kind not in DISPOSABLE_KINDS:
            raise ControllerError("INVALID_POLICY", f"unsupported disposable kind for {path}: {kind}")
        parsed.append((path, kind))
    return tuple(parsed)


def parse_policy(canonical_path: Path, data: dict[str, Any]) -> WorktreePolicy:
    """Validate policy data read from the checkout or a pinned publication tree."""
    path = canonical_path / ".agents" / "worktree-policy.toml"
    schema_version = data.get("schema_version")
    expected_by_version = {
        1: EXPECTED_TOP_LEVEL,
        2: EXPECTED_TOP_LEVEL_V2,
        3: EXPECTED_TOP_LEVEL_V3,
        4: EXPECTED_TOP_LEVEL_V4,
        5: EXPECTED_TOP_LEVEL_V5,
        6: EXPECTED_TOP_LEVEL_V6,
        7: EXPECTED_TOP_LEVEL_V7,
        8: EXPECTED_TOP_LEVEL_V8,
    }
    expected = expected_by_version.get(schema_version)
    if expected is None or type(schema_version) is not int:
        raise ControllerError("INVALID_POLICY", "unsupported policy schema version")
    if not (expected <= set(data) <= expected | OPTIONAL_TOP_LEVEL):
        raise ControllerError("INVALID_POLICY", f"policy schema v{schema_version} fields are invalid")
    keyed_documents = _parse_keyed_documents(data)
    disposable_resources = _parse_disposable_resources(data)

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
        if publication_remote.startswith("-") or not all(
            ch.isalnum() or ch in "._-" for ch in publication_remote
        ):
            raise ControllerError("INVALID_REMOTE", f"remote name is unsafe: {publication_remote}")
        publication_branch = _nonempty_string(publication, "branch")
        if (
            publication_branch.startswith("-")
            or ".." in publication_branch
            or any(ch.isspace() or ord(ch) < 32 for ch in publication_branch)
        ):
            raise ControllerError("INVALID_POLICY", f"invalid publication branch: {publication_branch}")

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
        keyed_documents=keyed_documents,
        disposable_resources=disposable_resources,
        disposable_resources_configured="disposable_resources" in data,
    )
