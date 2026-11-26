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
