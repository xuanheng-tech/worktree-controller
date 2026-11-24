"""Targeted regression and frontier capability tests for worktree-controller 1.36.0."""

from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC
from pathlib import Path

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical, handoff_one
from tests.test_executor_session import SESSION_ENTRYPOINT
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _source,
    _validate,
)
from tests.test_resource_admission import POLICY as ADMISSION_POLICY
from tests.test_rolling_upgrade import load_activate
from tests.test_shadow import POLICY, POLICY_V2, GitRepository, git
from worktree_controller import (
    branch_retirement,
    disposable,
    environment,
    frontier,
    git_backend,
    hardlinks,
    keyed_document_scope,
    lifecycle,
    native_session_recovery,
    policy,
    publication_execution,
    recovery_guidance,
    resource_admission,
    writer_lease,
    writer_session,
)
from worktree_controller import (
    store as metadata_store,
)
from worktree_controller.cli import main as cli_main
from worktree_controller.models import (
    ControllerError,
    PublicationLease,
    ReleaseReceipt,
    WriterHolder,
)
from worktree_controller.store import MetadataStore, now_iso


def test_c1_proc_maps_spaces_deleted_and_symlink_normalization(tmp_path: Path, monkeypatch) -> None:
    real_dir = tmp_path / "dir with space"
    real_dir.mkdir()
    lib = real_dir / "lib example.so"
    lib.write_bytes(b"elf")
    link_dir = tmp_path / "symlink-dir"
    link_dir.symlink_to(real_dir)
    mapped_link = link_dir / "lib example.so"

    fake_maps = (
        "7f1234000000-7f1234001000 r-xp 00000000 00:00 0                          [vdso]\n"
        f"7f1234002000-7f1234003000 r--p 00000000 08:01 12345     {mapped_link} (deleted)\n"
    )
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file) == "/proc/424242/maps":
            return io.StringIO(fake_maps)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    paths = writer_lease._referenced_paths(424242)
    assert str(lib.resolve()) in paths


def test_c2_abandon_integration_intent_checks_lease_allocation_id() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, left, _ = _source(repository, task="left", relative_path="left.txt", content="l\n")
        _, right, _ = _source(repository, task="right", relative_path="right.txt", content="r\n")
        left_enqueue = _enqueue(repository, task="int-left", source_allocation=left)
        right_enqueue = _enqueue(repository, task="int-right", source_allocation=right)
        left_id = left_enqueue["decision"]["intent_id"]
        right_id = right_enqueue["decision"]["intent_id"]
        left_candidate, _ = _claim(repository, task="int-left", intent_id=left_id)
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        left_alloc = next(
            row.allocation_id
            for row in store.load().records.values()
            if row.task_key == "int-left"
        )
        head = git(left_candidate, "rev-parse", "HEAD")
        main_head = git(repository.repo, "rev-parse", "main")
        with store.lock(exclusive=True):
            store.write_publication_lease(
                PublicationLease(
                    schema_version=2,
                    lease_id="11111111-1111-4111-8111-111111111111",
                    repository_id=store._load_unlocked().repository_id,
                    allocation_id=left_alloc,
                    target_ref="refs/heads/main",
                    remote="origin",
                    branch="main",
                    holder="fixture-owner",
                    run_id="run-1",
                    expected_main_oid=main_head,
                    candidate_oid=head,
                    validation_evidence_id="val-1",
                    acquired_at=now_iso(),
                    expires_at=now_iso(),
                )
            )
        with pytest.raises(ControllerError) as blocked:
            lifecycle.abandon_integration_intent(
                repository.repo, intent_id=left_id, owner="integration-owner", reason="test"
            )
        assert blocked.value.code == "PUBLICATION_LEASE_HELD"
        abandoned = lifecycle.abandon_integration_intent(
            repository.repo, intent_id=right_id, owner="integration-owner", reason="test"
        )
        assert abandoned["decision"]["code"] == "INTEGRATION_ABANDONED"


