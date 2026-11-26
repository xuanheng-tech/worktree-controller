"""Isolated end-to-end security tests for unified Claude & Grok host write-fence hooks."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_convergence import POLICY_V8
from tests.test_shadow import GitRepository
from worktree_controller.writer_lease import acquire_writer_lease

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_SCRIPT = ROOT / "scripts/controller-consumer"


@pytest.fixture
def consumer():
    return runpy.run_path(str(CONSUMER_SCRIPT))


def _make_fake_wc_home(tmp_path: Path, wc_src: Path, *, allow_paths: set[str] | None = None) -> tuple[Path, Path]:
    """Create a fake HOME/.local/bin with spy/delegating worktree-controller & worktree-session-end."""
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local/bin"
    local_bin.mkdir(parents=True)
    log_file = tmp_path / "calls.jsonl"
    allow_json = json.dumps(sorted(allow_paths or set()))
    wc_stub = local_bin / "worktree-controller"
    wc_stub.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
with open({str(log_file)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"bin": "worktree-controller", "args": args}}) + "\\n")
allowed = set({allow_json})
if args[:2] == ["writer-session", "describe"]:
    path = args[2]
    print(json.dumps({{"decision": {{"code": "WRITER_SESSION_DESCRIBED", "repository_path": path}}}}))
    raise SystemExit(0)
if args[:2] == ["writer-session", "check"]:
    target_path = str(Path(args[3]).resolve(strict=False))
    if target_path in allowed:
        print(json.dumps({{"decision": {{"admission": "GRANTED", "code": "WRITE_ALLOWED"}}}}))
        raise SystemExit(0)
    print(json.dumps({{"decision": {{"admission": "REFUSED", "code": "BLOCK_WRITER_LEASE_REQUIRED", "reason": "writer lease required"}}}}))
    raise SystemExit(2)
if args[:2] == ["writer-session", "host-check"]:
    target_path = str(Path(args[2]).resolve(strict=False))
    if target_path in allowed:
        print(json.dumps({{"decision": {{"admission": "GRANTED", "code": "WRITE_ALLOWED"}}}}))
        raise SystemExit(0)
    print(json.dumps({{"decision": {{"admission": "REFUSED", "code": "WRITER_LEASE_MISMATCH", "reason": "host worktree differs"}}}}))
    raise SystemExit(2)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    wc_stub.chmod(0o755)

    end_stub = local_bin / "worktree-session-end"
    end_stub.write_text(
        f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open({str(log_file)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"bin": "worktree-session-end", "args": args}}) + "\\n")
print(json.dumps({{
    "selection_complete": True,
    "allocations": [],
    "previous_allocations": [],
    "expected_allocations": [],
    "summary": {{"contract": "session-closeout/v1", "selected": 0, "pending": 0, "terminal_complete": True}},
}}))
""",
        encoding="utf-8",
    )
    end_stub.chmod(0o755)
    return fake_home, log_file


