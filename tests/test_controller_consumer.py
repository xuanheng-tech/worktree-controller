"""Tests for scripts/controller-consumer and scripts/install-controller-consumers."""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def consumer():
    return runpy.run_path(str(ROOT / "scripts/controller-consumer"))


@pytest.fixture
def installer():
    return runpy.run_path(str(ROOT / "scripts/install-controller-consumers"))


@pytest.mark.parametrize(
    ("event", "hosted"),
    [
        ("SessionStart", False),
        ("PreToolUse", False),
        ("SessionStart", True),
        ("PreToolUse", True),
        ("SessionEnd", True),
    ],
)
def test_hook_events_only_check_entitlement(consumer, monkeypatch, event, hosted):
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    if hosted:
        monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"hook_event_name": event, "cwd": "/fixture", "session_id": "native-session"})
        ),
    )
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer["hook"](event) == 0
    assert len(calls) == 1
    assert calls[0][1:3] == ["writer-session", "host-check" if hosted else "check"]
    assert not {"acquire", "release", "reconcile-session", "complete-no-change"}.intersection(
        calls[0]
    )
    if event != "SessionEnd":
        assert calls[0][-2:] == ["--access", "read" if event == "SessionStart" else "exec"]


def test_direct_write_check_carries_the_tool_target(consumer, monkeypatch):
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": "/fixture",
                    "session_id": "native-session",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/fixture/new.txt"},
                }
            )
        ),
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    assert consumer["hook"]("PreToolUse") == 0
    assert calls[0][1:8] == [
        "writer-session",
        "check",
        "claude",
        "/fixture",
        "ancestor:claude",
        "--session-id",
        "native-session",
    ]
    assert calls[0][8:] == ["--access", "write", "--target", "/fixture/new.txt"]


def test_direct_cd_only_bash_call_checks_destination_read_access(consumer, monkeypatch):
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": "/fixture/locked-checkout",
                    "session_id": "native-session",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cd /tmp"},
                }
            )
        ),
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    assert consumer["hook"]("PreToolUse") == 0
    assert calls[0][1:] == [
        "writer-session",
        "check",
        "claude",
        "/tmp",
        "ancestor:claude",
        "--session-id",
        "native-session",
        "--access",
        "read",
    ]


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp && rm -rf /tmp/x",
        "cd /tmp; echo hi",
        "cd $(pwd)",
        "cd /tmp\nls",
        "cd",
        "cd /tmp /var",
    ],
)
def test_compound_or_malformed_cd_commands_remain_exec_on_current_cwd(consumer, command):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert consumer["change_directory_target"](payload, "/fixture") is None


def test_consumer_declares_the_controller_capability_it_relies_on(consumer):
    assert consumer["CONTROLLER_REQUIREMENTS"] == {
        "host_handoff_continuation_version": 2,
        "session_closeout_report_version": 1,
        "task_checkpoint_park_version": 1,
        "checkout_owner_action_version": 1,
        "hosted_session_closeout_check_version": 1,
        "session_closeout_expectations_version": 1,
    }


