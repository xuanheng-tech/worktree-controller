"""The published storage layout is versioned and pinned by external consumers.

External consumers (the Git Finalizer retirement verifier and the frozen
activation gate) read this layout directly. Any store directory, lock name or
root layout change must therefore update the contract section deliberately.
"""

import json
import re
from pathlib import Path

from worktree_controller.store import MetadataStore

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "tool_cli_contract.json").read_text())
LAYOUT = CONTRACT["storage_layout"]
V3_LAYOUT = json.loads((Path(__file__).with_name("storage_layout_v3.json")).read_text())
V4_LAYOUT = json.loads((Path(__file__).with_name("storage_layout_v4.json")).read_text())
SOURCE = Path(__file__).resolve().parents[1] / "src" / "worktree_controller"
V5_DIRECTORIES = {
    "cleanup-actions",
    "cleanup-recoveries",
    "native-session-recoveries",
    "session-closeouts",
    "binding-registrations",
}

# Names that are not MetadataStore attributes: one computed evidence tree and two
# migration records, all living directly under the layout root.
NON_ATTRIBUTE_DIRECTORIES = frozenset({"source-cleanup", "namespace-migrations"})
NON_ATTRIBUTE_FILES = frozenset({"namespace-relocation.json"})


def _store() -> MetadataStore:
    return MetadataStore(Path("/repo/.git"))


def _attribute_names(store: MetadataStore, suffix: str) -> set[str]:
    """Enumerate the live layout instead of a hand-copied list.

    A hand-copied tuple cannot notice a directory nobody added to it, which is
    exactly how an undocumented storage location would ship.
    """

    return {
        getattr(store, name).name
        for name, value in vars(store).items()
        if name.endswith(suffix) and isinstance(value, Path)
    }


def test_every_store_directory_is_declared_in_the_layout_contract():
    declared = set(LAYOUT["directories"])
    actual = _attribute_names(_store(), "_dir") | NON_ATTRIBUTE_DIRECTORIES
    assert not actual - declared, f"undocumented storage directories: {sorted(actual - declared)}"
    assert not declared - actual, f"contract lists dead directories: {sorted(declared - actual)}"
    assert len(_attribute_names(_store(), "_dir")) >= 19, "layout enumeration must not go blind"


def test_lock_and_root_identity_match_the_contract():
    store = _store()
    assert store.lock_file.name == LAYOUT["lock"]["name"]
    assert store.root == Path("/repo/.git") / "worktree-controller" / "v1"
    assert LAYOUT["root"] == "<git-common-dir>/worktree-controller/v1"
    assert LAYOUT["version"] == 5


def test_single_file_identities_are_declared():
    store = _store()
    declared = set(LAYOUT["single_file"])
    files = _attribute_names(store, "_file") - {store.lock_file.name}
    actual = files | NON_ATTRIBUTE_FILES
    assert not actual - declared, f"undocumented single files: {sorted(actual - declared)}"
    assert not declared - actual, f"contract lists dead single files: {sorted(declared - actual)}"


def test_every_layout_version_added_exactly_one_name():
    """Each additive step states what it added, so a pinned consumer can tell what changed."""

    assert "branch-retirement-authorizations" in LAYOUT["directories"]
    assert "namespace-migrations" in LAYOUT["directories"]
    assert "registration-prune-receipts" in LAYOUT["directories"]
    assert (
        "v4 declares exactly one directory (registration-prune-receipts)" in LAYOUT["compatibility"]
    )
    assert "v3 adds exactly one directory (namespace-migrations)" in LAYOUT["compatibility"]
    assert (
        "v2 adds exactly one directory (branch-retirement-authorizations)"
        in LAYOUT["compatibility"]
    )


def test_v4_is_exactly_one_additive_directory_for_v3_consumers() -> None:
    """A v3 reader's known paths retain their identities; exact pins see a new version."""

    assert V3_LAYOUT["version"] == 3
    assert "registration-prune-receipts" not in V3_LAYOUT["directories"]
    assert {
        name: description
        for name, description in V4_LAYOUT["directories"].items()
        if name != "registration-prune-receipts"
    } == V3_LAYOUT["directories"]
    for field in ("root", "lock", "single_file", "consumers"):
        assert V4_LAYOUT[field] == V3_LAYOUT[field]
    assert V4_LAYOUT["version"] != V3_LAYOUT["version"]
    assert set(V4_LAYOUT["directories"]) - set(V3_LAYOUT["directories"]) == {
        "registration-prune-receipts"
    }


def test_v5_only_adds_declared_directories_for_v4_consumers() -> None:
    """v4 paths keep their identities; v5 names what earlier runtimes already wrote."""

    assert V4_LAYOUT["version"] == 4
    assert {
        name: description
        for name, description in LAYOUT["directories"].items()
        if name not in V5_DIRECTORIES
    } == V4_LAYOUT["directories"]
    for field in ("root", "lock", "single_file", "consumers"):
        assert LAYOUT[field] == V4_LAYOUT[field]
    assert set(LAYOUT["directories"]) - set(V4_LAYOUT["directories"]) == V5_DIRECTORIES
    assert LAYOUT["compatibility"].startswith("v5 declares four directories already used")
    assert "worktree-controller-registration" in LAYOUT["directories"]["binding-registrations"]


def test_no_module_creates_an_undeclared_name_under_the_layout_root() -> None:
    """Attribute enumeration cannot see ``store.root / "<name>"`` joins in other modules."""

    declared = set(LAYOUT["directories"]) | set(LAYOUT["single_file"]) | {LAYOUT["lock"]["name"]}
    joined = set()
    for path in SOURCE.glob("*.py"):
        joined.update(
            re.findall(r"""\bstore\.root\s*/\s*["']([^"']+)["']""", path.read_text(encoding="utf-8"))
        )
    assert joined, "the source scan must observe the existing root joins"
    assert not joined - declared, f"undeclared storage names: {sorted(joined - declared)}"


def test_v4_classifies_registration_prune_receipts() -> None:
    description = LAYOUT["directories"]["registration-prune-receipts"]
    assert "write-once" in description
    assert "authoritative terminal and recovery proof" in description
    assert "not repository identity or lock authority" in description
    assert "namespace migration merged digest" in description
    assert "not in the prior GC plan digest" in description


def test_layout_contract_pin_is_versioned_and_stable():
    assert isinstance(LAYOUT["version"], int) and LAYOUT["version"] >= 1
    assert LAYOUT["compatibility"], "the layout must state its compatibility rule"
    consumers = set(LAYOUT["consumers"])
    assert any("Finalizer" in item for item in consumers)
    assert any("check-writer-rollout" in item for item in consumers)
