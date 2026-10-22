# Getting started

Use Linux, Python 3.12, Git and readable `/proc`. Install WC in a virtual environment using the
[README instructions](../README.md#install). The example needs no credentials, running service,
companion tool or connection to a remote repository.

## Run the example

From the release's source checkout, with that environment active:

```bash
python examples/quickstart.py --workspace "$HOME/wc-first-run"
```

The parent directory must exist and the workspace path must be new, including on a second run.
The script refuses an existing directory even when it is empty. A PyPI source archive includes
the same `examples/quickstart.py` if you prefer downloading that over cloning the repository.

The script creates `main/` as a new repository and `inspection/` as its governed linked checkout.
An isolated `home/` keeps demo metadata separate from your normal WC state. `commands.jsonl`
records every Git and WC command, actual exit code and JSON response. No real project is changed.

## What to expect

| Step | Meaning |
| --- | --- |
| `adopt`, `plan`, `acquire` | Register the demo's canonical checkout, check admission, allocate an inspection task. |
| `writer-lease-acquire`, `WRITE_ALLOWED` | Bind the real demo process and check its exact task, owner and session. |
| `BLOCK_OWNER_MISMATCH` | An intentional negative control: another owner is refused with exit code **1**. No forbidden write runs. |
| `recovery-guide` | Read the current state and available formal next steps. This permission refusal does not itself change the allocation to `BLOCKED`. |
| Inspection | Read the README; this task produces no source edit or new task commit. |
| `complete-no-change`, `release --dry-run`, `release` | End the unchanged task and release its checkout through the Controller. |
| Strict closeout check, `doctor`, `PASS` | Require completion proof for that exact allocation and verify metadata integrity. |

The final `PASS` means the expected refusal was observed and the example's closeout checks passed.
It does not mean a main-branch integration or release occurred. The repository, inspection branch,
isolated HOME and log remain in the workspace; the linked `inspection/` directory is released.
The script never deletes the workspace or a branch.

## If it stops

Read the last output and `commands.jsonl`. Unexpected exits stop the script; admission refusals
are not retried under another identity. Resource requirements still apply on a small machine.
An interrupted run can leave an allocation or writer lease, so do not delete a checkout by hand
or rerun into its existing path.

To inspect the same isolated state, replace the workspace below with the actual absolute path:

```bash
workspace="$HOME/wc-first-run"
HOME="$workspace/home" XDG_STATE_HOME="$workspace/home/.local/state" \
  worktree-controller status --repo "$workspace/main" --json
HOME="$workspace/home" XDG_STATE_HOME="$workspace/home/.local/state" \
  worktree-controller recovery-guide --repo "$workspace/main" --path "$workspace/inspection" --json
```

Use the recorded owner, allocation, process/session identity and current record version when
following the returned formal recovery guidance. Never edit Controller metadata or weaken a guard.
An already released checkout is inspected with `lifecycle-history`; directory absence alone does
not establish completion.

## Move on to work that changes files

The example deliberately performs a genuine no-change inspection, so `complete-no-change` is
appropriate. A task that edits files or creates commits must preserve or deliver that work;
it cannot use this shortcut. See the [operations guide](operations.md) for the existing
handoff, integration and recovery contracts and [Git Finalizer](https://github.com/xuanheng-tech/git-finalizer)
for delivery evidence. WC itself does not commit, push or approve a production release.

For real session closeout, pass the complete recorded selector (`--client-id`, `--session-id`,
`--run-id` when present, and `--holder`) and every known `--expected-allocation-id` to
`worktree-session-end --check`. After resolving its blockers, use `--require-complete` and require
`summary.terminal_complete=true` together with each allocation's decision. The example uses a
read-only strict check after its explicit completion and release.