def _session_end(consumer, monkeypatch, result):
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"hook_event_name": "SessionEnd", "cwd": "/fixture", "session_id": "native-session"}
            )
        ),
    )
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if isinstance(result, BaseException):
            raise result
        if arguments[1:3] == ["writer-session", "describe"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "decision": {
                            "code": "WRITER_SESSION_DESCRIBED",
                            "repository_path": "/fixture",
                        }
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(arguments, result, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer["hook"]("SessionEnd") == 0
    return calls


def test_session_end_hands_only_this_session_back_to_controller(consumer, monkeypatch):
    calls = _session_end(consumer, monkeypatch, 0)
    assert len(calls) == 2
    assert calls[1][0].endswith("/.local/bin/worktree-session-end")
    assert calls[1][1:] == [
        "--path",
        "/fixture",
        "--client-id",
        "claude",
        "--session-id",
        "native-session",
        "--holder",
        "ancestor:claude",
        "--trigger",
        "task-terminal",
    ]
    assert "--owner" not in calls[1]


@pytest.mark.parametrize(
    "result",
    [
        2,
        subprocess.TimeoutExpired("worktree-session-end", 85),
        OSError("missing entry point"),
    ],
)
def test_session_end_never_fails_the_session_exit(consumer, monkeypatch, result):
    calls = _session_end(consumer, monkeypatch, result)
    assert len(calls) == (2 if isinstance(result, int) else 1)


@pytest.mark.parametrize("complete", [True, False])
def test_stop_checks_every_checkout_before_normal_exit(consumer, monkeypatch, capsys, complete):
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"hook_event_name": "Stop", "cwd": "/canonical", "session_id": "real-session"}
            )
        ),
    )
    payload = {
        "selection_complete": True,
        "summary": {
            "contract": "session-closeout/v1",
            "selected": 2,
            "pending": 0,
            "terminal_complete": True,
        },
        "allocations": [
            {
                "allocation_id": "11111111-1111-4111-8111-111111111111",
                "terminal_complete": True,
                "decision": {"code": "HANDOFF_RETAINED"},
            },
            {
                "terminal_complete": complete,
                "decision": {"code": "RETAINED" if complete else "NO_TERMINAL_ACTION"},
            },
        ],
    }
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        value = (
            {
                "decision": {
                    "code": "WRITER_SESSION_DESCRIBED",
                    "repository_path": "/canonical",
                    "allocation_id": "11111111-1111-4111-8111-111111111111",
                    "holder_matches": True,
                    "lease": {
                        "holder": {
                            "client_id": "claude",
                            "session_id": "real-session",
                            "run_id": "real-run",
                        }
                    },
                }
            }
            if arguments[1:3] == ["writer-session", "describe"]
            else payload
        )
        return subprocess.CompletedProcess(arguments, 0, json.dumps(value), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer["hook"]("Stop") == 0
    assert calls[1][-5:] == [
        "--run-id",
        "real-run",
        "--expected-allocation-id",
        "11111111-1111-4111-8111-111111111111",
        "--check",
    ]
    output = capsys.readouterr().out
    if complete:
        assert output == ""
    else:
        assert json.loads(output)["decision"] == "block"
    assert {"acquire", "release", "park"}.isdisjoint(calls[1])


def test_stop_outside_governance_does_not_block_an_ordinary_conversation(
    consumer, monkeypatch, capsys
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda a, **k: subprocess.CompletedProcess(
            a, 0, json.dumps({"decision": {"code": "OUTSIDE_CONTROLLER_GOVERNANCE"}}), ""
        ),
    )
    assert consumer["end_session"]("/documents", "real-session", checking=True) == 0
    assert capsys.readouterr().out == ""


def test_repeated_stop_feedback_preserves_pending_state_without_a_loop(
    consumer, monkeypatch, capsys
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("fixture unavailable")),
    )
    assert (
        consumer["end_session"]("/governed", "real-session", checking=True, already_continued=True)
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert "decision" not in output and "keep the checkout" in output["systemMessage"]


@pytest.mark.parametrize("complete", [True, False])
def test_hosted_stop_checks_the_verified_host_session_instead_of_provider_session(
    consumer, monkeypatch, capsys, complete
):
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setenv(
        "CONTROLLER_WRITER_CONTEXT",
        json.dumps(
            {
                "repository_path": "/canonical",
                "client_id": "host-client",
                "allocation_id": "11111111-1111-4111-8111-111111111111",
                "session_id": "host-session",
                "run_id": "host-run",
                "holder": {"pid": 123},
            }
        ),
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "cwd": "/starting-checkout",
                    "session_id": "provider-session",
                }
            )
        ),
    )
    payload = {
        "selection_complete": True,
        "allocations": [
            {
                "allocation_id": "11111111-1111-4111-8111-111111111111",
                "terminal_complete": complete,
                "decision": {"code": "SESSION_READY"},
            }
        ],
        "summary": {
            "contract": "session-closeout/v1",
            "selected": 1,
            "pending": 0 if complete else 1,
            "terminal_complete": complete,
        },
    }
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer["hook"]("Stop") == 0
    assert calls[0][1:] == ["writer-session", "host-check", "/starting-checkout", "--access", "read"]
    assert calls[1][1:] == [
        "--path",
        "/canonical",
        "--client-id",
        "host-client",
        "--session-id",
        "host-session",
        "--holder",
        "123",
        "--trigger",
        "task-terminal",
        "--run-id",
        "host-run",
        "--expected-allocation-id",
        "11111111-1111-4111-8111-111111111111",
        "--check",
    ]
    assert "provider-session" not in calls[1]
    output = capsys.readouterr().out
    assert output == "" if complete else json.loads(output)["decision"] == "block"


