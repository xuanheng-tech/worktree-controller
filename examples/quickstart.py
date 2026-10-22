#!/usr/bin/env python3
"""Run a local inspection task in a new workspace, preserving every result."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

POLICY = """schema_version = 8
[limits]
normal_limit = 6
hard_limit = 11
[canonical]
protected = true
allowed_roles = ["control-plane"]
[slots]
feature_or_audit = 3
integration_prepare_limit = 2
emergency = 1
recovery_reserve = 1
retained = 3
[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"
[release]
automatic_feature_audit = true
automatic_integration_continuation = false
preserved_auto_release = false
intent_ttl_seconds = 3600
[reconciler]
enabled = true
actor = "lifecycle-reconciler"
allow_legacy_adoption = false
[admission]
strategy = "resource-budget-v1"
checkout_growth_bytes = 2147483648
checkout_growth_inodes = 100000
[review]
stale_warning_days = 7
[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
[convergence]
enabled = true
grace_minutes = 30
idle_hours_max = 72
idle_hours_min = 6
standing_reserve = 3
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True,
                        help="new directory under an existing parent; existing paths are refused")
    args = parser.parse_args()
    workspace = args.workspace.absolute()
    if workspace.exists() or workspace.is_symlink() or not workspace.parent.is_dir():
        parser.error("--workspace must be a new path under an existing directory")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        parser.error("the demo requires Linux and Python 3.12")
    controller = Path(sys.executable).with_name("worktree-controller")
    if not controller.is_file():
        found = shutil.which("worktree-controller")
        if not found:
            parser.error("install worktree-controller in this Python environment first")
        controller = Path(found)
    session_end = controller.with_name("worktree-session-end")
    if not session_end.is_file() or not shutil.which("git"):
        parser.error("Git and worktree-session-end must be installed")
    workspace.mkdir()
    home, temporary = workspace / "home", workspace / "tmp"
    home.mkdir()
    temporary.mkdir()
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ")
                   if key in os.environ}
    environment.update(
        HOME=str(home), TMPDIR=str(temporary),
        XDG_STATE_HOME=str(home / ".local/state"),
        XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
    )
    repo, checkout = workspace / "main", workspace / "inspection"

    def run(*arguments, expected=0):
        result = subprocess.run([str(arg) for arg in arguments], cwd=workspace,
                                env=environment, capture_output=True, text=True, timeout=120,
                                check=False)
        with (workspace / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps({"argv": [str(arg) for arg in arguments],
                                     "returncode": result.returncode, "stdout": result.stdout,
                                     "stderr": result.stderr}) + "\n")
        if result.returncode != expected:
            raise RuntimeError(f"{arguments[0]} exited {result.returncode}, expected {expected}; "
                               f"see {workspace / 'commands.jsonl'}")
        return result.stdout

    def wc(command, *arguments, expected=0):
        payload = json.loads(run(controller, command, "--repo", repo, "--json", *arguments,
                                 expected=expected))
        if payload.get("errors"):
            raise RuntimeError(f"Controller refused {command}: {payload['errors']}")
        print(f"{command}: {(payload.get('decision') or {}).get('code', 'observed')}", flush=True)
        return payload

    def row():
        return next(item for item in wc("status")["worktrees"]
                    if item["path"] == str(checkout))

    run("git", "init", "--initial-branch=main", repo)
    policy = repo / ".agents/worktree-policy.toml"
    policy.parent.mkdir()
    policy.write_text(POLICY)
    (repo / "README.md").write_text("# WC demo\nA local inspection task.\n")
    run("git", "-C", repo, "add", "README.md", ".agents/worktree-policy.toml")
    run("git", "-C", repo, "-c", "user.name=WC Demo", "-c", "user.email=demo@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Initialize demo")
    wc("adopt", "--path", repo, "--task-key", "demo-control", "--authority-key", "demo-control",
       "--module", "demo", "--role", "control-plane", "--owner", "demo-owner",
       "--lifecycle", "ACTIVE")
    request = ["--task-key", "demo-inspection", "--authority-key", "demo-inspection",
               "--module", "demo", "--role", "audit"]
    if wc("plan", *request)["decision"]["code"] != "CREATE_ALLOWED":
        raise RuntimeError("admission refused; preserve the workspace and inspect its log")
    acquired = wc("acquire", *request, "--owner", "demo-owner", "--branch", "demo/inspection",
                  "--base-ref", "main", "--path", checkout, "--scope", "README.md")
    allocation = acquired["decision"]["allocation_id"]
    identity = ["--client-id", "wc-demo", "--session-id", "demo-session", "--run-id", "demo-run"]
    lease = wc("writer-lease-acquire", "--path", checkout, "--owner", "demo-owner",
               "--holder-pid", os.getpid(), *identity)["decision"]
    guard = ["--path", checkout, "--task-key", "demo-inspection", "--authority-key",
             "demo-inspection", "--holder", os.getpid(), *identity,
             "--operation", "feature-write"]
    allowed = wc("write-guard", *guard, "--owner", "demo-owner")
    if allowed["decision"]["code"] != "WRITE_ALLOWED":
        raise RuntimeError("the current writer was refused")
    refused = wc("write-guard", *guard, "--owner", "another-owner", expected=1)
    if refused["decision"]["code"] != "BLOCK_OWNER_MISMATCH":
        raise RuntimeError("the negative control did not refuse the wrong owner")
    wc("recovery-guide", "--path", checkout)
    print(f"Inspection: {len((checkout / 'README.md').read_text().splitlines())} README lines")
    holder = lease["holder"]
    wc("writer-lease-release", "--path", checkout, "--lease-id", lease["lease_id"],
       "--holder-pid", holder["pid"], "--holder-boot-id", holder["boot_id"],
       "--holder-start-ticks", holder["start_ticks"], *identity)
    wc("complete-no-change", "--path", checkout, "--owner", "demo-owner",
       "--expected-record-version", row()["record_version"])
    release = ["--path", checkout, "--owner-release", "demo-owner",
               "--expected-record-version", row()["record_version"]]
    wc("release", *release, "--dry-run")
    if wc("release", *release)["decision"]["code"] != "RELEASED":
        raise RuntimeError("checkout release did not complete")
    closeout = json.loads(run(session_end, "--path", repo, *identity, "--holder", os.getpid(),
                             "--expected-allocation-id", allocation, "--check"))
    if closeout["summary"]["terminal_complete"] is not True:
        raise RuntimeError("the expected allocation has no complete closeout proof")
    if wc("doctor")["decision"]["code"] != "PASS" or checkout.exists():
        raise RuntimeError("final verification failed")
    print(f"PASS: expected refusal exit 1; closeout complete; branch, repository and logs kept: {workspace}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Demo stopped; existing results are preserved. {error}", file=sys.stderr)
        raise SystemExit(1) from error
