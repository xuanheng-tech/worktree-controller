# Operations

Use this page to select a flow. The [CLI contract](../tool_cli_contract.json) owns exact command,
capability and result schemas; installed `--help` owns the arguments available on this host.
The [technical reference](reference.md) retains detailed protocols and compatibility history;
the [getting started guide](getting-started.md) provides an isolated first run.
Client implementers can follow the [AI client integration guide](client-integration.md) and its
offline cross-client continuation example.

## Repository lock availability

Capability `repository_lock_admission_version` 1 adds an entry gate for cooperating runtimes.
A writer that has entered the gate waits for current readers without admitting later readers
ahead of it. The existing `repo.lock` still excludes every metadata mutation, including calls
from older runtimes. Diagnostics retain their coherent snapshot through all checks.

Typed cleanup keeps a separate active marker while holding its shared repository lock, so
ordinary history queries remain available even when a metadata writer is waiting. Doctor
continues to wait for cleanup's filesystem/journal gate. A cleanup waiting to start does not
grant this read bypass. The entry gate and active marker use existing directories and kernel
flocks, introduce no persistent record, and disappear automatically when their process exits.

This is not a strict FIFO or a wait-time guarantee. A long existing check or cleanup can still
delay writers. Older calls do not join the new entry ordering or active marker; mixed-runtime
operation preserves exclusion but cannot promise the same read availability and ordering.
Let those owners finish normally, retain their original request and evidence, then reobserve.
Do not terminate another owner, remove locks, change authority or extend consumer timeouts to
declare recovery. A completed candidate release can precede its source releases: inspect every
`source_release_trigger.outcomes` entry and the original receipt query.

## Start or resume development

For everyday feature/audit work, the [task entrypoints](daily-workflow.md) resolve the original
checkout and record identity by task key, covering start, checkpoint/resume and finish. The
steps below remain the underlying authority and the route for advanced integration/recovery.

For a worked local task, use [your first change](first-change.md). Core inspection/allocation needs
WC and Git. The integration flow below additionally uses Git Finalizer and separate test and
Snapshot evidence; policy schema 4+ requires both receipt identities explicitly. The worked example
uses the public Snapshot Runner package. Context Loader and `tool-skill-sync` are optional.

1. Run Controller `--version`, `capabilities --json`, `status --json` and `doctor --json`.
   Read the selected repository policy and distinguish installed runtime from source candidates.
   If your site installed the optional maintenance checker, also run
   `tool-skill-sync doctor --repo <checkout> --summary` for its tool/Skill pairing checks.
2. Use `plan` for the existing task/authority. Reuse its allocation when supported; otherwise use
   `acquire` with explicit owner, branch, base and scope. A waiting request is not an allocation.
3. Bind the real writer process and session/run through `writer-session` or `writer-lease-acquire`.
   Require `write-guard` to allow the exact operation before editing.
4. Register rebuildable environments while ACTIVE. Run focused tests, the required project gate,
   and Snapshot review before Git Finalizer commits or publishes the task branch.

## Concurrent diagnostic reads

From 1.32.10, `resource-clean --typed` holds the existing repository lock shared and an
exclusive lock on the existing transactions directory. Exact closed `lifecycle-history`
queries continue to validate all metadata and evidence while cleanup runs. Ordinary mutations
and a second cleanup wait; activation refuses the live cleanup even before its journal exists.
No lock is released during an action, and cleanup never changes allocation/publication metadata.
Doctor also takes the cleanup observation gate, so its filesystem and journal diagnostics wait
for a coherent boundary. A dry-run may observe changing ignored content and refuse; rerun it
after cleanup. Registered-venv cleanup, checkout release and publication retain their exclusive
repository lock. This patch improves read availability; it does not promise a faster cleanup or
bound unrelated publication, release, storage or operating-system waits.

`doctor` refreshes Git inventory after acquiring the repository's shared lock and retains that
lock through metadata, semantic, transaction and retirement diagnostics. Normal Controller
mutations wait until those reads finish, so a guarded release cannot remove an inspected checkout
halfway through its scope or ancestry checks. This lock coordinates Controller operations; direct
Git changes and external processes still have their own observation boundaries.