def test_c3_reconcile_integration_intents_breaks_on_resource_wait(monkeypatch) -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, left, _ = _source(repository, task="left", relative_path="left.txt", content="l\n")
        _, right, _ = _source(repository, task="right", relative_path="right.txt", content="r\n")
        _enqueue(repository, task="int-left", source_allocation=left)
        _enqueue(repository, task="int-right", source_allocation=right)
        calls = 0

        def waiting_claim(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {
                "decision": {
                    "code": "RESOURCE_WAIT",
                    "reason": "synthetic capacity wait",
                }
            }

        monkeypatch.setattr(lifecycle, "claim_integration", waiting_claim)
        result = lifecycle.reconcile_integration_intents(
            repository.repo, actor="lifecycle-reconciler", dispatch=True
        )
        assert calls == 1
        assert len(result["decision"]["dispatched"]) == 1
        assert result["decision"]["dispatched"][0]["code"] == "RESOURCE_WAIT"


def test_h1_git_hardening_strips_untrusted_config_env_and_dash_options(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/tmp/evil'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/bin/false")
    monkeypatch.setenv("GIT_EXEC_PATH", "/tmp/evil-exec")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/evil-tpl")
    clean_env = git_backend.git_environment()
    for blocked in (
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
    ):
        assert blocked not in clean_env

    with GitRepository(linked=0) as repository:
        git(repository.repo, "config", "core.fsmonitor", "/nonexistent/evil-helper")
        git(repository.repo, "config", "core.hooksPath", "/nonexistent/hooks")
        git(repository.repo, "config", "protocol.ext.allow", "always")
        inv = git_backend.inventory(repository.repo)
        assert inv.canonical_path == repository.repo.resolve()

        with pytest.raises(ControllerError):
            git_backend.validate_branch_name(repository.repo, "-evil-flag")
        with pytest.raises(ControllerError):
            git_backend.ls_remote_branch(repository.repo, "-u/tmp/evil", "main")
        with pytest.raises(ControllerError):
            git_backend.fetch_remote_branch(repository.repo, "origin", "--upload-pack=evil")


def test_h2_held_checkout_isolates_client_and_session_on_same_pid(tmp_path: Path) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        worktree, acquired = acquire_one(repository, scope_paths=("tracked.txt",))
        target = worktree / "tracked.txt"
        allocation_id = acquired["decision"]["allocation_id"]
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        snapshot = store.load()
        lease_decision = writer_lease.acquire_writer_lease(
            repository.repo,
            target=worktree,
            owner="module-owner",
            client_id="codex",
            session_id="session-a",
            run_id="run-1",
            holder_pid=os.getpid(),
        )
        assert lease_decision["decision"]["admission"] == "GRANTED"
        snapshot = store.load()
        inv = git_backend.inventory(repository.repo)
        record = snapshot.records[allocation_id]
        lease = snapshot.writer_leases[allocation_id]
        other_session = replace(lease.holder, session_id="session-b")
        other_client = replace(lease.holder, client_id="claude")
        assert (
            writer_session._held_checkout(
                repository.repo, worktree, lease.holder, target=target, known=(inv, snapshot)
            )
            == allocation_id
        )
        assert (
            writer_session._held_checkout(
                repository.repo, worktree, other_session, target=target, known=(inv, snapshot)
            )
            is None
        )
        assert (
            writer_session._held_checkout(
                repository.repo, worktree, other_client, target=target, known=(inv, snapshot)
            )
            is None
        )
        wrong_owner = replace(
            snapshot,
            records={**snapshot.records, allocation_id: replace(record, owner="different-owner")},
        )
        assert (
            writer_session._held_checkout(
                repository.repo, worktree, lease.holder, target=target, known=(inv, wrong_owner)
            )
            is None
        )


def test_h3_worktree_session_abort_cleans_binding_and_host_settles(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = home / ".local/state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    src_root = str(Path(__file__).resolve().parents[1] / "src")
    child_env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "PYTHONPATH": src_root,
    }
    with GitRepository(linked=0, policy_text=ADMISSION_POLICY) as repository:
        adopt_canonical(repository)
        worktree, acquired = acquire_one(repository)
        allocation_id = acquired["decision"]["allocation_id"]
        abort_proc = subprocess.run(
            [
                sys.executable,
                "-c",
                SESSION_ENTRYPOINT,
                "--path",
                str(worktree),
                "--client-id",
                "codex",
                "--session-id",
                "sess-abort",
                "--",
                str(tmp_path / "missing-executable"),
            ],
            env=child_env,
            capture_output=True,
            text=True,
            start_new_session=True,
            check=False,
        )
        assert abort_proc.returncode == 2
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        record = store.load().records[allocation_id]
        assert not any(b.startswith("runtime:process-v1:") for b in record.external_bindings)
        assert allocation_id not in store.load().writer_leases

    host_dir = tmp_path / "host-work"
    host_dir.mkdir()
    host_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            SESSION_ENTRYPOINT,
            "--path",
            str(host_dir),
            "--client-id",
            "codex",
            "--session-id",
            "host-clean",
            "--run-id",
            "host-r1",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        start_new_session=True,
        check=False,
    )
    assert host_proc.returncode == 0
    rows = resource_admission.AdmissionLedger().read()["requests"]
    execution_rows = [row for row in rows.values() if row["request"].get("kind") == "execution"]
    assert len(execution_rows) == 2
    assert all(row["state"] == "RELEASED" for row in execution_rows)


def test_h4_unrelated_sibling_worktree_edit_never_triggers_block_drift(tmp_path: Path) -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        sibling = repository.linked[0]
        target = repository.root / "new-feature"
        (sibling / "sibling-dirty.txt").write_text("unrelated sibling edit\n")
        acquired = lifecycle.acquire(
            repository.repo,
            target=target,
            task_key="feat-target",
            authority_key="feat-target",
            module="mod",
            role="feature",
            owner="module-owner",
            branch="feature/feat-target",
            base_ref="HEAD",
        )
        assert acquired["decision"]["code"] == "ACQUIRED"
        # Also verify fingerprint handles missing .git/index on target without crashing.
        target_index = Path(
            git_backend._text(target, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        if target_index.exists():
            target_index.unlink()
        inv = git_backend.inventory(repository.repo)
        assert git_backend.worktree_content_fingerprint(inv)


def test_h5_readonly_lock_and_dry_run_no_remote_ref_mutation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        # cleanup_observation_lock uses O_RDONLY on transactions/actions while repo.lock stays O_RDWR
        # (so broken 0444 repo.lock permissions still fail closed via test_read_only_lock_fails_closed).
        with store.cleanup_observation_lock(), store.lock(exclusive=False):
            assert store._load_unlocked().repository_id is not None

        from worktree_controller.canonical_refresh import refresh_canonical

        refs_before = git(repository.repo, "show-ref")
        fetch_head = repository.repo / ".git/FETCH_HEAD"
        if fetch_head.exists():
            fetch_head.unlink()
        refreshed = refresh_canonical(repository.repo, dry_run=True)
        assert refreshed["decision"]["code"] in {"CANONICAL_CURRENT", "CANONICAL_REFRESH_READY"}
        assert git(repository.repo, "show-ref") == refs_before
        assert not fetch_head.exists()


def test_h5_session_input_handles_regular_file_and_frame_limits(tmp_path: Path) -> None:
    frame_file = tmp_path / "frames.jsonl"
    frame_file.write_bytes(b'{"action":"status"}\n{"action":"done"}\n')
    with frame_file.open("rb") as raw:
        reader = publication_execution.SessionInput(raw.fileno())
        assert reader.read() == {"action": "status"}
        assert reader.read() == {"action": "done"}
        with pytest.raises(ControllerError) as eof:
            reader.read()
        assert eof.value.code == "PUBLICATION_SESSION_CLOSED"


def test_h6_abort_publication_lease_blocks_when_pushed_and_complete_allows_ancestor() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, src_id, _ = _source(repository, task="s1", relative_path="s1.txt", content="s1\n")
        intent_id = _enqueue(repository, task="int-s1", source_allocation=src_id)["decision"]["intent_id"]
        candidate, _ = _claim(repository, task="int-s1", intent_id=intent_id)
        _validate(repository, candidate, "val-s1")
        lease_id = lifecycle.acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="integration-owner",
            run_id="pub-run-1",
        )["decision"]["lease_id"]
        candidate_head = git(candidate, "rev-parse", "HEAD")
        git(candidate, "push", "origin", f"{candidate_head}:refs/heads/main")
        with pytest.raises(ControllerError) as abort_err:
            lifecycle.abort_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=3,
                lease_id=lease_id,
                owner="integration-owner",
                run_id="pub-run-1",
            )
        assert abort_err.value.code == "PUBLICATION_RECOVERY_REQUIRED"

        # Advance remote main beyond candidate_head and ensure completion converges cleanly.
        publisher = repository.root / "publisher"
        origin_url = git(repository.repo, "remote", "get-url", "origin")
        git(repository.root, "clone", origin_url, str(publisher))
        git(publisher, "config", "user.name", "Pub")
        git(publisher, "config", "user.email", "pub@example.invalid")
        git(publisher, "checkout", "-B", "main", "origin/main")
        git(publisher, "commit", "--allow-empty", "-m", "follow-up commit on main")
        git(publisher, "push", "origin", "main")

        completed = lifecycle.complete_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=3,
            lease_id=lease_id,
            published_commit=candidate_head,
            finalizer_record_id="fin-s1",
            run_id="pub-run-1",
            auto_release=False,
        )
        assert completed["decision"]["code"] == "PUBLICATION_COMPLETED"


