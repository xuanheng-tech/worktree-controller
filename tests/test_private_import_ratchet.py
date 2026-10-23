"""Keep cross-module use of private helpers from growing.

Each entry is an existing import of an underscore-prefixed name from a sibling module. New code
should promote a shared helper to a public name instead of adding to this list; removing an
import requires removing its entry, so the baseline only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "worktree_controller"

BASELINE = frozenset({
    ("branch_retirement", "git_backend", "_git"),
    ("branch_retirement", "git_backend", "_text"),
    ("branch_retirement", "lifecycle", "_crash_point"),
    ("canonical_refresh", "canonical_transition", "_check_target_policy"),
    ("canonical_refresh", "canonical_transition", "_published_policy"),
    ("canonical_transition", "git_backend", "_git"),
    ("cleanup_evidence", "preserved_cleanup", "_document"),
    ("cleanup_recovery", "lifecycle", "_typed_cleanup_plan_locked"),
    ("cleanup_recovery", "metadata_codec", "_validate_timestamp"),
    ("convergence", "writer_lease", "_referenced_paths"),
    ("delivered_closeout", "git_backend", "_git"),
    ("delivered_closeout", "lifecycle", "_release_resource_actions"),
    ("delivered_closeout", "lifecycle", "_release_transaction"),
    ("delivered_closeout", "lifecycle", "_typed_runtime_blockers"),
    ("delivered_closeout", "lifecycle", "_typed_runtime_evidence"),
    ("delivered_closeout", "lifecycle", "_validate_release_environment"),
    ("disposable", "environment", "_sha256"),
    ("frozen_recovery", "lifecycle", "_cancel_release_intents"),
    ("frozen_recovery", "lifecycle", "_typed_runtime_blockers"),
    ("frozen_recovery", "lifecycle", "_typed_runtime_evidence"),
    ("governance", "controller", "_environment_first"),
    ("governance", "controller", "_repository_payload"),
    ("integration_conflict", "integration_retry", "_sources"),
    ("integration_conflict", "lifecycle", "_intent_for_allocation"),
    ("integration_conflict", "lifecycle", "_intent_payload"),
    ("integration_retry", "lifecycle", "_blocking_scope_intent"),
    ("integration_retry", "lifecycle", "_emit_lifecycle_event"),
    ("integration_retry", "lifecycle", "_resolve_integration_sources"),
    ("integration_retry", "lifecycle", "_scope_digest"),
    ("integration_retry", "lifecycle", "_scopes_overlap"),
    ("integration_retry", "lifecycle_history", "_release_view"),
    ("keyed_document_scope", "git_backend", "_git"),
    ("lifecycle", "controller", "_warning"),
    ("lifecycle", "delivered_closeout", "_eligible"),
    ("lifecycle", "delivered_closeout", "_publication"),
    ("lifecycle", "integration_retry", "_sources"),
    ("metadata_relocation", "disposable", "_inode"),
    ("metadata_relocation", "disposable", "_open_directory"),
    ("metadata_relocation", "preserved_cleanup", "_document"),
    ("multiple_cleanup_recovery", "delivered_closeout", "_digest"),
    ("multiple_cleanup_recovery", "lifecycle", "_release_plan_id"),
    ("multiple_cleanup_recovery", "lifecycle", "_typed_cleanup_plan_locked"),
    ("multiple_cleanup_recovery", "preserved_cleanup", "_document"),
    ("namespace_migration", "lifecycle", "_crash_point"),
    ("preservation", "disposable", "_Directory"),
    ("preservation", "disposable", "_inode"),
    ("preservation", "disposable", "_open_directory"),
    ("preservation", "lifecycle", "_typed_cleanup_plan_locked"),
    ("preserved_cleanup", "cleanup_attempts", "_typed_journal"),
    ("preserved_cleanup", "disposable", "_delete_fd_tree"),
    ("preserved_cleanup", "disposable", "_fd_inventory"),
    ("preserved_cleanup", "disposable", "_unlink_leaf"),
    ("preserved_cleanup", "lifecycle", "_preservation_source_relative"),
    ("preserved_cleanup", "lifecycle", "_release_plan_id"),
    ("preserved_cleanup", "lifecycle", "_validate_multiple_preservation_cleanup"),
    ("publication_execution", "git_backend", "_git"),
    ("publication_execution", "lifecycle", "_intent_for_allocation"),
    ("publication_execution", "lifecycle", "_validate_prepared_publication_receipt"),
    ("reconciler", "lifecycle", "_cancel_release_intents"),
    ("recovery_guidance", "integration_retry", "_sources"),
    ("resource_admission", "git_backend", "_git"),
    ("session_admission", "controller", "_plan_from_snapshot"),
    ("source_cleanup", "git_backend", "_git"),
    ("source_cleanup", "lifecycle", "_release_transaction"),
    ("source_cleanup", "lifecycle", "_transaction"),
    ("source_preservation", "disposable", "_Directory"),
    ("source_preservation", "disposable", "_inode"),
    ("source_preservation", "disposable", "_open_directory"),
    ("source_preservation", "git_backend", "_git"),
    ("source_preservation", "lifecycle", "_typed_runtime_blockers"),
    ("source_preservation", "lifecycle", "_typed_runtime_evidence"),
    ("source_preservation", "preserved_cleanup", "_document"),
    ("source_recovery", "lifecycle", "_typed_runtime_blockers"),
    ("source_recovery", "lifecycle", "_typed_runtime_evidence"),
    ("source_recovery", "source_cleanup", "_state"),
    ("source_recovery", "source_cleanup", "_store"),
    ("transaction_scope", "branch_retirement", "_load_authorization"),
    ("transaction_scope", "branch_retirement", "_load_plan"),
    ("transaction_scope", "delivered_closeout", "_archive_receipt_binding"),
    ("writer_lease", "environment", "_current_process_identity"),
})


def private_imports() -> set[tuple[str, str, str]]:
    found = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                found.update(
                    (path.stem, node.module, alias.name)
                    for alias in node.names
                    if alias.name.startswith("_")
                )
    return found


def test_no_new_private_cross_module_imports():
    added = sorted(private_imports() - BASELINE)
    assert not added, f"import a public name instead of these private helpers: {added}"


def test_private_import_baseline_only_shrinks():
    removed = sorted(BASELINE - private_imports())
    assert not removed, f"remove these resolved entries from BASELINE: {removed}"
