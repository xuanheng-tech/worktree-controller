"""Bind old preservation generations to one exact build registration transition."""

from __future__ import annotations

import os
from pathlib import Path

from . import preservation as p
from .build_resources import CONTRACT, EVENT
from .models import ControllerError


def _blocked(message):
    return ControllerError("PRESERVATION_COVERAGE_INVALID", message)


def identity_matches(previous, current, record):
    """Only the unchanged predecessor of the exact registration may cross CAS."""
    if previous == current:
        return True
    version = current.get("expected_record_version")
    if (record is None or type(version) is not int or version < 2
            or record.record_version != version or len(record.history) != version
            or previous != {**current, "expected_record_version": version - 1}):
        return False
    event = record.history[-1]
    proof = event.get("build_resources", {})
    return bool(
        event.get("event") == EVENT and event.get("from") == event.get("to") == record.lifecycle
        and record.lifecycle in {"HANDOFF_READY", "RETAINED"}
        and event.get("owner") == record.owner == current["owner"]
        and proof.get("contract") == CONTRACT and proof.get("record_version") == version
        and proof.get("allocation_id") == record.allocation_id == current["allocation_id"]
        and proof.get("owner") == current["owner"] and proof.get("head") == current["expected_head"]
        and record.authority_key == current["authority_key"]
    )


def generations(bound, record, *, topology=None, completed=(), completed_topology=None,
                verify_live=True):
    """Verify copies before deletion; recover absent sources only from pinned topology.

    Discovery reads official generation metadata. Returned raw hashes are frozen
    into the cleanup plan; they are never claimed to be historical observations.
    """
    from . import hardlinks
    from .cleanup_evidence import read_document
    from .preserved_cleanup import coverage, source_absent

    events = [event for event in record.history if event.get("event") == EVENT]
    if not events:
        return [], []
    if len(events) != 1 or events[0] != record.history[-1]:
        raise _blocked("build preservation requires the exact registration record")
    registration = events[0]["build_resources"]
    if not identity_matches({**bound, "expected_record_version": record.record_version - 1}, bound, record):
        raise _blocked("build preservation registration identity changed")
    allocation = p.evidence_root() / bound["repository_id"] / bound["allocation_id"]
    if not os.path.lexists(allocation):
        return [], []
    entries, plans = [], []
    with p._absolute_directory(allocation) as chain:
        for name in sorted(os.listdir(chain[-1].fd)):
            with p._absolute_directory(allocation / name) as generation:
                try:
                    os.stat("receipt.json", dir_fd=generation[-1].fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                receipt, digest = read_document(generation[-1], "receipt.json")
                if receipt.get("contract") != p.CONTRACT:
                    continue
                source = Path(receipt["source"])
                relative = p.source_relative_path(source.relative_to(bound["target_path"]).as_posix())
                if relative not in registration["paths"]:
                    continue
                previous = receipt["allocation_source_identity"]
                if not identity_matches(previous, bound, record):
                    raise _blocked("preserved build output has no exact registration CAS lineage")
                absent = relative in completed
                evidence, plan = coverage(
                    allocation / name / "receipt.json", digest, identity=previous,
                    verify_source=verify_live and not absent, source_topology=topology if not absent else None,
                )
                anchors = registration["anchors"]
                if (plan["source_anchor"]["source"] != anchors[relative]
                        or plan.get("source_ancestor_identities", {}).get(bound["target_path"]) != anchors["."]):
                    raise _blocked("preservation and registration source/root anchors differ")
                if absent or not verify_live:
                    original = completed_topology if completed_topology is not None else topology
                    if original is None or (verify_live and not source_absent(plan, Path(bound["target_path"]))):
                        raise _blocked("absent build output needs its exact completed action proof")
                    hardlinks.source_baseline(plan["source_tree"], original, resource=relative)
                    anchor = original["anchors"][relative]
                    if anchor != {"worktree": anchors["."], "resource": anchors[relative]}:
                        raise _blocked("completed build output topology anchors changed")
                    leaf_identities = hardlinks.identities(original, relative)
                    expected_contents = {
                        row["path"]: {key: row[key] for key in ("content_sha256", "target") if key in row}
                        for row in plan["source_tree"]["manifest"]
                        if row["path"] in leaf_identities
                    }
                    observed = {path: group["content"] for group in original["groups"].values()
                                for resource, path in group["members"] if resource == relative}
                    if observed != expected_contents:
                        raise _blocked("completed build bytes differ from the preserved generation")
                entries.append({"source_relative": relative, "evidence": evidence})
                plans.append(plan)
    return entries, plans