def _invoke_hook(
    tmp_path: Path,
    *,
    entry_name: str,
    event: str,
    payload: dict,
    fake_home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    entry = tmp_path / entry_name
    if not entry.exists():
        entry.write_bytes(CONSUMER_SCRIPT.read_bytes())
        entry.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONTROLLER_WRITER_")}
    env["HOME"] = str(fake_home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(entry), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _read_calls(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.parametrize(
    "command",
    [
        # (a) previously fail-open mutating Bash in canonical
        "bash -c 'echo x > tracked.txt'",
        "sh -c 'rm tracked.txt'",
        "python3 /tmp/w.py",
        "awk 'BEGIN{print \"x\" > \"tracked.txt\"}'",
        "git push origin main",
        "git commit --allow-empty -m x",
        "git -c core.fsmonitor='touch pwn' status",
        "make build",
        "just test",
        # (b) previously bypassed inline Python AST patterns
        "python3 -c 'from os import remove; remove(\"foo.py\")'",
        "python3 -c 'import os as o; o.remove(\"foo.py\")'",
        "python3 -c 'import shutil as s; s.rmtree(\".\")'",
        "python3 -c 'import os; fd=os.open(\"foo.py\", os.O_WRONLY); os.close(fd)'",
        # Other shell/command-substitution/sed-exec escapes
        "echo \"$(rm tracked.txt)\"",
        "echo `rm tracked.txt`",
        "ls | xargs rm",
        "sed -n '1e touch pwn' tracked.txt",
        "sed -n '1w pwn' tracked.txt",
        "sort -o tracked.txt tracked.txt",
        "find . -delete",
        "echo hi > tracked.txt",
        "worktree-session --path . --client-id claude --session-id s -- rm tracked.txt",
    ],
)
@pytest.mark.parametrize("client", ["claude", "grok"])
def test_canonical_blocks_all_non_readonly_bash_fail_closed(tmp_path: Path, client: str, command: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        fake_home, _ = _make_fake_wc_home(tmp_path, ROOT)
        if client == "claude":
            payload = {
                "hook_event_name": "PreToolUse",
                "cwd": str(repository.repo),
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
            entry_name = "claude-worktree-hook"
        else:
            payload = {
                "hookEventName": "pre_tool_use",
                "cwd": str(repository.repo),
                "sessionId": "g1",
                "toolName": "run_terminal_command",
                "toolInput": {"command": command},
            }
            entry_name = "grok-worktree-hook"
        res = _invoke_hook(tmp_path, entry_name=entry_name, event="PreToolUse", payload=payload, fake_home=fake_home)
        assert res.returncode == 2, f"{client} failed to block canonical command {command!r}: {res.stderr}"
        assert "canonical" in res.stderr.lower()


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short --branch",
        "git diff --stat",
        "git log -n 5 --oneline",
        "git branch --show-current",
        "git tag -l",
        "git config --get user.name",
        "ls -la",
        "cat README.md | head -n 20",
        "rg -n foo src/ 2>/dev/null | wc -l",
        "sed -n '1,40p' README.md",
        "find src -name '*.py'",
        "sort README.md | uniq",
        "worktree-controller status --json",
        "snapshot-runner repo-status --repo . --summary",
        "ruff check src/",
    ],
)
@pytest.mark.parametrize("client", ["claude", "grok"])
def test_canonical_allows_strict_readonly_bash_fast_path(tmp_path: Path, client: str, command: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        fake_home, log_file = _make_fake_wc_home(tmp_path, ROOT)
        if client == "claude":
            payload = {
                "hook_event_name": "PreToolUse",
                "cwd": str(repository.repo),
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
            entry_name = "claude-worktree-hook"
        else:
            payload = {
                "hookEventName": "pre_tool_use",
                "cwd": str(repository.repo),
                "sessionId": "g1",
                "toolName": "run_terminal_command",
                "toolInput": {"command": command},
            }
            entry_name = "grok-worktree-hook"
        res = _invoke_hook(tmp_path, entry_name=entry_name, event="PreToolUse", payload=payload, fake_home=fake_home)
        assert res.returncode == 0, f"{client} falsely blocked readonly command {command!r}: {res.stderr}"
        assert _read_calls(log_file) == []


def test_cross_worktree_writes_always_check_target_worktree_lease(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        other_wt, _ = acquire_one(repository, task="other-task", authority="other-auth")
        leased_wt, _ = acquire_one(repository, task="my-task", authority="my-auth")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "note.txt").write_text("ok\n", encoding="utf-8")
        symlink_to_canonical = outside_dir / "sneaky-canon.txt"
        symlink_to_canonical.symlink_to(repository.repo / "README.md")
        symlink_to_other = outside_dir / "sneaky-other.txt"
        symlink_to_other.symlink_to(other_wt / "README.md")

        # Only leased_wt is granted by Controller; other_wt is refused.
        fake_home, log_file = _make_fake_wc_home(
            tmp_path, ROOT, allow_paths={str(leased_wt.resolve())}
        )

        # (c1) Edit/Write to other_wt from outside_dir or canonical -> MUST call writer-session check on other_wt & BLOCK!
        for start_cwd in (outside_dir, repository.repo):
            for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
                log_file.unlink(missing_ok=True)
                res = _invoke_hook(
                    tmp_path,
                    entry_name="claude-worktree-hook",
                    event="PreToolUse",
                    payload={
                        "hook_event_name": "PreToolUse",
                        "cwd": str(start_cwd),
                        "session_id": "s1",
                        "tool_name": tool,
                        "tool_input": {key: str(other_wt / "README.md")},
                    },
                    fake_home=fake_home,
                )
                assert res.returncode == 2
                calls = _read_calls(log_file)
                assert len(calls) == 1
                assert calls[0]["args"][:4] == ["writer-session", "check", "claude", str(other_wt.resolve())]

        # (c2) Grok search_replace to other_wt from canonical -> BLOCKED via writer-session check on other_wt!
        log_file.unlink(missing_ok=True)
        res = _invoke_hook(
            tmp_path,
            entry_name="grok-worktree-hook",
            event="PreToolUse",
            payload={
                "hookEventName": "pre_tool_use",
                "cwd": str(repository.repo),
                "sessionId": "g1",
                "toolName": "search_replace",
                "toolInput": {"file_path": str(other_wt / "README.md")},
            },
            fake_home=fake_home,
        )
        assert res.returncode == 2
        assert _read_calls(log_file)[0]["args"][:4] == ["writer-session", "check", "grok", str(other_wt.resolve())]

        # (c3) Bash targeting other_wt from outside_dir or canonical -> BLOCKED!
        for cmd in (
            f"echo pwn > {other_wt}/README.md",
            f"python3 -c 'open({str(other_wt / 'README.md')!r}, \"w\").write(\"x\")'",
            f"cd {other_wt} && python3 /tmp/w.py",
        ):
            res = _invoke_hook(
                tmp_path,
                entry_name="claude-worktree-hook",
                event="PreToolUse",
                payload={
                    "hook_event_name": "PreToolUse",
                    "cwd": str(outside_dir),
                    "session_id": "s1",
                    "tool_name": "Bash",
                    "tool_input": {"command": cmd},
                },
                fake_home=fake_home,
            )
            assert res.returncode == 2, f"Failed to block cross-worktree bash {cmd!r}"

        # (c4) Symlinks from outside_dir into canonical or other_wt -> CAUGHT!
        res_canon_sym = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(outside_dir),
                "session_id": "s1",
                "tool_name": "Write",
                "tool_input": {"file_path": str(symlink_to_canonical)},
            },
            fake_home=fake_home,
        )
        assert res_canon_sym.returncode == 2
        res_other_sym = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(outside_dir),
                "session_id": "s1",
                "tool_name": "Write",
                "tool_input": {"file_path": str(symlink_to_other)},
            },
            fake_home=fake_home,
        )
        assert res_other_sym.returncode == 2

        # Positive: legitimate Edit and Bash into leased_wt SUCCEED!
        res_ok_edit = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "s1",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(leased_wt / "README.md")},
            },
            fake_home=fake_home,
        )
        assert res_ok_edit.returncode == 0
        res_ok_bash = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q"},
            },
            fake_home=fake_home,
        )
        assert res_ok_bash.returncode == 0

        # Positive: writing outside governance from outside_dir or canonical SUCCEEDS!
        res_out = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(repository.repo),
                "session_id": "s1",
                "tool_name": "Write",
                "tool_input": {"file_path": str(outside_dir / "note.txt")},
            },
            fake_home=fake_home,
        )
        assert res_out.returncode == 0

        # Positive: worktree-session targeting linked worktree allowed from canonical
        res_ws = _invoke_hook(
            tmp_path,
            entry_name="claude-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(repository.repo),
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"worktree-session --path {leased_wt} --client-id claude --session-id s1 -- pytest -q"
                },
            },
            fake_home=fake_home,
        )
        assert res_ws.returncode == 0