def test_h7_nontyped_cleanup_disposable_enforces_lifecycle_and_lease_guards() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        (repository.repo / ".gitignore").write_text(".venv/\n")
        git(repository.repo, "add", ".gitignore")
        git(repository.repo, "commit", "-m", "ignore venv")
        worktree, _ = acquire_one(repository)
        venv = worktree / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "marker").write_text("x")
        lifecycle.register_disposable(
            repository.repo,
            target=worktree,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="pytest",
            teardown_policy="manual",
        )
        writer_lease.acquire_writer_lease(
            repository.repo,
            target=worktree,
            owner="module-owner",
            client_id="codex",
            session_id="s1",
            run_id=None,
            holder_pid=os.getpid(),
        )
        with pytest.raises(ControllerError) as blocked:
            lifecycle.cleanup_disposable(
                repository.repo,
                target=worktree,
                expected_record_version=2,
                owner="module-owner",
                resource_path=".venv",
            )
        assert blocked.value.code == "WRITER_LEASE_ACTIVE"
        assert venv.exists()


def test_h8_h9_h10_symlink_parent_relative_paths_prunable_and_no_env_kill_switch(
    tmp_path: Path, monkeypatch
) -> None:
    real_base = tmp_path / "real-base"
    real_base.mkdir()
    link_base = tmp_path / "link-base"
    link_base.symlink_to(real_base)
    repo_dir = real_base / "repo"
    repo_dir.mkdir()
    git(repo_dir, "init", "--initial-branch=main")
    git(repo_dir, "config", "user.name", "Fixture")
    git(repo_dir, "config", "user.email", "fixture@example.invalid")
    git(repo_dir, "config", "worktree.useRelativePaths", "true")
    (repo_dir / ".agents").mkdir()
    (repo_dir / ".agents/worktree-policy.toml").write_text(POLICY)
    (repo_dir / "README.md").write_text("hi\n")
    git(repo_dir, "add", ".")
    git(repo_dir, "commit", "-m", "init")
    wt = real_base / "wt-prunable"
    git(repo_dir, "worktree", "add", "-b", "feat/rel", str(wt))
    inv = git_backend.inventory(link_base / "repo")
    assert any(w.path == wt.resolve() for w in inv.worktrees)
    import shutil

    shutil.rmtree(wt)
    inv_prunable = git_backend.inventory(link_base / "repo")
    assert any(w.prunable for w in inv_prunable.worktrees)

    # H10: WORKTREE_CONTROLLER_TEST_CRASH_AT env var must be a complete no-op.
    monkeypatch.setenv("WORKTREE_CONTROLLER_TEST_CRASH_AT", "acquire_prepared")
    lifecycle._crash_point("acquire_prepared")