A genuinely unavailable commit still refuses through the existing API/CLI contract. Keep the
original exit code and request, and compare the exact repository and checkout used by the failed
Git command before concluding that an object was lost. Repeating a diagnostic does not recover a
lifecycle, replace frozen source evidence or settle another owner's outstanding authorization.

If history returns `HISTORY_EVIDENCE_INCOMPLETE` because a release receipt's original plan is
missing, keep the citation unverified. `reconcile receipts` acknowledges existing publication
evidence; it does not reconstruct release plans. `release-recovery-inspect` requires both the
original pending journal and its plan and refuses an already completed receipt. First locate
trusted original plan bytes and prove their exact identity against the receipt. Existing task,
branch, ancestry, teardown evidence or a newly generated plan cannot replace that original.
No original evidence means there is no supported recovery to execute; preserve the receipt and
report the gap rather than copying metadata by hand or manufacturing completion.

## Handoff and integrate

Publish the source branch, hand off its exact commit and scope, then enqueue one integration intent.
Claim that intent, validate its candidate with current test/Snapshot evidence, acquire a publication
lease, and use Git Finalizer to publish the exact candidate. Return the verified record to
`lease-complete`; inspect candidate and source release outcomes separately.

Advance an idle ACTIVE canonical with `canonical-refresh`. A canonical frozen in HANDOFF_READY,
including one already on main/master, uses `canonical-transition` with exact owner, record CAS,
frozen HEAD, observed local main and published remote main identities.

A frozen source cannot accept feature writes. Same-session continuation requires a compatible
consumer to send `--access read|exec|write` and the actual write target. Continuing feature work on
the source uses the supported reopen flow; handing off does not end the whole business task.
`reopen` can retain runtime bindings only when the current live, unexpired writer lease proves
their allocation, owner and session/run identity. Client names are not owner credentials.
Missing or stale lease evidence and unknown bindings remain blocked; reopening grants no new writer.

## Resource wait and main advancing

`RESOURCE_WAIT` preserves the original durable request. Inspect its state with:

```text
worktree-controller admission-queue --repo <canonical-repo> --json
```

Resolve only the reported resource blocker through existing owner/retention/reconciliation flows.
Do not lower reserves, replace the authority, abandon the intent, or use a different filesystem to
evade the refused request. The selected admission strategy determines whether checkout counts are
limits or observations.

Once resources are available, replay the original frozen request with:

```text
worktree-controller admission-queue --repo <canonical-repo> --apply --json
```

This command can resume other eligible requests in that repository too. Read each outcome and
query status again to distinguish an allocation just acquired by the pass from its prior WAITING
row. Keep the original intent, source, scope and queue position.

If main advanced while an integration request waited without an allocation, a fresh
`integration-claim` may construct a different base and report `ADMISSION_REQUEST_DRIFT`. The guard
is intentional: it must not silently replace the existing request. Use the original-request replay
above, inspect the resulting candidate and continue its normal preparation. When its base is stale,
use the supported `integration-refresh` transition, repeat required candidate validation, and let
publication's main CAS check the refreshed base. Some states require preparation before refresh;
an `INVALID_LIFECYCLE` response is not permission to edit metadata or force a transition.

Other causes of request drift can be changed owner, target, branch or arguments. Compare the task's
original invocation and current observations; do not automatically apply the main-advance recipe
to an unrelated identity change. An unchanged clean PREPARING candidate can be observed again with
its exact original claim without allocating another checkout.

## Finished commands and runtime reconciliation

A command exit releases its writer entitlement; runtime occupancy remains until ordinary
reconciliation proves it clear. `RUNTIME_OCCUPIED` means positive holder, group or path-use
evidence. `RUNTIME_SCAN_UNCERTAIN` means visibility or identity evidence is incomplete. Both
retain the binding and execution reservation; neither proves task completion or permits cleanup.

After resolving the reported uncertainty or waiting for the actual workload to finish, rerun
`admission-queue --repo <canonical-repo> --apply --json` with the original request intact.
Inspect its per-request outcomes: exit 0 reports a successful queue observation even when a row
still has a blocker. Direct `binding-remove` returns exit 2 on either refusal. Once clear,
ordinary reconciliation removes the binding and releases the execution budget; commits and local
files remain in their checkout. Do not remove bindings manually, loosen guards or treat a retry
count as proof that the runtime must already be clear.

## Recovery guidance and session closeout

