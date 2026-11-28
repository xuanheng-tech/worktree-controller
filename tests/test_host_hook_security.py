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


def _make_fake_wc_home(
    tmp_path: Path,
    wc_src: Path,
    *,
    allow_paths: set[str] | None = None,
    pending_closeout: bool = False,
) -> tuple[Path, Path]:
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

    end_payload = (
        {
            "selection_complete": True,
            "allocations": [
                {
                    "allocation_id": "11111111-1111-4111-8111-111111111111",
                    "terminal_complete": False,
                    "decision": {"code": "NO_TERMINAL_ACTION"},
                }
            ],
            "previous_allocations": [],
            "expected_allocations": [],
            "summary": {
                "contract": "session-closeout/v1",
                "selected": 1,
                "pending": 1,
                "terminal_complete": False,
            },
        }
        if pending_closeout
        else {
            "selection_complete": True,
            "allocations": [],
            "previous_allocations": [],
            "expected_allocations": [],
            "summary": {
                "contract": "session-closeout/v1",
                "selected": 0,
                "pending": 0,
                "terminal_complete": True,
            },
        }
    )
    end_stub = local_bin / "worktree-session-end"
    end_stub.write_text(
        f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open({str(log_file)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"bin": "worktree-session-end", "args": args}}) + "\\n")
print({json.dumps(json.dumps(end_payload))})
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
    payload: dict | str,
    fake_home: Path,
    extra_env: dict[str, str] | None = None,
    proc_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    entry = tmp_path / entry_name
    if not entry.exists():
        entry.write_bytes(CONSUMER_SCRIPT.read_bytes())
        entry.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONTROLLER_WRITER_")}
    env["HOME"] = str(fake_home)
    if extra_env:
        env.update(extra_env)
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [str(entry), event],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(proc_cwd) if proc_cwd is not None else None,
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


def test_codex_hook_pretool_stop_and_session_end(tmp_path: Path) -> None:
    """Verify codex-worktree-hook PreToolUse/Stop/SessionEnd protocol and fail-closed guards."""
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        leased_wt, _ = acquire_one(repository, task="codex-mine", authority="codex-mine")
        other_wt, _ = acquire_one(repository, task="codex-other", authority="codex-other")
        fake_home, log_file = _make_fake_wc_home(
            tmp_path, ROOT, allow_paths={str(leased_wt.resolve())}
        )

        # 1. Legitimate readonly Bash and exec_command on canonical -> exit 0, stdout "{}" (no permissionDecision: allow).
        for tool, tool_input in (
            ("Bash", {"command": "git status --short"}),
            ("exec_command", {"cmd": "rg -n README ."}),
        ):
            res = _invoke_hook(
                tmp_path,
                entry_name="codex-worktree-hook",
                event="PreToolUse",
                payload={
                    "hook_event_name": "PreToolUse",
                    "cwd": str(repository.repo),
                    "session_id": "codex-s1",
                    "tool_name": tool,
                    "tool_input": tool_input,
                },
                fake_home=fake_home,
            )
            assert res.returncode == 0
            out = json.loads(res.stdout)
            assert out == {}
            assert "permissionDecision" not in out

        # 2. Legitimate exec_command and apply_patch in leased_wt -> exit 0, stdout "{}".
        res_ok_exec = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "tool_name": "exec_command",
                "tool_input": {"cmd": "pytest -q"},
            },
            fake_home=fake_home,
        )
        assert res_ok_exec.returncode == 0
        assert json.loads(res_ok_exec.stdout) == {}

        res_ok_patch = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "tool_name": "apply_patch",
                "tool_input": {
                    "input": "*** Begin Patch\n*** Update File: README.md\n@@\n+ok\n*** End Patch\n"
                },
            },
            fake_home=fake_home,
        )
        assert res_ok_patch.returncode == 0
        assert json.loads(res_ok_patch.stdout) == {}

        # 3. Canonical mutation via Bash / exec_command / apply_patch -> exit 2 + clear stderr.
        for tool, tool_input in (
            ("Bash", {"command": "echo x > README.md"}),
            ("exec_command", {"cmd": "python3 -c 'open(\"README.md\",\"w\").write(\"x\")'"}),
            (
                "apply_patch",
                {"input": f"*** Begin Patch\n*** Update File: {repository.repo}/README.md\n*** End Patch"},
            ),
        ):
            res = _invoke_hook(
                tmp_path,
                entry_name="codex-worktree-hook",
                event="PreToolUse",
                payload={
                    "hook_event_name": "PreToolUse",
                    "cwd": str(repository.repo),
                    "session_id": "codex-s1",
                    "tool_name": tool,
                    "tool_input": tool_input,
                },
                fake_home=fake_home,
            )
            assert res.returncode == 2
            assert "canonical" in res.stderr.lower()

        # 4. Unleased other linked worktree via exec_command and multi-target apply_patch -> exit 2.
        res_other_exec = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(other_wt),
                "session_id": "codex-s1",
                "tool_name": "exec_command",
                "tool_input": {"cmd": "touch leaked.txt"},
            },
            fake_home=fake_home,
        )
        assert res_other_exec.returncode == 2

        res_other_patch = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "tool_name": "apply_patch",
                "tool_input": {
                    "input": (
                        "*** Begin Patch\n"
                        "*** Update File: README.md\n"
                        f"*** Update File: {other_wt}/README.md\n"
                        "*** End Patch\n"
                    )
                },
            },
            fake_home=fake_home,
        )
        assert res_other_patch.returncode == 2

        # 5. SessionEnd routes to worktree-session-end with codex identity and exits 0.
        log_file.unlink(missing_ok=True)
        res_end = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="SessionEnd",
            payload={
                "hook_event_name": "SessionEnd",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "reason": "other",
            },
            fake_home=fake_home,
        )
        assert res_end.returncode == 0
        calls = _read_calls(log_file)
        assert calls[1] == {
            "bin": "worktree-session-end",
            "args": [
                "--path",
                str(leased_wt),
                "--client-id",
                "codex",
                "--session-id",
                "codex-s1",
                "--holder",
                "ancestor:codex",
                "--trigger",
                "task-terminal",
            ],
        }

        # 6. Stop: clean exits 0 with "{}"; pending blocks once then yields systemMessage on stop_hook_active.
        res_stop_clean = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="Stop",
            payload={
                "hook_event_name": "Stop",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "stop_hook_active": False,
            },
            fake_home=fake_home,
        )
        assert res_stop_clean.returncode == 0
        assert json.loads(res_stop_clean.stdout) == {}

        pending_home, _ = _make_fake_wc_home(
            tmp_path / "pending", ROOT, allow_paths={str(leased_wt.resolve())}, pending_closeout=True
        )
        res_stop_block = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="Stop",
            payload={
                "hook_event_name": "Stop",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "stop_hook_active": False,
            },
            fake_home=pending_home,
        )
        assert res_stop_block.returncode == 0
        assert json.loads(res_stop_block.stdout)["decision"] == "block"

        res_stop_active = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="Stop",
            payload={
                "hook_event_name": "Stop",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "stop_hook_active": True,
            },
            fake_home=pending_home,
        )
        assert res_stop_active.returncode == 0
        active_out = json.loads(res_stop_active.stdout)
        assert "decision" not in active_out and "systemMessage" in active_out

        # 7. Abnormal recovery: malformed apply_patch fails closed (rc=2); SessionEnd never blocks exit on controller error.
        res_bad_patch = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="PreToolUse",
            payload={
                "hook_event_name": "PreToolUse",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "tool_name": "apply_patch",
                "tool_input": {"input": "not a valid patch"},
            },
            fake_home=fake_home,
        )
        assert res_bad_patch.returncode == 2
        broken_home = tmp_path / "broken-codex-home"
        broken_home.mkdir()
        res_broken_end = _invoke_hook(
            tmp_path,
            entry_name="codex-worktree-hook",
            event="SessionEnd",
            payload={
                "hook_event_name": "SessionEnd",
                "cwd": str(leased_wt),
                "session_id": "codex-s1",
                "reason": "other",
            },
            fake_home=broken_home,
        )
        assert res_broken_end.returncode == 0
        assert "unverified" in res_broken_end.stderr.lower()