@pytest.mark.parametrize("observation", ["released", "pending", "omitted", "different", "malformed"])
def test_hosted_stop_requires_proof_for_a_known_checkout_without_a_current_lease(
    consumer, monkeypatch, capsys, observation
):
    allocation = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setenv(
        "CONTROLLER_WRITER_CONTEXT",
        json.dumps(
            {
                "repository_path": "/canonical",
                "client_id": "host-client",
                "allocation_id": allocation,
                "session_id": "host-session",
                "run_id": "host-run",
                "holder": {"pid": 123},
            }
        ),
    )
    rows = (
        []
        if observation == "omitted"
        else [
            {
                "allocation_id": (
                    "22222222-2222-4222-8222-222222222222"
                    if observation == "different"
                    else allocation
                ),
                "terminal_complete": observation != "pending",
                "task_completed": False,
                "decision": {
                    "code": (
                        "ALLOCATION_NOT_SETTLED"
                        if observation == "pending"
                        else "CHECKOUT_RELEASED"
                    ),
                    "release_receipt_id": "fixture-release-proof",
                },
            }
        ]
    )
    payload = {
        "selection_complete": True,
        "allocations": [],
        "previous_allocations": [],
        "expected_allocations": {} if observation == "malformed" else rows,
        "summary": {
            "contract": "session-closeout/v1",
            "selected": len(rows),
            "pending": int(observation == "pending"),
            "terminal_complete": observation != "pending",
        },
    }
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer["end_session"]("/provider-cwd", "provider-session", checking=True, hosted=True) == 0
    command = calls[0]
    assert command[command.index("--expected-allocation-id") + 1] == allocation
    assert "--check" in command and "provider-session" not in command
    assert {"release", "park", "acquire"}.isdisjoint(command)
    output = capsys.readouterr().out
    if observation == "released":
        assert output == ""
    else:
        assert json.loads(output)["decision"] == "block"


@pytest.mark.parametrize("allocation", [None, "", "invalid-identity"])
def test_hosted_stop_keeps_work_when_known_allocation_identity_is_unavailable(
    consumer, monkeypatch, capsys, allocation
):
    context = {
        "repository_path": "/canonical",
        "client_id": "host-client",
        "session_id": "host-session",
        "run_id": None,
        "holder": {"pid": 123},
    }
    if allocation is not None:
        context["allocation_id"] = allocation
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    assert consumer["end_session"]("/provider-cwd", "provider-session", checking=True, hosted=True) == 0
    assert calls == []
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_hook_refusal_preserves_existing_permission_boundary(consumer, monkeypatch):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": "/fixture",
                    "session_id": "native-session",
                }
            )
        ),
    )
    monkeypatch.setattr(
        subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 2, "", "")
    )
    with pytest.raises(ValueError, match="entitlement check refused"):
        consumer["hook"]("PreToolUse")


def test_v1_spelling_does_not_rewrite_child_arguments(consumer):
    assert consumer["controller_arguments"](
        ["release", "/fixture", "--provider", "claude", "--", "--provider", "data"]
    ) == ["release", "/fixture", "--client-id", "claude", "--", "--provider", "data"]


def test_terminal_launcher_delegates_full_lifecycle_and_inherits_cli_flags(consumer, monkeypatch):
    calls = []
    monkeypatch.setattr("os.execv", lambda path, args: calls.append((path, args)))
    consumer["main"](["claude-worktree", "--model", "opus"])
    assert len(calls) == 1
    path, args = calls[0]
    assert path.endswith("/worktree-session")
    assert args[-4:] == ["--", "claude", "--model", "opus"]
    assert "--session-id" in args