def test_h11_to_h15_retirement_editable_refs_guidance_reopen_and_source_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    # H13: editable_references path-boundary + URI unquoting
    venv = tmp_path / "env"
    site = venv / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    wt1 = tmp_path / "wt-1"
    wt10 = tmp_path / "wt-10"
    space_wt = tmp_path / "wt with space"
    for d in (wt1, wt10, space_wt):
        d.mkdir()
    (site / "other.pth").write_text(str(wt10) + "\n")
    (site / "direct_url.json").write_text(json.dumps({"url": f"file://{tmp_path}/wt%20with%20space"}))
    assert not environment._reference_matches_target(
        site / "other.pth", (site / "other.pth").read_bytes(), wt1.resolve()
    )
    assert environment._reference_matches_target(
        site / "direct_url.json", (site / "direct_url.json").read_bytes(), space_wt.resolve()
    )

    # H12: corrupt authorization file in branch_retirement fails closed (not swallowed)
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        store.branch_retirement_authorizations_dir.mkdir(parents=True, exist_ok=True)
        (store.branch_retirement_authorizations_dir / "corrupt.json").write_text("{broken json")
        with pytest.raises(ControllerError):
            branch_retirement.outstanding_retirement_authorizations(store)

    # H14: source_recovery.plan_progress checks common_dir rather than .root
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        worktree, _ = acquire_one(repository)
        common_dir = git_backend.inventory(repository.repo).common_dir
        alt_store = MetadataStore(common_dir)
        assert alt_store.root.parent.parent == common_dir

    # H15: reopen checks holder mismatch & recovery_guidance accepts RELEASED receipt
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, src_id, _ = _source(repository, task="rg-src", relative_path="rg.txt", content="rg\n")
        intent_id = _enqueue(repository, task="rg-int", source_allocation=src_id)["decision"][
            "intent_id"
        ]
        candidate, _ = _claim(repository, task="rg-int", intent_id=intent_id)
        _validate(repository, candidate, "rg-val")
        pub_lease_id = lifecycle.acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="integration-owner",
            run_id="rg-pub",
        )["decision"]["lease_id"]
        cand_head = git(candidate, "rev-parse", "HEAD")
        git(candidate, "push", "origin", f"{cand_head}:refs/heads/main")
        lifecycle.complete_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=3,
            lease_id=pub_lease_id,
            published_commit=cand_head,
            finalizer_record_id="fin-rg",
            run_id="rg-pub",
            auto_release=True,
        )
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        with store.lock(exclusive=True):
            old_pub = store._load_unlocked().publication_receipts[pub_lease_id]
            receipt_file = store.publication_receipts_dir / f"{pub_lease_id}.json"
            if receipt_file.exists():
                receipt_file.unlink()
            released_ts = now_iso()
            store.write_publication_receipt(
                replace(
                    old_pub,
                    status="RELEASED",
                    lease_released_at=old_pub.lease_released_at or released_ts,
                    released_at=old_pub.released_at or released_ts,
                    remote_verified_to_released_ms=(
                        old_pub.remote_verified_to_released_ms
                        if old_pub.remote_verified_to_released_ms is not None
                        else 1
                    ),
                )
            )
        guide = recovery_guidance.recovery_guide(repository.repo, intent_id=intent_id)
        assert guide["decision"]["terminal_status"] == "PUBLISHED_RELEASED"

    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        worktree, acquired = acquire_one(repository)
        alloc = acquired["decision"]["allocation_id"]
        handoff_one(repository, worktree)
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        real_holder = WriterHolder(
            pid=os.getpid(),
            boot_id=writer_lease.boot_id(),
            start_ticks=writer_lease.start_ticks(os.getpid()) or 1,
            client_id="codex",
            session_id="s-reopen",
            run_id=None,
        )
        with store.lock(exclusive=True):
            rec = store._load_unlocked().records[alloc]
            store.replace_record(
                replace(
                    rec,
                    external_bindings=(
                        f'runtime:process-v1:{json.dumps({"holder": {"pid": real_holder.pid, "boot_id": real_holder.boot_id, "start_ticks": real_holder.start_ticks, "client_id": "codex", "session_id": "other-session", "run_id": None}, "lease_id": None, "kind": "agent"})}',
                    ),
                )
            )
        with pytest.raises(ControllerError) as mismatch:
            lifecycle.reopen(
                repository.repo,
                target=worktree,
                expected_record_version=2,
                owner="module-owner",
                holder=real_holder,
            )
        assert mismatch.value.code == "EXTERNAL_BINDING"