def test_agy_hook_camel_case_pretool_and_stop(tmp_path: Path) -> None:
    """Verify agy-worktree-hook camelCase protojson parsing, payload cwd precedence, and contracts."""
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        leased_wt, _ = acquire_one(repository, task="agy-mine", authority="agy-mine")
        other_wt, _ = acquire_one(repository, task="agy-other", authority="agy-other")
        config_dir = tmp_path / "gemini-config"
        config_dir.mkdir()
        fake_home, log_file = _make_fake_wc_home(
            tmp_path, ROOT, allow_paths={str(leased_wt.resolve())}
        )

        # 1. Legitimate operations (payload cwd != process cwd) -> exit 0, {"decision": "allow"}.
        for tool_name, args, payload_cwd in (
            ("run_command", {"CommandLine": "git status --short"}, repository.repo),
            ("write_file", {"Path": str(leased_wt / "README.md"), "Content": "ok"}, leased_wt),
            ("edit_file", {"Path": "README.md"}, leased_wt),
            ("write_to_file", {"TargetFile": str(leased_wt / "README.md")}, leased_wt),
            ("replace_file_content", {"TargetFile": str(leased_wt / "README.md")}, leased_wt),
            ("multi_replace_file_content", {"TargetFile": str(leased_wt / "README.md")}, leased_wt),
            ("delete_file", {"AbsolutePath": str(leased_wt / "README.md")}, leased_wt),
            ("edit_notebook", {"TargetNotebook": str(leased_wt / "nb.ipynb")}, leased_wt),
            (
                "apply_patch",
                {"input": "*** Begin Patch\n*** Update File: README.md\n*** End Patch"},
                leased_wt,
            ),
            ("run_command", {"CommandLine": "pytest -q"}, leased_wt),
            ("send_command_input", {"Input": "y\n"}, leased_wt),
        ):
            res = _invoke_hook(
                tmp_path,
                entry_name="agy-worktree-hook",
                event="PreToolUse",
                payload={
                    "hookEventName": "PreToolUse",
                    "cwd": str(payload_cwd),
                    "conversationId": "agy-conv-1",
                    "toolCall": {"id": "tc-1", "name": tool_name, "args": args},
                },
                fake_home=fake_home,
                proc_cwd=config_dir,
            )
            assert res.returncode == 0, f"agy {tool_name} failed: {res.stderr}"
            assert json.loads(res.stdout) == {"decision": "allow"}, f"agy {tool_name}: {res.stdout}"

        # 2. Protected canonical violations -> exit 0, {"decision": "deny", "reason": ...}.
        for tool_name, args, payload_cwd in (
            ("write_file", {"Path": str(repository.repo / "README.md")}, config_dir),
            ("edit_file", {"Path": "README.md"}, repository.repo),
            ("delete_file", {"Path": str(repository.repo / "README.md")}, leased_wt),
            ("run_command", {"CommandLine": "echo x > README.md"}, repository.repo),
            ("run_command", {"CommandLine": "touch x", "Cwd": str(repository.repo)}, config_dir),
            ("send_command_input", {"Input": "rm -rf .\n"}, repository.repo),
            (
                "apply_patch",
                {"patch": f"*** Begin Patch\n*** Update File: {repository.repo}/README.md\n*** End Patch"},
                leased_wt,
            ),
        ):
            res = _invoke_hook(
                tmp_path,
                entry_name="agy-worktree-hook",
                event="PreToolUse",
                payload={
                    "hookEventName": "PreToolUse",
                    "cwd": str(payload_cwd),
                    "conversationId": "agy-conv-1",
                    "toolCall": {"id": "tc-2", "name": tool_name, "args": args},
                },
                fake_home=fake_home,
                proc_cwd=config_dir,
            )
            assert res.returncode == 0
            out = json.loads(res.stdout)
            assert out["decision"] == "deny", f"agy failed to deny canonical {tool_name}: {out}"
            assert "canonical" in out["reason"].lower()

        # 3. Unleased foreign linked worktree -> exit 0, {"decision": "deny", "reason": ...}.
        for tool_name, args, payload_cwd in (
            ("edit_file", {"Path": str(other_wt / "README.md")}, leased_wt),
            ("run_command", {"CommandLine": "pytest -q"}, other_wt),
            ("send_command_input", {"Input": "echo hi\n"}, other_wt),
        ):
            log_file.unlink(missing_ok=True)
            res = _invoke_hook(
                tmp_path,
                entry_name="agy-worktree-hook",
                event="PreToolUse",
                payload={
                    "hookEventName": "PreToolUse",
                    "cwd": str(payload_cwd),
                    "conversationId": "agy-conv-1",
                    "toolCall": {"id": "tc-3", "name": tool_name, "args": args},
                },
                fake_home=fake_home,
                proc_cwd=config_dir,
            )
            assert res.returncode == 0
            out = json.loads(res.stdout)
            assert out["decision"] == "deny"
            assert _read_calls(log_file)[0]["args"][:4] == [
                "writer-session",
                "check",
                "agy",
                str(other_wt.resolve()),
            ]

        # 4. Malformed stdin JSON fails closed with {"decision": "deny"}.
        res_bad = _invoke_hook(
            tmp_path,
            entry_name="agy-worktree-hook",
            event="PreToolUse",
            payload="{not-json",
            fake_home=fake_home,
            proc_cwd=config_dir,
        )
        assert res_bad.returncode == 0
        assert json.loads(res_bad.stdout)["decision"] == "deny"

        # 5. Stop: continue ONLY when NO_TOOL_CALL and continuationActive != true and pending lease exists.
        pending_home, _ = _make_fake_wc_home(
            tmp_path / "agy-pending",
            ROOT,
            allow_paths={str(leased_wt.resolve())},
            pending_closeout=True,
        )
        res_stop_continue = _invoke_hook(
            tmp_path,
            entry_name="agy-worktree-hook",
            event="Stop",
            payload={
                "hookEventName": "Stop",
                "cwd": str(leased_wt),
                "conversationId": "agy-conv-1",
                "terminationReason": "NO_TOOL_CALL",
                "continuationActive": False,
            },
            fake_home=pending_home,
            proc_cwd=config_dir,
        )
        assert res_stop_continue.returncode == 0
        stop_out = json.loads(res_stop_continue.stdout)
        assert stop_out["decision"] == "continue" and "reason" in stop_out

        # Already continued -> {}.
        res_stop_loop_guard = _invoke_hook(
            tmp_path,
            entry_name="agy-worktree-hook",
            event="Stop",
            payload={
                "hookEventName": "Stop",
                "cwd": str(leased_wt),
                "conversationId": "agy-conv-1",
                "terminationReason": "NO_TOOL_CALL",
                "continuationActive": True,
            },
            fake_home=pending_home,
            proc_cwd=config_dir,
        )
        assert res_stop_loop_guard.returncode == 0
        assert json.loads(res_stop_loop_guard.stdout) == {}

        # Non-NO_TOOL_CALL termination (e.g. USER_CANCELED / ERROR) -> {}.
        for term in ("USER_CANCELED", "ERROR", "MAX_INVOCATIONS"):
            res_term = _invoke_hook(
                tmp_path,
                entry_name="agy-worktree-hook",
                event="Stop",
                payload={
                    "hookEventName": "Stop",
                    "cwd": str(leased_wt),
                    "conversationId": "agy-conv-1",
                    "terminationReason": term,
                    "continuationActive": False,
                },
                fake_home=pending_home,
                proc_cwd=config_dir,
            )
            assert res_term.returncode == 0
            assert json.loads(res_term.stdout) == {}

        # Clean session (no pending lease) or outside governance -> {}.
        for stop_cwd in (leased_wt, config_dir):
            res_clean = _invoke_hook(
                tmp_path,
                entry_name="agy-worktree-hook",
                event="Stop",
                payload={
                    "hookEventName": "Stop",
                    "cwd": str(stop_cwd),
                    "conversationId": "agy-conv-1",
                    "terminationReason": "NO_TOOL_CALL",
                    "continuationActive": False,
                },
                fake_home=fake_home,
                proc_cwd=config_dir,
            )
            assert res_clean.returncode == 0
            assert json.loads(res_clean.stdout) == {}

        # Abnormal controller failure during non-hosted Stop returns {} without trapping exit.
        broken_agy_home = tmp_path / "broken-agy-home"
        broken_agy_home.mkdir()
        res_broken_stop = _invoke_hook(
            tmp_path,
            entry_name="agy-worktree-hook",
            event="Stop",
            payload={
                "hookEventName": "Stop",
                "cwd": str(leased_wt),
                "conversationId": "agy-conv-1",
                "terminationReason": "NO_TOOL_CALL",
                "continuationActive": False,
            },
            fake_home=broken_agy_home,
            proc_cwd=config_dir,
        )
        assert res_broken_stop.returncode == 0
        assert json.loads(res_broken_stop.stdout) == {}


