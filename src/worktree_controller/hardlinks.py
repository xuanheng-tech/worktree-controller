"""Pre-deletion, allocation-scoped hardlink topology and exact unlink effects."""

from __future__ import annotations

import copy
import errno
import os
import stat
from pathlib import Path
from typing import Any

from . import preservation as p
from .models import ControllerError

CONTRACT = "cleanup-hardlink-topology/v1"
EXTERNAL_CONTRACT = "cleanup-hardlink-topology/v2"
MUTATION_CONTRACT = "cleanup-induced-hardlink-mutation/v1"
EXTERNAL_MUTATION_CONTRACT = "cleanup-induced-hardlink-mutation/v2"


def _blocked(message: str) -> ControllerError:
    return ControllerError("HARDLINK_TOPOLOGY_BLOCKED", message)


def _effects(group: dict[str, Any], resources: list[str]) -> dict[str, Any]:
    owners = {m[0] for m in group["members"]}
    classification = (
        "single-link"
        if len(group["members"]) == 1
        else "same-resource"
        if len(owners) == 1
        else "typed-preserved"
        if ".venv" in owners
        else "typed-typed"
    )
    surviving = group["members"]
    steps = []
    for resource in resources:
        removed = [m for m in surviving if m[0] == resource]
        if removed:
            surviving = [m for m in surviving if m[0] != resource]
            steps.append(
                {
                    "resource": resource,
                    "deleted_links": removed,
                    "surviving_paths": surviving,
                    "expected_nlink": len(surviving) + group.get("external_links", 0),
                }
            )
    return {"classification": classification, "expected_steps": steps}


def inventory(root: Path, resources: list[str]) -> dict[str, Any]:
    """Record every non-directory inode and all its in-scope names; hash opaque bytes.

    Names outside the exact cleanup resources are never searched or authorized.
    Version 2 records their link-count remainder. Every unlink of such an inode
    requires the pinned inode's unchanged bytes and exact metadata decrement.
    Directory identity remains bound by the FD-relative deletion primitive.
    """
    if len(resources) != len(set(resources)):
        raise _blocked("invalid or overlapping cleanup resources")
    for name in resources:
        p.source_relative_path(name)
        if any(name != other and Path(name).is_relative_to(Path(other)) for other in resources):
            raise _blocked("invalid or overlapping cleanup resources")
    groups: dict[str, dict[str, Any]] = {}
    anchors = {}
    for resource in resources:
        with p._absolute_source(root / resource) as chain:
            tree = p.scan_tree(chain[-1], chain)
            tree.require_complete()
            anchors[resource] = {
                "worktree": chain[-len(Path(resource).parts) - 1].identity,
                "resource": chain[-1].identity,
            }
        for row in tree.manifest:
            identity = tree.identities[row["path"]]
            if stat.S_ISDIR(identity[2]):
                continue
            key = f"{identity[0]}:{identity[1]}"
            content = {k: row[k] for k in ("content_sha256", "target") if k in row}
            member = [resource, row["path"]]
            if key in groups:
                group = groups[key]
                if group["identity"] != identity or group["content"] != content:
                    raise _blocked("inode identity changed during topology scan")
                group["members"].append(member)
            else:
                groups[key] = {"identity": identity, "content": content, "members": [member]}
    external = any(g["identity"][8] > len(g["members"]) for g in groups.values())
    for group in groups.values():
        group["members"].sort()
        if group["identity"][8] < len(group["members"]):
            raise _blocked("hardlink topology is unstable")
        if external:
            group["external_links"] = group["identity"][8] - len(group["members"])
        group.update(_effects(group, resources))
    payload = {
        "contract": EXTERNAL_CONTRACT if external else CONTRACT,
        "resources": resources,
        "anchors": anchors,
        "groups": groups,
    }
    return {**payload, "sha256": p._sha(payload)}


def validate(graph: dict[str, Any]) -> None:
    if graph.get("contract") not in {CONTRACT, EXTERNAL_CONTRACT} or graph.get("sha256") != p._sha(
        {k: v for k, v in graph.items() if k != "sha256"}
    ):
        raise _blocked("topology evidence changed")
    for key, group in graph["groups"].items():
        identity = group["identity"]
        members = group["members"]
        external = group.get("external_links", 0)
        if (
            key != f"{identity[0]}:{identity[1]}"
            or type(external) is not int
            or external < 0
            or (graph["contract"] == CONTRACT and "external_links" in group)
            or (graph["contract"] == EXTERNAL_CONTRACT and "external_links" not in group)
            or len(members) + external != identity[8]
            or len({tuple(m) for m in members}) != len(members)
            or any(m[0] not in graph["resources"] for m in members)
        ):
            raise _blocked("incomplete topology evidence")
        if any(group.get(k) != v for k, v in _effects(group, graph["resources"]).items()):
            raise _blocked("invalid expected link-count effects")


def verify(root: Path, expected: dict[str, Any]) -> None:
    validate(expected)
    try:
        actual = inventory(root, expected["resources"])
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ESTALE}:
            raise ControllerError(
                "RESOURCE_PLAN_MISMATCH", "resource path identity changed"
            ) from exc
        raise
    if actual["anchors"] != expected["anchors"]:
        raise ControllerError("RESOURCE_PLAN_MISMATCH", "resource directory identity changed")
    if actual != expected:
        raise _blocked("topology changed since the last recorded baseline")