When `capabilities --json` advertises `recovery_guidance_version` 1, inspect one checkout:

```text
worktree-controller recovery-guide --repo <canonical-repo> --path <checkout> --json
```

The result explains the observed state, blockers and the next formal step. Match its exact
allocation, owner, task, authority, branch, HEAD and record version to the existing Task. Execute
an authorized `next_steps[].argv` as an argument list, then inspect again. The guide never executes
recovery or verifies all execution guards, and never predicts a future record version. Its exit 0
means the observation succeeded; it does not mean recovery or Task completion succeeded.

For a clean, unchanged `BLOCKED` integration, direct `integration-abort` is intentionally refused
with `INVALID_LIFECYCLE` (exit 2). First use the guide's exact `quarantine` request; expected HEAD may
equal observed HEAD. Reinspect `QUARANTINED` and record formal abort, then inspect guarded release.
Unknown ignored output requires preservation and typed cleanup through the flow below. Extra
commits, changed source/scope, live leases, bindings and unfinished transactions require their
existing owner/recovery path. Do not manufacture pollution or change the frozen request.

After complete release, select the **original** integration intent:

```text
worktree-controller recovery-guide --repo <canonical-repo> --intent-id <original-intent> --json
```

Run the returned `integration-retry --dry-run`; only its verified public `decision.plan_id` may
be used as `--expected-plan-id` with the identical request. Reclaim the same intent through normal
admission, explicitly resolve its returned frozen source when necessary, and repeat candidate
tests and validation. Main advancing does not authorize scope changes. `doctor PASS` establishes
metadata integrity; it does not establish lifecycle, integration or production eligibility.

For an already published and released intent, 1.30.2 verifies complete matching immutable
publication and release evidence, then reports `terminal_status=PUBLISHED_RELEASED`. The only
next step is `lifecycle-history` inspection; do not retry an integration that already finished.
Missing proof or owner/task/authority/candidate drift remains blocked. This terminal explanation
does not assert business Task completion or authorize any mutation.

Before normal exit, use the real client/session/holder/run and check **all** of its checkouts:

```text
worktree-session-end --path <canonical-repo> --client-id <client> --session-id <session> \
  --holder <real-pid-or-ancestor-selector> --run-id <recorded-run> --check \
  --expected-allocation-id <first-known-allocation> --expected-allocation-id <second-known-allocation>
```

Omit `--run-id` only when the recorded run is null. With `session_closeout_expectations_version` 1,
repeat `--expected-allocation-id` for the allocations the existing Task/session knows it acquired.
These IDs add proof obligations; they never select extra mutation targets or narrow the real
selector. A missing live checkout/lease remains pending; a removed checkout counts only with
complete matching immutable release evidence. Expected IDs are opt-in for older clients.

Read every row in `allocations`, `previous_allocations` and `expected_allocations`, and require
`summary.terminal_complete=true`. Pending live rows include `recovery_argv`; missing observations
include a lifecycle-history query. A frozen handoff must still match its Finalizer commit and
record; a retained checkpoint must match HEAD and have no replacement writer or external binding.
Deliver accepted files through Finalizer/handoff, or durably save the existing Task checkpoint,
classify artifacts and park. Unknown files stay preserved. After preparation, end the session
with the same selector and expected IDs, replacing `--check` with `--require-complete`.
Both strict modes return 2 for incomplete closeout. The legacy advisory exit 0 and `SESSION_ENDED`
alone are never success evidence. Checkout settlement does not assert business completion.

These capabilities are additive to CLI 60/storage 5; older clients keep their previous interface.
They do not authorize cross-repository edits, main integration, Bundle promotion or deployment.

## Aborted integration with preserved build output

Require `aborted_integration_preserved_cleanup_version` 1 from `capabilities --json`.
Use the same version's Python for the public `worktree_controller.preservation` module.
No consumer computes Controller digests or opens its private ledger.

Capability 2 also verifies registered environment transitions after the abort. The
original abort receipt remains immutable. New resource events identify their exact
resource; historical events are accepted only when the allocation's unique `.venv`
registration and optional cleanup form an unambiguous ordered chain with unchanged
owner and lifecycle. A cleaned environment must still be absent (including dangling
symlinks); a registered environment must retain its Python marker and local identity.
Other events, ambiguous resources, changed proofs or reconstructed environments block.
This verifies the current producer history; it does not invent a historical resource
receipt or attest unrecorded bytes. Re-run preservation at the current record CAS;
normal guarded release, not environment cleanup, settles the admission reservation.