def _install_fake_research_workspace(fake_home: Path, *, mode: int = 0o755) -> Path:
    rw_bin = fake_home / ".local/bin/research-workspace"
    rw_bin.parent.mkdir(parents=True, exist_ok=True)
    rw_bin.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    rw_bin.chmod(mode)
    return rw_bin


def _build_shell_hook_payload(client: str, cwd: Path, command: str) -> tuple[str, dict]:
    if client == "agy":
        return (
            "agy-worktree-hook",
            {
                "hookEventName": "PreToolUse",
                "cwd": str(cwd),
                "conversationId": "agy-conv-job",
                "toolCall": {"id": "tc-job", "name": "run_command", "args": {"CommandLine": command}},
            },
        )
    if client == "claude":
        return (
            "claude-worktree-hook",
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(cwd),
                "session_id": "s-job",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )
    if client == "codex":
        return (
            "codex-worktree-hook",
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(cwd),
                "session_id": "c-job",
                "tool_name": "exec_command",
                "tool_input": {"cmd": command},
            },
        )
    return (
        "grok-worktree-hook",
        {
            "hookEventName": "pre_tool_use",
            "cwd": str(cwd),
            "sessionId": "g-job",
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
        },
    )


def _assert_hook_decision(client: str, res: subprocess.CompletedProcess[str], *, allow: bool) -> None:
    if client == "agy":
        assert res.returncode == 0, f"agy hook crashed: {res.stderr}"
        decision = json.loads(res.stdout)["decision"]
        assert decision == ("allow" if allow else "deny"), f"agy unexpected decision: {res.stdout}"
    elif allow:
        assert res.returncode == 0, f"{client} unexpectedly denied: {res.stderr}"
    else:
        assert res.returncode == 2, f"{client} unexpectedly allowed: stdout={res.stdout} stderr={res.stderr}"
        assert "canonical" in res.stderr.lower()


