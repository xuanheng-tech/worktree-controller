from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from tests.test_cleanup_recovery import interrupted as interrupted
from tests.test_cleanup_recovery import prepare, preserve
from tests.test_preserved_cleanup import clean, dry_run
from tests.test_preserved_cleanup import preserved as preserved
from tests.test_typed_disposable import target as target
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as recovery
from worktree_controller import disposable, hardlinks, lifecycle
from worktree_controller import preservation as p
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def renew_preservation(fixture):
    repository, worktree, allocation, copied, _ = fixture
    plan = p.plan_preservation(repository.repo, worktree, allocation, "module-owner", 2)
    copied.update(p.copy_preservation(plan))


def assert_survivor(path, original, removed):
    current = p._identity(path.lstat())
    expected = list(original)
    expected[8] -= removed
    expected[5] = current[5]
    assert current == expected


@pytest.mark.parametrize("pair", ["typed", "same-resource", "cross-typed", "cross-preserved"])
def test_external_alias_retained_after_composed_cleanup_and_release(preserved, tmp_path, pair):
    repository, worktree, allocation, copied, _ = preserved
    source = worktree / "frontend/dist/assets/index.js"
    data = source.read_bytes()
    outside = tmp_path / "cache"
    outside.mkdir()
    alias = outside / "retained"
    os.link(source, alias)
    if pair != "typed":
        second = (
            worktree
            / {
                "same-resource": "frontend/dist/assets/second.js",
                "cross-typed": "frontend/node_modules/vite/shared.js",
                "cross-preserved": ".venv/agent-workspace/shared.js",
            }[pair]
        )
        os.link(source, second)
    if pair == "cross-preserved":
        renew_preservation(preserved)
    original = p._identity(alias.lstat())
    preserved_receipt = Path(copied["receipt"]).read_bytes()
    planned = dry_run(preserved)["plan"]
    assert not planned["blockers"]
    graph = planned["hardlink_topology"]
    assert graph["contract"] == hardlinks.EXTERNAL_CONTRACT
    key = f"{original[0]}:{original[1]}"
    group = graph["groups"][key]
    assert group["external_links"] == 1
    # The cleanup never needs to traverse the cache or know this alias's name.
    outside.chmod(0)
    try:
        result = clean(preserved, planned)
    finally:
        outside.chmod(0o700)
    assert result["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    assert_survivor(alias, original, len(group["members"]))
    assert alias.read_bytes() == data
    store = MetadataStore(inventory(repository.repo).common_dir)
    receipt, _ = e.read(
        store.completion_receipts_dir / (result["decision"]["receipt_id"] + ".json")
    )
    assert receipt["post_scan"]["blockers"] == []
    for action in receipt["action_evidence"]:
        before, _ = e.read(Path(action["before"]["path"]), action["before"]["sha256"])
        after, _ = e.read(Path(action["after"]["path"]), action["after"]["sha256"])
        assert (
            hardlinks.transition(
                before["topology"],
                after["topology"],
                action["resource"],
                after["mutation"].get("external_survivors"),
            )
            == after["mutation"]
        )
    lifecycle.release(
        repository.repo, target=worktree, expected_record_version=2, owner_release="module-owner"
    )
    assert store.load().records[allocation].lifecycle == "RELEASED"
    assert not worktree.exists() and alias.read_bytes() == data
    assert Path(copied["receipt"]).read_bytes() == preserved_receipt


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_external_special_inode_uses_no_endpoint_io(preserved, tmp_path, monkeypatch, kind):
    _, worktree, _, _, outside = preserved
    source = worktree / ".venv/agent-workspace" / ("pipe" if kind == "fifo" else "external")
    alias = tmp_path / "retained-special"
    os.link(source, alias, follow_symlinks=False)
    renew_preservation(preserved)
    original = p._identity(alias.lstat())
    original_open = os.open

    def no_endpoint_io(name, flags, *args, **kwargs):
        if str(name) in {"pipe", "external"}:
            assert flags & os.O_PATH
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_endpoint_io)
    result = clean(preserved, dry_run(preserved)["plan"])
    assert result["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    assert_survivor(alias, original, 1)
    if kind == "symlink":
        assert alias.is_symlink() and os.readlink(alias) == str(outside)
        assert (outside / "sentinel").read_bytes() == b"external sentinel"


def delete_tree(root, graph):
    resource = "frontend/dist"
    with (
        p._absolute_directory(root / "frontend") as chain,
        disposable._open_directory(
            chain[-1].fd, "dist", disposable._inode((root / resource).lstat())
        ) as directory,
    ):
        entries, _ = disposable._fd_inventory(directory.fd)
        return disposable._delete_fd_tree(
            directory,
            ".",
            entries,
            (*chain, directory),
            external_contents=hardlinks.external_contents(graph, resource),
        )


@pytest.fixture
def shared(tmp_path):
    root = tmp_path / "scope"
    directory = root / "frontend/dist"
    directory.mkdir(parents=True)
    source = directory / "data"
    source.write_bytes(b"immutable shared bytes")
    alias = tmp_path / "external"
    os.link(source, alias)
    return root, source, alias, hardlinks.inventory(root, ["frontend/dist"])


@pytest.mark.parametrize("mutation", ["same-size-content", "link-add", "link-remove", "mode"])
def test_external_race_at_unlink_blocks_completion(shared, tmp_path, monkeypatch, mutation):
    root, source, alias, graph = shared
    original = os.unlink
    metadata = alias.stat()

    def raced(name, *args, **kwargs):
        if name == "data":
            if mutation == "same-size-content":
                alias.write_bytes(b"X" * metadata.st_size)
                os.utime(alias, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            elif mutation == "link-add":
                os.link(alias, tmp_path / "unexpected")
            elif mutation == "link-remove":
                original(alias)
            else:
                alias.chmod(0o400)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", raced)
    with pytest.raises(ControllerError):
        delete_tree(root, graph)
    assert source.parent.is_dir()


@pytest.mark.parametrize("mutation", ["missing", "content", "identity", "extra"])
def test_external_survivor_evidence_is_required_and_exact(shared, mutation):
    root, _, _, graph = shared
    proof = delete_tree(root, graph)
    after = hardlinks.inventory(root, [])
    good = hardlinks.transition(graph, after, "frontend/dist", proof)
    assert good["contract"] == hardlinks.EXTERNAL_MUTATION_CONTRACT
    altered = copy.deepcopy(proof)
    first = next(iter(altered))
    if mutation == "missing":
        altered.clear()
    elif mutation == "content":
        altered[first]["content"]["content_sha256"] = "0" * 64
    elif mutation == "identity":
        altered[first]["identity"][8] += 1
    else:
        altered["unobserved"] = altered[first]
    with pytest.raises(ControllerError):
        hardlinks.transition(graph, after, "frontend/dist", altered)


def test_closed_graph_keeps_v1_encoding_and_rejects_external_field(tmp_path):
    root = tmp_path / "scope"
    directory = root / "frontend/dist"
    directory.mkdir(parents=True)
    (directory / "data").write_bytes(b"one")
    graph = hardlinks.inventory(root, ["frontend/dist"])
    assert graph["contract"] == hardlinks.CONTRACT
    assert all("external_links" not in group for group in graph["groups"].values())
    assert delete_tree(root, graph) == {}
    mutation = hardlinks.transition(graph, hardlinks.inventory(root, []), "frontend/dist")
    assert mutation["contract"] == hardlinks.MUTATION_CONTRACT
    assert "external_survivors" not in mutation
    next(iter(graph["groups"].values()))["external_links"] = 0
    graph["sha256"] = p._sha({k: v for k, v in graph.items() if k != "sha256"})
    with pytest.raises(ControllerError):
        hardlinks.validate(graph)


def test_external_cleanup_receipt_loss_retains_partial_journal_and_refuses_replay(
    preserved, tmp_path, monkeypatch
):
    repository, worktree, _, _, _ = preserved
    source = worktree / "backend/.venv/bin/python"
    alias = tmp_path / "retained-interpreter"
    os.link(source, alias)
    original = p._identity(alias.stat())
    planned = dry_run(preserved)["plan"]

    def lost(*args, **kwargs):
        raise OSError(5, "fixture after-receipt write loss")

    monkeypatch.setattr(e, "after", lost)
    with pytest.raises(ControllerError):
        clean(preserved, planned)
    assert not source.parent.parent.exists()
    assert_survivor(alias, original, 1)
    store = MetadataStore(inventory(repository.repo).common_dir)
    journal = next(store.transactions_dir.glob("*.json"))
    journal_bytes = journal.read_bytes()
    assert json.loads(journal_bytes)["actions"][0]["status"] == "FAILED_OR_PARTIAL"
    assert not (store.completion_receipts_dir / "None.json").exists()
    retry = clean(preserved, planned)
    assert retry["decision"]["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert journal.read_bytes() == journal_bytes
    assert (worktree / "frontend/dist").exists()


def test_represervation_recovery_accepts_v2_proofs_without_replaying_old_action(
    interrupted, tmp_path
):
    repository, worktree, _, store, context, _, old_bytes, _ = interrupted
    alias = tmp_path / "external-state"
    os.link(worktree / ".venv/agent-workspace/opaque.sqlite", alias)
    original = p._identity(alias.stat())
    new = preserve(interrupted)
    ref = prepare(interrupted, new, execute=True)["decision"]["recovery_receipt"]
    record, _ = e.read(Path(ref["path"]), ref["sha256"])
    assert record["plan"]["hardlink_topology"]["contract"] == hardlinks.EXTERNAL_CONTRACT
    result = recovery.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["code"] == "RECOVERY_CLEANED"
    assert recovery.completed(store, context["original_transaction_id"], verify_generations=True)
    assert (
        recovery.rollout_check(repository.repo)["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
    )
    assert_survivor(alias, original, 1)
    assert all(path.read_bytes() == raw for path, raw in old_bytes.items())
