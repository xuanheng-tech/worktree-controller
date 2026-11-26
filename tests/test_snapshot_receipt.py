import json
import subprocess
from pathlib import Path

import pytest

from worktree_controller.models import ControllerError
from worktree_controller.snapshot_receipt import (
    repository_display_name,
    verify_candidate_snapshot,
)

DIGEST = "a" * 64
HEAD = "b" * 40
BASE = "c" * 40
CHECKOUT = Path("/test/integration-candidate")
BRANCH = "integration/release"


@pytest.fixture
def review(monkeypatch):
    response = {
        "evidence_schema_version": 1,
        "command": "branch-review",
        "snapshot_id": DIGEST,
        "repository": CHECKOUT.name,
        "status": "complete",
        "truncated": False,
        "evidence_gap": False,
        "evidence": {
            "fields": {
                "target_ref": "refs/heads/" + BRANCH,
                "target_head": HEAD,
                "base_commit": BASE,
                "merge_base_commit": BASE,
            }
        },
    }

    def read(argv, **_kwargs):
        assert argv[:5] == ["snapshot-runner", "read", DIGEST, "--repo", str(CHECKOUT)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(response).encode())

    monkeypatch.setattr(subprocess, "run", read)
    return response


def verify(ref=DIGEST):
    return verify_candidate_snapshot(ref, CHECKOUT, BRANCH, HEAD, BASE)


def test_accepts_content_addressed_exact_range(review):
    assert verify("snapshot-runner:" + DIGEST) == {
        "snapshot_id": DIGEST,
        "status": "complete",
        "truncated": False,
        "evidence_gap": False,
    }


@pytest.mark.parametrize("value", ["a" * 38, "", "opaque"])
def test_rejects_malformed_identity_before_read(value):
    with pytest.raises(ControllerError, match="full Snapshot Runner SHA-256"):
        verify(value)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("evidence_schema_version", 2),
        ("repository", "another"),
        ("command", "diff-audit"),
        ("snapshot_id", "d" * 64),
        ("target_ref", "refs/heads/other"),
        ("target_head", BASE),
        ("base_commit", HEAD),
        ("merge_base_commit", HEAD),
    ],
)
def test_rejects_wrong_artifact_or_range(review, field, wrong):
    target = review if field in review else review["evidence"]["fields"]
    target[field] = wrong
    with pytest.raises(ControllerError, match="exact candidate range"):
        verify()


@pytest.mark.parametrize("truncated,gap", [(True, True), (False, False)])
def test_partial_coverage_requires_explicit_scope(review, truncated, gap):
    # Conversion omissions can make the range partial without truncating file contexts.
    review.update(status="partial", truncated=truncated, evidence_gap=gap)
    with pytest.raises(ControllerError, match="bounded"):
        verify()
    receipt = verify("snapshot-runner:bounded:" + DIGEST)
    assert (receipt["status"], receipt["truncated"], receipt["evidence_gap"]) == (
        "partial", truncated, gap,
    )


def test_unreadable_snapshot_fails_before_preparation(monkeypatch):
    def fail(argv, **_kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ControllerError, match="could not verify"):
        verify()


def test_repository_display_name_sanitization_matches_snapshot_runner(monkeypatch):
    special_checkout = Path("/test/my repo (v2)+中文")
    expected_repo = repository_display_name(special_checkout.name)
    assert expected_repo != special_checkout.name
    assert expected_repo.startswith("my_repo__v2-")
    response = {
        "evidence_schema_version": 1,
        "command": "branch-review",
        "snapshot_id": DIGEST,
        "repository": expected_repo,
        "status": "complete",
        "truncated": False,
        "evidence_gap": False,
        "evidence": {
            "fields": {
                "target_ref": "refs/heads/" + BRANCH,
                "target_head": HEAD,
                "base_commit": BASE,
                "merge_base_commit": BASE,
            }
        },
    }

    def read(argv, **kwargs):
        assert kwargs.get("timeout") == 60
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(response).encode())

    monkeypatch.setattr(subprocess, "run", read)
    receipt = verify_candidate_snapshot("snapshot-runner:" + DIGEST, special_checkout, BRANCH, HEAD, BASE)
    assert receipt["status"] == "complete"


@pytest.mark.parametrize(
    ("branch", "head", "base"),
    [
        (None, HEAD, BASE),
        ("", HEAD, BASE),
        ("refs/heads/main", HEAD, BASE),
        (BRANCH, "short", BASE),
        (BRANCH, HEAD, "not-a-sha"),
    ],
)
def test_rejects_invalid_candidate_identity_before_invoking_runner(monkeypatch, branch, head, base):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("should reject invalid identity before read")
    )
    with pytest.raises(ControllerError) as exc:
        verify_candidate_snapshot("snapshot-runner:" + DIGEST, CHECKOUT, branch, head, base)
    assert exc.value.code == "SNAPSHOT_CANDIDATE_MISMATCH"


def test_rejects_inconsistent_gap_with_complete_status_and_never_auto_accepts_bounded(review):
    review.update(status="complete", truncated=False, evidence_gap=True)
    with pytest.raises(ControllerError) as exc:
        verify("snapshot-runner:" + DIGEST)
    assert exc.value.code == "INVALID_SNAPSHOT_RECEIPT"
    with pytest.raises(ControllerError) as exc_bounded:
        verify("snapshot-runner:bounded:" + DIGEST)
    assert exc_bounded.value.code == "INVALID_SNAPSHOT_RECEIPT"

    # Complete snapshot must reject bounded prefix too.
    review.update(status="complete", truncated=False, evidence_gap=False)
    with pytest.raises(ControllerError) as exc_mismatch:
        verify("snapshot-runner:bounded:" + DIGEST)
    assert exc_mismatch.value.code == "SNAPSHOT_COVERAGE_MISMATCH"


def test_timeout_fails_closed_as_invalid_snapshot_receipt(monkeypatch):
    def timeout(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ControllerError) as exc:
        verify()
    assert exc.value.code == "INVALID_SNAPSHOT_RECEIPT"
