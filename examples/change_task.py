#!/usr/bin/env python3
"""Deliver a README change to a local bare remote, with an optional handoff pause."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from quickstart import POLICY

OWNER = "demo-owner"
CONTENT = "# WC demo\nA delivered change.\n"
CHECK = """
import pathlib, sys
if (pathlib.Path(sys.argv[1]) / 'README.md').read_text() != sys.argv[2]:
    raise SystemExit('README change did not match the reviewed result')
print('README change verified')
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--finalizer", type=Path, help="installed public git-finalize entrypoint")
    parser.add_argument("--snapshot-runner", type=Path, help="installed public snapshot-runner entrypoint")
    parser.add_argument("--pause-after-handoff", action="store_true")
    parser.add_argument("--resume", action="store_true", help="continue this demo's saved handoff")
    args = parser.parse_args()
    workspace = args.workspace.absolute()
    if args.resume:
        if args.pause_after_handoff:
            parser.error("--resume and --pause-after-handoff are separate invocations")
        if workspace.is_symlink() or not (workspace / "handoff.json").is_file():
            parser.error("--resume needs this demo's existing workspace and handoff.json")
    elif workspace.exists() or workspace.is_symlink() or not workspace.parent.is_dir():
        parser.error("--workspace must be a new path under an existing directory")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        parser.error("the demo requires Linux and Python 3.12")
    controller = Path(sys.executable).with_name("worktree-controller")
    if not controller.is_file():
        found = shutil.which("worktree-controller")
        if not found:
            parser.error("install WC in this Python environment first")
        controller = Path(found)
    finalizer = args.finalizer or shutil.which("git-finalize")
    if not finalizer or not Path(finalizer).is_file() or not os.access(finalizer, os.X_OK):
        parser.error("install Git Finalizer, or pass --finalizer /absolute/path/to/git-finalize")
    finalizer = Path(finalizer).absolute()
    runner = args.snapshot_runner or shutil.which("snapshot-runner")
    if not runner or not Path(runner).is_file() or not os.access(runner, os.X_OK):
        parser.error("install Snapshot Runner, or pass --snapshot-runner /absolute/path/to/snapshot-runner")
    runner = Path(runner).absolute()
    session_end = controller.with_name("worktree-session-end")
    if not session_end.is_file() or not shutil.which("git"):
        parser.error("Git and worktree-session-end must be installed")
    if not args.resume:
        workspace.mkdir()
        (workspace / "home").mkdir()
        (workspace / "tmp").mkdir()
    repo, source, candidate = (workspace / name for name in ("main", "change", "integration"))
    for path in (repo, source, candidate, workspace / "origin.git", workspace / "handoff.json"):
        require(not path.is_symlink(), f"demo paths must not be symlinks: {path}")
    environment = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ}
    home = workspace / "home"
    state = home / ".local/state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    environment.update(
        PATH=os.pathsep.join((str(controller.parent), str(finalizer.parent), os.environ.get("PATH", os.defpath))),
        HOME=str(home), TMPDIR=str(workspace / "tmp"),
        XDG_STATE_HOME=str(home / ".local/state"), XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"), GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
    )
    identity = ["--client-id", "wc-change-demo", "--session-id", "change-demo", "--run-id", "change-demo"]

    def run(*arguments, expected=0):
        result = subprocess.run([str(arg) for arg in arguments], cwd=workspace, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=120, check=False)
        with (workspace / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps({"argv": [str(arg) for arg in arguments],
                                     "returncode": result.returncode, "stdout": result.stdout,
                                     "stderr": result.stderr}) + "\n")
        require(result.returncode == expected,
                f"{arguments[0]} exited {result.returncode}, expected {expected}; see commands.jsonl")
        return result.stdout

    def save(name, value):
        (workspace / name).write_text(json.dumps(value, indent=2) + "\n")

    def wc(command, *arguments):
        if command == "task":
            action, *arguments = arguments
            value = json.loads(run(controller, command, action, "--repo", repo, "--json", *arguments))
        else:
            value = json.loads(run(controller, command, "--repo", repo, "--json", *arguments))
        require(not value.get("errors"), f"Controller refused {command}; see commands.jsonl")
        print(f"{command}: {(value.get('decision') or {}).get('code', 'observed')}", flush=True)
        return value

    def row(path):
        return next(item for item in wc("status")["worktrees"] if item["path"] == str(path))

    def gf(name, *arguments):
        value = json.loads(run(finalizer, "--summary", *arguments))
        save(name, value)
        require(value["status"] == "success", f"Finalizer stopped; see {name}")
        return value

    def check(path, name):
        output = run(sys.executable, "-I", "-c", CHECK, path, CONTENT)
        require(not run("git", "-C", path, "status", "--porcelain"), "candidate must be clean")
        save(name, {"status": "PASS", "checked_file": "README.md", "stdout": output,
                    "commit": run("git", "-C", path, "rev-parse", "HEAD").strip()})

    def snapshot(path, name, *, committed):
        arguments = ["branch-review", "--repo", path, "main"] if committed else [
            "diff-audit", "--repo", path, "--scope-path", "README.md"]
        summary = json.loads(run(runner, *arguments, "--summary"))
        save(name, summary)
        require(summary["status"] == "complete" and not summary["truncated"]
                and not summary["evidence_gap"] and not summary["warnings"], "Snapshot evidence incomplete")
        evidence = json.loads(run(runner, "read", summary["snapshot_id"], "--repo", path, "--path", "README.md"))
        save(name.replace(".json", "-read.json"), evidence)
        require(evidence["found"] and not evidence["truncated"] and not evidence["evidence_gap"],
                "README snapshot could not be read")
        return summary["snapshot_id"]

    if not args.resume:
        run("git", "init", "--bare", "--initial-branch=main", workspace / "origin.git")
        run("git", "init", "--initial-branch=main", repo)
        for key, value in (("user.name", "WC Demo"), ("user.email", "demo@example.invalid")):
            run("git", "-C", repo, "config", key, value)
        run("git", "-C", repo, "remote", "add", "origin", workspace / "origin.git")
        policy = repo / ".agents/worktree-policy.toml"
        policy.parent.mkdir()
        policy.write_text(POLICY)
        (repo / "README.md").write_text("# WC demo\nA local change task.\n")
        gf("bootstrap-finalizer.json", "--initial-publish", "--remote", "origin", "--repo", repo,
           "--message", "Initialize local demo", "--", "README.md", ".agents/worktree-policy.toml")
        wc("adopt", "--path", repo, "--task-key", "demo-control", "--authority-key", "demo-control",
           "--module", "demo", "--role", "control-plane", "--owner", OWNER, "--lifecycle", "ACTIVE")
        acquired = wc("task", "start", "--task-key", "demo-change", "--module", "demo",
                      "--owner", OWNER, "--branch", "demo/change", "--base-ref", "main",
                      "--path", source, "--scope", "README.md")
        allocation = acquired["decision"]["operation"]["allocation_id"]
        lease = wc("writer-lease-acquire", "--path", source, "--owner", OWNER,
                   "--holder-pid", os.getpid(), *identity)["decision"]
        guard = wc("write-guard", "--path", source, "--task-key", "demo-change",
                   "--authority-key", "demo-change", "--owner", OWNER, "--holder", os.getpid(),
                   *identity, "--operation", "feature-write")
        require(guard["decision"]["code"] == "WRITE_ALLOWED", "writer was refused")
        (source / "README.md").write_text(CONTENT)
        run(sys.executable, "-I", "-c", CHECK, source, CONTENT)
        source_snapshot = snapshot(source, "source-snapshot.json", committed=False)
        delivered = gf("source-finalizer.json", "--initial-branch-publish", "--remote", "origin",
                       "--remote-branch", "demo/change", "--repo", source,
                       "--message", "Deliver the README change", "--", "README.md")
        commit = delivered["commit"]["sha"]
        holder = lease["holder"]
        wc("writer-lease-release", "--path", source, "--lease-id", lease["lease_id"],
           "--holder-pid", holder["pid"], "--holder-boot-id", holder["boot_id"],
           "--holder-start-ticks", holder["start_ticks"], *identity)
        wc("task", "finish", "--task-key", "demo-change", "--owner", OWNER,
           "--outcome", "handoff", "--commit", commit,
           "--finalizer-record-id", workspace / "source-finalizer.json")
        queued = wc("integration-enqueue", "--task-key", "demo-integration", "--authority-key",
                    "demo-integration", "--module", "demo", "--owner", OWNER,
                    "--source-allocation", allocation, "--source-snapshot", source_snapshot)["decision"]
        checkpoint = {"schema_version": 1, "intent_id": queued["intent_id"],
                      "source_allocation": allocation, "source_commit": commit,
                      "repository_id": wc("status")["repository"]["repository_id"]}
        save("handoff.json", checkpoint)
        if args.pause_after_handoff:
            print(f"PAUSED: source published to local origin, writer released; resume with --resume --workspace {workspace}")
            return
    else:
        checkpoint = json.loads((workspace / "handoff.json").read_text())
        require(checkpoint["schema_version"] == 1, "unsupported demo checkpoint")
        require(run("git", "-C", repo, "rev-parse", "--show-toplevel").strip() == str(repo),
                "repository path changed")
        require(run("git", "-C", repo, "remote", "get-url", "origin").strip() == str(workspace / "origin.git"),
                "demo remote changed")
        require(wc("status")["repository"]["repository_id"] == checkpoint["repository_id"],
                "repository identity changed")
        require(run("git", "-C", repo, "rev-parse", "demo/change^{commit}").strip() == checkpoint["source_commit"],
                "saved source identity changed")
        for path in (repo, source, candidate):
            if path.exists():
                require(not run("git", "-C", path, "status", "--porcelain"),
                        f"uncommitted files remain in {path}; preserve them and follow recovery-guide")

    guide = wc("task", "recover", "--intent-id", checkpoint["intent_id"])["decision"]["guidance"]
    if guide.get("terminal_status") != "PUBLISHED_RELEASED":
        require(guide["intent"]["state"] in {"QUEUED", "PREPARING_INTEGRATION"},
                "this demo resumes only queued or unchanged claimed work; follow recovery-guide")
        claimed = wc("integration-claim", "--intent-id", checkpoint["intent_id"], "--owner", OWNER,
                     "--path", candidate, "--branch", "demo/integration")["decision"]
        check(candidate, "candidate-check.json")
        candidate_snapshot = snapshot(candidate, "candidate-snapshot.json", committed=True)
        wc("integration-validate", "--path", candidate, "--owner", OWNER,
           "--expected-record-version", row(candidate)["record_version"],
           "--validation-evidence-id", workspace / "candidate-check.json",
           "--tests-receipt-id", workspace / "candidate-check.json",
           "--snapshot-receipt-id", candidate_snapshot)
        publication = wc("lease-acquire", "--path", candidate, "--owner", OWNER,
                         "--expected-record-version", row(candidate)["record_version"],
                         "--run-id", "demo-publication")["decision"]
        published = gf("publication-finalizer.json", "--publish-integration-candidate", claimed["candidate_oid"],
                       "--repo", candidate, "--lease-id", publication["lease_id"], "--run-id", "demo-publication")
        require(published["remote_verify"]["oid"] == claimed["candidate_oid"], "remote identity mismatch")
        wc("lease-complete", "--path", candidate, "--expected-record-version", row(candidate)["record_version"],
           "--lease-id", publication["lease_id"], "--published-commit", claimed["candidate_oid"],
           "--finalizer-record-id", published["record_id"], "--run-id", "demo-publication")
        candidate_allocation = claimed["allocation_id"]
    else:
        candidate_allocation = guide["intent"]["allocation_id"]
    wc("canonical-refresh")
    check(repo, "main-check.json")
    strict = json.loads(run(session_end, "--path", repo, *identity, "--holder", os.getpid(),
                            "--expected-allocation-id", checkpoint["source_allocation"],
                            "--expected-allocation-id", candidate_allocation, "--check", "--require-complete"))
    save("closeout.json", strict)
    require(strict["summary"]["terminal_complete"] is True, "checkout closeout incomplete")
    require(wc("doctor")["decision"]["code"] == "PASS", "doctor did not pass")
    require(not source.exists() and not candidate.exists(), "linked checkouts remain; follow their release guidance")
    print(f"PASS: change integrated into local main; both checkouts released; branches and evidence kept: {workspace}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        print(f"Demo stopped; results are preserved. {error}", file=sys.stderr)
        raise SystemExit(1) from error
