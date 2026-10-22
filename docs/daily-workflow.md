# Everyday task operations

Use `worktree-controller task` for named feature/audit checkout tasks. It resolves the recorded
path, owner and record version, then calls the existing guarded lifecycle operation. The existing
low-level commands remain available. No separate task database or lifecycle is introduced.

These entrypoints require **WC 1.31.0, CLI 61, task_workflow_version 1**. Storage layout remains 5.
Check the installed `--version`, `capabilities --json` and `task --help` before using them. A newer
source checkout does not update a separately installed Controller. Governed installations still
need their normal compatibility, deployment and activation checks.

Start in an already governed repository: inspect `status`, `doctor` and its selected policy;
adopt existing checkouts explicitly. Examples below use `/project` as that repository's canonical
checkout. These commands operate locally and do not grant permission to publish or deploy.

## See the current work

```bash
worktree-controller task list --repo /project
worktree-controller task show --repo /project --task-key report-fix
```

`list` shows live linked checkouts, their recorded owner, lifecycle, writer lease observation,
cleanliness and any checkpoint dependency. It is not a list of all business Tasks or queued
integrations. `show` explains the named checkout and its next formal step. After release, the
task key queries the original lifecycle history instead of inventing a new task.

Task keys can be ambiguous in imported or historical work. Mutations never pick a checkout by
owner, age or directory order. If multiple live checkouts share a key, select its exact `--path`
instead. Historical results retain their proof status and pagination; absence is not completion.

## 1. Start and finish a task with no changes

```bash
worktree-controller task start --repo /project --task-key report-fix \
  --owner alice --scope src/reports

# An inspection that made no file changes and produced no task commit:
worktree-controller task finish --repo /project --task-key report-fix \
  --owner alice --outcome no-change
```

Start requires an explicit task key, owner and file scope. It defaults to feature role,
`authority_key=task_key`, module `task`, branch `task/<task-key>`, and the repository policy's
publication remote/branch as base. Override `--authority-key`, `--module`, `--branch`, `--base-ref`
or `--path` when your existing task requires different values. It does not fetch or substitute
another base when the policy base is unavailable locally. An existing live task is not duplicated.

Admission creates a checkout, **not write entitlement**. Before editing, bind the real writer
with the existing writer-session/lease flow and require `write-guard` to allow the operation.
Use the admitted path and declared task/authority. End only that writer's exact lease before
finishing. See [start or resume development](operations.md#start-or-resume-development).

The no-change finish first records the existing `complete-no-change` result, then attempts normal
guarded release. It retains the branch. A task commit or dirty worktree refuses no-change completion.
Unknown ignored files or registered resources can block the separate release step. In that case,
exit status is **2**, `TASK_RELEASE_BLOCKED` retains the completed prefix and provides a release
dry-run command. Preserve/classify those files through their existing owner flow. Repeating this
finish while `NO_CHANGE_COMPLETED` retries release without creating another completion receipt.
After successful release, use `task show`; another finish refuses the absent live allocation.

## 2. Pause and continue the original task

First save a checkpoint in your existing Task system. Obtain its content-addressed ID and the
exact HEAD it records. WC records that owner's attestation; it does not create or verify the
checkpoint payload and does not copy uncommitted files into it. Then, after the writer ends:

```bash
worktree-controller task finish --repo /project --task-key report-fix --owner alice \
  --outcome checkpoint --checkpoint-id "$checkpoint_id" --checkpoint-head "$checkpoint_head" \
  --waiting-on 'Awaiting source data'

worktree-controller task resume --repo /project --task-key report-fix --owner alice
```

The checkpoint ID must use the existing `sha256:<64 hex>` or `art_<64 hex>` format. Its HEAD
must still match the checkout. Parking preserves the same checkout, branch, uncommitted files
and unknown files; work remains unfinished. Resume uses the same allocation and owner, without
creating a replacement task or writer lease. Obtain fresh write entitlement before editing again.
This entry does not release an unfinished checkout; that remains a separate reviewed owner flow.

## 3. Hand off a completed change

Run appropriate tests/review and publish the task branch through your authorized Git Finalizer
flow. Use the actual returned record and the exact commit it binds; do not invent evidence.
After ending the exact writer:

```bash
worktree-controller task finish --repo /project --task-key report-fix --owner alice \
  --outcome handoff --finalizer-record-id "$finalizer_record_id" --commit "$commit"
```

`TASK_HANDED_OFF` freezes the original scope and commit. It does not integrate or publish main,
release the checkout, declare the business Task complete or authorize a production deployment.
Continue the existing [handoff and integration](operations.md#handoff-and-integrate) flow, or use
the complete [first change example](first-change.md). Scope/commit drift and unknown files keep
their existing refusals. Active **or expired but still recorded** writer leases remain blocked;
these entrypoints never steal, renew or remove them.

## Inspect a blocked integration

```bash
worktree-controller task recover --repo /project --task-key original-integration-task
# Also works after its candidate checkout has been released:
worktree-controller task recover --repo /project --intent-id "$original_intent_id" --json
```

`recover` is a read-only entry to the existing `recovery-guide`. It shows the recorded owner,
blockers and exact next formal command, retaining the original intent and evidence. For an
unchanged BLOCKED integration, the supported path starts with quarantine, then formal abort,
guarded release and retry of the original intent. Execute only the current owner-authorized
step, then run `recover` again: every transition can change the next command and record version.
Source/scope drift, dirty files, active publication and missing evidence still block recovery.
The entry does not run commands, edit Controller metadata or make a recovery plan executable.

## Read results accurately

All actions accept `--json`. `list`, `show` and `recover` return 0 when observation succeeds,
including when that observation describes a blocker. This is not recovery or task completion.
Mutating actions return 0 only when the requested supported operation finishes; refusals,
`RESOURCE_WAIT` and incomplete release return 2. A waiting admission keeps its existing request;
inspect `admission-queue` instead of creating another task or branch.

Task selectors do not replace exact session closeout. At normal session exit, use the real
client/session/holder/run and every known expected allocation with `worktree-session-end --check
--require-complete`; read all rows and `summary.terminal_complete`. Business Task completion,
checkout release and published integration each require their own evidence.