def test_grok_session_end_invokes_worktree_session_end(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        wt, _ = acquire_one(repository, task="grok-task", authority="grok-auth")
        fake_home, log_file = _make_fake_wc_home(tmp_path, ROOT)
        res = _invoke_hook(
            tmp_path,
            entry_name="grok-worktree-hook",
            event="SessionEnd",
            payload={
                "hookEventName": "session_end",
                "cwd": str(wt),
                "sessionId": "grok-sess-42",
            },
            fake_home=fake_home,
        )
        assert res.returncode == 0
        calls = _read_calls(log_file)
        assert len(calls) == 2
        assert calls[0] == {
            "bin": "worktree-controller",
            "args": ["writer-session", "describe", str(wt), "ancestor:grok"],
        }
        assert calls[1] == {
            "bin": "worktree-session-end",
            "args": [
                "--path",
                str(wt),
                "--client-id",
                "grok",
                "--session-id",
                "grok-sess-42",
                "--holder",
                "ancestor:grok",
                "--trigger",
                "task-terminal",
            ],
        }


def test_real_controller_lease_enforced_end_to_end(tmp_path: Path) -> None:
    """Verify real writer_session.check against an actual leased vs unleased linked worktree."""
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        leased_wt, _ = acquire_one(repository, task="real-mine", authority="real-mine", scope_paths=("README.md",))
        other_wt, _ = acquire_one(repository, task="real-other", authority="real-other")
        acquire_writer_lease(
            repository.repo,
            target=leased_wt,
            client_id="testclient",
            holder_pid=os.getpid(),
            session_id="sess-real",
        )
        from worktree_controller import writer_session

        # Real controller grants leased_wt for README.md, refuses out-of-scope file, refuses other_wt.
        ok = writer_session.check(
            leased_wt,
            client_id="testclient",
            selector=str(os.getpid()),
            session_id="sess-real",
            access="write",
            target=leased_wt / "README.md",
        )
        assert ok["admission"] == "GRANTED"
        out_of_scope = writer_session.check(
            leased_wt,
            client_id="testclient",
            selector=str(os.getpid()),
            session_id="sess-real",
            access="write",
            target=leased_wt / "other.txt",
        )
        assert out_of_scope["admission"] == "REFUSED"
        unleased = writer_session.check(
            other_wt,
            client_id="testclient",
            selector=str(os.getpid()),
            session_id="sess-real",
            access="write",
            target=other_wt / "README.md",
        )
        assert unleased["admission"] == "REFUSED"