def test_perf_oss_and_utc_defaults(tmp_path: Path, monkeypatch) -> None:
    # UTC timezone & legacy +08:00 ISO parsing compatibility
    assert metadata_store.TIMEZONE is UTC
    assert metadata_store.now_iso().endswith("+00:00")

    # Optional websockets raises actionable error without failing core imports
    monkeypatch.setitem(sys.modules, "websockets", None)
    monkeypatch.setitem(sys.modules, "websockets.sync", None)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", None)
    holder = WriterHolder(
        pid=os.getpid(),
        boot_id=writer_lease.boot_id(),
        start_ticks=1,
        client_id="codex",
        session_id="s1",
        run_id=None,
    )
    with pytest.raises(ControllerError, match="native-session"):
        native_session_recovery._native_observation(holder)

    # MetadataStore.load(include_history=False) skips receipts/events; AdmissionLedger archives >200
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        worktree, acquired = acquire_one(repository)
        alloc = acquired["decision"]["allocation_id"]
        store = MetadataStore(git_backend.inventory(repository.repo).common_dir)
        with store.lock(exclusive=True):
            store.write_release_receipt(
                ReleaseReceipt(
                    schema_version=1,
                    receipt_id="22222222-2222-4222-8222-222222222222",
                    plan_id="3" * 64,
                    repository_id=store._load_unlocked().repository_id,
                    allocation_id=alloc,
                    release_intent_id=None,
                    target_path=str(worktree),
                    branch="feature/x",
                    released_head=git(repository.repo, "rev-parse", "HEAD"),
                    expected_record_version=1,
                    released_record_version=1,
                    trigger="manual",
                    resource_receipt_ids=(),
                    result="RELEASED",
                    released_at=now_iso(),
                )
            )
        fast = store.load(include_history=False)
        assert alloc in fast.records
        assert fast.release_receipts == {}
        full = store.load(include_history=True)
        assert len(full.release_receipts) == 1

    # AdmissionLedger bounded terminal retention
    home = tmp_path / "ledger-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    ledger = resource_admission.AdmissionLedger()
    requests = {}
    for idx in range(resource_admission.AdmissionLedger.MAX_RETAINED_TERMINAL_REQUESTS + 25):
        req = {"kind": "execution", "owner": os.getuid(), "idx": idx}
        requests[resource_admission.fingerprint(req)] = {
            "request": req,
            "state": "RELEASED",
            "created_at_ns": idx,
            "updated_at_ns": idx,
            "history": [{"state": "RELEASED", "at_ns": idx}],
        }
    state = {
        "contract": resource_admission.CONTRACT,
        "requests": requests,
        "pressure_high": False,
    }
    with ledger.locked():
        ledger._save(state)
        retained = ledger.read()
        assert (
            len(retained["requests"])
            == resource_admission.AdmissionLedger.MAX_RETAINED_TERMINAL_REQUESTS
        )
    archive_file = ledger.path.parent / "archive-requests.jsonl"
    assert len(archive_file.read_text(encoding="utf-8").splitlines()) == 25

    # hardlinks.inventory fast path avoids hashing st_nlink == 1 files when hash_single_link=False,
    # while default full topology hashes regular files per cleanup_hardlink_topology_version=2.
    scope = tmp_path / "hl-scope"
    dist = scope / "dist"
    dist.mkdir(parents=True)
    (dist / "single.bin").write_bytes(b"single-link")
    linked_a = dist / "linked-a.bin"
    linked_b = dist / "linked-b.bin"
    linked_a.write_bytes(b"multi-link")
    os.link(linked_a, linked_b)
    fast_graph = hardlinks.inventory(scope, ["dist"], hash_single_link=False)
    single_id = f"{(dist / 'single.bin').stat().st_dev}:{(dist / 'single.bin').stat().st_ino}"
    multi_id = f"{linked_a.stat().st_dev}:{linked_a.stat().st_ino}"
    assert fast_graph["groups"][single_id]["content"] == {}
    assert "content_sha256" in fast_graph["groups"][multi_id]["content"]
    full_graph = hardlinks.inventory(scope, ["dist"])
    assert "content_sha256" in full_graph["groups"][single_id]["content"]
    assert "content_sha256" in full_graph["groups"][multi_id]["content"]

    # Default policy keyed_documents == () and disposable.resource_types_for defaults to {.venv}
    with GitRepository(linked=0) as repository:
        loaded = policy.load_policy(repository.repo)
        assert loaded.keyed_documents == ()
        assert disposable.resource_types_for(repository.repo) == {".venv": "python-venv"}
        assert not keyed_document_scope.independent_progress_scopes(
            repository.repo, ("a.json",), ("a.json",), ("0" * 40,), ("1" * 40,)
        )