1. Read `status`, `integration-queue`, `admission-queue` and `doctor`. Identify the exact owner,
   allocation, branch, HEAD, record version and failed intent. Retain the candidate's branch and
   original source/failure evidence. A clean checkout or expired lease grants no removal authority.
2. A `QUARANTINED` candidate stays fenced against direct cleanup and writes. Its recorded owner
   may use the existing explicit `integration-abort --path ... --owner ...
   --expected-record-version ... --reason ...`. Review its new record version and immutable abort
   receipt. Missing or changed receipt evidence, publication evidence or a still-bound intent blocks
   the new cleanup path. Keep the quarantine history; do not clear fields or metadata manually.
3. On `INTEGRATION_ABORTED`, create a preservation plan and copy for **each explicitly reviewed
   ignored tree**. This includes generated output which is otherwise recognized as disposable:

   ```text
   python -m worktree_controller.preservation plan --repo <repo> --path <candidate> \
     --allocation-id <allocation> --owner <owner> --expected-record-version <version> \
     --source <relative-tree> --plan-file <new-task-owned-plan-file>
   python -m worktree_controller.preservation copy --plan-file <plan-file>
   ```

   Use the returned receipt paths and SHA-256 values. Copies preserve opaque content and literal
   links rather than trusting directory names. Tracked content, dirty user files, unignored paths,
   leases, bindings, incomplete runtime observation and unresolved transactions keep their guards.
4. Run `resource-clean --typed --dry-run` with the exact owner/CAS, primary
   `--preservation-receipt` / `--preservation-receipt-sha256`, and paired repeatable
   `--additional-preservation-receipt` / `--additional-preservation-receipt-sha256` options.
   Review every path and blocker. Apply with the producer's `--expected-plan-id`; no threshold,
   policy change or private digest recipe is needed. Every deletion rechecks current evidence.
5. Cleanup retains `INTEGRATION_ABORTED` and never releases the checkout. Consumed live-source
   receipts cannot be replayed; they remain refused if the original source is absent. A fresh
   empty cleanup plan verifies the completed receipts and performs no deletion. Interrupted or
   partial operations use the existing exact `resource-recover` contracts, never a fresh replay.
6. Use normal `release --dry-run`, then the same exact owner/CAS release. It keeps the branch,
   records the release receipt, and settles the original durable admission reservation. An abort
   or preservation alone never cancels that reservation. Preserve release evidence before any
   separately authorized branch retirement.
7. For unchanged sources, retain the existing `integration-retry` identity and queue rules. For
   changed sources, use their own frozen reviewed handoffs and queued intent; do not replace sources
   in an old request or inherit its priority. Terminating an old intent's scope claim, when needed,
   remains a separate explicit `integration-abandon`. After release, claim the selected new intent
   through ordinary admission, build and validate anew. Historical PASS does not authorize it.

## Release and maintenance

