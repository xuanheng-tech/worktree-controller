#!/usr/bin/env python3
"""Exercise two independent client processes continuing one unfinished task, offline."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from quickstart import POLICY

OWNER = "demo-task-owner"
TASK = "continued-work"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True,
                        help="new directory under an existing parent on the first invocation")
    parser.add_argument("--resume", action="store_true", help="continue the example's original task")
    args = parser.parse_args()
    workspace = args.workspace.absolute()
    if workspace.is_symlink():
        parser.error("workspace must not be a symlink")
    if not args.resume and (workspace.exists() or not workspace.parent.is_dir()):
        parser.error("workspace must be a new path under an existing directory")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        parser.error("the example requires Linux and Python 3.12")
    controller = Path(sys.executable).with_name("worktree-controller")
    if not controller.is_file():
        found = shutil.which("worktree-controller")
        if not found:
            parser.error("install WC 1.31.0 or later in this Python environment first")
        controller = Path(found)
    session_end = controller.with_name("worktree-session-end")
    if not session_end.is_file() or not shutil.which("git"):
        parser.error("Git and worktree-session-end must be installed")

    # Never read a caller's checkpoint or append logs through a symlink.
    if args.resume:
        for name in ("task-checkpoint.json", "commands.jsonl", "home", "main", "continued-work"):
            path = workspace / name
            require(path.exists() and not path.is_symlink(), f"missing or symlink example path: {name}")
        checkpoint_bytes = (workspace / "task-checkpoint.json").read_bytes()
        saved = json.loads(checkpoint_bytes)
        require(saved["example"] == "wc-client-continuation/v1", "unknown example checkpoint")
    else:
        workspace.mkdir()
        (workspace / "home").mkdir()
        (workspace / "tmp").mkdir()

    home, repo = workspace / "home", workspace / "main"
    checkout, audit = workspace / TASK, workspace / "inspection"
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ}
    environment.update(HOME=str(home), TMPDIR=str(workspace / "tmp"),
                       XDG_STATE_HOME=str(home / ".local/state"),
                       XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    client = "example-client-b" if args.resume else "example-client-a"
    identity = ["--client-id", client, "--session-id", client + "-session", "--run-id", client + "-run"]

    def run(*arguments, expected=0):
        command = [str(value) for value in arguments]
        result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                                text=True, timeout=120, check=False)
        with (workspace / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps({"argv": command, "returncode": result.returncode,
                                     "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        require(result.returncode == expected,
                f"{command[0]} exited {result.returncode}, expected {expected}; see commands.jsonl")
        return result.stdout

    def wc(command, *arguments, expected=0):
        if command == "task":
            action, *arguments = arguments
            command_args = [command, action, "--repo", repo, "--json", *arguments]
        else:
            command_args = [command, "--repo", repo, "--json", *arguments]
        payload = json.loads(run(controller, *command_args, expected=expected))
        if expected == 0:
            require(not payload.get("errors"), f"Controller refused {command}")
        return payload

    def row(path):
        return next(item for item in wc("status")["worktrees"] if item["path"] == str(path))

    def guard(path, task=TASK, *, selected_identity=None, owner=OWNER, expected=0):
        return wc("write-guard", "--path", path, "--task-key", task, "--authority-key", task,
                  "--owner", owner, "--holder", os.getpid(), "--operation", "feature-write",
                  *(identity if selected_identity is None else selected_identity), expected=expected)

    def acquire(path):
        return wc("writer-lease-acquire", "--path", path, "--owner", OWNER,
                  "--expected-record-version", row(path)["record_version"],
                  "--holder-pid", os.getpid(), *identity)["decision"]

    def closeout(allocations, name, *, expected=2, checking=True):
        options = [option for allocation in allocations
                   for option in ("--expected-allocation-id", allocation)]
        result = json.loads(run(session_end, "--path", repo, *identity, "--holder", os.getpid(),
                                *options, *(["--check"] if checking else []),
                                "--require-complete", expected=expected))
        (workspace / name).write_text(json.dumps(result, indent=2) + "\n")
        require(result["summary"]["terminal_complete"] is (expected == 0), "incorrect closeout summary")
        return result

    capabilities = wc("capabilities")["decision"]
    for capability in ("task_workflow_version", "session_closeout_expectations_version",
                       "session_closeout_report_version", "task_checkpoint_park_version"):
        require(capabilities.get(capability) == 1, f"unsupported capability: {capability}")

    if not args.resume:
        run("git", "init", "--initial-branch=main", repo)
        policy = repo / ".agents/worktree-policy.toml"
        policy.parent.mkdir()
        policy.write_text(POLICY)
        (repo / "README.md").write_text("# Offline client continuation\n")
        (repo / ".gitignore").write_text("generated/\n")
        run("git", "-C", repo, "add", "README.md", ".gitignore", ".agents/worktree-policy.toml")
        run("git", "-C", repo, "-c", "user.name=WC Example", "-c", "user.email=demo@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Initialize disposable fixture")
        wc("adopt", "--path", repo, "--task-key", "control", "--authority-key", "control",
           "--module", "example", "--role", "control-plane", "--owner", OWNER, "--lifecycle", "ACTIVE")
        for path, task in ((checkout, TASK), (audit, "inspection")):
            wc("task", "start", "--task-key", task, "--owner", OWNER, "--scope", "README.md",
               "--base-ref", "main", "--path", path)
        original = row(checkout)
        allocations = [original["allocation_id"], row(audit)["allocation_id"]]
        leases = [acquire(checkout), acquire(audit)]
        require(guard(checkout)["decision"]["code"] == "WRITE_ALLOWED", "writer refused")
        wrong = guard(checkout, owner="another-owner", expected=1)
        require(wrong["decision"]["code"] == "BLOCK_OWNER_MISMATCH", "owner negative control failed")
        refused = wc("writer-lease-acquire", "--path", checkout, "--owner", OWNER,
                     "--client-id", "competing-client", "--session-id", "competing-session",
                     "--holder-pid", os.getpid(), expected=2)
        require(refused["errors"][0]["code"] == "WRITER_LEASE_HELD", "competing writer was not refused")
        (checkout / "README.md").write_text("# Offline client continuation\nClient A unfinished edit.\n")
        (checkout / "owner-note.bin").write_bytes(b"preserve unknown task evidence\x00\xff")
        require(guard(audit, "inspection")["decision"]["code"] == "WRITE_ALLOWED", "audit writer refused")
        generated = audit / "generated"
        generated.mkdir()
        (generated / "example-owned.bin").write_bytes(b"example-owned generated fixture\x00")
        saved = {"example": "wc-client-continuation/v1", "task": {key: original[key] for key in
                 ("allocation_id", "task_key", "authority_key", "owner", "path", "branch", "head")},
                 "allocations": allocations, "first_holder": leases[0]["holder"],
                 "files": {name: (checkout / name).read_bytes().hex() for name in ("README.md", "owner-note.bin")}}
        checkpoint_bytes = (json.dumps(saved, sort_keys=True) + "\n").encode()
        (workspace / "task-checkpoint.json").write_bytes(checkpoint_bytes)
        closeout(allocations, "a-before-end.json")
        # End through the real selector while it can still observe both leases.
        # This releases only this session's writers and retains pending owner work.
        closeout(allocations, "a-ended.json", checking=False)
        wc("task", "finish", "--task-key", TASK, "--owner", OWNER, "--outcome", "checkpoint",
           "--checkpoint-id", "sha256:" + hashlib.sha256(checkpoint_bytes).hexdigest(),
           "--checkpoint-head", original["head"], "--waiting-on", "Continue with another client")
        partial = wc("task", "finish", "--task-key", "inspection", "--owner", OWNER,
                     "--outcome", "no-change", expected=2)["decision"]
        require(partial["code"] == "TASK_RELEASE_BLOCKED" and not partial["operation_complete"],
                "partial release was reported as success")
        closeout(allocations, "a-incomplete.json", expected=2)
        # Only this example's exact fixture is moved. Real unknown output needs its owner flow.
        (generated / "example-owned.bin").rename(workspace / "preserved-generated.bin")
        generated.rmdir()
        retried = wc("task", "finish", "--task-key", "inspection", "--owner", OWNER,
                     "--outcome", "no-change")["decision"]
        require([step["code"] for step in retried["operations"]] == ["RELEASED"]
                and retried["checkout_released"], "completion was replayed")
        final = closeout(allocations, "a-closeout.json")
        require(final["summary"]["complete"] == 1 and final["summary"]["pending"] == 1,
                "expected one released checkout and one unfinished checkout")
        print(f"PAUSED: checkpoint saved; inspection released; unfinished task still needs its owner: {workspace}")
        return

    current = row(checkout)
    require(all(current[key] == value for key, value in saved["task"].items()), "original task identity changed")
    require(current["lifecycle"] == "RETAINED", "original task is not retained")
    require(current["wait_checkpoint"]["artifact_id"] == "sha256:" + hashlib.sha256(checkpoint_bytes).hexdigest(),
            "original checkpoint changed")
    for name, value in saved["files"].items():
        require(not (checkout / name).is_symlink() and (checkout / name).is_file(),
                f"unfinished file is not a regular non-symlink file: {name}")
        require((checkout / name).read_bytes().hex() == value, f"unfinished file changed: {name}")
    refused = wc("task", "resume", "--task-key", TASK, "--owner", "another-owner", expected=2)
    require(refused["errors"][0]["code"] == "OWNER_MISMATCH", "owner change was accepted")
    wc("task", "resume", "--task-key", TASK, "--owner", OWNER)
    lease = acquire(checkout)
    require(any(lease["holder"][key] != saved["first_holder"][key] for key in ("pid", "boot_id", "start_ticks")),
            "use a separate process to resume")
    old = ["--client-id", "example-client-a", "--session-id", "example-client-a-session",
           "--run-id", "example-client-a-run"]
    require(guard(checkout, selected_identity=old, expected=1)["decision"]["code"] == "BLOCK_WRITER_LEASE_MISMATCH",
            "old client identity still authorizes writes")
    require(guard(checkout)["decision"]["code"] == "WRITE_ALLOWED", "new writer refused")
    with (checkout / "README.md").open("a") as stream:
        stream.write("Client B continued the same task.\n")
    next_checkpoint = {**saved, "previous_checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
                       "second_holder": lease["holder"], "files": {name: (checkout / name).read_bytes().hex()
                       for name in saved["files"]}}
    next_bytes = (json.dumps(next_checkpoint, sort_keys=True) + "\n").encode()
    require(not (workspace / "continued-checkpoint.json").exists(), "continued checkpoint already exists")
    (workspace / "continued-checkpoint.json").write_bytes(next_bytes)
    closeout(saved["allocations"], "b-ended.json", checking=False)
    wc("task", "finish", "--task-key", TASK, "--owner", OWNER, "--outcome", "checkpoint",
       "--checkpoint-id", "sha256:" + hashlib.sha256(next_bytes).hexdigest(), "--checkpoint-head", current["head"],
       "--waiting-on", "Example finished; business work remains unfinished")
    final_closeout = closeout(saved["allocations"], "b-closeout.json")
    require(final_closeout["summary"]["complete"] == 1 and final_closeout["summary"]["pending"] == 1,
            "unfinished files must remain pending")
    final = row(checkout)
    require(final["allocation_id"] == saved["task"]["allocation_id"] and final["lifecycle"] == "RETAINED",
            "continuation replaced the original task")
    require((checkout / "owner-note.bin").read_bytes().hex() == saved["files"]["owner-note.bin"], "unknown file lost")
    require(wc("doctor")["decision"]["code"] == "PASS", "doctor failed")
    result = {"protocol_acceptance_complete": True, "business_task_completed": False,
              "published": False, "checkout_retained": True, "session_closeout_complete": False,
              "allocation_id": final["allocation_id"],
              "first_holder": saved["first_holder"], "second_holder": lease["holder"]}
    (workspace / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS: two client processes continued one task; closeout remains pending; files and logs retained: {workspace}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        print(f"Example stopped; existing results are preserved. {error}", file=sys.stderr)
        raise SystemExit(1) from error
