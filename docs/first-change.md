# Deliver your first change

This example changes one README, commits and publishes its branch through Git Finalizer, hands
it off to WC, reviews and validates an integration candidate, and publishes local main. It can
pause after handoff and continue in a new process with the original task and integration request.
It creates a **local bare Git remote**, never contacts a hosting service, and needs no credentials.
Use a disposable workspace; do not point it at an existing project.

## Install the example's tools

WC itself needs Linux, Python 3.12, Git and readable `/proc`. Install the released version using
the [README](../README.md#install). On Ubuntu, a Python installation without `ensurepip` also
needs its distribution's `python3.12-venv` package before `python3.12 -m venv` can work.

This complete delivery example also uses these public tools:

- [Git Finalizer 1.9.3](https://github.com/xuanheng-tech/git-finalizer#installation): download its
  published release tarball and checksum file, verify them, and keep all `git-finalize*` companions
  together as its installation instructions describe. It is not a PyPI package.
- [Snapshot Runner 2.5.11](https://github.com/xuanheng-tech/snapshot-runner#requirements-and-installation):
  install its PyPI package in a separate virtual environment. Its own interpreter needs
  Python 3.12.13 or a later supported version; using Python 3.12.13+ for both environments meets
  WC's Python 3.12 requirement too. WC's core Python requirement is unchanged.

For example, with a suitable Python 3.12 already installed and a new `runner-venv` path:

```bash
python3.12 --version
python3.12 -m venv /absolute/path/to/runner-venv
/absolute/path/to/runner-venv/bin/python -m pip install 'snapshot-runner==2.5.11'
```

Use the WC environment's Python to run the example. Pass the two installed entrypoints
explicitly, or put their directories on PATH. `tool-skill-sync`, Context Loader, agent clients,
model APIs and personal Skills are not required by this example.

WC policy schema 4 and newer requires separate explicit **test and Snapshot receipt identities**
at `integration-validate`. The example creates real Snapshot Runner artifacts and reads their
README evidence; it checks the expected README content and clean candidate separately. A tool's
successful evidence collection alone is not a passed test or code review. No fake receipt, lowered
policy version or changed Controller state is used.

## Run the whole task

From this release's source checkout, with the WC environment active:

```bash
python examples/change_task.py --workspace "$HOME/wc-first-change" \
  --finalizer /absolute/path/to/git-finalize \
  --snapshot-runner /absolute/path/to/runner-venv/bin/snapshot-runner
```

The workspace path must be new and its parent must exist. The script refuses an existing path
without changing it. The source archive includes both `change_task.py` and its neighboring
`quickstart.py`; keep them together. Missing tools are refused before the workspace is created.

`PASS` means all of the following were checked:

1. The admitted feature's real writer passed the write guard before changing `README.md`.
2. The exact file scope was reviewed, committed and published to the local remote with Finalizer.
3. The writer ended and its exact commit was handed off, then queued and claimed through WC.
4. Tests and Snapshot evidence were bound to the candidate before its main-publication lease.
5. Finalizer and WC independently verified the published commit; WC released both linked checkouts.
6. Canonical main was refreshed, the delivered README was checked, strict closeout proof covered
   both known allocations, and `doctor` passed.

The example intentionally changes only a known text file, so its content review is narrow and
deterministic. Real application changes need their own tests and appropriate review.
This local demonstration does not authorize a hosted publication or production deployment.

## Pause and resume the same task

Use a different new workspace for this exercise:

```bash
python examples/change_task.py --workspace "$HOME/wc-change-resume" \
  --finalizer /absolute/path/to/git-finalize \
  --snapshot-runner /absolute/path/to/runner-venv/bin/snapshot-runner \
  --pause-after-handoff
```

The output is **`PAUSED`**, not `PASS`. The change and its branch have reached the local remote,
the feature writer is released, and `handoff.json` records the original task's source, repository
and integration identities. The integration is queued, with no candidate checkout yet.
The preserved source is still awaiting integration; this is not completed business work.

Run in a new process with the **same** workspace and installed tools:

```bash
python examples/change_task.py --workspace "$HOME/wc-change-resume" \
  --finalizer /absolute/path/to/git-finalize \
  --snapshot-runner /absolute/path/to/runner-venv/bin/snapshot-runner --resume
```

Resume checks the existing repository, local remote, source identity and uncommitted files. It
claims the original queued intent, or observes the same unchanged claimed candidate. It does not
create another task, replace source evidence, reset a branch or delete unknown files. Repeating
resume after verified completion only rechecks the delivered result and closeout evidence.

## Inspect a failure and keep the evidence

Every command's actual exit code, stdout and stderr is saved in `commands.jsonl`. The workspace
also retains Finalizer receipts, Snapshot summaries and artifacts, content-check results, branches,
the local remote and final `closeout.json`. Linked checkouts are removed only through WC.

An unexpected refusal stops with nonzero exit status and preserves the current result. This
example's resume entry covers queued or unchanged claimed handoffs and verified completion;
it is not a general crash-recovery engine. For a different interruption point, unknown files,
identity drift or a publication in progress, inspect the saved command and the formal
[recovery guidance](operations.md#recovery-guidance-and-session-closeout). Do not rerun without
`--resume` into an old workspace or clear metadata to make the example continue.

The isolated state is under the workspace's `home/`. For inspection, activate the WC environment
and use the actual workspace path:

```bash
workspace="$HOME/wc-change-resume"
HOME="$workspace/home" XDG_STATE_HOME="$workspace/home/.local/state" \
  worktree-controller status --repo "$workspace/main" --json
```

Use the original `intent_id` from `handoff.json` with `recovery-guide --intent-id` even after a
checkout is released. Follow each returned owner/CAS step and reobserve. `doctor PASS` and an
observation command's exit 0 do not prove recovery, integration or task completion.

## Repeat the regression as a contributor

The default test suite checks refusal of existing workspaces and missing prerequisites. The real
cross-tool regression is explicit opt-in, runs entirely against temporary local repositories,
and covers unknown-file preservation, source drift, pause/resume and completed replay:

```bash
WC_DEMO_FINALIZER=/absolute/path/to/git-finalize \
WC_DEMO_SNAPSHOT_RUNNER=/absolute/path/to/runner-venv/bin/snapshot-runner \
  uv run --locked --python 3.12 --no-env-file python -m pytest -q tests/test_change_task.py \
  -p no:cacheprovider --basetemp /absolute/path/to/a/new/demo-pytest
```

Install the contributor environment as described in [CONTRIBUTING](../CONTRIBUTING.md). Without
both opt-in entrypoints, the cross-tool case is skipped; a skipped case is not acceptance evidence.
