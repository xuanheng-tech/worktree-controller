# Integrating an AI client

Use the existing public CLI as the boundary between a client and WC. The client implements the
business task and keeps its durable checkpoint; WC remains the authority for admission, writer
identity, lifecycle and release. Client names do not grant ownership or write permission.

## Check the installed contract

Read `worktree-controller --version`, `capabilities --json` and the installed command's `--help`.
The task-oriented flow below requires CLI 61 and `task_workflow_version=1`. For checkpoint and
strict closeout, require `task_checkpoint_park_version=1`, `session_closeout_report_version=1`
and `session_closeout_expectations_version=1`. Storage layout is independently versioned (5 in
1.31.0). A client must explicitly support the reported contract and required capabilities; a
newer version number alone is not sufficient. Refuse an unsupported contract without mutation.

Invoke commands as argument lists and parse JSON, process exit status and the specific result
together. Never parse human descriptions, evaluate a returned shell string, open WC's private
state, recreate a digest or replace a refused operation with raw Git worktree commands.

## Keep the task and executor identities separate

| Durable business Task keeps | Current executor obtains or observes |
| --- | --- |
| Repository, task key, authority key, recorded owner | Actual live holder PID, boot identity and start time |
| Exact allocation ID, checkout path and branch | Client, session and run identity |
| Checkpoint payload, content-addressed ID and bound HEAD | Fresh writer lease and current record version |
| All allocations acquired by this task/session | Current HEAD, scope, lifecycle and blockers |
| Finalizer, integration and release evidence when applicable | Current public recovery guidance |

Changing AI clients changes the executor identity. It does not create a new business Task,
transfer its owner, replace its allocation or inherit the previous client's lease. An owner
transfer uses the existing explicit review/transfer flow. Re-observe identity, HEAD, scope and
record version before every mutation; a checkpoint is not a reusable authorization credential.

## Start, pause and continue

1. Inspect `status`, `doctor`, the selected policy and an existing compatible task. Use
   `task start` only for a new task. Admission returns a checkout without writer entitlement.
2. Bind the actual executor through the supported `writer-session` handshake or
   `writer-lease-acquire`. Pass the exact returned holder/session/run identity to `write-guard`
   for the recorded task, authority, owner, path and operation before writing.
3. To pause, first persist the existing Task's checkpoint, including its exact HEAD and enough
   context to continue. WC does not copy or verify that payload. Preserve uncommitted files,
   unknown files and generated output. Inspect normal exit with the real full session selector
   while its leases still exist. End through that same selector to record its allocations and
   release only its own leases; inspect any incomplete result. Then use `task finish --outcome
   checkpoint` with its content-addressed ID and HEAD and repeat the strict check. Releasing leases
   individually before session end can lose the live selector's association: expected IDs add
   proof obligations but cannot select a retained checkout or establish that association.
4. A different client reloads the Task checkpoint, uses public `task show`/`status` to match the
   original allocation, owner, path, HEAD and scope, and refuses drift. Use `task resume` with the
   original owner, then acquire fresh entitlement for the new live executor and repeat the guard.
   A stale recorded lease must be resolved through the original formal recovery; never steal it.
5. When work is deliverable, use real Finalizer evidence and `task finish --outcome handoff`,
   followed by the existing integration flow. A no-change finish is only for genuine unchanged
   work. A checkpoint pause leaves work unfinished and retains the checkout.

See [everyday commands](daily-workflow.md) for exact examples and [operations](operations.md)
for the existing writer, cleanup, recovery and integration entrypoints.

## End every allocation, and report failures accurately

Use the real client/session/holder/run selector with `worktree-session-end --check
--require-complete`. Add every allocation the Task/session knows it acquired with repeatable
`--expected-allocation-id`; these are additional proof obligations, not mutation selectors.
Inspect every live, previous and expected row and require `summary.terminal_complete=true`.
After preparation, end the session with the same selector and expected IDs using
`--require-complete` without `--check`. Reobserve any refusal through the public recovery guide.

An advisory exit 0 is not settlement evidence. A read-only guide may successfully describe a
blocker. `RESOURCE_WAIT` and an incomplete no-change release return 2; retain the original request
and any completed prefix. Retry only the pending formal step. Checkout settlement, business
completion and main publication each require their own evidence. A retained checkpoint can
settle an executor session while its business Task remains unfinished, but dirty or unclassified
files still require owner action and keep strict closeout incomplete. Checkpoint success alone
does not settle that obligation. Preserve the pending result when the original owner continues
the same Task in a new client; do not recreate an old holder to make it appear complete.

## Run the offline continuation example

From the current source checkout, with WC 1.31.0 or later installed (the example is scheduled for
the next source distribution):

```bash
python examples/client_continuation.py --workspace "$HOME/wc-client-example"
python examples/client_continuation.py --workspace "$HOME/wc-client-example" --resume
```

The first invocation requires a new path under an existing parent. Each invocation is a real,
separate executor process with a different client/session/run identity. The example uses its
own HOME and a disposable repository, no remote, credentials, companion delivery tools or AI
service. It verifies original-task continuation, wrong-owner and competing-writer refusals,
two-checkout closeout, partial-release exit 2 and retry without duplicate completion.

The second invocation verifies that the new writer is permitted and the old client identity is
refused. It preserves the unfinished edit, unknown binary note, original checkpoint and its new
generation. Only the exact generated fixture created by the example is moved to its preservation
path; real unknown output still requires its owner's classification/preservation flow.
`PASS` means the protocol exercise completed. No business task is declared complete or published.
The original work stays `RETAINED`, with its branch, files and command/closeout evidence available.
Strict closeout correctly returns 2: the inspection checkout has complete release proof, while
the dirty unfinished checkout remains pending. Each executor formally ends its own writer session.
An existing workspace, identity/file drift or repeated resume is refused, preserving evidence.

This demonstrates two CLI consumers, not actual invocation or discovery by two AI vendors.
Vendor-specific discovery and live-client use require their own observed acceptance, as described
in [checks and deployment evidence](operations.md#checks-and-deployment).

## Upgrade an existing managed installation

A public package installation in a new environment is separate from activation of a shared
Controller. Use the site's supported installer, published toolchain compatibility catalog,
consumer checks and rollout gate. Finish with installed smoke and the default installed doctor.
Do not repoint the active runtime, modify another client's session or widen accepted versions.

The current `controller-activate` upgrade requires the target commit to descend from the active
commit. An installation built from private history cannot infer an upgrade path into an
independent public root. It needs an explicitly reviewed migration contract before activation;
ordinary bootstrap, copying the shared Skill or treating matching storage versions as proof is
not a substitute. A catalog refusal blocks deployment, while isolated protocol validation and
source delivery can continue. Record staged, activated and verified results separately.