@pytest.mark.parametrize("client", ["agy", "claude", "codex", "grok"])
def test_canonical_research_job_entry_allowlist_and_denials(tmp_path: Path, client: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        fake_home, log_file = _make_fake_wc_home(tmp_path, ROOT)
        rw_bin = _install_fake_research_workspace(fake_home)
        user_bin_link = fake_home / "bin/research-workspace"
        user_bin_link.parent.mkdir(parents=True, exist_ok=True)
        user_bin_link.symlink_to(rw_bin)

        safe_req = tmp_path / "safe_request.json"
        safe_req.write_text('{"task_id":"u3-2a-r1"}\n', encoding="utf-8")
        symlink_req = tmp_path / "link_request.json"
        symlink_req.symlink_to(safe_req)

        extra_env = {"PATH": f"{fake_home / '.local/bin'}:{os.environ.get('PATH', '')}"}

        allowed_commands = [
            "research-workspace job submit --request-b64 eyJ0YXNrX2lkIjogInUzIn0= --run --json",
            f"research-workspace job submit --request-file {safe_req} --policy-id u3-policy --task-id u3-2a-r1 --idempotency-key run-1 --max-segments 2 --json",
            f"{rw_bin} job status --job-id job-20261128-01 --json",
            "~/bin/research-workspace job run --job-id job-20261128-01 --wait --json",
            "research-workspace job recover --job-id job-20261128-01 --resume --json",
            "research-workspace job cancel --job-id job-20261128-01 --reason manual_stop --json",
            "research-workspace job result --job-id job-20261128-01 --json",
            "research-workspace job list --state VERIFIED_DELIVERED --limit 20 --json",
        ]
        for command in allowed_commands:
            entry_name, payload = _build_shell_hook_payload(client, repository.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=True)
            assert _read_calls(log_file) == []

        fake_evil_bin = tmp_path / "evil/research-workspace"
        fake_evil_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_evil_bin.write_text("#!/bin/sh\necho pwn\n", encoding="utf-8")
        fake_evil_bin.chmod(0o755)

        denied_commands = [
            # Excluded subcommands (policy admin and direct workspace ops)
            "research-workspace policy register --policy-file /tmp/p.json",
            "research-workspace register --task-id t1 --client agy",
            "research-workspace exec --workspace-id ws1 -- python3 -c 1",
            "research-workspace deliver --workspace-id ws1 --dest gdrive:foo",
            # Shell metacharacters / chaining / redirection / substitution / comments
            "research-workspace job status --job-id j1 && rm tracked.txt",
            "research-workspace job status --job-id j1; touch pwn",
            "research-workspace job status --job-id j1 | cat",
            "research-workspace job status --job-id j1 > out.txt",
            "research-workspace job status --job-id $(whoami)",
            "research-workspace job status --job-id `id`",
            "research-workspace job status --job-id j1 # comment",
            # Environment override and untrusted binary path
            "PATH=/tmp research-workspace job status --job-id j1",
            f"{fake_evil_bin} job status --job-id j1",
            "./research-workspace job status --job-id j1",
            # Flag validation, passthrough, missing/duplicate/conflicting flags
            "research-workspace job submit --json",
            f"research-workspace job submit --request-file {safe_req} --request-b64 e30=",
            "research-workspace job status --job-id j1 -- --extra",
            "research-workspace job status --job-id j1 --unknown x",
            "research-workspace job status --job-id j1 extra",
            "research-workspace job status --job-id j1 --job-id j2",
            "research-workspace job status --json",
            # Unsafe request file paths
            f"research-workspace job submit --request-file {symlink_req}",
            "research-workspace job submit --request-file relative.json",
            f"research-workspace job submit --request-file {fake_home}/.ssh/req.json",
            f"research-workspace job submit --request-file {fake_home}/.config/rclone/rclone.conf",
            f"research-workspace job submit --request-file {fake_home}/.local/state/agent-toolchain/research-jobs/v1/policies/p.json",
            "research-workspace job submit --request-file /etc/passwd.json",
            f"research-workspace job submit --request-file {tmp_path}/.env.json",
            # Non-readonly tools stay blocked in canonical
            "rclone copy /tmp/a gdrive:b",
            "python3 /tmp/script.py",
            "agent-session launch --workspace-id ws1",
        ]
        for command in denied_commands:
            entry_name, payload = _build_shell_hook_payload(client, repository.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=False)

        # Untrusted PATH shadowing bare `research-workspace` must fail closed.
        shadow_env = {"PATH": f"{fake_evil_bin.parent}:{fake_home / '.local/bin'}:{os.environ.get('PATH', '')}"}
        entry_name, payload = _build_shell_hook_payload(
            client, repository.repo, "research-workspace job status --job-id j1 --json"
        )
        res_shadow = _invoke_hook(
            tmp_path,
            entry_name=entry_name,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=shadow_env,
        )
        _assert_hook_decision(client, res_shadow, allow=False)

        # Symlink inside ~/bin pointing outside trusted dirs (e.g. /bin/sh) must fail closed.
        user_bin_link.unlink()
        user_bin_link.symlink_to("/bin/sh")
        entry_name, payload = _build_shell_hook_payload(
            client, repository.repo, "~/bin/research-workspace job status --job-id j1 --json"
        )
        res_bad_link = _invoke_hook(
            tmp_path,
            entry_name=entry_name,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=extra_env,
        )
        _assert_hook_decision(client, res_bad_link, allow=False)

        # World-writable binary must fail closed.
        rw_bin.chmod(0o777)
        entry_name, payload = _build_shell_hook_payload(
            client, repository.repo, f"{rw_bin} job status --job-id j1 --json"
        )
        res_writable = _invoke_hook(
            tmp_path,
            entry_name=entry_name,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=extra_env,
        )
        _assert_hook_decision(client, res_writable, allow=False)


def _install_fake_user_cli(fake_home: Path, rel_dir: str, name: str, *, mode: int = 0o755) -> Path:
    cli_path = fake_home / rel_dir / name
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    cli_path.chmod(mode)
    return cli_path


def _refusal_text(client: str, res: subprocess.CompletedProcess[str]) -> str:
    if client == "agy":
        return json.loads(res.stdout)["reason"]
    return res.stderr


@pytest.mark.parametrize("client", ["agy", "claude", "codex", "grok"])
def test_canonical_compound_readonly_discovery_and_refusal_guidance(
    tmp_path: Path, client: str
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        fake_home, log_file = _make_fake_wc_home(tmp_path, ROOT)
        rw_bin = _install_fake_research_workspace(fake_home)
        tss_bin = _install_fake_user_cli(fake_home, ".local/bin", "tool-skill-sync")
        extra_env = {"PATH": f"{fake_home / '.local/bin'}:{os.environ.get('PATH', '')}"}

        allowed_commands = [
            # Compound readonly pipelines, chaining, fd dups, /dev/null sinks
            "git status | head -n 20",
            "git status; git log -n 5 --oneline",
            "git status 2>&1 || true",
            "git status 2>&1 | head -n 20",
            "git status > /dev/null",
            "git status 2> /dev/null",
            "git status 1> /dev/null",
            "git status > /dev/null 2>&1 || true",
            "git status &>/dev/null",
            "git status &> /dev/null",
            "git status >&2",
            "git status |& head -n 20",
            # Expanded strictly readonly git subcommands
            "git remote",
            "git remote -v",
            "git remote get-url origin",
            "git worktree list",
            "git worktree list --porcelain",
            "git stash list",
            "git submodule status",
            "git tag -n5",
            "git config --get-regexp ^remote",
            # Pure readonly POSIX predicates, printf/uniq/cd safe forms, and safe locale env prefixes
            "test -f README.md && cat README.md | head -n 5 || true",
            "[ -f README.md ] && echo ok",
            "printf '%s\\n' ok",
            "sort README.md | uniq -c",
            "echo hi | uniq -",
            "git grep -o README",
            "git ls-files -o",
            "systemctl --user -p ActiveState show foo.service",
            "systemctl --user status foo.service",
            "LC_ALL=C git status --short",
            # Strictly readonly tool-skill-sync commands
            "tool-skill-sync --version",
            "tool-skill-sync --help",
            "tool-skill-sync doctor --summary",
            "tool-skill-sync doctor --repo . --summary",
            f"tool-skill-sync doctor --repo {repository.repo} --summary",
            "tool-skill-sync doctor --repo=. --summary --previous-fingerprint sha256:abc123",
            "tool-skill-sync doctor --repo . --summary 2>&1 || true",
            f"{tss_bin} doctor --summary | head -n 20",
            "tool-skill-sync status",
            "tool-skill-sync skill-check --strict-clients",
            "tool-skill-sync check --source-only worktree-controller",
            # Strictly readonly research-workspace discovery commands
            "research-workspace --help",
            "research-workspace -h",
            "research-workspace --version",
            f"{rw_bin} job --help",
            "research-workspace job submit --help",
            "research-workspace job list -h",
            "research-workspace --help | head -n 20",
            "research-workspace --version 2>&1 || true",
        ]
        for command in allowed_commands:
            entry_name, payload = _build_shell_hook_payload(client, repository.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=True)
            assert _read_calls(log_file) == []

        denied_commands = [
            # Redirects, backgrounding, dangling operators, mutating git/rg/tree flags
            "git status > out.txt",
            "git status >> /dev/null",
            "git status &> out.txt",
            "git status >&3",
            "git status &",
            "git status |",
            "git status && ",
            "git remote add evil https://example.invalid/repo.git",
            "git worktree add /tmp/wt",
            "git stash pop",
            "git submodule update",
            "git tag v9.9.9",
            "git grep -O sh README",
            "git grep -Osh README",
            "git grep -iOsh README",
            "git diff -opwn.txt",
            "git stash show -Osh",
            "git ls-remote --upload-pack='touch /tmp/pwn' .",
            "git ls-remote --exec='touch /tmp/pwn' .",
            "git ls-remote --upload='touch /tmp/pwn' .",
            "git diff --out=pwn.txt",
            "git cat-file --filters HEAD:README.md",
            "git help -w status",
            "rg --pre sh README .",
            "tree -o out.txt",
            "tree -ao out.txt",
            "sort -opwn.txt README.md",
            "sort -uo pwn.txt README.md",
            "sort --ou=pwn.txt README.md",
            "sort --compress-program=/tmp/helper.sh README.md",
            "sort --com=/tmp/helper.sh README.md",
            "ruff check -opwn.txt src/",
            "ruff check -qo pwn.txt src/",
            "ruff check --add-noqa src/",
            "ruff check --config 'fix = true' src/",
            "ruff check --config=/tmp/fix.toml src/",
            "ruff check --cache-dir /tmp/c src/",
            "ruff check -w src/",
            "ruff check -qw src/",
            "uniq README.md pwn.txt",
            "echo pwn | uniq - tracked.txt",
            # systemctl flag-value smuggling and remote host/root flags
            "systemctl --user -p status stop foo.service",
            "systemctl --user --property status stop foo.service",
            "systemctl --user --unknown status foo.service",
            # ANSI-C / locale quoting, line-continuation \<LF>, \r, "${...}" var mutation, printf -v
            r"echo $'\'' && touch pwn #'",
            'echo $"hello"',
            'echo "$\\\n(echo PWNED)"',
            "echo $\\\n'\\'' && touch pwn #'",
            "echo ok\rgit status",
            f'cd /tmp && echo "${{CDPATH:={repository.repo.parent}}}" && cd {repository.repo.name} && worktree-session --path . --client-id claude --session-id s -- rm tracked.txt',
            'echo "$[1+1]"',
            "printf -v PATH /tmp/evil && ls",
            "printf -vPATH /tmp/evil && ls",
            # Untracked / flag-smuggled cd into canonical + worktree-session --path .
            "cd - && worktree-session --path . --client-id claude --session-id s -- rm tracked.txt",
            f"cd /tmp && cd -- {repository.repo} && worktree-session --path . --client-id claude --session-id s -- rm tracked.txt",
            f"cd /tmp && cd -P {repository.repo} && worktree-session --path . --client-id claude --session-id s -- rm tracked.txt",
            # Untrusted relative / arbitrary executable paths spoofing readonly names
            "./ls",
            "/tmp/evil/git status",
            "/tmp/evil/worktree-controller status --json",
            # Dangerous env var injection
            "LD_PRELOAD=/tmp/evil.so git status",
            "GIT_EXEC_PATH=/tmp/evil git status",
            "GIT_EXTERNAL_DIFF=/tmp/evil git diff",
            "PAGER=sh git log -n 1",
            "PATH=/tmp/evil ls",
            # tool-skill-sync RCE override flags and mutating subcommands
            "tool-skill-sync --bin-dir /tmp/evil doctor --summary",
            "tool-skill-sync doctor --controller /tmp/evil --summary",
            "tool-skill-sync --source-root /tmp/evil status",
            "tool-skill-sync doctor --codex-home /tmp/evil",
            "tool-skill-sync skill-check --claude-home /tmp/evil",
            "tool-skill-sync install",
            "tool-skill-sync upgrade",
            "tool-skill-sync rollback",
            "tool-skill-sync skill-install",
            "PATH=/tmp tool-skill-sync doctor --summary",
            "./tool-skill-sync doctor --summary",
            # rclone and forbidden research-workspace subcommands stay strictly blocked
            "rclone listremotes",
            "cd /tmp && rclone listremotes",
            "research-workspace policy --help",
            "research-workspace deliver --help",
            "research-workspace register --help",
            "research-workspace exec --workspace-id ws1 -- ls",
            "research-workspace inspect --workspace-id ws1",
            "research-workspace guard-hook",
        ]
        for command in denied_commands:
            entry_name, payload = _build_shell_hook_payload(client, repository.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=False)
            reason = _refusal_text(client, res)
            assert "worktree-controller acquire" in reason
            assert "research-workspace" in reason
            assert "research-workspace job" in reason
            assert "rclone" in reason


@pytest.mark.parametrize("client", ["agy", "claude", "codex", "grok"])
def test_canonical_host_configured_readonly_clis_allowlist_and_isolation(
    tmp_path: Path, client: str
) -> None:
    with (
        GitRepository(linked=0, policy_text=POLICY_V8) as repo_a,
        GitRepository(linked=0, policy_text=POLICY_V8) as repo_b,
    ):
        adopt_canonical(repo_a)
        adopt_canonical(repo_b)
        fake_home, log_file = _make_fake_wc_home(tmp_path, ROOT)
        scope_bin = _install_fake_user_cli(fake_home, "bin", "gov-scope")
        nexus_bin = _install_fake_user_cli(fake_home, ".local/bin", "gov-nexus")
        extra_env = {
            "PATH": f"{fake_home / 'bin'}:{fake_home / '.local/bin'}:{os.environ.get('PATH', '')}"
        }

        config_dir = fake_home / ".config/worktree-controller"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "canonical-readonly-clis.toml"
        config_path.write_text(
            f"""schema_version = 1

[[commands]]
executable = "gov-scope"
repos = [{str(repo_a.repo.resolve())!r}]
[[commands.profiles]]
subcommand = ["show"]
bool_flags = ["--json", "-h", "--help"]
value_flags = ["--repo"]
allow_positional = false
[[commands.profiles]]
subcommand = []
bool_flags = ["-h", "--help"]

[[commands]]
executable = "gov-nexus"
repos = [{str(repo_a.repo.resolve())!r}]
[[commands.profiles]]
subcommand = ["brief"]
bool_flags = ["--json", "-h", "--help"]
value_flags = ["--repo", "--max-bytes", "--state", "--bundle-dir", "--studio-root"]
allow_positional = false
[[commands.profiles]]
subcommand = ["read"]
bool_flags = ["-h", "--help"]
value_flags = ["--state", "--snapshot", "--module"]
allow_positional = false
""",
            encoding="utf-8",
        )
        config_path.chmod(0o600)

        allowed_in_repo_a = [
            "gov-scope show",
            "gov-scope show --json",
            "gov-scope show --repo .",
            "cd /tmp && gov-scope show",
            "gov-scope --help",
            f"{scope_bin} show | head -n 20",
            "gov-nexus brief --repo .",
            "gov-nexus brief --repo . --max-bytes 1500 --json",
            "gov-nexus brief --repo . 2>&1 || true",
            "gov-nexus read --module core",
            f"{nexus_bin} brief --repo . 2> /dev/null | head -n 30",
        ]
        for command in allowed_in_repo_a:
            entry_name, payload = _build_shell_hook_payload(client, repo_a.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=True)
            assert _read_calls(log_file) == []

        denied_in_repo_a = [
            # Mutating / disallowed subcommands
            "gov-scope apply --input /tmp/p.json --expect abc",
            "gov-scope grok",
            "gov-nexus refresh",
            "gov-nexus install-schedule",
            "gov-nexus progress --goal x",
            "gov-nexus serve",
            # Disallowed / RCE / smuggling flags
            "gov-nexus read --systemctl /tmp/evil",
            "gov-nexus brief --controller /tmp/evil",
            "gov-scope show --unknown",
            "gov-scope show extra-arg",
            "gov-scope show --repo -evil",
            "gov-scope show -- --extra",
            # Cross-repo scope violation
            f"gov-nexus brief --repo {repo_b.repo}",
            f"gov-scope show --repo {repo_b.repo}",
            # Shell escapes and env prefixes
            "gov-scope show > tracked.txt",
            "gov-scope show && rm tracked.txt",
            "gov-scope show --repo \"$(whoami)\"",
            "PATH=/tmp gov-scope show",
            "LC_ALL=C gov-scope show",
            "./gov-scope show",
        ]
        for command in denied_in_repo_a:
            entry_name, payload = _build_shell_hook_payload(client, repo_a.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=False)

        # Repo-scoped rule for repo_a must NEVER admit commands in repo_b.
        for command in ("gov-scope show", "gov-nexus brief --repo ."):
            entry_name, payload = _build_shell_hook_payload(client, repo_b.repo, command)
            res = _invoke_hook(
                tmp_path,
                entry_name=entry_name,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res, allow=False)

        # Canonical session cannot modify hook config or trusted bin dirs via write tools (direct or symlink).
        outside_link = tmp_path / "sneaky-config.toml"
        outside_link.symlink_to(config_path)
        for target_cfg in (config_path, outside_link, scope_bin, nexus_bin):
            if client == "agy":
                w_entry, w_payload = (
                    "agy-worktree-hook",
                    {
                        "hookEventName": "PreToolUse",
                        "cwd": str(repo_a.repo),
                        "conversationId": "agy-cfg",
                        "toolCall": {
                            "id": "tc-cfg",
                            "name": "write_file",
                            "args": {"Path": str(target_cfg), "Content": "x"},
                        },
                    },
                )
            elif client == "grok":
                w_entry, w_payload = (
                    "grok-worktree-hook",
                    {
                        "hookEventName": "pre_tool_use",
                        "cwd": str(repo_a.repo),
                        "sessionId": "g-cfg",
                        "toolName": "write",
                        "toolInput": {"file_path": str(target_cfg)},
                    },
                )
            else:
                w_entry = f"{client}-worktree-hook"
                w_payload = {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(repo_a.repo),
                    "session_id": f"{client}-cfg",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target_cfg)},
                }
            res_cfg_write = _invoke_hook(
                tmp_path,
                entry_name=w_entry,
                event="PreToolUse",
                payload=w_payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision(client, res_cfg_write, allow=False)


def test_canonical_host_config_security_invariants_fail_closed(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        fake_home, _ = _make_fake_wc_home(tmp_path, ROOT)
        _install_fake_user_cli(fake_home, "bin", "gov-scope")
        _install_fake_user_cli(fake_home, "bin", "rclone")
        extra_env = {
            "PATH": f"{fake_home / 'bin'}:{fake_home / '.local/bin'}:{os.environ.get('PATH', '')}"
        }
        config_dir = fake_home / ".config/worktree-controller"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "canonical-readonly-clis.toml"

        # 1. Forbidden executables (rclone, bash, python3, python3.12, research-workspace) always rejected.
        for bad_exe in ("rclone", "bash", "python3", "python3.12", "research-workspace", "tool-skill-sync"):
            config_path.write_text(
                f"""schema_version = 1
[[commands]]
executable = {bad_exe!r}
[[commands.profiles]]
subcommand = ["show"]
""",
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            entry, payload = _build_shell_hook_payload("agy", repository.repo, f"{bad_exe} show")
            res = _invoke_hook(
                tmp_path,
                entry_name=entry,
                event="PreToolUse",
                payload=payload,
                fake_home=fake_home,
                extra_env=extra_env,
            )
            _assert_hook_decision("agy", res, allow=False)

        # 2. World-writable config file fails closed.
        valid_toml = """schema_version = 1
[[commands]]
executable = "gov-scope"
[[commands.profiles]]
subcommand = ["show"]
"""
        config_path.write_text(valid_toml, encoding="utf-8")
        config_path.chmod(0o666)
        entry, payload = _build_shell_hook_payload("agy", repository.repo, "gov-scope show")
        res_ww = _invoke_hook(
            tmp_path,
            entry_name=entry,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=extra_env,
        )
        _assert_hook_decision("agy", res_ww, allow=False)

        # 3. Symlinked config file fails closed.
        real_cfg = tmp_path / "external.toml"
        real_cfg.write_text(valid_toml, encoding="utf-8")
        real_cfg.chmod(0o600)
        config_path.unlink()
        config_path.symlink_to(real_cfg)
        res_sym = _invoke_hook(
            tmp_path,
            entry_name=entry,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=extra_env,
        )
        _assert_hook_decision("agy", res_sym, allow=False)

        # 4. Empty subcommand with non-help flags fails closed.
        config_path.unlink()
        config_path.write_text(
            """schema_version = 1
[[commands]]
executable = "gov-scope"
[[commands.profiles]]
subcommand = []
bool_flags = ["--json"]
""",
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        entry_j, payload_j = _build_shell_hook_payload("agy", repository.repo, "gov-scope --json")
        res_empty_sub = _invoke_hook(
            tmp_path,
            entry_name=entry_j,
            event="PreToolUse",
            payload=payload_j,
            fake_home=fake_home,
            extra_env=extra_env,
        )
        _assert_hook_decision("agy", res_empty_sub, allow=False)

        # 5. Untrusted PATH shadowing bare `gov-scope` fails closed even with valid config.
        config_path.write_text(valid_toml, encoding="utf-8")
        config_path.chmod(0o600)
        evil_dir = tmp_path / "evil-bin"
        evil_dir.mkdir()
        evil_cli = evil_dir / "gov-scope"
        evil_cli.write_text("#!/bin/sh\necho pwn\n", encoding="utf-8")
        evil_cli.chmod(0o755)
        shadow_env = {"PATH": f"{evil_dir}:{fake_home / 'bin'}:{os.environ.get('PATH', '')}"}
        res_shadow = _invoke_hook(
            tmp_path,
            entry_name=entry,
            event="PreToolUse",
            payload=payload,
            fake_home=fake_home,
            extra_env=shadow_env,
        )
        _assert_hook_decision("agy", res_shadow, allow=False)
