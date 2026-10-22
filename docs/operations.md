# Operations

Use this page to select a flow. The [CLI contract](../tool_cli_contract.json) owns exact command,
capability and result schemas; installed `--help` owns the arguments available on this host.
The [technical reference](reference.md) retains detailed protocols and compatibility history;
the [getting started guide](getting-started.md) provides an isolated first run.

## Start or resume development

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

Deliver tool changes through reviewed source, contracts and source-owned Skills, then supported
stage, rollout and activation. CLI, capability and storage versions are independent checks.
Use the existing rollback flow; never repoint the active runtime by hand or activate another
owner's unintegrated candidate. Finish with installed smoke, rollout/deployment checks and doctor.

For each external consumer, distinguish three outcomes:

| Check | What it proves |
| --- | --- |
| Capability and compatibility check | The producer advertises the contract the consumer requires |
| Installed hook/adapter smoke | The installed adapter sends correct identity, access class and target |
| Actual client tool invocation | The client discovers and invokes that adapter in its running session |

Record an unobserved client invocation as unobserved. A copied hook, passing doctor or successful
direct probe does not establish that a running client has reloaded its discovery metadata.