After an official runtime rollback, synchronize its source-owned Skill with the
published installer's `sync-skill --version <active-full-oid>
--skill-rollback-from <previous-full-oid> --dry-run`, then apply the same arguments
without `--dry-run`. The previous OID must identify a retained, verified installed
runtime and be a strict descendant of the now-active commit. Installed Skill bytes
must match that exact committed source; the companion must remain unchanged. The
normal activation lock, active-pointer CAS, owned-path checks and atomic receipt
still apply. Repeating a completed sync is safe. Unknown instructions, a missing or
modified runtime, unrelated history and companion changes remain refused. This
optional installer argument does not change the runtime, storage or CLI contract;
old upgrade calls retain their ancestor-only behavior. Finish with the actual
installed `tool-skill-sync doctor --repo <checkout> --summary` and reread changed
instructions. Never copy a backup over the shared Skill to bypass a refusal.

Use `release --dry-run` to review the same guards used by execution. A clean checkout can still be
leased, in use, referenced by another environment, or lack publication/recovery evidence.
An incomplete environment scan is a blocker: inspect the reported candidate file or directory and
resolve its ownership/access/reference through the owning environment. Do not remove a reference
just to obtain a passing release.

`BINDING_IDENTITY_DRIFT` means the checkout at a bound key is not the Git registration the
binding was written for (it was removed and another worktree reused its name outside the
Controller). The checkout is unmanaged and the old allocation keeps its record. Do not write
into it under the old task: identify the new checkout's real owner and `adopt` it explicitly, or
remove it through its owner; the old task resumes from its retained branch through normal
admission.

A typed `resource-clean` refused with `HARDLINK_TOPOLOGY_BLOCKED` (for example while another
environment hardlinks the same `uv` cache files) leaves a journal that blocks the checkout and
activation. Close it with `resource-recover --stage reconcile-typed --dry-run` and the same command
with `--expected-plan-id`, then plan a fresh cleanup. Journals from before 1.28.0 that marked the
first action `FAILED_OR_PARTIAL` are accepted only when that action's `0000-before.json` is the
sole evidence and the resource tree is unchanged.

Bindings written before 1.26.0 have no registration. Run `reconcile binding-registrations
--dry-run --json`, review `candidates` and `refused`, then repeat without `--dry-run` and with
`--expected-plan-id <plan_id>`. A changed plan means the repository moved; dry-run again. Refused
bindings keep their legacy meaning; resolve them through their owner (for example release and
re-acquire) rather than writing a marker by hand.

Use `reconcile retention --actor <policy-reconciler> --json` for a read-only explanation. Apply
belongs to the delegated reconciler. Dirty or active work stays with its owner; retained branches
and immutable receipts are not disposable checkout payloads. For an interrupted release, use
`release-recovery-inspect` and replay its exact published recovery arguments.

From 1.32.11, an already removed feature/audit checkout at `RELEASED_RECORDED` may finish
its original receipt with its own retained `METADATA_WRITTEN` acquire journal present. The
original plan and both journal identities must agree with the acquired/released history,
exact +1 record CAS, retained HEAD and absent checkout, binding and writer lease. Other
transactions, changed requests and earlier incomplete phases remain blocked. The acquire
journal is preserved and stays nonterminal until the immutable release receipt is complete.
This recovery completes evidence only; it does not grant owner authority or business acceptance.

If that exact pending release blocks a normal runtime rollout, first deliver and verify the
repair through source review, tests and normal publication, then stage the exact published
release through `controller-activate stage`. Its versioned `bin/worktree-controller` is the
ordinary public CLI: an independently authorized operator may invoke only the original
`release-recovery-inspect` / returned `release` request with that pinned installed binary.
Check the public version, staged source manifest and published commit first. No candidate
activation, active-pin edit, journal edit or rollout exception is involved. Require complete
exact receipt history, then rerun normal activation with the full governed repository set.


`reconcile release-plans` keeps plans referenced by durable release receipts. They remain part
of closed lifecycle and branch-retirement evidence after the checkout is gone. It retires only
unreferenced leftover plans for already released allocations with durable release evidence.
An absent historical plan continues to block its dependent queries and retirement; preserve the
remaining evidence and recover the original plan rather than infer it from a branch or receipt.

## Checks and deployment

`bash scripts/check-shards [SHARDS]` is the fast integration gate; SHARDS is 1–64. Collection and
workers use isolated environments. Collection failure stops before workers start; empty shards
are skipped. Success removes its temporary output, while failure/interruption prints and retains
the output directory for diagnosis. Keep that evidence outside the checkout and clean it through
its owning temporary-directory workflow after review. `scripts/check-project` remains the complete
release gate, including builds and installed smokes.

The development [cross-tool acceptance cohorts](../CONTRIBUTING.md#cross-tool-acceptance)
declare staging provenance and distinguish ordinary predecessors from historical capability/CLI
migrations. An explicitly selected cohort with unmet prerequisites refuses before dependency or
output changes. Every selected case must pass without skips; unrequested cohorts are reported
separately. This does not replace the deployment gate or grant activation authority.
Test/check-script and documentation maintenance can be integrated on main for a later release;
keep the installed runtime pin when its payload and public contracts are unchanged.

Deliver tool changes through reviewed source, contracts and source-owned Skills, then supported
stage, rollout and activation. CLI, capability and storage versions are independent checks.
Use the existing rollback flow; never repoint the active runtime by hand or activate another
owner's unintegrated candidate. Finish with installed smoke, rollout/deployment checks and doctor.

### Moving an exact exported release to its public history

Normal `activate` requires descendant history and normal `rollback` requires ancestor history.
An independent public export does not satisfy either rule. Since 1.32.0, the existing
`scripts/controller-activate` entry supports `release-history-handover/v1`; it does not modify
Controller lifecycle data, Git ancestry, admission policy or writer leases. Run the exact
published target's maintenance script, with the normal site configuration:

```bash
export CONTROLLER_ROLLOUT_REPO=/absolute/public/canonical-checkout
export CONTROLLER_ROLLOUT_GIT_SOURCE=https://github.com/your-org/worktree-controller.git
export CONTROLLER_ROLLOUT_GOVERNED=/absolute/repo-one:/absolute/repo-two
controller-activate stage --version FULL_TARGET_OID --temp-helper /absolute/tool-temp-dir
controller-activate history-activate --version FULL_TARGET_OID \
  --request-id original-export-request --handover-baseline FULL_PUBLIC_ROOT_OID \
  --previous-source-repo /absolute/retained-original-source \
  --consumer /absolute/installed-host-capability-command --dry-run
