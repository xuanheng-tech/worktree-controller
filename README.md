# Worktree Controller

Worktree Controller (WC) helps people and AI agents share a Git repository without losing track of
who owns each working directory, what it may change, and whether it is safe to close.
It checks repository policy before allocating a worktree, grants writing authority to a live
process, and records handoff, integration, recovery and release evidence. Git remains the source
of truth for files, branches, commits and registered worktrees.

**Current release: 1.31.0 · Linux · Python 3.12 · Apache-2.0**

## Install

Use a virtual environment in a new directory:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install worktree-controller==1.31.0
worktree-controller --version
worktree-controller capabilities --json
```

WC requires Git and readable Linux `/proc`. Other operating systems and Python versions are not
supported by this release. Its one runtime dependency is `websockets>=15,<16`.
On Ubuntu, if creating the environment reports that `ensurepip` is unavailable, install the
distribution's `python3.12-venv` package first (for example, `sudo apt install python3.12-venv`).
Software version 1.31.0 uses CLI contract **61** and storage layout **5**.

## Try it in a disposable repository

With the environment above active:

```bash
git clone https://github.com/xuanheng-tech/worktree-controller.git wc
cd wc
git checkout v1.31.0
python examples/quickstart.py --workspace "$HOME/wc-first-run"
```

The workspace path must not exist; its parent must already exist. The example creates its own
repository and isolated HOME, uses no credentials or remote, and leaves existing projects alone.
It runs a small inspection task: admit a worktree, bind a live writer, check permission, observe an
expected wrong-owner refusal, inspect recovery guidance, and formally close the unchanged task.
On success it prints `PASS`, releases the linked checkout through WC, and keeps the repository,
branch and command log. On failure it stops and preserves results for inspection.

The [getting started guide](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/getting-started.md)
explains the output and how to proceed to tasks that change files. The example also ships in the
source distribution; it requires only WC and Git, with no companion delivery tools.

To actually change a file, publish to a local bare remote, integrate and recover a paused task,
follow [your first change](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/first-change.md).
That complete example additionally uses public Git Finalizer and Snapshot Runner installations.

## Everyday tasks

Use `worktree-controller task list` to see live checkout tasks and `task show --task-key <task>`
for the recorded owner and next formal step. The [everyday task guide](docs/daily-workflow.md)
covers simpler start, original-task checkpoint/resume, guarded no-change release and exact
Finalizer handoff. These CLI 61 entrypoints retain the existing ownership, lease and policy checks.

For client developers, the [AI client integration guide](docs/client-integration.md) describes the
public identity and closeout contract, with an offline example that continues the same unfinished
task from two independent client processes while preserving its files and checkpoint.

## Use WC in a real project

A governed repository tracks `.agents/worktree-policy.toml` and registers its checkouts with WC.
Start with read-only `status` and `doctor`; adopt existing checkouts explicitly rather than
assuming that a clean directory is unowned. Follow the
[operations guide](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/operations.md)
for task admission, writer identity, handoff, integration and safe release.

WC manages checkout authority and lifecycle. It does not implement your task, commit changes or
publish a branch. Real handoff and integration flows use separate delivery evidence from
[Git Finalizer](https://github.com/xuanheng-tech/git-finalizer).
Context Loader and the maintenance checker are optional. The complete integration example uses
Snapshot Runner for the explicit review receipts required by its policy; see
[optional integrations](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/reference.md#optional-integrations-and-dependencies).

When blocked, run `worktree-controller recovery-guide --repo /path/to/repo --path /path/to/checkout --json`
and follow the state-specific guidance. Preserve unfinished changes and unknown files.
At session end, use the full recorded session identity and check every allocation's result;
a successful process exit alone does not prove that all worktrees are settled.

## Documentation and help

- [Getting started](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/getting-started.md)
- [Operations and recovery](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/operations.md)
- [Technical reference](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/reference.md)
- [Architecture](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/architecture.md)
- [Exact CLI and capability contract](https://github.com/xuanheng-tech/worktree-controller/blob/main/tool_cli_contract.json)
- [Changelog](https://github.com/xuanheng-tech/worktree-controller/blob/main/CHANGELOG.md)
- [Publishing and recovery](https://github.com/xuanheng-tech/worktree-controller/blob/main/docs/publishing.md)
- [Releases](https://github.com/xuanheng-tech/worktree-controller/releases) · [PyPI](https://pypi.org/project/worktree-controller/)
- [Report a bug](https://github.com/xuanheng-tech/worktree-controller/issues) · [Contribute](https://github.com/xuanheng-tech/worktree-controller/blob/main/CONTRIBUTING.md)
- [Report a security issue](https://github.com/xuanheng-tech/worktree-controller/blob/main/SECURITY.md)

Licensed under the [Apache License 2.0](https://github.com/xuanheng-tech/worktree-controller/blob/main/LICENSE).