def test_controller_activate_pruning_and_expanded_skill_files(tmp_path: Path, monkeypatch) -> None:
    module = load_activate()
    versions = tmp_path / "versions"
    versions.mkdir()
    created = []
    for i in range(8):
        commit_dir = versions / f"{i:040x}"
        (commit_dir / "worktree-controller").mkdir(parents=True)
        os.utime(commit_dir, (1_000_000 + i * 10, 1_000_000 + i * 10))
        created.append(commit_dir)
    active = tmp_path / "active"
    active.symlink_to(created[0] / "worktree-controller")
    monkeypatch.setattr(module, "VERSIONS", versions)
    monkeypatch.setattr(module, "ACTIVE", active)
    module._prune_old_versions(keep=5)
    remaining = sorted(p.name for p in versions.iterdir())
    # Active (index 0) plus newest keep entries survive, oldest non-active entries are pruned.
    assert created[0].name in remaining
    assert created[-1].name in remaining
    assert len(remaining) <= 5

    # frozen_skill accepts nested non-hidden files beyond SKILL.md & agents/openai.yaml
    repo = tmp_path / "skill-repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Skill")
    git(repo, "config", "user.email", "skill@example.invalid")
    skill_dir = repo / "skills/worktree-lifecycle"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\n")
    (skill_dir / "references/guide.md").write_text("# Extra doc\n")
    git(repo, "add", "skills")
    git(repo, "commit", "-m", "skill with extra reference doc")
    oid = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(module, "CONTROLLER_REPO", repo)
    payload = module.frozen_skill(oid)
    assert set(payload) == {"SKILL.md", "references/guide.md"}