def _hosted_pretool(consumer, monkeypatch, payload, result=0):
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": "/fixture",
                    "session_id": "native-session",
                    **payload,
                }
            )
        ),
    )
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, result, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    ("payload", "access"),
    [
        ({"tool_name": "Read", "tool_input": {"file_path": "/fixture/a.py"}}, ["--access", "read"]),
        ({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, ["--access", "read"]),
        ({"tool_name": "Bash", "tool_input": {"command": "echo ok"}}, ["--access", "exec"]),
        ({"tool_name": "mcp__other__tool", "tool_input": {}}, ["--access", "exec"]),
        (
            {"tool_name": "Edit", "tool_input": {"file_path": "/candidate/a.py"}},
            ["--access", "write", "--target", "/candidate/a.py"],
        ),
        (
            {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/tmp/n.ipynb"}},
            ["--access", "write", "--target", "/tmp/n.ipynb"],
        ),
    ],
)
def test_hosted_tool_calls_name_one_access_class_for_controller(
    consumer, monkeypatch, payload, access
):
    calls = _hosted_pretool(consumer, monkeypatch, payload)
    assert consumer["hook"]("PreToolUse") == 0
    assert calls == [[calls[0][0], "writer-session", "host-check", "/fixture", *access]]


@pytest.mark.parametrize("tool_input", [{}, {"file_path": ""}, None])
def test_write_without_a_target_is_refused_before_controller(consumer, monkeypatch, tool_input):
    calls = _hosted_pretool(
        consumer, monkeypatch, {"tool_name": "Write", "tool_input": tool_input}
    )
    with pytest.raises(ValueError, match="target is missing"):
        consumer["hook"]("PreToolUse")
    assert calls == []


def test_controller_refusal_of_a_classified_call_still_blocks(consumer, monkeypatch):
    _hosted_pretool(consumer, monkeypatch, {"tool_name": "Read", "tool_input": {}}, result=2)
    with pytest.raises(ValueError, match="entitlement check refused"):
        consumer["hook"]("PreToolUse")


def test_hosted_session_start_checks_read_access(consumer, monkeypatch):
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "cwd": "/fixture",
                    "session_id": "native-session",
                }
            )
        ),
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    assert consumer["hook"]("SessionStart") == 0
    assert calls[0][-2:] == ["--access", "read"]


def _all_capabilities(consumer):
    return dict(consumer["CONTROLLER_REQUIREMENTS"])


def test_installer_installs_upgrades_legacy_marker_and_checks_cleanly(
    consumer, installer, tmp_path: Path
):
    caps = _all_capabilities(consumer)
    source = ROOT / "scripts/controller-consumer"
    legacy_target = tmp_path / "bin/claude-worktree-hook"
    legacy_target.parent.mkdir(parents=True)
    legacy_target.write_text(
        '#!/usr/bin/env python3\n"""Agent Workspace Controller consumer adapter; legacy."""\n',
        encoding="utf-8",
    )

    dry = installer["install_consumers"](
        home=tmp_path,
        source=source,
        dry_run=True,
        capabilities_probe=lambda _: caps,
    )
    assert len(dry["written"]) == 5
    assert "legacy" in legacy_target.read_text(encoding="utf-8")

    installed = installer["install_consumers"](
        home=tmp_path,
        source=source,
        capabilities_probe=lambda _: caps,
    )
    assert len(installed["written"]) == 5
    for relative in installer["CONSUMER_TARGETS"]:
        path = tmp_path / relative
        assert path.read_bytes() == source.read_bytes()
        assert os.access(path, os.X_OK)

    report = installer["check_consumers"](
        home=tmp_path,
        source=source,
        capabilities_probe=lambda _: caps,
    )
    assert report["ok"] is True
    assert all(row["state"] == "current" for row in report["targets"])

    repeat = installer["install_consumers"](
        home=tmp_path,
        source=source,
        capabilities_probe=lambda _: caps,
    )
    assert repeat["written"] == []
    assert len(repeat["unchanged"]) == 5


def test_installer_refuses_symlinks_unowned_files_and_missing_capabilities(
    consumer, installer, tmp_path: Path
):
    source = ROOT / "scripts/controller-consumer"
    caps = _all_capabilities(consumer)
    error_cls = installer["ConsumerInstallError"]

    # Missing capability fails closed before touching any destination.
    weak_caps = {**caps, "host_handoff_continuation_version": 1}
    with pytest.raises(error_cls, match="host_handoff_continuation_version>=2"):
        installer["install_consumers"](
            home=tmp_path,
            source=source,
            capabilities_probe=lambda _: weak_caps,
        )
    assert not (tmp_path / "bin").exists()

    # Unowned target refuses all writes atomically.
    unowned = tmp_path / "bin/claude-worktree"
    unowned.parent.mkdir(parents=True)
    unowned.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    with pytest.raises(error_cls, match="unowned target"):
        installer["install_consumers"](
            home=tmp_path,
            source=source,
            capabilities_probe=lambda _: caps,
        )
    assert not (tmp_path / ".local/bin/codex-worktree").exists()

    # Symlink target refuses all writes atomically.
    unowned.unlink()
    unowned.symlink_to("/bin/true")
    with pytest.raises(error_cls, match="symlink target"):
        installer["install_consumers"](
            home=tmp_path,
            source=source,
            capabilities_probe=lambda _: caps,
        )
    assert not (tmp_path / ".local/bin/codex-worktree").exists()