def transition(
    before: dict[str, Any],
    after: dict[str, Any],
    resource: str,
    external_survivors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Accept only the exact removal of one recorded resource's inode names."""
    validate(before)
    validate(after)
    if resource not in before["resources"] or after["resources"] != [
        r for r in before["resources"] if r != resource
    ]:
        raise _blocked("unexpected cleanup action or resource set")
    if after["anchors"] != {k: v for k, v in before["anchors"].items() if k != resource}:
        raise _blocked("surviving directory identity changed")
    expected_keys = set()
    mutations = {}
    proofs = external_survivors or {}
    expected_proofs = set()
    for key, old in before["groups"].items():
        members = [m for m in old["members"] if m[0] != resource]
        removed = len(old["members"]) - len(members)
        external = old.get("external_links", 0)
        if external and removed:
            expected_proofs.add(key)
        if not members and not external:
            continue
        if members:
            expected_keys.add(key)
            current = after["groups"].get(key)
        else:
            current = proofs.get(key)
        if current is None or current["content"] != old["content"]:
            raise _blocked("surviving path, inode, content or symlink literal changed")
        if members and (
            current["members"] != members or current.get("external_links", 0) != external
        ):
            raise _blocked("surviving hardlink membership changed")
        wanted = list(old["identity"])
        wanted[8] -= removed
        if removed:
            wanted[5] = current["identity"][5]
        if current["identity"] != wanted:
            raise _blocked("unrecorded metadata or link-count mutation")
        if (
            external
            and removed
            and proofs.get(key) != {"identity": current["identity"], "content": current["content"]}
        ):
            raise _blocked("pinned external inode verification is missing or differs")
        if removed:
            mutations[key] = {
                "removed_links": removed,
                "before": old["identity"],
                "after": current["identity"],
                "surviving_paths": members,
            }
    if set(after["groups"]) != expected_keys:
        raise _blocked("unexpected surviving inode set")
    if set(proofs) != expected_proofs:
        raise _blocked("unexpected or missing pinned external inode evidence")
    return {
        "contract": mutation_contract(before),
        "completed_resource": resource,
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
        "mutations": mutations,
        **({"external_survivors": proofs} if before["contract"] == EXTERNAL_CONTRACT else {}),
    }


def mutation_contract(graph: dict[str, Any]) -> str:
    return (
        EXTERNAL_MUTATION_CONTRACT if graph["contract"] == EXTERNAL_CONTRACT else MUTATION_CONTRACT
    )


def external_contents(graph: dict[str, Any], resource: str) -> dict[str, dict[str, Any]]:
    validate(graph)
    return {
        path: group["content"]
        for group in graph["groups"].values()
        if group.get("external_links", 0)
        for name, path in group["members"]
        if name == resource
    }


def pinned_content(pin: int, expected: list[int]) -> dict[str, Any]:
    """Observe the authorized inode, including after its last in-scope name is gone.

    The kernel FD is the only handle: no external pathname is discovered, followed,
    opened or changed. FIFO endpoints remain unopened. This is an observation at
    the unlink boundary, not a claim that external writers are globally excluded.
    """
    if p._identity(os.fstat(pin)) != expected:
        raise _blocked("pinned inode changed before content verification")
    if stat.S_ISREG(expected[2]):
        fd = os.open(f"/proc/self/fd/{pin}", os.O_RDONLY | os.O_CLOEXEC | os.O_NOATIME)
        try:
            if p._identity(os.fstat(fd)) != expected:
                raise _blocked("pinned content identity changed")
            size, digest = p._transfer(fd)
            if size != expected[3] or p._identity(os.fstat(fd)) != expected:
                raise _blocked("pinned bytes changed during verification")
            content = {"content_sha256": digest}
        finally:
            os.close(fd)
    elif stat.S_ISLNK(expected[2]):
        content = {"target": os.readlink("", dir_fd=pin)}
    elif stat.S_ISFIFO(expected[2]):
        content = {}
    else:
        raise _blocked("unsupported pinned inode type")
    if p._identity(os.fstat(pin)) != expected:
        raise _blocked("pinned inode changed during content verification")
    return content


def identities(graph: dict[str, Any], resource: str) -> dict[str, list[int]]:
    validate(graph)
    return {
        path: group["identity"]
        for group in graph["groups"].values()
        for name, path in group["members"]
        if name == resource
    }


def source_baseline(
    manifest: dict[str, Any], graph: dict[str, Any], *, resource: str = ".venv"
) -> dict[str, Any]:
    """Overlay a previously verified, recorded graph; never changes the receipt."""
    expected = copy.deepcopy(manifest)
    current = identities(graph, resource)
    if resource not in graph["resources"] or set(current) != {
        name for name, identity in manifest["source_identities"].items() if not stat.S_ISDIR(identity[2])
    }:
        raise _blocked("preserved source topology coverage is incomplete")
    for path, identity in current.items():
        old = expected["source_identities"].get(path)
        if old is None or any(
            a != b for i, (a, b) in enumerate(zip(old, identity, strict=True)) if i not in {5, 8}
        ):
            raise _blocked("preserved source identity cannot be rebased")
        expected["source_identities"][path] = identity
    return expected