```

Repeat `--consumer` for every effective external host and include the site's published
`--toolchain-compatibility /absolute/catalog.json` when one is configured. The optional checker
must understand CLI 61 and `controller-runtime-source/v1` (Git Finalizer 1.9.4 or later for the
bundled checker). Stage writes that immutable, owned source manifest beside the runtime; it binds
the exact commit, primary source checkout, Git common directory, history root, contract hash and
Skill hash. Legacy installations without it retain their existing source selection. Invalid or
changed manifests must fail rather than silently fall back. Keep both source histories and both
installed runtimes available for verification and rollback.

The gate requires a root release whose runtime, packaging, maintenance scripts and complete Skill
payload exactly equal the original frozen predecessor. Both public baseline and target must have
live matching stable release tags. The target must descend from that baseline and preserve storage,
metadata and writer-lease formats and installed Skill bytes. This entry cannot combine namespace,
policy, Skill or cleanup migrations. Existing resource, metadata and consumer gates still run.
Live compatible writer sessions may continue; incompatible, stale or incomplete evidence is not
made eligible by this procedure.

Inspect the single JSON result. Dry-run exit 0 means `plan_ready=true`, **not** completed activation.
Apply the identical request without `--dry-run`, adding its exact `--expected-plan-id`. The plan
binds the current active pointer, scope and compatibility catalog. The producer saves an immutable
receipt before switching. Preserve its `receipt` and `receipt_sha256` from the result. On interruption
or an uncertain result, inspect the original request and active pin before retrying; do not invent
another request identity. Resume using only the original receipt and SHA256:

```bash
controller-activate history-activate --history-receipt /absolute/original-receipt.json \
  --history-receipt-sha256 ORIGINAL_SHA256 --consumer /absolute/installed-host-capability-command \
  --dry-run
controller-activate history-rollback --history-receipt /absolute/original-receipt.json \
  --history-receipt-sha256 ORIGINAL_SHA256 --consumer /absolute/installed-host-capability-command \
  --dry-run
```

For either action, inspect a fresh plan and apply with its exact plan ID. Keep the original governed
repositories and consumers; adding current checks is allowed, omitting original checks is refused.
An explicit rollback request fences subsequent forward reuse of that request. Unknown pending files
are preserved. An interrupted producer write is preserved byte for byte before an exact retry.
Exit 0 with `operation_complete=true` proves the requested active pin and postchecks; a repeated
verified operation may report `activation_performed=false`. Refusal exits 2. Failed activation
followed by verified predecessor restoration exits 3 with `operation_complete=false`; this is a
failure, not successful upgrade. If restoration itself cannot pass current gates, the entry reports
the observed active commit and preserves evidence for the original recovery. Finish with installed
smoke and the default deployment doctor; runtime activation never grants business release approval.

For each external consumer, distinguish three outcomes:

| Check | What it proves |
| --- | --- |
| Capability and compatibility check | The producer advertises the contract the consumer requires |
| Installed hook/adapter smoke | The installed adapter sends correct identity, access class and target |
| Actual client tool invocation | The client discovers and invokes that adapter in its running session |

Record an unobserved client invocation as unobserved. A copied hook, passing doctor or successful
direct probe does not establish that a running client has reloaded its discovery metadata.
