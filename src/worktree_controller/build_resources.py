"""Owner-attested, frozen Node project outputs; cleanup remains the typed executor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .disposable import path_identity, project_identity, project_json
from .git_backend import head_paths, inventory, path_is_ignored, tracked_paths
from .models import ControllerError, MetadataRecord, WorktreeGitState
from .preservation import source_relative_path
from .record_updates import updated_record
from .semantics import assert_record_version, managed_record
from .store import MetadataStore

CONTRACT = "registered-build-resources/v1"
EVENT = "BUILD_RESOURCES_REGISTERED"
OUTPUTS = {".astro", ".generated", "dist", "node_modules"}


def _fail(message: str) -> ControllerError:
    return ControllerError("BUILD_RESOURCE_INVALID", message)


def project_inputs(root: Path, project: str, recipe: str) -> dict[str, str]:
    """Pin committed build recipes and dependency inputs without running any command."""
    source_relative_path(project)
    if any(part in {"research", "evidence", "experiments"} for part in Path(project).parts):
        raise _fail("research evidence is outside the build-resource contract")
    names = [f"{project}/package.json", f"{project}/package-lock.json"]
    values = {name: project_identity(root, name) for name in names}
    package = project_json(values[names[0]])
    lock = project_json(values[names[1]])
    scripts = package.get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get(recipe), str) or not scripts[recipe].strip():
        raise _fail("recipe must name a nonempty committed package.json script")
    if not package.get("name") or lock.get("name") != package["name"]:
        raise _fail("package and dependency lock identities differ")
    return {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}


def declarations(item: WorktreeGitState, record: MetadataRecord) -> dict[str, dict[str, Any]]:
    rows = [row["build_resources"] for row in record.history if row.get("event") == EVENT]
    if not rows:
        return {}
    if len(rows) != 1:
        raise _fail("ambiguous build registration history")
    proof = rows[0]
    if (proof["head"] != item.head or proof["owner"] != record.owner
            or proof["allocation_id"] != record.allocation_id
            or proof["record_version"] != record.record_version
            or proof["contract"] != CONTRACT
            or proof["inputs"] != project_inputs(item.path, proof["project"], proof["recipe"])):
        raise _fail("build registration identity or committed inputs changed")
    return {path: {"registration": proof, "path": path} for path in proof["paths"]}


def check_resource(root: Path, relative: str, declaration: dict[str, Any]) -> None:
    proof = declaration["registration"]
    if (proof["contract"] != CONTRACT or declaration["path"] != relative
            or relative not in proof["paths"]
            or Path(relative).parent.as_posix() != proof["project"]
            or Path(relative).name not in OUTPUTS):
        raise _fail("resource is outside the registered exact project outputs")
    if proof["inputs"] != project_inputs(root, proof["project"], proof["recipe"]):
        raise _fail("registered build inputs changed")
    for name, expected in proof["anchors"].items():
        # A completed action can remove another registered output; retain all ancestors.
        if name in proof["paths"] and name != relative:
            continue
        path = root if name == "." else root / name
        if path.is_symlink() or path_identity(path) != expected:
            raise _fail("registered resource or ancestor identity changed")


def register(
    repository_path: Path, *, target: Path, owner: str, expected_record_version: int,
    expected_head: str, project: str, recipe: str, paths: tuple[str, ...],
    attest_rebuildable: bool, dry_run: bool = True, expected_plan_id: str | None = None,
) -> dict[str, Any]:
    from . import lifecycle as life
    from .controller import envelope

    if not attest_rebuildable or not paths or len(set(paths)) != len(paths):
        raise _fail("explicit owner rebuildability attestation and unique outputs are required")
    repo = inventory(repository_path)
    store = MetadataStore(repo.common_dir)
    with store.lock(exclusive=not dry_run):
        repo = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = managed_record(repo, snapshot, target)
        assert_record_version(record, expected_record_version)
        if owner != record.owner:
            raise ControllerError("OWNER_MISMATCH", "build registration requires the recorded owner")
        if expected_head != item.head:
            raise ControllerError("HEAD_MISMATCH", "build registration HEAD changed")
        if record.role not in {"feature", "audit", "hotfix"} or record.lifecycle not in {"RETAINED", "HANDOFF_READY"}:
            raise ControllerError("INVALID_LIFECYCLE", "build registration requires an idle retained or frozen source")
        if any(row.get("event") == EVENT for row in record.history):
            raise _fail("allocation already has its exact build registration")
        life._validate_release_core(
            repo, snapshot, item, record, store=store, owner_release=owner,
            strict_identity=True, allow_registered_venv=False,
            allow_retained_cleanup=record.lifecycle == "RETAINED",
        )
        if record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "end the writer before build registration")
        if snapshot.publication_lease and snapshot.publication_lease.allocation_id == record.allocation_id:
            raise ControllerError("PUBLICATION_LEASE_PRESENT", "publication lease blocks registration")
        if not item.branch or life.resolve_commit(repo.canonical_path, f"refs/heads/{item.branch}") != item.head:
            raise ControllerError("MISSING_RETAINED_REF", "registration requires an exact retained branch")
        if life._release_transaction(store, target_path=str(item.path), allocation_id=record.allocation_id):
            raise ControllerError("TRANSACTION_PENDING", "finish existing cleanup before registration")
        life._validate_runtime_clear(item, strict=True)
        blockers = life._typed_runtime_blockers(life._typed_runtime_evidence(item, record, snapshot))
        if blockers:
            raise ControllerError(blockers[0]["code"], blockers[0]["message"])
        inputs = project_inputs(item.path, project, recipe)
        anchors = {".": path_identity(item.path)}
        for relative in paths:
            source_relative_path(relative)
            if Path(relative).parent.as_posix() != project or Path(relative).name not in OUTPUTS:
                raise _fail("only explicitly attested immediate Node project outputs are supported")
            absolute = item.path / relative
            if tracked_paths(item.path, relative) or head_paths(item.path, relative):
                raise ControllerError("TRACKED_RESOURCE", "build output contains tracked content")
            if not path_is_ignored(item.path, relative) or not absolute.is_dir():
                raise _fail("registered output must exist and be Git-ignored")
            for path in (absolute, *absolute.parents):
                if path == item.path:
                    break
                if path.is_symlink() or path.is_mount():
                    raise _fail("build output ancestors must be local real directories")
                anchors[path.relative_to(item.path).as_posix()] = path_identity(path)
        proof = {"contract": CONTRACT, "allocation_id": record.allocation_id, "owner": owner,
                 "head": item.head, "record_version": record.record_version + 1,
                 "project": project, "recipe": recipe, "paths": sorted(paths),
                 "inputs": inputs, "anchors": anchors, "attestation": "owner-rebuildable-no-evidence"}
        plan_id = hashlib.sha256((json.dumps(proof, sort_keys=True, ensure_ascii=True,
                                              separators=(",", ":")) + "\n").encode()).hexdigest()
        if not dry_run:
            if expected_plan_id != plan_id:
                raise ControllerError("RESOURCE_PLAN_MISMATCH", "build registration plan changed")
            updated = updated_record(record, EVENT)
            updated = replace(updated, history=(*record.history, {**updated.history[-1], "build_resources": proof}))
            life._cancel_release_intents(store, snapshot, record.allocation_id,
                                         event=EVENT, blocker="RESOURCE_IDENTITY_CHANGED")
            store.replace_record(updated)
    result = envelope("build-resource-register")
    result["decision"] = {"code": "BUILD_RESOURCE_PLAN" if dry_run else EVENT,
                          "plan_id": plan_id, "registration": proof,
                          "record_version": record.record_version if dry_run else updated.record_version}
    return result