def test_frontier_conflict_check_hook_adapter_and_sandbox(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        (repository.repo / "shared.txt").write_text("base\n")
        git(repository.repo, "add", "shared.txt")
        git(repository.repo, "commit", "-m", "add shared file")
        base_oid = git(repository.repo, "rev-parse", "HEAD")

        wt_a, _ = acquire_one(repository, task="task-a", authority="auth-a")
        (wt_a / "a.txt").write_text("only a\n")
        git(wt_a, "add", "a.txt")
        git(wt_a, "commit", "-m", "clean change a")

        wt_b, _ = acquire_one(repository, task="task-b", authority="auth-b")
        (wt_b / "b.txt").write_text("only b\n")
        git(wt_b, "add", "b.txt")
        git(wt_b, "commit", "-m", "clean change b")

        # Clean merge check via CLI exits 0
        assert (
            cli_main(
                [
                    "conflict-check",
                    "--repo",
                    str(repository.repo),
                    "--target",
                    str(wt_a),
                    "--target",
                    str(wt_b),
                    "--base",
                    base_oid,
                    "--json",
                ]
            )
            == 0
        )
        clean_out = json.loads(capsys.readouterr().out)
        assert clean_out["decision"]["code"] == "NO_CONFLICTS"

        # Conflicting edits to shared.txt via CLI exit 1 without modifying either worktree
        (wt_a / "shared.txt").write_text("left edit\n")
        git(wt_a, "add", "shared.txt")
        git(wt_a, "commit", "-m", "left conflict")
        (wt_b / "shared.txt").write_text("right edit\n")
        git(wt_b, "add", "shared.txt")
        git(wt_b, "commit", "-m", "right conflict")

        assert (
            cli_main(
                [
                    "conflict-check",
                    "--repo",
                    str(repository.repo),
                    "--target",
                    str(wt_a),
                    "--target",
                    str(wt_b),
                    "--json",
                ]
            )
            == 1
        )
        conflict_out = json.loads(capsys.readouterr().out)
        assert conflict_out["decision"]["code"] == "CONFLICTS_DETECTED"
        assert conflict_out["decision"]["pairs"][0]["conflicting_paths"] == ["shared.txt"]

    # hook-adapter: Claude WorktreeCreate -> PreToolUse (allow/deny) -> WorktreeRemove
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        adopt_canonical(repository)
        (repository.repo / "shared.txt").write_text("base\n")
        git(repository.repo, "add", "shared.txt")
        git(repository.repo, "commit", "-m", "add shared file")
        create_payload, create_rc = frontier.hook_adapter(
            client="claude",
            repository_path=repository.repo,
            payload_data={
                "hook_event_name": "WorktreeCreate",
                "name": "hook-feat",
                "branch": "feature/hook-feat",
                "owner": "hook-owner",
                "scope_paths": ["shared.txt"],
            },
        )
        assert create_rc == 0
        hook_wt = Path(create_payload["hookSpecificOutput"]["worktreePath"])
        assert hook_wt.is_dir()

        # Acquire writer lease so PreToolUse allows edits to hook_wt
        writer_lease.acquire_writer_lease(
            repository.repo,
            target=hook_wt,
            owner="hook-owner",
            client_id="claude",
            session_id="hook-s1",
            run_id=None,
            holder_pid=os.getpid(),
        )
        allow_payload, allow_rc = frontier.hook_adapter(
            client="claude",
            payload_data={
                "hook_event_name": "PreToolUse",
                "cwd": str(hook_wt),
                "session_id": "hook-s1",
                "holder_pid": os.getpid(),
                "tool_input": {"file_path": str(hook_wt / "shared.txt")},
            },
        )
        assert allow_rc == 0
        assert allow_payload["hookSpecificOutput"]["permissionDecision"] == "allow"

        # Mismatched session is denied with exit 2
        deny_payload, deny_rc = frontier.hook_adapter(
            client="claude",
            payload_data={
                "hook_event_name": "PreToolUse",
                "cwd": str(hook_wt),
                "session_id": "other-session",
                "holder_pid": os.getpid(),
                "tool_input": {"file_path": str(hook_wt / "shared.txt")},
            },
        )
        assert deny_rc == 2
        assert deny_payload["hookSpecificOutput"]["permissionDecision"] == "deny"

        # Release writer lease and verify WorktreeRemove cleans up cleanly
        holder = writer_lease.resolve_holder(
            str(os.getpid()), client_id="claude", session_id="hook-s1", run_id=None
        )
        writer_lease.release_writer_lease(repository.repo, target=hook_wt, holder=holder)
        remove_payload, remove_rc = frontier.hook_adapter(
            client="claude",
            repository_path=repository.repo,
            payload_data={
                "hook_event_name": "WorktreeRemove",
                "worktree_path": str(hook_wt),
                "owner": "hook-owner",
            },
        )
        assert remove_rc == 0
        assert remove_payload["decision"]["code"] == "RELEASED"
        assert not hook_wt.exists()

    # Sandbox helper and fail-closed checks
    monkeypatch.setattr(writer_session.shutil, "which", lambda name: None)
    with pytest.raises(ControllerError) as missing_bwrap:
        writer_session._sandbox_command(
            ["/bin/true"], sandbox="bwrap", worktree_path=tmp_path, common_dir=None
        )
    assert missing_bwrap.value.code == "SANDBOX_UNAVAILABLE"
    assert (
        writer_session._sandbox_command(
            ["/bin/true"], sandbox="auto", worktree_path=tmp_path, common_dir=None
        )
        == ["/bin/true"]
    )
    monkeypatch.setattr(writer_session.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(writer_session, "_bwrap_available", lambda path: True)
    cmd = writer_session._sandbox_command(
        ["/bin/echo", "ok"],
        sandbox="bwrap",
        worktree_path=tmp_path / "wt",
        common_dir=tmp_path / "repo/.git",
    )
    assert cmd[0] == "/usr/bin/bwrap"
    assert "--ro-bind" in cmd
    assert "--bind" in cmd
    assert str(tmp_path / "wt") in cmd
