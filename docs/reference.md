# Technical reference

For the task-oriented CLI 61 entrypoints, see [everyday task operations](daily-workflow.md).
They delegate to the existing lifecycle guards and introduce no additional stored state.

For a first run, use the [getting started guide](getting-started.md). This reference retains
the detailed contracts, historical compatibility notes and benchmark evidence previously in the README.
The [CLI contract](../tool_cli_contract.json) is authoritative for exact interfaces.

## Repository lock admission (1.32.12)

`repository_lock_admission_version: 1` retains CLI contract 61 and storage layout 5. All new
entrants take an exclusive flock on the existing metadata-root directory before acquiring
`repo.lock`, then release that entry gate once admitted. A writer waiting for current readers
therefore excludes later cooperating readers. A shared read already held by the same PID and
thread can nest across `MetadataStore` instances without waiting behind its own writer.

Typed cleanup still serializes on the transactions directory and holds `repo.lock` shared.
After admission, it holds a flock on the existing cleanup-actions directory until its final
scan or failure receipt completes. Readers that find the entry gate occupied can confirm that
active marker and take their compatible shared `repo.lock`; filesystem/journal diagnostics
retain their separate cleanup-observation gate. The marker is not release or write authority.
These locks use the same Linux directory-flock support as the existing cleanup gate.
Lock diagnostics should include `repository.metadata_path` itself, `repo.lock`,
`transactions` and `cleanup-actions`; new waiters can be at the entry directory before
reaching `repo.lock`.

The admission gate does not provide strict global FIFO or a finite latency bound. Old code
continues to obey `repo.lock` but ignores the new entry gate and active marker. Its exclusion
is preserved; mixed-runtime ordering and history availability can remain limited until those
calls finish. No lease, receipt, metadata migration, new database or service is introduced.

Repeated cleanup checks retain only the allocation scope of unrelated completion documents
inside the current thread's outermost repository flock, including fresh `MetadataStore`
instances used by one diagnostic. Each reuse pins and rechecks the regular file and
ancestor identities, including inode, size, mtime and ctime. Replacement, in-place mutation,
ownership/type change or symlink substitution forces a fresh read or refusal. Relevant
documents always receive full byte/hash, plan, action and generation verification; this is
neither a cached verdict nor a source-tree cache. The scope map clears when the outer flock
exits. An isolated six-check fixture with sixteen 1 MiB unrelated receipts reduced full document
reads from 96 to 16 with equal results; this does not establish a production latency bound or
explain every historical long-held lock.

## No-change preservation history reads (1.32.13)

Terminal no-change checks now use the same lock-scoped allocation filter as preserved
cleanup. This includes the check for cleanup completed before the original no-change
transition: it previously decoded unrelated completion documents again in each nested
verification. Relevant documents are still read and checked on every lookup. Only unrelated
allocation scopes with unchanged file identities may be reused, never a receipt verdict;
the map expires at the end of the outermost flock. A new authority lock reads those bytes again.

An isolated preservation plan for five files totaling 27,479 bytes, with eight unrelated
2 MiB completion documents, reduced unrelated full-document reads from 64 to 16
(approximately 128 MiB to 32 MiB decoded). Both authority observations, the source scan,
original completion proof and existing refusal conditions remain mandatory. The fixture
also included 16 MiB of ignored files outside the selected source. This addresses repeated
history decoding; the first scan, preservation-tree verification, Git observation and
retirement authority scans retain their existing costs. It establishes no site latency
bound and does not identify the cause of a historical timeout.

## Optional integrations and dependencies

The core control plane has a single runtime dependency (`websockets>=15,<16`) and works standalone
against any local Git repository on the supported platform. The integrations below are optional
for core inspection/allocation; selecting a delivery flow can add explicit evidence requirements:

- **Companion delivery tools.** WC composes with, but does not require or embed, the separate
  **Context Loader** (context reading), **Snapshot Runner** (read-only review evidence) and
  **Git Finalizer** (commit and publication). Each owns its own authority; WC never merges,
  publishes or commits on their behalf. The handoff and integration flows in
  [docs/operations.md](operations.md) assume Git Finalizer is installed for delivery evidence.
  Policy schema 4+ also requires explicit test and Snapshot receipt identities for preparation;
  [your first change](first-change.md) uses the public Snapshot Runner package to produce them.
  Context Loader and the optional maintenance checker are not required by that example.
- **Client-specific recovery.** The `archived_codex_binding_recovery_version` capability
  (`--recover-archived-codex-session`) recovers a released writer lease whose original session
  belonged to the Codex CLI client, by verifying that client's own archived-session and
  app-server-control state under the user's home directory. It is inert for other clients.
- **Keyed document scope.** The `integration_keyed_document_scope_version` capability proves
  independent progress-document records from frozen Git objects for one specific known handbook
  profile (`repo_id` `quant-platform`, schema 2). It activates only for that exact document profile
  and otherwise leaves the ordinary file-scope fence unchanged.
- **Legacy namespace migration.** `namespace_migration` performs the one-time move of a repository's
  historical `codex-worktree/v1` metadata namespace to the current `worktree-controller/v1` layout.
  Repositories created under the current namespace never invoke it.
- **Maintainer deployment.** `scripts/controller-activate` and `scripts/controller-runtime` are the
  source-owned runtime activation and start entry points used by a maintainer's host; they are not
  required to install or use the published package. Since 1.32.0, their explicit
  `release-history-handover/v1` and `controller-runtime-source/v1` contracts support exact public
  exports and immutable runtime provenance; see the [maintenance procedure](operations.md#moving-an-exact-exported-release-to-its-public-history).

From 1.30.1, a completed cleanup at a retained source may survive normal park/resume
cycles followed by a formal handoff of the same HEAD and owner. Verification requires
the complete exact-CAS history, original cleanup identity, action proofs and preserved
generations. It rewrites no receipt and accepts no arbitrary CAS or HEAD drift; normal
runtime, lease and release guards still apply.

From 1.30.0, read-only `recovery-guide` and explicit expected-allocation coverage are available for
strict session closeout. Both are additive capabilities on CLI 60/storage 5; inspect installed
capabilities before use. See [recovery and closeout](operations.md#recovery-guidance-and-session-closeout).

From 1.30.2, `recovery-guide --intent-id` also explains an already published and released
integration through complete matching immutable evidence. It points to lifecycle history instead
of retrying a completed integration. Incomplete or drifted evidence remains blocked.

From 1.29.2, scope attribution accepts a retained formal publication reflog suffix
only when it includes the exact frozen task base and every retained transition is
continuous and forward, ending at the current formal ref. Git may expire older
entries without making already-published background changes part of the task's
scope. A suffix missing the task base, a broken or rewritten chain, unavailable
history, replacement graphs, symbolic refs and ambiguous merge bases still keep
the frozen comparison bound. No reflog, scope or allocation metadata is rewritten.

Capability `aborted_integration_preserved_cleanup_version` 1 extends the existing exact
preservation and typed cleanup interfaces to explicitly aborted unpublished integrations.
Every removed tree needs immutable preservation coverage; quarantine, request identity,
leases, runtime, ownership and release guards remain mandatory. Only formal checkout release
settles its admission reservation. See the [operational flow](operations.md#aborted-integration-with-preserved-build-output).

Capability 2 additionally validates post-abort registered `.venv` resource lineage,
including the unambiguous legacy single-resource history. New resource events bind
the exact resource payload. No other post-abort lifecycle transition is accepted,
and no historical abort receipt or failure event is rewritten.

The supported runtime is Python 3.12 on Linux with Git and readable procfs. Writer identity,
runtime evidence and repository locking depend on Linux process interfaces and POSIX flock;
other operating systems are not supported by this release.

From 1.28.2, `reconcile release-plans` preserves every plan referenced by a durable release
receipt. Closed lifecycle queries and branch retirement continue to use the original plan bytes;
only unreferenced leftover plans for already released allocations may be retired. Missing
historical plans still require their original evidence and are never inferred from Git ancestry.

From 1.28.1, admission reconciliation observes completed checkout requests in one batch per
repository under a shared repository lock. Host-ledger accounting and execution observations
also use shared locks: they do not change repository metadata. This avoids unnecessary writer
lock contention with status/history readers and repeated full Git/metadata scans. Snapshots
never survive their held lock or authorize a later queued acquisition. Acquire recovery that
changes repository evidence still takes an exclusive lock; runtime removal and new acquisitions
retain their original guarded mutation paths. Missing locks, registration or owner drift,
uncertain transactions and incomplete release evidence still block reconciliation.

From 1.28.0, typed `resource-clean` verifies the hardlink topology before it marks an action
`STARTED`, so a topology refusal leaves no action evidence and `resource-recover --stage
reconcile-typed` closes it as a no-op. A journal left by an earlier runtime, whose first action is
`FAILED_OR_PARTIAL` with `HARDLINK_TOPOLOGY_BLOCKED` and whose only evidence is that action's
`0000-before.json`, is accepted too: unchanged whole-tree resource evidence and in-scope topology
prove no deletion began. ctime/nlink drift is tolerated only on inodes that have links outside the
scope before and after (another environment sharing the files). Capability
`typed_cleanup_verify_boundary_version` 1.

From 1.27.0, `reconcile binding-registrations --dry-run` lists the live linked bindings written
before 1.26.0 and the evidence for each; applying the same command with `--expected-plan-id`
writes their registration marker and document. A binding qualifies only when its record exists, its
Git `gitdir` pointer names the observed checkout, no marker is already present, Git created the
registration no later than 900 seconds after the allocation's `created_at`, and any recorded base
commit is in the checkout's history. Refused bindings are listed with typed blockers and keep their
legacy meaning. Records, bindings and history are unchanged. Capability
`binding_registration_backfill_version` 1.

From 1.26.0, a linked binding belongs to the exact Git registration it was written for.
`acquire` and `adopt` write a `worktree-registration/v1` token into the worktree's Git
administrative directory (`worktree-controller-registration`) and record it under
`binding-registrations/`. If the checkout is removed outside the Controller and Git later
registers another worktree under the same administrative name, the token is missing: the binding
is withdrawn from every snapshot, `status` warns `BINDING_IDENTITY_DRIFT`, and the new checkout is
unmanaged, so write-guard, writer leases and release refuse it instead of granting the old task's
authority. A binding whose administrative directory is gone keeps the existing `ORPHAN_BINDING`
meaning, and bindings written before 1.26.0 keep their previous meaning until
`reconcile binding-registrations` ties them to their registration (1.27.0, below). Git commands run by the
Controller ignore inherited repository selection (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
`GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY` and related variables), so a call from a Git hook observes
the repository it names. An unreadable metadata directory now fails closed
(`FILESYSTEM_PERMISSION`/`INVALID_METADATA`) instead of reading as empty. The release runtime scan
also refuses a checkout whose path or inode changes during the scan; same-UID processes whose procfs
entries deny access remain host visibility gaps, as in typed cleanup. Storage layout 5 declares the
existing `cleanup-actions`, `cleanup-recoveries`, `native-session-recoveries` and
`session-closeouts` directories and adds `binding-registrations`; closeout observations whose holder
is provably gone are pruned. CLI contract 57.

From 1.22.4, direct `writer-session check` and hosted `host-check` do not retain decoded
metadata or historical mapping caches during their single-snapshot decisions. Each new call
still reads and validates every metadata file under the same locks, with unchanged byte decoding,
JSON error details and authority checks. Commands that reload snapshots retain command-local reuse.

From 1.22.3, direct `writer-session check` holds one shared repository lock through its guard
and handoff continuation. It loads one snapshot instead of reloading after a refusal, so concurrent
Controller lease/lifecycle changes wait for the decision. Each new call still reads and validates
current metadata; grants, refusals, CLI contract 54 and storage layout 4 are unchanged.

From 1.22.2, runtime scans compare path boundaries without repeatedly splitting ordinary procfs
paths for each checkout root. Lexical path rules, deleted references, scan scope and fresh release
checks are unchanged. Five alternating pairs of three host-namespace acceptance cases had median
times of 18.90 / 14.04 seconds before / after (26% lower); this local shared-host sample does not
establish a full-suite or portable latency improvement. CLI contract 54 and storage layout 4.

From 1.22.1, an entitlement check decodes repository metadata once instead of three times: the
caller's locked snapshot is passed to the write guard, and governance discovery leaves validation to
that load. Decisions and fail-closed behavior are unchanged; the median hook check dropped from
about 0.47 s to 0.31 s on a repository with about 20,000 metadata records. CLI contract 54.

From 1.22.0, `writer-session check` accepts the same `--access`/`--target` continuation for a
direct (non-hosted) holder, and a frozen holder may also write into another checkout whose writer
lease the same live process holds, subject to that checkout's own feature-write guard. Entitlement
checks skip the full status scan (about a 0.5 s saving per hook call). `handoff --scope-changed`
records the exact paths changed since the publication base; an integration-ready handoff of
unscoped changed work now returns warning `MISSING_HANDOFF_SCOPE` instead of failing later at
`integration-enqueue`. A consumer may report `required_controller_capabilities`; the rollout gate
refuses activation or rollback to a runtime that lacks any declared minimum. Capability
`host_handoff_continuation_version` 2; CLI contract 54, storage layout 4.

From 1.21.0, a host that names an access class keeps its own frozen handoff's Controller flow.
`writer-session host-check <path> --access read|exec|write [--target <absolute-path>]` returns
`CONTINUATION_GRANTED` (`HOST_HANDOFF_CONTINUATION`) only to the exact live holder of the current
lease on its own `HANDOFF_READY` or `PRESERVED` feature allocation. Reads and commands continue;
writes continue only outside Controller governance or into an integration candidate built from
that allocation. The frozen checkout, other checkouts and checks without `--access` keep their
refusal, and HEAD drift, a missing lease or a foreign holder refuse every class. Capability
`host_handoff_continuation_version` 1; CLI contract 53, storage layout 4.

From 1.20.3, session-end reconciliation pins the selected allocation and original record
version. Terminal parking checks for a writer lease under the same repository lock as
the state change, including a new lease whose runtime binding has not yet been written.
A delayed hook reports its per-allocation refusal and preserves the new writer; path-only
hooks also refuse a current writer. Explicit owner `park` keeps its existing semantics.

From 1.20.2, `release` and `lease-complete` resolve a stable canonical repository context
before checkout removal. Passing `--repo` from the released checkout or its subdirectory
completes receipt writes, publication acknowledgement and source release in the same call.
Release guards, record CAS, retained refs and exact recovery identity remain unchanged.

From 1.20.1, command-local history reuse retains a private copy of validated metadata mappings.
Each snapshot still scans current directory entries and checks every file's identity; added,
removed or changed files use the existing decode and filename validation path. Snapshot mappings
remain independent, and no history cache survives the command. Locking, authority rechecks,
durable writes, CLI contract and storage layout are unchanged.

From 1.19.23, nonempty metadata string validation reuses its first whitespace-normalization
result. Accepted text, empty/NUL rejection and error priority stay unchanged. Snapshot freshness,
schema checks, command-local decode reuse and durable write boundaries retain their existing gates.

From 1.20.0, `integration_keyed_document_scope_version` 1 proves independent QP Handbook
record changes from frozen Git objects. Distinct records may prepare concurrently; source scope,
ownership, validation and serialized main publication retain their existing authority.

From 1.19.20, uncached historical metadata reads open each fresh directory entry directly,
avoiding a per-file `Path` construction. UTF-8 text/newline handling, JSON object checks,
errors and full metadata validation are unchanged. Each snapshot scans current directory
entries; command-local decode reuse keeps its existing file-identity checks and lifetime.

From 1.19.18, optional metadata UUIDs in lowercase hyphenated form are validated by a
compiled ASCII pattern. All other inputs retain the standard-library UUID validator, including
legacy accepted formats and their original text after outer whitespace is stripped. There are no
UUID version or variant restrictions. Schema validation, errors, snapshot freshness and
command-local cache lifetime are unchanged.

Git evidence uses the stored commit graph with replacement objects disabled. Inventory accepts
Unicode ref names and literal rename/copy paths. Separate Git directories are resolved through Git;
when invoking from a linked checkout, configure `core.worktree` with the canonical checkout path.
Metadata timestamps retain the UTC+08:00 offset without a system timezone database requirement.
Expected filesystem failures on the CLI return the ordinary error envelope (`FILESYSTEM_ERROR`),
including under `--json`.

Storage layout v4 declares the existing `registration-prune-receipts/` directory introduced in
1.9.2; upgrading the contract does not move data. Controller writes each
`registration-prune/v1` receipt once and refuses a different document at the same path. A receipt
is authoritative proof when classifying an interrupted registration-prune transaction as terminal,
but it is not repository identity or lock authority. The receipt records the reviewed GC plan id;
its bytes are not an input to that earlier plan digest. Namespace migration includes these JSON
documents in its merged-content digest and moves or verifies them byte for byte. The separate
`metadata-reference-relocation/v1` cleanup inventory does not include this directory because it
has no historical cleanup reference. Existing v3 readers can continue reading their known paths;
consumers that pin the exact layout must accept v4 before activation.

## Resource admission and completion

From 1.1.0, checkout and writer counts are statistics across every readable policy
schema. One per-user host ledger under
`$XDG_STATE_HOME/worktree-controller/resource-admission/v1` atomically reserves
filesystem bytes/inodes, expected growth and preservation space across repositories.
Existing occupied space is already reflected by kernel counters; private materialized
blocks receive credit without charging shared caches or hardlinks twice. Execution
uses a memory budget and CPU/memory/I/O pressure hysteresis. Initial checkout growth
is 2 GiB and 100,000 inodes, with 16 GiB/100,000 inode filesystem headroom; execution
initially reserves 2 GiB with 4 GiB available-memory headroom. New admission tightens
under pressure; protected running work is not preempted.

From 1.19.17, host ledger saves use sorted-key compact UTF-8 JSON with a final newline.
Existing indented ledgers remain readable, and readers do not rewrite them. Request and holder
digests, ledger contract v1, save boundaries, locking, private file permissions, file fsync,
atomic replacement and directory fsync are unchanged. Repository metadata and immutable receipts
retain their existing encoding.

`admission-queue --all-repositories` observes durable requests; `--apply` reconciles
through ordinary acquisition and recovery. Waiting grants no writer ownership.
Agent Workspace's existing process reaper periodically forwards this reconciliation
and approved release-intent reconciliation, including after a restart. Request keys,
allocation identities and formal receipts prevent duplicate creation or action replay.
An attempted operation with uncertain effects requires formal reconciliation.

From 1.29.1, an explicitly abandoned integration intent settles its matching, unattempted
checkout request as `CANCELLED`. Reconciliation verifies the original owner and frozen source
identity, no transaction/allocation, and absent target and branch under the repository lock,
then compares the exact ledger row before saving the terminal event. Dry-run reports the same
decision without writing. Attempted or uncertain requests stay blocked; this does not complete
the Task or change its source handoff.

With `integration_lineage_admission_version` 1, a queued integration task may share its frozen
feature/audit/hotfix/emergency source authority. Controller verifies that exact intent and source
lineage under the repository lock before reserving another checkout; the source request, frozen
evidence, scope and queue position remain intact. Unrelated or same-task request drift is still
refused. Reuse the original intent and authority. If a durable resource request is already waiting
and main has advanced, replay that request through `admission-queue --repo <canonical-repo> --apply`
before preparation/refresh; a fresh claim can otherwise report `ADMISSION_REQUEST_DRIFT`.
See the [resource-wait recovery sequence](operations.md#resource-wait-and-main-advancing).
A repeated exact claim of an unchanged clean `PREPARING_INTEGRATION` candidate returns its existing
allocation without another reservation, event or publication lease. A resource wait reports
`RESOURCE_WAIT` with the original queued intent and no candidate; scope fences remain separate.

Failed acquisition retains its journal whenever Git effects are partial or uncertain. Only the
existing positive no-effect classifier permits archiving the exact journal in the host request's
history and making its reservation retryable. Proof is saved before journal removal; interrupted
settlement resumes through `admission-queue --apply`, without replaying the acquire in that pass.
An exact guarded release remains terminal after its retained branch is retired through a matching
Controller authorization and observed deletion receipt. The release record, receipt, allocation,
branch and OID must agree; missing or consumer-only retirement proof retains the acquire fence.
During settlement the exact open authorization can supply that proof, while its independent
outstanding-authorization fence remains until completion. Historical journals stay unchanged.
`acquire_no_effect_recovery_version=1` declares this recovery support. While archived proof still
belongs to a reserved attempt, runtime switching requires that capability; settle the attempt before
rolling back to an older runtime.
Standalone execution admission reconciles finished execution bindings through the existing
holder, descendant and runtime checks when that can change its answer: a request that would wait, or
one refused because an earlier execution of the same task is still recorded, reconciles and is asked
once more with the same durable request. An admission that is immediately available proceeds without
that pass and leaves finished executions to their host's own exit handling and the periodic reaper.
From 1.19.11, standalone execution admission and cancellation do not retain decoded metadata on
their first, single-snapshot attempt. Admission enables command-local decode reuse only if it
needs recovery, which can decode unchanged history twice. Composite session admission keeps
its existing cache across plan, acquisition and execution admission. Validation and guards are
unchanged; this does not guarantee faster recovery.
Live or uncertain processes keep their budgets. A final exited session can be settled by `admission-queue --all-repositories --apply`
or the existing external reaper; each newly launched execution uses a new run identity.
That pre-admission pass skips a reservation whose exact recorded holder is still running: it has
nothing to recover yet, and its repository is neither scanned nor locked until the holder exits.
An execution admitted but never bound to a holder keeps its reservation while its allocation is held,
because a launch may still be in flight. Formal allocation release settles it with the allocation's
release receipt; the same passes settle one left behind by an older release, only with that unique
receipt and the existing Git, path and journal checks.
A checkout budget whose repository no longer exists settles only in the all-repositories
`admission-queue --apply` pass, and only on positive absence: the repository, its common directory
and the checkout path each report ENOENT, and each one's nearest existing ancestor is a real directory
(not a symlink) on the very filesystem the budget was reserved on. The row records that
`vanished-repository/v1` evidence. Anything present, any failed observation (EACCES, EIO, ENOTDIR and
the like), or an ancestor on another filesystem keeps the reservation.
The same fresh absence proof excludes a vanished checkout and its exact terminal execution
from repository maintenance enrollment. Historical rows for live repositories still enroll them,
so approved release intents remain discoverable after the last admission has settled. Unmatched
or nonterminal executions retain their own observations and budgets.

Executor hosts with capability `session_admission_version` 1 may admit a session in two
commands instead of one command per step. `session-admit` runs `plan`, `acquire` and
`execution-admit` in one process for a new feature allocation acquired from the canonical
checkout, and answers every other start (an existing allocation for the task, a non-canonical
workspace, a plan other than `CREATE_ALLOWED`) with `SESSION_ADMISSION_FALLBACK` without changing
anything. `session-bind --holder-pid PID` then runs `writer-lease-acquire` for that exact live
process, the holder-aware `write-guard` and the runtime `binding-add`. Each step is the existing
command's own function with unchanged guards, journals, receipts and record-version CAS; only
the repository status every intermediate command would attach is omitted. A bind that fails after
its lease was granted attempts to release that lease and reports `decision.lease_outcome` for
Controller and filesystem errors. If release also fails, the outcome retains the exact lease and
holder identity for recovery; it does not report a successful release. If the execution budget
was recorded before the runtime binding write failed or was interrupted, that budget stays
occupied while the holder or its process group is alive, even when lease compensation succeeds.
After runtime exit, `admission-queue --apply` settles the budget through the existing runtime-clear
gates without completing the task; a later run resumes the same allocation when needed and
requests its own execution admission. If the runtime binding was durably written but the
`SESSION_BOUND` response was lost, retrying with the exact live holder, client/session/run
identity and binding kind reuses the same lease ID, binding and execution budget; the existing
lease renewal may update its renewal time and lease record version. A different live holder
is refused with `WRITER_LEASE_HELD`; a different session/run identity is refused with
`EXTERNAL_BINDING`, preserving the original binding and budget. Exit recovery still waits for
the holder and its process group to finish and does not complete the task. If runtime binding
removal was persisted but the execution-budget release was interrupted or its ledger write
failed, the budget remains reserved. `admission-queue` observes this pending recovery without
writing; `admission-queue --apply` rechecks runtime clearance and releases the budget once,
without restoring a synthetic binding or completing the task. A subsequent session waits while
the previous holder or its process group is alive, then resumes the retained allocation and
requests its own budget before binding a new lease. A delayed old `binding-remove` keeps its
original record-version CAS; stale requests fail with `CAS_MISMATCH`. A queue pass that read the
old execution before the handover rechecks current runtime occupancy. Neither path can clear
the new session's binding, lease or budget; replaying an old lease after runtime exit still
does not release the new lease's budget.
The detached retention pass that admission starts runs
after the acquisition and is skipped while one is running or within 300 seconds of the last
one started.

`execution-admit` / `execution-cancel` budget a never-launched managed execution.
`host-execution` uses the same budget for a process outside a repository; it grants
no writer lease. `binding-transfer` formally transfers a verified process descendant
through the existing runtime binding contract. Writer ownership, process occupancy,
business completion and allocation release remain separate facts.
Host reservation recovery is scoped to its exact typed holder chain (boot ID, PID,
start ticks, client/session/run and process group). Another active allocation using
the same repository or canonical path never keeps a dead reservation alive. Missing
or malformed identity, any writer lease, and a host target spanning the user home
or a broader ancestor remain fail-closed. From 1.4.0 a home-or-broader reservation is
released only by `host-execution --action clear --recover-dead-holder`, which requires the
caller to present that reservation's own recorded holder digest plus a matching terminal
record for its session and run, and writes an immutable `host-recoveries/<request-id>.json`
receipt before the release. An unqualified clear of such a row is still refused.
The producer refuses it up front: `host-execution --action admit` rejects the home
root and any of its ancestors, including symlinked aliases, so an unbounded
reservation cannot be minted again; a concrete working directory is admitted
unchanged. `host-execution --action inspect` is the read-only recovery
intelligence surface: for each host-execution reservation owned by the caller's
uid it publishes the canonical holder bytes (sorted-key compact JSON over
client_id, pid, boot_id, start_ticks, session_id, run_id) and their SHA-256, the
live kernel observation proving the recorded holder dead, a target observation
(`PRESENT`, `ABSENT` or `IDENTITY_CHANGED`), and the terminal-evidence template a
typed caller must present — no caller needs to read the private ledger layout,
and inspection never writes ledger bytes. A reservation whose target has
provably vanished closes through the same typed authority: the orphan's typed
clear records `target_observation: "ABSENT"` in the single immutable receipt,
while other actions against a missing target stop with
`RESOURCE_IDENTITY_CHANGED`. From 1.19.19, normal `cancel` also accepts a missing
concrete target for an unattempted `WAITING` or `RESERVED` host request owned by
the caller's uid, with no allocation, holder, runtime binding or writer lease.
The original request identity remains unchanged; repeat cancellation is idempotent
and accounts zero memory. A present target must still match its recorded device
and inode, and an uncertain observation never proves absence. Cancellation grants
no dead-holder recovery authority or task completion. Crash-replayed identical recovery authority reuses
the original immutable receipt; differing bytes are always refused.

From 1.1.2, acquiring or renewing a writer never creates an execution reservation.
AW and `worktree-session` admit resources before launch and bind the existing typed
runtime occupancy before command execution. A waiting session holds no writer.
The independent terminal wrapper retains its occupancy through parent exit; the
existing reconciler verifies exit and descendants, clears occupancy, and runs the
ordinary closeout path. Nested host-managed commands only check their parent's
full identity. Old writer-only budget records are settled against exact formal
allocation release receipts, without claiming their coordinator process exited.

Status separates `writer_leases_recorded` from `runtime_occupied`. The latter counts
registered allocations with a `runtime:` binding, including retained and canonical
allocations; multiple bindings on one allocation count once. `live_executions` is
retained as an occupancy compatibility alias, with its meaning in
`live_executions_semantics`. These metadata counts do not observe process liveness
or imply business completion. A dead historical writer lease is never runtime use.

New task resources belong to their recorded owner. With the approved automatic
feature policy, exact remote-verified, handed-off results carry receipt-bound cleanup
authority; unknown content and unpublished work remain protected. Persistent data
must live outside disposable checkouts, or be explicitly preserved before cleanup.
Only registered reproducible resources and exact verified preservation contracts
authorize deletion. A normal `worktree-session-end --path <checkout>` resolves its
allocation and existing evidence itself; it never marks unfinished work successful.
Stale intents are revalidated into new identities. Integration and publication keep
their existing serialization; this repository does not enable automatic integration.

## Retention convergence

Admission is resource based and does not count checkouts, so the population only stays bounded
if unused checkouts leave again. From 1.12.0, policy schema 8 adds:

```toml
[convergence]
enabled = true
grace_minutes = 30       # published / no-change / closed checkouts
idle_hours_max = 72      # unfinished work while the population is within its reference
idle_hours_min = 6       # floor under pressure
standing_reserve = 3     # idle checkouts tolerated beside the live ones
release_unintegrated_handoffs = true   # optional, default false (see below)
```

`reconcile retention --actor <reconciler> [--trigger manual|admission|background] [--apply]`
reports (default) or applies convergence. Only the `[reconciler]` actor may apply. A checkout is
live when it holds a writer lease or bindings or a publication lease, or when any process uses
it. An `ACTIVE` feature/audit allocation with none of that evidence is dormant: clients without
writer leases end sessions without telling the Controller, so after `idle_hours_max` (pressure
never shortens it for an `ACTIVE` lane) it is parked exactly as a finished session would park
itself. Clean, unused feature/audit checkouts, and frozen hotfix handoffs, are routed to an
existing guarded verb and then to the unchanged `release`:

| Facts | Route | Waits |
| --- | --- | --- |
| HEAD, base and task commit are in the publication branch | `close-delivered` | grace |
| HEAD equals base | `complete-no-change` | grace |
| Frozen feature/audit/hotfix handoff equals HEAD and is published | release | grace |
| `release_unintegrated_handoffs`: integration-ready feature/audit/hotfix handoff not yet integrated, with task/handoff/Finalizer identities, local branch and `<remote>/<branch>` all equal to HEAD and no open integration intent | release (branch kept) | dynamic idle threshold |
| Already `RELEASE_READY` / `ARCHIVED` / `NO_CHANGE_COMPLETED` | release | grace |
| Unfinished commits | `archive-retained` (branch kept) | dynamic idle threshold |
| `ACTIVE` without lease, process, binding or Git activity | `park`, then the row above that fits | `idle_hours_max` (never shortened by pressure) |

The idle threshold is `idle_hours_max` while `registered <= live + standing_reserve`; above that
it is `max(idle_hours_min, idle_hours_max / pressure^2)`. Idle time is the latest of the
checkout's Git admin files (HEAD, index, reflog) and its lifecycle events. Dirty, detached,
locked and missing checkouts are reported as `ATTENTION_REQUIRED` and never touched. An
unregistered root `.venv` is registered as a reproducible `python-venv`; the release teardown
preflight still decides whether it may be removed. A hotfix `.venv` is the exception: it needs
its owner's `handoff-owner-attestation`, so the row stays `BLOCKED` with that recovery command.
Branches, tags and task evidence remain.

Releasing an unintegrated handoff loses only the checkout directory. Its record keeps the frozen
evidence, and `integration-enqueue` resolves a source without a checkout from its release receipt,
but only while the retained branch still equals the handoff commit; a moved branch fails with
`SOURCE_BRANCH_UNAVAILABLE`. Without the switch such handoffs stay `BLOCKED` with a reason that
names the missing integration step. Release is still not business completion or publication.

Every result carries `reason` and `outcome_counts` built from the actual findings, `RELEASED`
rows carry `receipt_id` / `release_plan_id` of the immutable release evidence, and dirty rows carry
`changes` plus `owner_action` (review, commit through Git Finalizer or discard explicitly, then
`park`/`handoff`). Convergence never commits, stashes, resets or discards uncommitted work.

Clients whose launch directory is canonical but whose writer leases sit in linked checkouts
end with `worktree-session-end --path <repo> --client-id <client> --session-id <session> --holder
ancestor:<program>`: only leases held by exactly that live session are released, and each
allocation then takes the ordinary terminal route (unfinished work is retained). The hook keeps
the selected allocation and record CAS: a new writer lease, resumed record or replacement
allocation blocks terminal parking. Read each allocation's decision; exit status remains advisory
even when parking is refused. Hosted children never end their parent's allocation, and a repeated
notification does not repeat lifecycle mutations.

With `hosted_session_closeout_check_version` 1, hosted children may run the read-only preparation
check with their verified host selector. That check never ends, parks or releases an allocation;
hosted session-ending calls remain refused.

With `session_closeout_report_version` 1, run the same selector with `--check` before normal
exit. It is read-only and returns 2 while files, artifact classification, a Task checkpoint or a
handoff still need owner work. Include `--run-id` when the lease records one; omitting it never
inherits a replacement run's lease. `--require-complete` makes the ending call return 2 for an
incomplete selection or any unsettled allocation. Default hook exit remains 0 for compatibility:
neither that exit nor `SESSION_ENDED` proves success. Consume `allocations[].terminal_complete`
and `summary.terminal_complete`. `previous_allocations` re-observes the original selection after
its lease was released, so retrying an incomplete closeout cannot turn it into an empty success.
Those persisted observations grant no mutation authority. A retained dirty checkout is preserved,
not closed.

Status, convergence and session results expose `checkout-owner-action/v1` requests carrying the
allocation, original owner/task/authority, HEAD, expected record CAS and a stable action ID. The
existing Task consumer matches its binding and re-observes these facts before restoring its owner;
the request never grants a writer, commit, discard or cleanup. The human `owner_action` convergence
explanation stays available to older clients.

For dependency waiting, persist the checkpoint in the existing Task first. Once its writer and
bindings end normally, `park --checkpoint-id <content-addressed-artifact> --checkpoint-head <HEAD>
--waiting-on <dependency>` pins the owner's attestation without claiming content verification or
completion. A clean retained checkout may immediately use the reviewed `archive-retained` plan
and ordinary guarded `release`; no idle timeout is required for this owner's explicit closeout.
Registered resources still use normal teardown; unknown files still block release. The Task keeps
the dependency, branch and receipt, and obtains a controlled checkout from the exact retained HEAD
when it resumes. No branch or tag is deleted by waiting or release.

Triggers: `acquire` starts a detached pass in its own session (reported as
`retention_convergence: CONVERGENCE_SCHEDULED`), so it neither slows admission nor can be
interrupted by a caller's timeout; applying passes are serialized per repository by a
non-blocking host lock (`CONVERGENCE_BUSY`). Each applying pass writes `RUNNING`, then
`COMPLETED` (code, reason, counts, released receipt ids) or `FAILED` (error code) beside that
lock; a dry run returns it as `last_applied_pass`, and a `RUNNING` report whose lock is free reads
as `INTERRUPTED`. The report is an observation for detached callers, never release evidence.
The background timer runs `reconcile retention --trigger
background --apply` beside `reconcile-writers`, which first moves provably dead writers from
`ACTIVE` to `RETAINED`.

## Commands

- `status [--json] [--namespace-pid PID]`: join live Git inventory with controller metadata
  and report advisory lifecycle and environment observations when the read succeeds. If it
  fails, the CLI retains the classified error code; use `doctor` for mount evidence.
- `plan`: make a read-only reuse/budget/authority decision.
- `write-guard`: bind the selected allocation to the declared task and operation before writes;
  integration allocations reject ordinary feature implementation.
- `adopt`: register metadata for an existing real worktree without changing Git.
- `doctor [--namespace-pid PID]`: distinguish Controller authority or lock failures,
  filesystem permission failures, mount namespace mismatches, and `UNKNOWN`.
  It probes repo, `.git`, Git common dir, lock path and parent, and namespace identity; unavailable
  evidence remains `UNKNOWN`.
- `gc-plan [--namespace-pid PID] [--path PATH --owner-release OWNER | --intent-id ID]`:
  classify every Git registration with lifecycle, owner, session, lease, filesystem and
  namespace observations; a selected target with release authority also runs the existing
  read-only release preflight.
- `gc-apply --path PATH --plan-id ID [--owner-release OWNER | --intent-id ID]`: revalidate one
  reviewed plan and execute it through the existing release path and receipt.
- `acquire`: create one explicitly specified policy-approved linked worktree.
- `canonical-transition`: move a clean frozen canonical handoff to one exact published commit on
  `main` or `master` selected by repository policy, without releasing the canonical checkout.
  It requires the old record CAS, owner, branch,
  HEAD, local publication branch and remote HEAD, plus explicit new control-plane identity. The
  old handoff branch remains at its commit; the publication branch only fast-forwards. A retry
  accepts the exact partial
  state after the ref or checkout moves, and an invalid target policy restores the old checkout.
  With `canonical_main_refresh_version` 1, a handoff already frozen on the policy publication
  branch uses the same entry with `--expected-branch main` (or `master`), the original frozen
  `--expected-head` and the observed `--expected-main-head`. It validates the immutable published
  policy before advancing the checked-out branch and records `CANONICAL_MAIN_REFRESHED`, preserving
  the old handoff history without an extra baseline ref. Interrupted progress keeps main advanced
  and metadata frozen: retry with exact observed main and remote OIDs after proving their ancestry;
  never rewind main or fabricate a release receipt. Unpublished or divergent main stays blocked.
- `integration-enqueue` / `integration-queue`: persist and inspect ordered `IntegrationIntentV2`
  records without occupying a checkout; source branch, handoff, Finalizer, Snapshot, and scope
  identities are frozen once. An explicit schema v1 policy can use this manual flow with its
  default `origin/main` publication target; no automatic continuation is enabled.
- `integration-claim`: claim the oldest selected intent when a candidate slot is available and build
  its exact revision from frozen handoffs plus live main without touching canonical checkout state.
- `integration-refresh`: rebuild a `NEEDS_REPREPARE` candidate on live main and clear old prepared
  eligibility, or move only that candidate to `ATTENTION_REQUIRED` on a real conflict.
- `integration-validate`: persist an immutable prepared-candidate receipt binding tests, Snapshot,
  base-main, tree, scope, attempt, and candidate identities.
- `integration-reopen`: invalidate prepared evidence before allowing more integration writes.
- `lifecycle-events` / `lifecycle-event-ack`: consume and idempotently acknowledge the durable
  integration lifecycle outbox.
- `integration-recovery-matrix`: expose evidence, repeat, resume, and fail-closed rules by phase.
- `integration-equivalent`: bind reviewed equivalent-replay evidence and advance the candidate
  identity before validation.
- `review`: record owner review or explicit owner transfer under record-version CAS. A clean
  `ACTIVE` source task can repair omitted committed files with `--add-scope <exact-file>`
  (repeatable), `--expected-head <full-oid>` and `--scope-reason <reviewed-reason>`.
  Scope review only adds actual out-of-scope files from the original base-to-HEAD diff and records
  the owner, reason, HEAD, base, CAS and before/after scopes in history. Frozen handoffs require
  `reopen` first; integration scopes and sources bound by active intents cannot be amended.
  The ordinary handoff and publication gates still apply after review.
- `close-preserved`: close verified archived source without declaring task completion or requiring a fabricated Finalizer record.
- `close-delivered`: explicitly close a retained or legacy preserved checkout whose HEAD and historical commits
  are already in the live publication branch; retain original task evidence and require
  ordinary guarded release for physical removal.
- `archive-retained`: preserve a clean unfinished retained task on its exact branch, without
  asserting completion or publication; physical removal still uses guarded release.
  Completed preservation cleanup receipts are frozen by actual receipt ID, path and raw SHA.
  Release rechecks these immutable bytes and the exact archive record transition
  (`after CAS = before CAS + 1`); later unrelated metadata changes require a new reviewed plan.
  Formal abandoned integration references use their existing validated effective terminal identity;
  historical source handoffs must still be ancestors of the retained branch. Other open integration
  obligations remain blockers in archive, delivered closeout and retention convergence.
- `handoff`: record either `integration-ready` or `preserved` evidence.
- `quarantine`: isolate a semantically drifted integration candidate.
- `recovery-adopt`: explicitly move a compatible existing checkout under recovery capacity.
- `externalize`: bind quarantined lineage to a dedicated recovery allocation and retained branch.
- `resource-register` / `resource-clean`: classify and explicitly clean a task-local Python
  `.venv` using its reproducibility/creator/teardown contract. In addition to existing active,
  retained and receipt-authorized paths, capability `published_handoff_resource_registration_version`
  1 allows a published `HANDOFF_READY` hotfix to classify its existing reproducible on-release
  `.venv` with exact owner/CAS and `--creator handoff-owner-attestation:<owner>`. It verifies frozen
  task/handoff/Finalizer identity, live publication and branch/base ancestry, no open integration
  or transaction obligations, and the existing runtime/environment preflight. It appends proof
  to the new resource event without reopening or changing frozen evidence; physical cleanup and
  release still use their existing guards. Explicit `resource-clean --typed` also supports the
  three nested resources described below.
- Capabilities `retained_typed_cleanup_version` 1 and `preservation_leaf_version` 1 permit explicit
  exact preservation/cleanup on clean `RETAINED` feature/audit allocations and selected ignored
  regular-file or literal symlink leaves. The existing preservation `plan` keeps `--repo`, `--path`,
  `--allocation-id`, `--owner`, `--expected-record-version`, `--plan-file` and exact `--source`.
  Copy and verify use that immutable plan; they do not authorize cleanup. `resource-clean --typed`
  continues to require reviewed plan identity and the existing primary/additional receipt pairs.
  Original ancestor pins and all allocation, runtime, binding, ref and transaction guards remain
  mandatory. A symlink target is not followed or removed; a regular leaf cannot authorize deleting
  its tracked parent. Cleanup, archival and physical release are separate guarded operations.
- `lease-acquire`: refresh configured main, validate candidate lineage, and acquire the single
  publication lease.
- `publication-execution-session`: public, provider-neutral JSON-line verification, authorization
  and result observation for an exact leased candidate; the Controller holds the repository lock
  while the caller performs its Git push. See the protocol below.
- `lease-complete` / `lease-abort`: verify published remote state or release unchanged authority;
  both bind the exact publication run, and completion defaults to receipt-authorized auto-release.
- `release-intent`: bind one short-lived feature/audit release authority to exact HEAD, record CAS,
  handoff/Finalizer identity, integration identity, and trigger.
- `reopen`: invalidate handoff and pending release authority before continuing feature work.
- `binding-add` / `binding-remove`: maintain explicit Agent Workspace/runtime bindings under CAS;
  new bindings require `ACTIVE` lifecycle.
- `reconcile receipts`: plan or record immutable receipt reconciliation without rewriting history.
- `reconcile retention`: report or apply delegated retention convergence (see above).
- `reconcile release-intents`: evaluate only existing intents and, when policy allows, invoke guarded
  release; capacity pressure never discovers arbitrary clean worktrees.
- `reconcile integration-intents`: plan or converge v1 migration, historical terminal drift, scope
  waits, missing outbox events, and optional dispatch of already queued intents.
- `release`: build the same guarded `ReleasePlan` for dry-run and execution, remove only the exact
  evidence-cleared linked checkout, and tear down only explicitly registered `on-release` resources;
  branches and tags remain.
- `acquire-across`: admit one task in every required repository, or in none; `acquire-when-free`:
  acquire, waiting a bounded time for capacity that may free.
- `adopt-delivered`: adopt an allocation whose deliverable is already in the publication branch.
- `integration-abort` / `integration-abandon`: terminate a failed or owner-aborted integration
  preparation, or one whose preparation is already gone.
- `integration-retry`: plan/CAS a retry of the original failed intent after its aborted candidate
  has complete release evidence; optionally bind a verified source-ancestor prerequisite.
- `park`: retain an unfinished checkout and free its writer slot.
- `reconcile-session`: terminal reconciliation when a task session stops; `reconcile-writers`:
  report, and optionally converge, provably inactive writer entitlement.
- `writer-lease-acquire` / `writer-lease-release`: bind write entitlement to a verifiable live
  process, or give it up without asserting the task finished.
- `admission-queue`: inspect or resume durable resource admission requests across repositories.
- `execution-admit` / `execution-cancel`: budget a never-launched managed execution.
- `host-execution`: budget and track a process outside a repository writer lease; `--action
  inspect` is the read-only recovery-intelligence surface, `--action clear --recover-dead-holder`
  is the only authority that closes a provably dead or orphaned historical reservation.
- `branch-retirement-plan`: after release, classify one exact allocation/branch, recheck checkout,
  allocation, writer/runtime, intent/transaction, protection, upstream, OID, and ancestry state, then
  persist an immutable execution plan only for release-backed `ANCESTRY` integration.
  Released `feature` and `audit` allocations remain eligible. A released `integration` candidate
  additionally needs its exact remote-verified publication receipt, linked by the release plan
  or by a unique typed release acknowledgement after owner release. The acknowledgement must match
  the publication, integration intent and released allocation version. Repository, allocation,
  candidate, Finalizer and policy target identities must match. Missing
  or mismatched evidence is `INTEGRATION_PUBLICATION_NOT_VERIFIED`; all occupancy and CAS guards
  still apply. Release retains the branch; retirement remains a separate authorized operation.
- `branch-retirement-status` / `branch-retirement-record`: revalidate the plan and file an
  independent local/remote result. Partial completion is expected and safely resumable. A record is
  accepted only when the Controller observes the ref gone and governance unchanged.
- `branch-retirement-verify`: the published pre-mutation verdict for exactly one operation
  (`EXECUTABLE`, `STALE`, `BLOCKED`, or `COMPLETE`) plus the CAS fields a ref mutation must carry.
- `namespace-migration-plan` / `namespace-migrate`: report and, on the caller's exact plan id,
  perform the one-time move of a repository's legacy `codex-worktree/v1` metadata namespace to
  `worktree-controller/v1` under the destination repository lock. Idempotent, replayable, and
  fail-closed on conflict or untrusted evidence; see the migration section below.
- `namespace-migration-receipts` / `namespace-migration-verify`: list a repository's write-once
  migration receipts, and re-derive a completed migration from that evidence instead of trusting a
  flag.

The tool never pushes, commits, merges, rebases, resets, performs `git clean`, force-removes, deletes
a branch/tag, or runs a daemon. `lease-acquire` and `lease-complete` are the only network operations:
they fetch exactly the configured publication branch into its remote-tracking ref. Actual main
publication remains Git Finalizer's authority. Legacy `resource-clean --resource-path .venv`
requires an owner-registered environment. `resource-clean --typed` requires an exact classified
plan and the separate cleanup contract below.

## Branch retirement

Release deliberately retains refs. A formally released feature/audit allocation can then request an
OID-bound plan without transferring Git mutation authority to Controller.

Select the required operations from the authorized task: local retirement and remote retirement
are separate actions. `branch-retirement-status` counts filed local/remote receipts;
`pending_operations` lists missing receipts for both actions, including a remote action the task
did not select. A candidate branch that was never published can therefore remain
`BRANCH_RETIREMENT_PARTIAL` after its local ref is retired. Preserve that status and its original
receipts. Record an unselected remote action as outside this task's retirement scope, with a
separate current remote observation; never manufacture a deletion receipt or execute an extra
deletion to make the global status complete.

After each selected action, query its existing operation-specific entry:

```bash
worktree-controller branch-retirement-verify \
  --repo /absolute/repository \
  --plan-id <sha256-plan-id> --operation local --json
```

For current closeout, require all three facts together: `RETIREMENT_OPERATION_COMPLETE` with the
matching `receipt`, `minimum_revalidation.ref_state = ABSENT`, and empty `blockers`. Check the exact
plan and operation identities. A completed operation may survive an ordinary integrated-main
fast-forward when its exact receipt matches, its ref stays absent, all other plan identity and
governance facts are unchanged, and the original integrated OID is an ancestor of the current OID.
The original plan and receipt retain their OIDs; `minimum_revalidation` reports the current
observation. `RETIREMENT_OPERATION_COMPLETE` remains a historical marker even when current
revalidation has blockers; it never suffices for closeout alone. Recreated refs, lost ancestry,
mismatched receipts and actual governance drift fail
closed. Pending operations keep exact integrated-OID CAS, including the other half of a partially
completed plan. Repeated read-only queries neither delete refs nor file receipts. Close a task
requiring both actions only after checking both operations independently.

The remote operation's `ref_state` observes the local remote-tracking ref. Use Git Finalizer's
live remote verification or a successful exact `git ls-remote --exit-code <remote> <full-ref>` to
verify the actual remote as well. Accept only an exact returned full ref name: exit 0 with its OID
means present; exit 2 with no match means absent. Unexpected output and transport/authentication
errors are unknown and do not prove absence. A remote ref that remains
present must be reported explicitly, and selected remote retirement stays open. An absent ref
without an existing receipt yields `ABSENT_UNATTESTED`; keep that verdict instead of claiming a
deletion occurred. Release, main publication and task/business completion keep their separate
evidence requirements.

Capability `canonical_transition_retirement_version` 1 also covers the old baseline branch left by
`canonical-transition`. Select that branch explicitly with `--branch <old-baseline-branch>` rather
than `--allocation-id`. Exactly one complete transition event in the current canonical allocation
must prove its old HEAD and the recorded publication ancestry on the policy remote/target. The plan
reports `CANONICAL_TRANSITION_BACKED`, a null release receipt, and `canonical_transition_evidence`
bound into its identity. Canonical stays registered on main; no release receipt is synthesized.
Ordinary unregistered refs remain report-only. Missing, ambiguous or changed history, occupied
branches, leases, runtime bindings, recovery state and unresolved obligations still block retirement.

If an owner released the checkout without naming an existing release intent, its exact completed
release plan and receipt can resolve that intent for retirement, including an intent later marked
`STALE`. Repository, allocation, frozen owner/task/authority, HEAD and record-version identities must
match; the original intent remains unchanged. An unrelated or incomplete receipt still blocks
retirement, and checkout, ancestry, publication, runtime and transaction guards still apply.

For a published integration candidate released through `--owner-release`, run the existing
`reconcile receipts --receipt-id <publication-receipt-id>` after release. Retirement accepts its
unique `RELEASE_STATE_ACKNOWLEDGED` receipt only for the exact publication and released record
version. Original release plans and publication receipts stay unchanged; missing, ambiguous or
stale acknowledgements fail closed. Retirement plans bind acknowledgement bytes and recheck them
before execution.

```bash
worktree-controller branch-retirement-plan \
  --repo /absolute/repository \
  --allocation-id <released-allocation-uuid> \
  --remote origin --integrated-into main --json
```

An eligible response includes a provider-neutral `execution` recipe plus, for compatibility only,
the released `finalizer` argument array. The consumer then asks the Controller for a grant:

```bash
worktree-controller branch-retirement-authorize \
  --repo /absolute/repository \
  --plan-id <sha256-plan-id> --operation local \
  --request-id <uuid> --holder self --json
```

The grant is single-use and durable. It is minted while the Controller holds `repo.lock`
exclusively, and it binds the plan identity, the `controller_state_digest` of that section, the
`minimum_revalidation` eight-tuple (`plan_id`, `operation`, `branch`, `expected_oid`,
`expected_integrated_oid`, `integrated_ref`, `ref_state`, `controller_state_digest`) and an
`observed` vector of the literal Git facts that digest cannot see: watched ref to OID, checked-out
paths, upstream ref and target, classification and blockers. It also returns a 256-bit execution
`token` — the only capability the grant carries. The consumer performs its own expected-OID
`git update-ref -d` / `push --force-with-lease` and settles:

```bash
worktree-controller branch-retirement-complete \
  --repo /absolute/repository \
  --authorization-id <sha256> --token <token> --result-file execution-result.json --json
```

`complete` re-reads the refs, re-derives the digest and re-evaluates governance inside its own
exclusive section. It files `RETIREMENT_EXECUTION_COMPLETE` only for exactly one authorized delta —
the target ref moving from its expected OID to absent — and answers
`RETIREMENT_EXECUTION_REFUSED` with `OBSERVATION_*` blockers, or `EXECUTION_OBSERVATION_MISMATCH`
when the ref is still there. A result document is never a witness; `abandon` closes a grant with
immutable evidence and deliberately never files a receipt, because absence alone does not prove who
caused it. Outstanding grants are visible in `branch-retirement-executions --action list`, in
`branch-retirement-verify` (`RETIREMENT_AUTHORIZATION_ACTIVE`, for the granted operation only) and
in `doctor` (`RETIREMENT_AUTHORIZATION_OUTSTANDING`).

A grant expires on its own clock without releasing anything: expiry means the claim needs
verification, never that the work is done. An expired grant still fences occupancy, still has to be
settled or abandoned with its token, and a settlement that lands late is recorded as
`grant_expired_at_settle` rather than refused, so a slow remote compare-and-delete can still be
attested honestly. `--holder` binds the grant to the caller's own process tree (`self` or
`ancestor:<program>`); a detached pid is refused, because naming somebody else's process proves
nothing.

`branch-retirement-verify` remains the released pre-mutation verdict, and
`branch-retirement-record` remains the released filing path. Both are now Controller-observed rather
than caller-attested: `record` refuses a completion whose ref it can still see, refuses an operation
holding a grant, and refuses with `EXECUTION_GOVERNANCE_MOVED` when authority or occupancy changed
after the plan. Receipts carry `provenance` — `AUTHORIZED_EXECUTION`, `CONTROLLER_OBSERVED`, or
`CONSUMER_CLAIM` for history filed before this contract existed — so an auditable distinction
replaces the assumption that a receipt implies an authorized execution. Consumer-specific argument
and summary vocabulary lives only in `retirement_adapters.py`; governance speaks
`retirement-execution-result/v1` and never names a consumer product.

Contract compatibility is checked against a real consumer in
`tests/test_branch_retirement_git_finalizer_acceptance.py`, which is skipped unless
`WORKTREE_CONTROLLER_GIT_FINALIZER` and `WORKTREE_CONTROLLER_GIT_FINALIZER_COMPANION` point at an
installation; the default suite needs only Git, and drives grant, settle, recovery and the installed
wheel through the mock executor in `tests/test_retirement_standalone.py`.

Only direct ancestry is executable in v1. `CHERRY_EQUIVALENT`, `MERGE_ONLY`, `UNIQUE`, `MIXED`, and
historical unregistered refs are classification/report output only. Protected publication refs and
rescue/archive/recovery lineage are never eligible. Controller does not invoke `git branch`,
`update-ref`, or remote deletion.

When Controller is available for a repository with enabled policy, linked worktree creation and
removal must use `acquire`/`release`. If Controller is unavailable or metadata is corrupt, use
`doctor`/recovery and fail closed instead of raw worktree add/remove. Read-only Git and branch,
merge, and commit operations remain outside Controller mutation authority.

## Public integration publication session

Capability `integration_publication_execution_version` 1 advertises
`integration-publication-session/v1`. Start it with the exact candidate checkout and the identities
returned by `lease-acquire`:

```bash
worktree-controller publication-execution-session --repo /absolute/candidate \
  --candidate-oid <full-oid> --lease-id <uuid> --run-id <run> --json
```

This command exchanges UTF-8 JSON lines rather than the ordinary status envelope. It emits
`verified` with an opaque public `identity`, an `action` and `remote_oid`. Send
`{"schema_version":1,"contract":"integration-publication-session/v1","action":"authorize","identity":<identity>}`.
The producer rechecks authority and returns `authorized`; only `PUSH_CANDIDATE` authorizes the
exact non-force branch push. `RECOVER_ALREADY_PUBLISHED` requires no push. Afterwards send
`complete` with the same identity and `result` containing `record_id`,
`executor: {name, version}`, boolean `push_executed` and the observed `remote_oid`. The Controller
independently reobserves Git before returning `completed` with matching execution evidence.

One Controller-owned exclusive lock spans all three phases. Frames are limited to 64 KiB and
input waits to 300 seconds. Wrong identities, duplicate keys, missing or drifted authority,
disconnects and timeouts fail closed. A disconnected executor may already have pushed: retry the
same lease/run/candidate after observing the remote, and an already-published candidate is
recovered without another mutation even after lease expiry. The local caller and OS are trusted;
this does not protect against arbitrary interference by other processes running as the same user.

A completed session does not change allocation lifecycle or release authority. Pass its consumer
`record_id` to the existing `lease-complete`; that command persists the publication receipt and
owns lease release and guarded cleanup. No daemon, new state directory or external executor is
introduced. `tests/test_publication_execution.py` tests the producer and enables real Finalizer
acceptance when `GIT_FINALIZER_ACCEPTANCE` names the consumer entrypoint.

## Closed lifecycle history

`status` observes currently registered Git worktrees and their live allocation bindings. Releasing a
checkout removes that registration and binding; it retains the allocation record, immutable release
plan/receipt and any integration, preparation and publication evidence. From 1.13.0, the read-only
`closed-lifecycle/v1` query exposes those existing records without creating another state source:

```bash
worktree-controller lifecycle-history --repo /absolute/repository --allocation-id <uuid> --json
worktree-controller lifecycle-history --repo /absolute/repository --authority-key <authority> --json
worktree-controller lifecycle-history --repo /absolute/repository --task-key <task> --json
worktree-controller lifecycle-history --repo /absolute/repository --integration-intent-id <uuid> --json
worktree-controller lifecycle-history --repo /absolute/repository --release-receipt-id <uuid> --json
```

Exactly one selector is required. `decision.live_allocations` is a current observation, while
`decision.closed_releases` is historical evidence keyed by immutable `release_receipt_id`. The latter
contains allocation/authority/task, the owner from the release plan, original branch/path/HEAD,
release status/time, and existing integration candidate, publication time, prepared validation receipt
and Finalizer references. Publisher identity is separate from the source task owner. An authority/task
may have several allocations: never choose a historical owner by recency or reuse the current owner.
Source delivery joins use the immutable publication receipt's `source_results`, not the current
integration intent. All matching immutable preparations are returned; an ambiguous single preparation
is null. Later publication receipts can enrich the query, while existing receipt identities remain
stable. A closed receipt remains queryable if its mutable integration intent is no longer available.
Closed releases sort by receipt ID; paginate with `--limit` (1..1000, default 100) and the returned
`next_after` as `--after`. A missing match returns empty lists, never an inferred completion.

Receipt joins reuse query-local indexes, and only the returned page's evidence is serialized.
Every metadata file and every matching release remains validated, including linked identity
conflicts beyond that page. Repository locking and atomic disk storage live in `store.py`;
versioned format validation and serialization live in `metadata_codec.py`, with the existing
store import interface retained.

Legacy RELEASED records without a formal release receipt appear in `incomplete_history`. Missing
optional publication/validation evidence is null; contradictory linked identities fail closed. The
query neither backfills evidence nor changes retention/release gates. Storage layout and metadata
schema remain unchanged, and no database, daemon or history tracker is added.

Handbook consumers should retain only the stable receipt reference in their narration and fetch this
query during verification. Verify `proof_status=COMPLETE`, exact allocation/authority/task/owner and
the required delivery's existing publication receipt; confirm its candidate exists and is an ancestor
of the Handbook baseline. Resource release alone, including archived unfinished work, does not prove
publication or business completion. A `release:<integration intent>` citation can continue naming the
same intent: use its historical source allocation's release receipt to prove the source owner, rather
than the integration publisher. Keep live writer authorization for narrative writes and do not copy
Controller history into Handbook state. Old missing evidence stays unverified until its owner uses
an existing supported recovery path.

## Legacy namespace migration

`namespace-migration/v1` is the supported per-repository transition in 1.11.0. Plan and verify are
read-only. Apply requires an operator, the exact reviewed plan id, and the destination lock. The
runtime defects from the 1.8.0 audit are closed in this release. Storage layout v4 only documents
the already existing registration-prune receipt directory. This release does not migrate any
repository.

Repositories created before the current layout keep their metadata under
`<git-common-dir>/codex-worktree/v1`. Nothing in the runtime discovers a namespace, so such a
repository reads as unmanaged and its real state stays invisible. `namespace-migrate` is the only
supported way to move it; a caller never copies, edits or deletes namespace JSON:

```bash
worktree-controller namespace-migration-plan --repo /absolute/repository --json
worktree-controller namespace-migrate --repo /absolute/repository \
  --actor <who-is-responsible> --expected-plan-id <plan-id-from-the-previous-command> --json
worktree-controller namespace-migration-receipts --repo /absolute/repository --json
worktree-controller namespace-migration-verify --repo /absolute/repository --json
```

`namespace-migration-plan` is read-only, and its decision is exactly what `namespace-migrate` will
do with it:

| Legacy and current state | Decision | Effect |
| --- | --- | --- |
| content only under `codex-worktree/v1` | `NAMESPACE_MIGRATION_REQUIRED` | documents move and `repo.json` lands last |
| legacy absent and no unsettled prepared receipt | `NAMESPACE_ALREADY_CURRENT` | an exact no-op CAS writes nothing; a settled transition replays its original receipt |
| both namespaces hold equivalent content, or the destination holds a document the runtime cannot parse | `NAMESPACE_CONVERGENCE_REQUIRED` | redundant legacy copies clear and the destination document is rewritten into its authoritative form |
| both namespaces hold the same path with different content | `NAMESPACE_CONFLICT` | refuses; nothing moves and no receipt is written |
| evidence the Controller cannot trust: a non-JSON or unreadable document, a symlink, a missing `repo.lock`, no `repo.json` in either namespace, an unfinished journal | `NAMESPACE_EVIDENCE_INVALID`, `NAMESPACE_LOCK_MISSING`, `NAMESPACE_IDENTITY_MISSING`, `NAMESPACE_UNSAFE` | refuses before any write |

The plan id digests the reviewed merged content and is the compare-and-set the caller must present.
A new transition re-derives it inside the lock. A `PREPARED` receipt stores that original identity,
the per-document digests, lease conversions and policy move. If interrupted, the read-only plan
reports `recovery_required` and the original plan id; apply accepts only that id and checks source
and immutable evidence before finishing. Later Controller updates to mutable destination records
do not create a second migration identity.

The whole transition holds an exclusive `flock` on the **destination** `repo.lock`, creating it
before any content lands. That is the same lock `adopt`, `acquire` and `integration-enqueue` take
when they bootstrap metadata, so a concurrent verb queues behind the migration instead of minting a
second repository identity beside the tree being moved. The legacy lock is taken without waiting: a
caller holding it is an activation in flight, and blocking there would invert that flow's lock
order, so the migration fails closed with `NAMESPACE_BUSY` and is retried. A read-only plan never
queues at all; it defers the unfinished-journal question to the fence and reports
`"preflight": "under_fence"`.

`repo.json` is the only file that makes a namespace authoritative, so it is the last thing to land
and the destination copy is re-digested against the plan before and after the switch. After that the
legacy namespace is removed only when it is demonstrably empty (`NAMESPACE_RESIDUE` otherwise), and
the `metadata-reference-relocation/v1` record is written only once it is gone - the two together are
what lets immutable completion receipts keep naming the old path and still resolve. Converted leases
are published complete, and interrupted JSON scratch files do not change the plan identity. Ten
fault injection points cover the prepared receipt, conversion and rewrite windows, authority switch,
relocation record, policy move and settlement; the exact names are in `tool_cli_contract.json`.

Receipts under `namespace-migrations/` are write-once `0400` documents. The receipts verb reports
them, and `namespace-migration-verify` flags an unsettled `PREPARED` receipt or invalid settlement
evidence. A settled receipt must still describe the moved tree, the relocation record must validate
against its own directory identity, and the legacy namespace must be gone. Migration does not change
retirement semantics: plans, grants and receipts move byte for byte, and a released branch's plan
keeps resolving after the move.

## Unified release plan and disposable resources

From 1.32.11, removed-release replay also admits its exact retained acquire predecessor
only for a feature/audit `RELEASED_RECORDED` release. Original plan/journal identity, acquired
and released history, +1 record CAS, retained HEAD, absence of checkout/binding/writer lease,
and all existing request/resource recovery guards remain mandatory. No journal is classified
terminal or changed by that admission; complete immutable release evidence is still required.
Other transactions and unsupported partial phases remain refused. This changes no command,
option, capability, CLI contract or storage format.

From 1.32.10, typed cleanup serializes on the existing transactions-directory inode while
holding `repo.lock` shared. This excludes normal metadata writers and older exclusive cleanup
implementations for the entire operation, while closed history readers retain their existing
shared repository lock and full evidence checks. Only the existing atomic cleanup journal,
append-only action/completion evidence and exact authorized ignored resources may change.
Allocation, binding, release and publication metadata stay stable. Doctor additionally takes a
shared transactions-directory gate before its repository lock; activation probes the exclusive
cleanup gate. The directory is initialized under the normal exclusive repository lock; it adds
no pathname or storage format. Cleanup gates are process-owned file locks, released by the kernel
on exit; partial operations retain their original recovery evidence and never gain release authority.
Registered-venv cleanup and physical checkout release keep their exclusive repository locks.

Clean `RETAINED` feature/audit checkouts can use `close-delivered` when their committed
content is already published. First run `close-delivered --path PATH
--expected-record-version N --owner-release OWNER --actor ACTOR --dry-run --json`.
Review its exact record, branch and publication evidence, then repeat with
`--plan-id PLAN_ID` in place of `--dry-run`. This writes a closeout receipt and moves
the checkout to `RELEASE_READY`. It does not declare the business task complete or
change HEAD, files, ownership, original task commits or refs. A live publication query
must agree with the local tracking ref; stale tracking is refused rather than fetched.

From 1.13.4, the same reviewed plan also closes a legacy `PRESERVED` feature/audit checkout
whose HEAD, base and task commit are already contained in live publication, but which lacks
frozen handoff/Finalizer evidence. It preserves that original evidence rather than inventing a
handoff or declaring completion. Bindings, leases, runtime uncertainty, ignored content and any
uncleaned resource still block it. Verified source archives keep their existing preservation
release flow; this recovery does not enter automatic convergence or enable preserved auto-release.

Use `gc-plan --path PATH --owner-release OWNER` and the returned plan with `gc-apply`,
or the normal `release --dry-run` / `release`, to remove the checkout. Every release
rechecks the immutable closeout receipt, publication ancestry, live processes, bindings,
resources and ignored content. Dirty, ACTIVE, frozen-handoff, unintegrated or uncertain
targets cannot use this closeout. A retry after interruption uses the same plan and
receipt. `RELEASE_READY` requires `delivered_closeout_version` 1; older runtimes reject
that lifecycle, so rollback must pass the rollout gate with the actual repository state.

For clean `RETAINED` work that is still unpublished, use `archive-retained --path PATH
--expected-record-version N --owner-release OWNER --actor ACTOR --dry-run --json`.
Review the branch and original task record, then repeat with `--plan-id PLAN_ID`.
The immutable receipt moves the allocation to `ARCHIVED` without changing its commits,
owner or task evidence. The exact local branch must contain the recorded task lineage;
unresolved integration, active use, unknown ignored content and unsafe resources block
archiving. `gc-plan` / `gc-apply` then apply the ordinary release gates. An unreleased
archive can return to `RETAINED` through `reopen`. `ARCHIVED` requires
`retained_archive_version` 1, so rollback must pass the actual repository-state gate.

`release --dry-run` is an official read-only preflight for every managed releasable worktree,
including worktrees without `.venv`. Dry-run and execution call the same `build_release_plan()`
guard path. Execution persists the exact plan, rechecks HEAD and record-version CAS plus all runtime,
editable, external, dirty, ignored-content, and retained-ref guards, and writes an immutable release
receipt. An interrupted execution resumes only from that persisted identity; an exact repeated call
returns `ALREADY_RELEASED`.

Governance v1.1 adds no lifecycle state or deletion mechanism. `status` and `gc-plan` observe
recorded owner, lifecycle, writer lease, host session and runtime occupancy. Host session health
is `UNKNOWN` without independent evidence. Lease renewal is reported as its own evidence; it is
not an independent heartbeat. Stale and orphan findings are advisory; neither changes stored
lifecycle state nor grants release eligibility. `gc-plan`
reports allocation, path, branch, lifecycle, owner, dirty state, session, lease, candidate reason,
filesystem and namespace health, and blocking facts for every registered checkout. A global
plan probes each registered checkout's repo path and `.git` entry independently, alongside its
view of the Git common dir and lock path. A path that cannot be inspected has an `UNKNOWN` mount state;
the plan does not infer another candidate's mount state from the requested checkout. The global
report is advisory: a clean worktree alone is never release authority. For one reviewed target,
provide its exact recorded owner via `--owner-release` or an existing `--intent-id`; the resulting
`gc_plan_id` is stable across timestamp-only ReleasePlan changes. `gc-apply` checks that identity
again inside the release lock before any mutation. Drift or a current lease stops the apply;
normal `release` remains the recovery entry for an interrupted release. Release plans, receipts,
and storage layout remain unchanged.

Environment diagnosis distinguishes Controller authority or lock faults from filesystem permission
faults and mount differences seen in a selected process namespace. A repo can be writable while
its `.git` entry is read-only in another namespace. `doctor` retains detailed mount evidence and
reports it before attributing an observed lock failure to Controller authority. A successful
`status` response includes the same environment diagnosis; on failure, its CLI response keeps the
classified error code and `doctor` supplies the detailed evidence. Controller lock attribution
requires the relevant environment probes to confirm writable access. An
unavailable namespace comparison remains `UNKNOWN` for that comparison, while a confirmed
read-only mount still establishes a filesystem fault. A namespace difference alone does not
identify the sandbox provider. No diagnostic changes mounts or permissions.
`mount_diagnostics.environment` and top-level `environment_diagnosis` classify the observed
environment as `HEALTHY`, `MOUNT_NAMESPACE_MISMATCH`, `FILESYSTEM_PERMISSION`, or `UNKNOWN`;
`doctor` also exposes `decision.primary_diagnosis` to distinguish `CONTROLLER_LOCK_AUTHORITY`,
other `CONTROLLER_METADATA` findings, and `NONE` after environment checks.

`status` and `gc-plan` include `governance_capacity` in `observe` mode. `active_limit` and
`retained_limit` use the policy feature/audit slot declarations and count those linked roles in
their respective lifecycle states. `total_limit` uses `hard_limit` and counts all Git registrations.
These observations do not alter resource admission or the historical top-level `retained_limit`
pool statistic.

`resource-clean --typed --dry-run` adds `typed-disposable-resource/v1` for a source allocation in
`HANDOFF_READY`, without reopening it or changing its handoff/publication identity. The initial
paths are deliberately limited to `backend/.venv` (tracked Python project and uv lock, venv
identity and standard structure), `frontend/node_modules` (tracked npm project/lock and matching
installed dependency identities), and `frontend/dist` (the declared default Vite build output).
Root `.venv`, user/provider state, arbitrary caches, other package managers and ambiguous build
configurations are outside this contract. Existing release-cache handling is unchanged and receives
no typed cleanup action; unknown ignored content blocks the entire cleanup.

Every dry-run resource reports `resource_type`, `path`, `classification`, `cleanup_action` and
`blocker`. A disposable identity alone does not authorize deletion: paths must be canonical and
owned, with no tracked or staged content, mounts, special objects, external bindings, writer lease,
or an allocation-scoped managed/observed runtime reference. Runtime scanning pins each proc directory, reads
`stat`/`status`, and reopens the numeric PID to compare directory identity, `start_ticks`, UIDs and
whether it is multi-threaded (a resizing thread pool is the same process; becoming single- or
multi-threaded is not). Identity changes cause at most three immediate attempts, without waiting. A
missing field alone is not process-exit evidence: `ENOENT`/`ESRCH` require a PID identity recheck; a
missing fd requires confirmation that that descriptor closed. A process whose fields vanish, with
or without its fd directory also denying access, is rescanned after a short pause up to the same
bound: a finished exit resolves to terminal or absent, a live process stays uncertain. A stable single-thread `Z`/`X` process has
already released its mm/files/fs in Linux `do_exit`, so inaccessible fields cannot represent active
references. A zombie leader with other threads does not qualify for this exclusion.

Typed cleanup uses `allocation-runtime-authority/v1`:

| Classification | Authority/evidence | Effect |
| --- | --- | --- |
| `MANAGED_REFERENCE` | Allocation external/runtime binding, writer/publication lease, recorded holder, or verified holder descendant | Block |
| `OBSERVED_REFERENCE` | Readable `cwd`/`exe`/`fd`/`maps` points into the target | Block |
| `MANAGED_VISIBILITY_UNCERTAIN` | Recorded holder or positively associated lineage has inaccessible fields or unverifiable identity | `RUNTIME_SCAN_UNCERTAIN`, block |
| `HOST_VISIBILITY_GAP` | `EACCES`/`EPERM` without any formal holder/lineage association to the allocation | Warning/evidence for authenticated typed resources |

AW Run and Native Session identity enters Controller through the existing external bindings or
writer leases; these registrations remain blocking until formally removed. Opaque bindings block
without needing a PID. A PID holder uses the existing boot ID/start ticks/session/run identity.
Descendant proof follows numeric PPIDs, checks parent start ticks, and revalidates every identity
and PPID edge. Reuse or changing lineage cannot become a host permission gap. No names, cgroups,
provider names, process ages, command lines or environment variables grant an exclusion.

`plan.runtime_evidence` records governance identities, observed references, managed uncertainties
and per-PID host gaps with exact field/errno. `plan.warnings` exposes the gap; an otherwise eligible
nonempty plan reports `CLEANUP_READY_WITH_HOST_VISIBILITY_GAP`. Unknown ignored content, invalid
resource identity, malformed proc entries, unresolved races and global scan errors still block.
Cleanup receipts preserve the original plan, every resource's runtime preflight and the post-scan
evidence immutably. Release after typed cleanup rechecks the same managed-runtime boundary.

This boundary does not prove that an unassociated host process has no references. The existing
trusted-user/OS scope observes same-UID processes, plus any positively associated managed process
across UIDs. Foreign UID exclusion alone is not a filesystem-access proof. Unregistered or reparented
processes without retained governance lineage remain outside provable managed authority; readable
positive references still block. This is a bounded observation, not an atomic host-wide snapshot or
process isolation. A compliant launcher must retain its runtime registration until its workload is
quiescent. The existing leases, bindings and repeated cleanup preflights remain necessary.

Internal symlinks may stay within the resource. The only external-link exception is
the standard venv `bin/python`, `bin/python3`, and matching `bin/python3.x` interpreter aliases:
their shape, common executable target, version/home declaration and executable identity are checked.
Plans bind the worktree, parent and resource directory device/inode/type identities. Cleanup opens
each directory component with `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`, verifies its identity, and
matches a fresh FD inventory against the planned tree fingerprint. Recursive deletion uses only
relative `unlink`/`rmdir` with verified directory FDs. The directory chain and entry identities are
rechecked around deletion; renamed, missing or substituted entries retain the blocking journal.
Symlink aliases are pinned without following them and unlinked without modifying their targets.

Run `resource-clean --typed --expected-plan-id <dry-run-plan-id>` with the same `--repo`, `--path`,
`--owner` and `--expected-record-version`. Execution rechecks the plan, journals before deletion,
rechecks before each resource, rescans, validates release eligibility, and writes a new cleanup
receipt. It never releases the allocation. Then use the ordinary `release --dry-run` and `release`
commands. Release following typed cleanup also requires managed-runtime clearance. Cleanup failure
or interruption retains a blocking journal for explicit recovery; it is not automatically retried
through partial deletion. Historical receipts and handoff identities are never rewritten.

The existing typed runtime binding covers agent, test, preview, background and command
occupancy with allocation/worktree, boot/PID/start ticks and run/session identity.
Execution exit does not require closing a human terminal that provably predates all
declared holders: kernel creation order excludes that process from their descendants.
Unknown or newer observed processes and managed uncertainties still block execution
clearance. Allocation cleanup continues to reject every observed reference, including
that preexisting terminal. No second use-lease schema or supervisor is introduced.

`.venv` is not a prerequisite for a plan. It is one supported disposable resource and must be
registered before handoff with path, type, reproducibility, creator, and `manual` or `on-release`
teardown policy. The controller never guesses that an ignored directory is disposable. Unknown
ignored content stays fail-closed. The legacy `--teardown-reproducible-venv` flag remains an explicit
compatibility assertion; registered `on-release` resources are already represented in the plan.

The preflight requires a clean, non-active, releasable checkout; matching task, handoff, Finalizer,
and HEAD commits; the recorded owner and authority; tracked `uv.lock` and `pyproject.toml`; regular
`pyvenv.cfg` and standard `CACHEDIR.TAG`; an executable Python identity; no external binding; no
other managed environment referring to the checkout; and no same-UID `cwd`, executable, file
descriptor, or memory mapping into the checkout that is visible through Linux procfs. Procfs may
hide non-dumpable same-UID system services; those remain inside the documented trusted-user/OS
boundary and must be represented by Controller external bindings when they own repository runtime.

Receipts under Controller metadata bind the worktree, HEAD, owner/authority, lock and project
checksums, Python/environment identity, pre-removal byte/entry counts, and final result. A crash
before Git worktree removal leaves a fail-closed journal; rerunning the identical command with the
same CAS identity resumes from the receipt. A started removal may resume across partial deletion
only while its recorded root inode and authority still match; path or root-identity drift remains
blocked for review.

If teardown already reached `VENV_REMOVED` or `PREPARED`, the identical `release --dry-run` returns
`RELEASE_RECOVERY_PLAN_READY` only after revalidating the persisted plan, journal, receipt, exact
HEAD and record version, retained branch, recovery lineage, worktree content, and all runtime,
editable, ignored-content, and external bindings. Execution then continues from checkout removal;
it does not recreate or reclassify the removed `.venv`. Missing or altered transaction evidence and
an unjournaled missing resource remain blocked.

## Legacy preserved state

From 1.18.8 (`no_change_cleanup_closeout_version` 1), a genuine `complete-no-change` also freezes
its original record and the exact successful cleanup receipt set. Its write-once completion
receipt is pinned by raw SHA256 in the normal +1 history transition. Release derives the full
original record before rechecking cleanup at its unchanged exact CAS, including every action,
preserved generation and absent source. Completion does not repeat cleanup or authorize runtime
or resource guard exceptions.

An older no-change completion created after successful typed cleanup may be blocked because the
old completion advanced the record without freezing this relationship. Recover that exact task
through the explicit append-only entrance below, using observed trusted receipt IDs and raw
digests. A newly observed digest is a new baseline, not proof of an earlier observation. The
producer requires one unique legacy completion, the entire successful cleanup set, original
allocation/owner/HEAD/branch and exact +1 transition, clean resources, no leases/bindings/runtime
references or conflicting transaction. Missing, duplicate, changed or ambiguous evidence blocks.

```bash
worktree-controller reconcile no-change-completion --repo /absolute/canonical-repo \
  --path /absolute/source-worktree --owner <recorded-owner> --expected-record-version <version> \
  --completion-receipt-id <uuid> --completion-receipt-sha256 <trusted-raw-sha256> \
  --cleanup-receipt-id <uuid> --cleanup-receipt-sha256 <trusted-raw-sha256> --dry-run --json
```

Repeat the same command without `--dry-run` and with `--expected-plan-id <reviewed-plan-id>`.
Additional cleanup IDs/digests use paired repeatable flags. Formal recovery writes a new receipt
and advances only the normal record/history CAS; the old receipts, refs and resources remain
unchanged. While the checkout remains registered, an identical original request returns the same
pinned receipt without another transition. Physical release remains a separate guarded command;
after release, use its normal replay or exact `lifecycle-history` receipt query.

`python -m worktree_controller.preservation` implements `legacy-preserved-state/v1`.
The directory example below uses the original root `.venv/agent-workspace` legacy state shape.
Capabilities `retained_typed_cleanup_version` 1 and `preservation_leaf_version` 1 also support
clean `RETAINED` feature/audit allocations and an exact `--source` selecting a Git-ignored,
untracked directory, regular file or literal symlink leaf. This is a separate preservation
entry point, not a disposable resource type. Preservation does not mutate allocations, bindings,
handoffs, historical receipts or Controller transactions. Cleanup requires the separate exact
receipt contract below and does not itself release an allocation.

The command binds an eligible clean allocation's repository/owner/authority, record
version, HEAD, worktree/source node identities, and a fresh complete source manifest.
It uses the existing allocation runtime guards: managed and observed references or uncertain
managed visibility block; host permission gaps are recorded without claiming host-wide absence
of references. Inspection FDs are closed before runtime scans; there is no self-PID exclusion.
Per-resource classification blockers are recorded as `cleanup_not_ready` during preservation
of the selected source; allocation, runtime, binding and editable-reference guards still block
unsafe preservation. Copy and verify do not authorize cleanup, including nested typed resources.

Directory ancestors and tree directories are traversed with directory FDs and
`O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC` at every directory component. Regular files are pinned
with `O_PATH | O_NOFOLLOW`, confirmed regular by
`fstat`, then read through the kernel's `/proc/self/fd` descriptor link. This prevents a changed
source pathname from opening a symlink target or a FIFO endpoint. Provider/private files,
including DB/WAL/SHM, and unknown files are copied and hashed only as opaque bytes; their
contents are never parsed or printed. Unreadable files and unstable entry identities block.

Canonical, sorted JSON manifest rows use distinct node types:

| Node | Tree digest input, in addition to relative path/type/mode |
| --- | --- |
| Regular file | Size and SHA-256 of every byte |
| Directory | No content payload; empty directories are included |
| Symlink | Literal link target, including broken or external links; never followed |
| FIFO | No content digest or payload; reconstructed with fd-relative `mkfifo` and exact mode |

UID/GID are recorded for every source node, but never restored with `chown`. Device/inode,
mode/size, mtime/ctime, UID/GID and link count form the separate source race identity. Inodes
and ownership are excluded from source/destination tree equality; directory sizes and timestamps
are not portable tree equality fields. FIFO is never opened for I/O, read, waited on, or converted
to a regular file. Its creation temporarily clears and immediately restores this single-threaded
command's process umask. Sockets, devices and other node types remain blocked.

Generations live under the formal private state root
`~/.local/state/worktree-controller/preservations/v1/<repository-id>/<allocation-id>/<uuid>/`,
outside all repository worktrees. Plans pin existing destination ancestors. Creation is exclusive,
all writes use verified directory FDs, and destination identity races or collisions fail closed.
Failures retain an incomplete generation without a successful receipt; retry requires a new plan
and UUID, never overwriting an earlier generation.

```bash
python -m worktree_controller.preservation plan \
  --repo /absolute/canonical-repo --path /absolute/source-worktree \
  --allocation-id <uuid> --owner <recorded-owner> --expected-record-version <version> \
  --source .venv --plan-file /absolute/task-owned-directory/preservation-plan.json
python -m worktree_controller.preservation copy --plan-file /absolute/task-owned-directory/preservation-plan.json
python -m worktree_controller.preservation verify --plan-file /absolute/generation/plan.json
```

Planning hashes the entire current source and reports counts, bytes, unreadable/special entries,
runtime evidence and the proposed generation. It creates only the explicitly requested, exclusive
plan file, not the generation or a source modification. Copy rechecks the plan and every source
entry, copies the complete tree, verifies destination contents, then rescans source contents and
inode identities. Reverification detects missing/changed destination files, changed FIFO modes,
wrong receipts and any source change since planning.

A successful generation contains `tree` (a directory, regular file or literal symlink matching
the selected source), `plan.json`, `manifest.json` and `receipt.json`.
The receipt binds source/allocation/generation identities, manifest and equal tree hashes, node
counts and bytes, source verification time, runtime warnings and implementation digest. Evidence
files are write-once `0400`; the generation is finalized `0500`. These permissions and hashes
provide the existing trusted-user/OS evidence boundary, not protection from hostile same-UID
modification. Later source or destination drift invalidates current coverage. There is no atomic
snapshot of unregistered host writers and no claim that host permission gaps prove absence.
`LEGACY_PRESERVATION_COMPLETE` means the recorded preservation checks passed.

### Exact receipt cleanup

`legacy-preserved-state-cleanup/v1` requires the exact source already covered by a complete
`legacy-preserved-state/v1` generation; the example below uses the root `.venv`. Supply the
canonical receipt path and previously recorded SHA-256 explicitly; the Controller does not discover receipts by directory name or trust a newly
computed hash as proof of an earlier receipt. The ordinary disposable types stay unchanged.

```bash
worktree-controller resource-clean --typed --dry-run \
  --repo /absolute/canonical-repo --path /absolute/source-worktree \
  --owner <recorded-owner> --expected-record-version <version> \
  --preservation-receipt /absolute/generation/receipt.json \
  --preservation-receipt-sha256 <trusted-receipt-sha256> --json
```

Capability `multiple_preservation_cleanup_version` 1 (CLI contract 40) also accepts paired,
repeatable `--additional-preservation-receipt` and `--additional-preservation-receipt-sha256`
options after the primary pair. Every receipt must cover the same exact allocation identity and
one distinct, non-overlapping classified source path. Missing pairs, duplicate receipts, overlap
or any invalid source blocks the entire cleanup. The normalized proof set is bound to the plan
ID and immutable journal/receipt under `legacy-preserved-state-multiple-cleanup/v1`; single-input
plans retain their existing contract and shape. All resources and global guards remain mandatory.
Every action verifies its own source against the full recorded hardlink topology. Only a source
with a verified `COMPLETED` action may use destination-only verification, and it must remain absent.
Release re-verifies every preserved generation and the complete proof/resource/action relationship.

Capability `multiple_preservation_recovery_version` 1 adds one bounded recovery path: a nonempty
verified `COMPLETED` prefix followed only by never-started `PENDING` actions. Supply the original
transaction ID and its previously recorded journal and failed-receipt SHA-256 values:

```bash
worktree-controller resource-recover --stage prepare-multiple --dry-run \
  --repo /absolute/canonical-repo --path /absolute/source-worktree \
  --owner <recorded-owner> --expected-record-version <version> \
  --transaction-id <original-transaction-id> \
  --transaction-sha256 <trusted-journal-sha256> \
  --cleanup-receipt-sha256 <trusted-failed-receipt-sha256> --json
```

After reviewing the dry-run, repeat it without `--dry-run` and with the exact fresh
`--expected-plan-id`. Formal preparation writes an immutable
`multiple-preservation-cleanup-recovery/v1` record and returns its path and SHA-256. It retains
the full original proof set, verifies every completed generation's official metadata, and freezes
those metadata paths and hashes together with the allocation owner, CAS and history. Resume
uses that exact new reference:

```bash
worktree-controller resource-recover --stage resume --dry-run \
  --repo /absolute/canonical-repo \
  --recovery-receipt /absolute/recovery/record.json \
  --recovery-receipt-sha256 <trusted-recovery-record-sha256> --json
```

Run the same resume command without `--dry-run` only after a passing review. It uses the existing
cleanup executor for the untouched suffix; it never replays the completed prefix or reduces the
proof set. A started or partial recovery cannot replay deletion. If the successor cleanup already
completed, resume may only finish its missing terminal evidence after verifying the unique exact
successful receipt. The original journal, failed receipt and all generations remain unchanged.
Every existing allocation, root, resource, topology, runtime, reference and transaction guard still
applies. Historical terminal verification continues after an exact official release by checking
the frozen authority, complete metadata set and release evidence; checkout absence is not proof.
Unsupported interrupted shapes still block release and rollout. Legacy single-receipt recovery
retains its existing flow, and no interrupted multi plan may be downgraded into it.

Capability `preservation_supersession_version` 1 recognizes an exact official frozen handoff,
reopen and new handoff lineage. The checkout root and source parent/ancestor identities must
remain unchanged. A rebuilt source leaf or directory inode is accepted only when the new exact
verified proof and actual completed action cover that path under the same allocation and owner,
with the recorded CAS/HEAD ancestry. Every older completed generation's official metadata remains
verified and retained; neither a newer copy nor a missing source alone grants cleanup authority.

The plan revalidates receipt bytes, supported contract, allocation/HEAD/record/owner/authority,
exact worktree/source and generation identities, manifest digest, PASS status, every preserved
node and the complete current source tree and inode identities. Missing, changed or mismatched
evidence blocks. Tracked/staged files and unknown ignored resources still block. Managed and
observed references and managed visibility uncertainty still block; host visibility gaps remain
explicit warnings and evidence, without claiming host-wide absence of references.
Editable links inside the same verified legacy tree are preserved and removed with it; links
from other managed environments remain release blockers.

An eligible plan reports `CLEANUP_READY_WITH_PRESERVATION`. Run the same command without
`--dry-run`, adding `--expected-plan-id <fresh-plan-id>`, only after explicit cleanup authorization.
In this root `.venv` example, the three typed resources are classified separately and removed
first; exact-preserved `.venv` is last. Each action repeats the safety checks. Deletion uses the existing FD-bound primitive,
validates directory and entry identities, unlinks symlinks themselves, and handles FIFOs only as
nodes with `O_PATH` metadata pins. No FIFO endpoint or external symlink target is opened for I/O.
The preservation generation is outside the deletion tree and is never deleted by cleanup.

The immutable cleanup receipt records the original plan, per-action progress, runtime warnings,
and post-scan. Any failed/partial action retains its transaction and receipt, blocks allocation
release, and requires review; there is no blind retry. Successful cleanup rescans Git, resources,
runtime authority and release eligibility before clearing its transaction. A separate ordinary
`release` still applies every existing gate and re-verifies the preserved destination from the
cleanup receipt. Historical preservation and handoff receipts are never rewritten.

After cleanup or release, verification can still hash the preserved tree without requiring the
removed source:

```bash
python -m worktree_controller.preservation verify --plan-file /absolute/generation/plan.json \
  --destination-only --receipt-sha256 <trusted-receipt-sha256>
```

## Policy v2, v3, and v4

Policy v2 separates checkout preparation capacity from publication concurrency and reserves
governance recovery independently from ordinary feature work:

```toml
schema_version = 2

[limits]
normal_limit = 6
hard_limit = 8

[slots]
integration_candidate_limit = 2
feature_or_audit = 3
emergency = 1
recovery_reserve = 1

[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"
```

The remaining `[canonical]`, `[review]`, and `[authority]` tables retain their v1 fields. Policy v1
remains readable, but 1.1.0 uses resource admission in place of aggregate quantity limits.

Policy v3 retains every v2 capacity/publication field and adds only the automatic-release switch and
short intent lifetime:

```toml
schema_version = 3

[release]
automatic_feature_audit = false
intent_ttl_seconds = 3600
```

When enabled, automatic release applies only to `feature`/`audit` allocations in `HANDOFF_READY`
with exact clean committed handoff/Finalizer identity, released and remote-verified integration
evidence (ancestry or explicit equivalent replay), no unintegrated result, no runtime/editable/
external binding, classified disposable resources, a retained branch, passing official plan, and
passing execution-time HEAD/record CAS. `PRESERVED`, canonical, emergency, and recovery allocations
are excluded; integration candidates continue to use publication lease completion.

Policy v4 keeps total-capacity accounting unchanged, names preparation capacity explicitly, and
adds the continuation boundary consumed by Agent Workspace:

```toml
schema_version = 4

[slots]
integration_prepare_limit = 3
feature_or_audit = 11
emergency = 1
recovery_reserve = 1

[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"

[release]
automatic_feature_audit = true
automatic_integration_continuation = true
preserved_auto_release = false
intent_ttl_seconds = 3600
```

Count observations no longer require the historical slot sums to equal `normal_limit` or
`hard_limit`; their positive values and `normal_limit < hard_limit` remain validated.
Integration preparation and the single main-publication lease remain enforced independently.
`preserved_auto_release=true` is rejected. Schema v1-v3
remain readable and do not enable automatic integration continuation.

## Integration and recovery gates

Repository-writing agents call `write-guard` at the task/write entry boundary after selecting an
allocation. `feature-write` is never valid for an integration allocation. Same-task
`integration-write`, explicit recovery, and finalization remain available under their lifecycle
rules. A `HANDOFF_READY` integration whose HEAD no longer matches its evidence returns
`BLOCK_RECOVERY_REQUIRED` before additional work. This is a trusted-agent workflow gate, not
same-UID process isolation; `doctor`, handoff, lease, and release continue to revalidate durable
state independently.

Feature handoff evidence binds a base commit, task commit, Finalizer commit, and repository-relative
path scope. An integration allocation names one or more integration-ready sources by allocation ID,
or resolves a retained handoff commit with `acquire --source-handoff <commit>`; its scope is exactly
their union. `handoff`, `doctor`, and `lease-acquire` reject novel non-merge commits,
out-of-scope paths, or task/handoff/Finalizer commits that no longer equal HEAD. These are delivery
gates, not same-UID process isolation: direct Git writes can still occur, but cannot obtain Controller
publication authority after semantic drift.

`HANDOFF_READY` is a logical write fence. `feature-write` and new active bindings are rejected until
`reopen` moves the allocation back to `ACTIVE`; reopen clears old handoff/Finalizer identity and
cancels pending intents. Out-of-band dirty, HEAD, record, or binding drift makes old intents/plans
stale or blocked rather than granting cleanup authority.

Two explicit owner/CAS recovery commands cover published historical drift:

- `recover-handoff --path PATH --owner OWNER --expected-record-version N --expected-head OID`
  accepts an attached frozen source whose old handoff has an exact ancestry publication receipt
  and whose advanced HEAD is already on the configured publication ref. It preserves the entire
  old record and any proven dead writer lease in an immutable recovery preparation receipt,
  leaves modified/untracked files and the index untouched, and reopens at the observed base.
  The owner must then test, commit and record a new handoff before normal release.
- `recover-published` takes the same identity arguments plus `--branch recovery/NAME`. It requires
  a clean detached `PUBLISHED` integration, an exact remote-verified receipt with its lease released,
  and ancestry on the publication ref. It creates a fresh retained branch at the same HEAD and
  attaches it without checking out files or running checkout hooks. Normal release remains separate.

Both reject active integration obligations, staged state, runtime references, live writers and
uncertain visibility. Neither edits historical publication evidence or grants publication authority.
Interrupted preparation can be retried with the same arguments; an existing branch is accepted
only when the immutable preparation proves this exact creation attempt and its OID still matches.
These receipts use the existing completion-receipts storage, without changing its layout.

An integration intent freezes task/authority, durable queue order, source allocation/branch,
handoff/Finalizer/Snapshot, path-scope digest, base main, candidate attempt/OID, and exact prepared
receipt. Queue entries consume no checkout. Non-overlapping intents may use up to the policy
preparation limit; a later overlapping intent enters `WAITING_SCOPE`. An intent-bound integration
checkout rejects feature writes. `READY_TO_PUBLISH` is write-fenced until explicit reopen;
out-of-band HEAD, content, record, or binding drift makes old evidence unusable.

The bounded `integration-keyed-document-scope/v1` refinement applies only when the entire
overlap is exactly `docs/nexus/business-progress.json` with `merge=qp-nexus-progress`, or
the historical `docs/handbook/business-progress.json` with `merge=qp-handbook-progress`.
Cross-path moves retain the ordinary file fence. Every pair of frozen source commits must have
one non-ancestor merge base and the same committed `.gitattributes` declaring the matching profile. QP schema 2 records are compared by whole `tasks.authority_key`
or `development_lines/modules/decisions.id`, including additions and deletions. All changed key
sets must be disjoint. Metadata, schema, ordering, duplicate keys, missing/oversized/non-regular
blobs, unknown evidence, broad paths and code overlaps keep the ordinary file fence. Explicit
prerequisites and earlier publishing/recovery/attention states still block. The normal claim or
reconciler can clear an existing `WAITING_SCOPE` without changing its frozen source or queue order.
This proof grants preparation only; publishing still requires a fresh main base and normal validation.

At lease acquisition Controller compares live `origin/main` to the immutable prepared base. If main
advanced, the candidate enters `NEEDS_REPREPARE`; `integration-refresh` then rebuilds from frozen
handoffs without rewriting source branches and requires new tests/Snapshot evidence. A true semantic
conflict enters `ATTENTION_REQUIRED`; other preparation slots and the publisher remain independent.

Capability `integration_retry_version` 1 supports an aborted, unpublished intent whose candidate
has formally been released. Run `integration-retry --intent-id ID --owner OWNER --reason TEXT
--dry-run`, then repeat the exact request with `--expected-plan-id PLAN_ID`. The plan pins frozen
source/scope, original queue sequence, failure receipts and queue evidence. Retry increments the
attempt and clears old candidate, preparation, tests, Snapshot and validation eligibility;
`integration-claim`, validation and publication remain separate mandatory steps. Default retry
branches have an attempt suffix, so the original failed branch and receipts remain recoverable.

Capability `integration_conflict_resolution_version` 1 also permits the same retry plan/CAS for an
exact `INTEGRATION_CONFLICT_BLOCKED` failure that never allocated a candidate. It requires no invented
abort or release receipt: the original failure event/base, frozen sources and queue evidence are
retained. Unknown missing-allocation shapes still fail closed.

After retry, `integration-claim --intent-id ID --owner OWNER --resolve-conflicts` may claim an actual
single-source conflict at the exact fetched main base. Normal scope order and resource gates apply;
the decision names `pending_merge_source`. This is an unfinished candidate, never publication proof.
Within that governed checkout, merge that exact source, explicitly resolve only the frozen scope,
and commit through Git Finalizer's resolved-merge mode. Then run `integration-resolve --path PATH
--expected-record-version N --owner OWNER --expected-head FULL_OID --finalizer-record-id RECEIPT`.
It checks the unchanged source identity, clean attached HEAD, exact two parents (pinned main, frozen
source), and normal commit/path scope before binding the new candidate revision. It does not merge,
commit or choose conflict contents. Fresh tests, Snapshot, `integration-validate`, publication lease,
remote verification and guarded release remain required. Default claims still refuse conflicts;
multiple-source conflicts, arbitrary candidate replacements and scope widening are unsupported.

When a necessary dependency is already an ancestor of the frozen source but is outside its scope,
enqueue its own formally evidenced handoff and use the same plan/apply entry with
`--prerequisite-intent CHILD_ID`. The child must be unclaimed, later in the queue, owned by the same
integration owner and contain only strict ancestors of the parent source. One append-only parent
decision gives it the parent's scheduling position before later overlapping waits; earlier
unrelated intents still fence it. Both stored queue sequences, source identities and scopes stay
unchanged. The parent stays failed until the prerequisite is formally published, released and
absorbed into main, then retry it without `--prerequisite-intent`. This entry never broadens scope,
republishes an existing result or resolves a merge conflict by editing another owner's files.

`lease-acquire --run-id ...` binds repository ID, target ref, holder/run, expected main OID, exact
candidate OID, validation evidence, and expiry. Git Finalizer consumes that same lease with
`--publish-integration-candidate`; both it and Controller serialize on the existing repository lock.
An expired or replaced executor cannot enter the mutation gate. On restart, Finalizer reads live
remote first: exact candidate means receipt recovery without another push; unchanged expected main
means re-entering the fenced gate.

`lease-complete --run-id ...` live-verifies the candidate, persists an immutable publication receipt
and lease duration, releases the lease, then by default calls the unified guarded release. Once the
integration intent is both remote verified and `RELEASED`, Controller creates source `ReleaseIntentV1`
records and reconciles them under policy. Repository-control, Finalizer, and Agent Workspace provide
events and bindings; only Controller removes worktrees. Historical receipt or stale-intent drift is
represented by a new immutable reconciliation receipt: safe historical drift is `DEGRADED`, while
identity ambiguity remains a blocking doctor failure.

With capability `source_release_summary_version` 1, `source_release_trigger.outcomes` reports each
source separately while preserving `created_intent_ids`, `skipped`, and `reconciliation`.
An already closed source reports `status: RELEASED`, `result: ALREADY_RELEASED`, and its immutable
`release_receipt_id` only when unique, complete `lifecycle-history` evidence matches the frozen
source branch, handoff, Finalizer identity, and publication association, with no live binding.
Retiring its branch afterward does not invalidate that historical closure. Missing or conflicting
proof remains blocked with a diagnostic and `recovery_commands`; candidate cleanup still pending
reports `WAITING`, and live source reconciliation retains its original manual, retryable, or stale
result. These observations do not create release authority or prove business completion. Older
runtimes require querying `lifecycle-history` separately; path absence alone never proves closure.

Recovery is explicit: `quarantine` records the last legitimate commit and observed polluted HEAD;
an `acquire --role recovery --source-allocation ... --scope ...` checkout starts at that observed
HEAD and explicitly scopes the recovered feature rather than inheriting the integration scope;
`externalize` binds its retained branch as lineage evidence. Only then may the polluted checkout be
released. Both the original branch and recovery branch remain.

## Lifecycle dogfood evidence

A local lifecycle dogfood covers `plan`, `acquire`, owner review, a Finalizer-linked commit,
`integration-ready` handoff, local control-plane integration, explicit owner release, and guarded
checkout release. It must retain the feature branch and finish with a clean canonical checkout and
passing `doctor` result. Local-only evidence does not prove remote publication or justify mandatory
Controller enforcement; those require a separate publication-linked lifecycle run.

A publication-linked lifecycle acceptance additionally requires non-force control-plane
integration, Git Finalizer publication with live remote verification, and release guards evaluated
against the published main lineage. A successful acceptance removes only the linked checkout,
retains its branch as a recoverable ref, and ends with a clean canonical checkout and passing
`doctor` result.

## Development

### Explicit writer session contract (1.0.0)

The single `worktree-session` entry point accepts any executable. Its client identity is
an opaque identifier, with no provider list, private session reader or adapter family:

```bash
worktree-session --path /absolute/worktree --client-id terminal \
  --session-id task-session --run-id task-run -- bash
worktree-controller writer-session acquire my-client /absolute/worktree 12345 \
  --session-id task-session --run-id task-run
worktree-controller writer-session check my-client /absolute/worktree 12345 \
  --session-id task-session --run-id task-run
```

Admission is `GRANTED`, `NOT_APPLICABLE`, or `REFUSED`. Applicability follows the declared
role. A broken policy, corrupt metadata, a conflicting lease or a nested repository cannot
turn a governed path into an ungoverned one. Any acquisition/check failure stops execution.
The holder retains its exact Linux boot ID, PID and start ticks, plus the explicit client,
session and run identities. The resolver also accepts an explicit `ancestor:<program>`;
it never infers task identity from command arguments or an executor's session files.

The launcher acquires and checks once before starting its child. It exports the exact lease
through `CONTROLLER_WRITER_MODE=host-managed-v2` and `CONTROLLER_WRITER_CONTEXT`. A host may
supply the same context directly. The context contains `holder` (PID, boot ID, start ticks),
`client_id`, `session_id`, explicit nullable `run_id`, `repository_path`, `worktree_path`,
`allocation_id`, and `lease_id`. `writer-session host-check <actual-caller-path>` is read-only
and requires the current allocation, lease, holder, lifecycle and ordinary write-guard to
agree. Missing, duplicate, unknown or mismatched fields refuse the operation. Hosted child
commands never acquire, release or reconcile their parent's allocation.

A per-tool host gate passes one `--access` class: `read`, `exec`, or `write` with exactly one
absolute `--target`. While the allocation is `ACTIVE` the answer is the unchanged `GRANTED`.
After this holder's own handoff (`HANDOFF_READY` or `PRESERVED`), the same holder receives
`CONTINUATION_GRANTED` for reads, commands, writes outside governance, and writes into an
`ACTIVE` integration candidate whose `source_allocations` include the frozen allocation and whose
`integration-write` guard allows them. This is what lets a session that handed off its checkout
continue with `integration-enqueue`, `integration-claim`, candidate work, `lease-acquire`,
Finalizer publication, `lease-complete` and session end. Writes into the frozen checkout (symlinks
resolved), other governed checkouts or repositories, and checks without `--access` keep the
original refusal; commands keep their own Controller guards, which still detect drift. A missing
or replaced lease, a foreign holder or runtime binding, HEAD drift, an unknown class or a relative
target refuse every class. The check never mutates leases, bindings or lifecycle.

On ordinary child completion, the launcher preserves the command's exit code, releases only
its exact lease, and invokes the existing guarded terminal reconciliation. A command that
cannot start releases its new lease and leaves the task ACTIVE. Cleanup remains advisory;
it never invents task completion, removes another holder or relaxes release evidence.

Public CLI contract 30, Writer Session 2, writer lease schema 2 and host context version 2
replace the former executable contracts; branch retirement publishes contract
`branch-retirement/v5` with execution authorization `branch-retirement-execution/v1` on plan schema 1. The package exposes `worktree-controller`,
`worktree-session`, and `worktree-session-end` only. Historical releases remain unchanged.

### Governed deployment and rollback

An archived Codex thread can leave a stale agent binding on a shared, still-live
app-server after its writer lease was released. Ordinary `binding-remove` still
requires the holder to exit. The explicit `--recover-archived-codex-session`
option instead verifies the original daemon through its same-user Unix socket,
the exact archived rollout/session identity, native `notLoaded` status and loaded
thread inventory, and absence of checkout references. It refuses active leases,
active/unarchived threads, different peers, identity drift and uncertain scans.
Use `--dry-run` first, then the same owner/binding/record CAS without dry-run.
The immutable `native-session-recoveries` receipt supports interrupted completion
and exact idempotent readback. This action never archives a thread, terminates a
daemon, changes source/lifecycle/owner, or weakens the ordinary live-holder gate.
Archiving the original thread is a separate authorized native operation; other
loaded threads remain running. Native status/transport unavailability fails closed.

`scripts/controller-activate stage --version <commit>` installs a frozen, commit-pinned
runtime beside the active one. Staging leaves live entry points unchanged. Activation and
rollback both verify source bytes and the installation receipt, then run the frozen
`scripts/check-writer-rollout` gate. Declare every external host using repeatable
`--consumer /absolute/capability-command`; every host reports the same
`--writer-session-capabilities` contract. There is no built-in application prerequisite.

For a first pinned installation, initialize the declared repositories with the staged Controller
and deploy `scripts/controller-runtime` as the executable `~/bin/controller-runtime` and the three
`~/.local/bin/{worktree-controller,worktree-session,worktree-session-end}` entry points. Then run
`scripts/controller-activate bootstrap --version <full-commit-oid>` with the same governed repository
and consumer declarations. Bootstrap verifies the staged package and runs the same frozen rollout
gate, then creates the active pointer only if it is still absent under the activation lock. Existing
or dangling pointers require normal activation/recovery. A failed read-only smoke removes only its
own new pointer; concurrent pointer drift blocks restoration. Ordinary upgrades keep the verified
previous-runtime and ancestry gates.

Sites using `tool-skill-sync` declare the reviewed owner catalog with
`--toolchain-compatibility /absolute/canonical-owner/toolchain_compatibility.json`, or
`CONTROLLER_ROLLOUT_TOOLCHAIN_COMPATIBILITY`. Deliver the catalog registration to its owner main
before staging or activating a newly advertised CLI, capability or storage version. The deployment
entry checks catalog schema 1 / toolchain contract 6 and the target's independent version bindings.
The catalog must match its tracked bytes on the owner's canonical `main` or `master`, whose HEAD
must equal live `origin`; dirty catalogs, candidate branches, missing/unreachable sources and
unreviewed versions refuse acceptance before any runtime switch. Other owner files are not written.
The check runs for staged acceptance, before rollout, and again under the switch lock, including
bootstrap, rollback and automatic restoration. A changed catalog is rechecked rather than granting
authority from a previous observation. Logs identify the owner commit and catalog SHA256; failures
are permanent deployment refusals, not temporary rollout retry authority. Sites without a cross-tool
workflow retain the independent deployment path when no catalog is declared. After activation,
run the default installed `tool-skill-sync doctor --repo <actual-checkout> --summary`.

Forward activation requires a commit descended from the active runtime and a newer software
version. The frozen public
contract, installed package and `capabilities --json` must agree on the software, CLI,
capability-set and storage-layout versions independently. Every previously advertised
versioned capability must remain present without regression; rollback keeps its separate
compatibility gate and accepts only an ancestor of the active runtime. The local v1.10.0
tag is not an activation target from 1.9.4 because its contract omits the already active
`archive-retained` capability.

The gate requires the declared repositories' governance state, pinned neutral entry points,
a runnable staged version, compatible session identities, and quiescent publication and
mutation state. Incompatible hosts and live sessions whose transition cannot be proved safe
block activation. The exact re-preservation recovery exception below remains supported.

If the repository mutation lock is busy, the gate defers transaction inspection and reports only
the temporary mutation blocker for that repository. A live command's intermediate journal is not
classified as interrupted recovery evidence. The next complete gate still verifies the lock,
pending transactions and publication lease before granting readiness.

From 1.24.6, completed feature/audit acquisition evidence also survives ordinary commits in an
ACTIVE or RETAINED task. The staged check requires its original acquisition history, unchanged
task/base identity, exact Git registration and allocation binding, an attached branch and base
ancestry. It leaves the journal and unfinished task untouched; handoff, writer, publication and
release continue to require their own authority.

From 1.24.7, the same acquisition proof survives formal archival and a later guarded release.
An ARCHIVED task must match its immutable archive receipt, original acquisition identity and
attached descendant branch. RELEASED progress additionally requires the exact release receipt,
absence of checkout/registration/bindings/lease, and the matching retained ref or official retirement
proof. Archive identity checks read metadata only; preserved payload verification remains in the
normal archive-release guards. Journals and task-completion/publication claims remain unchanged.

When an unfinished transaction blocks activation, the staged read-only check reports the
first refused transaction's ID, command, phase, concrete error and recovery entry under
`decision.transaction_diagnostics`. The frozen gate retains these fields, adds the repository,
and passes them to the activation log. Older staged errors retain their code and message;
failed or malformed checks report an explicit diagnostic and remain blocked.
An interrupted `release`, including `RELEASED_RECORDED` without its formal release receipt,
requires the original guarded `release` request. With capability
`release_recovery_inspection_version=1`, inspect that exact transaction through the producer:

```bash
worktree-controller release-recovery-inspect --repo /absolute/repository \
  --transaction-id <original-uuid> --json
```

The query verifies the original journal and ReleasePlan, their repository/allocation/path/branch/
HEAD/CAS identity and any referenced publication or release intent. It returns the original
owner and `request`, existing evidence paths with raw SHA-256 digests, and `release_argv` as
individual arguments. The original CAS comes from the plan, even when the current `RELEASED`
record has advanced and its owner is `unassigned`. Missing, malformed or inconsistent evidence
is refused. Consumers pass the returned arguments to `worktree-controller` through an argument
array, never shell evaluation. Publication recovery preserves `--publication-receipt-id`;
intent recovery preserves `--intent-id`.
Inspection is read-only and sets `release_authorized=false` and `execution_guards_checked=false`:
the original task must still verify its authority and use normal guarded `release` recovery.
It does not declare that live resource, binding, retained-ref or other execution guards pass,
create a receipt, execute recovery, or clear the rollout gate. On older runtimes, inspect the
persisted journal and release plan and verify the exact original identity before normal replay.
Once the checkout is removed,
`release --dry-run` refuses with `INTERRUPTED_TRANSACTION` and requires formal recovery;
this diagnostic adds no dry-run recovery authority. Only supported legacy cleanup requires a new
preservation baseline; unknown commands and cleanup contracts remain refused. Recovery entries
are hints, require their original identity/evidence arguments, and grant no execution
authority. The check never repairs metadata or relaxes activation and retry guards.

`controller-runtime` supplies the three stable entry points. Each invocation resolves one
concrete active runtime and stays on it through completion. A broken active pointer fails
closed. Switching uses the activation lock and compares the prior pointer with preflight;
a failed post-activation check restores that exact prior runtime only if no later activation
has changed the pointer. Rollback has the same gates and no skip-gate escape hatch.

For temporary concurrent activity, add `--wait-ready-seconds 300` to an ordinary `activate`
or `rollback` command. The optional integer budget is 1..3600 seconds; omitting it preserves
the immediate-refusal behavior. Only `CONTROLLER_MUTATION_IN_FLIGHT:<repository>`,
`CONTROLLER_TRANSACTION_SET_CHANGED:<repository>` and the legacy
`LIVE_CONTROLLER_INVOCATION:<pid>` shape are retryable, and every blocker must be one of
those types. From 1.30.4 the frozen gate never blocks on a process count: live Controller
processes are reported as the `live_controller_invocations` inventory, and
`CONTROLLER_MUTATION_IN_FLIGHT` names an exclusive repository-lock holder, so shared-lock
admission accounting no longer masquerades as a mutation. Compatibility, publication,
unfinished-transaction, unobservable-process, unknown and malformed decisions stop
immediately. Each attempt repeats the complete gate from the same frozen source
with the same repository, consumer and recovery-proof declarations.

From 1.24.0, a host may release a writer lease after its runtime reaped the holder: the exact
recorded identity plus `--lease-id` releases an exited holder's lease (`holder_exited: true`) when
no member of its process group survives and no other process still uses the checkout; anything
else still refuses. An intent that was claimed,
aborted and retried can now resolve a later pre-allocation conflict: retry judges only the current
attempt (`integration_conflict_resolution_version` 2). Renewal of a stale release intent for an
unchanged record and HEAD backs off exponentially from the second repeat (TTL doubled per repeat,
capped at a day; `deferred: RENEWAL_BACKOFF`). From 1.24.1 retention convergence applies the
same backoff before it creates a release intent to classify a frozen handoff's `.venv`
(`RENEWAL_BACKOFF` on the row). `scripts/check-shards [N]` runs the test
suite in isolated parallel shards plus ruff; `.agents/integration.toml` points Agent Workspace's
`just integrate` at it. CLI contract 56, storage layout 4.

From 1.23.0, `canonical-refresh [--dry-run]` fast-forwards an ACTIVE canonical checkout to the
fetched publication branch after integrations publish from candidates. It acts only when the
checkout is clean, on the publication branch, holds no writer lease, runtime binding or unfinished
transaction, is used by no live process other than the caller's own ancestry, and is an ancestor
of published main whose policy remains governable. It reports `CANONICAL_REFRESHED`,
`CANONICAL_CURRENT` or `CANONICAL_REFRESH_READY`, and refuses otherwise (a frozen canonical keeps
`canonical-transition`). Controller metadata is unchanged. Capability `canonical_refresh_version` 1;
CLI contract 55, storage layout 4.

From 1.24.8, live-process `CANONICAL_IN_USE` refusals additionally expose
`decision.process_diagnostics` and the same details in text output. At most five processes show
kernel process names (64 characters), parent PID/name and three distinct matched paths (512
characters each); omission counts and path-truncation flags mark the evidence limits. The process
path scan is reused; identity metadata accepts at most 4096 characters, with one extra character
read to detect oversize. Command arguments and
environments are not read. Exited, unreadable or malformed identity observations become `unknown`;
the original positive occupancy still blocks the attempt. Parent/name details are advisory
observations, not verified task ownership or permission to terminate a process.

Use the owning daemon's native session inventory to identify the exact session and other active
work. End only the completed task-owned session through its normal lifecycle; ending the current
CLI alone may leave shared-daemon tools running. After the references clear, run the returned
`retry_argv` (a guarded `canonical-refresh --dry-run --json`), then repeat without `--dry-run` once
ready. These diagnostics do not archive sessions, stop processes or grant cleanup authority.
The optional diagnostic field is additive; CLI contract 56 and storage layout 4 are unchanged.

From 1.22.5, the gate observes transaction membership before and after the staged check, including
an initially empty set. Readiness requires the same membership at both observations and in the
staged result. Only a successful, well-formed check matching either observed boundary, together
with an actual before/after membership change, produces the retryable set-change blocker.
Recovery errors, contradictory results, unreadable evidence and a reported set matching neither
boundary retain the permanent refusal. These are bounded observations, not an atomic snapshot
of the entire gate; the existing mutation, process and publication checks remain mandatory.

The monotonic budget includes gate execution and the pauses between attempts. Each gate runs in
its own process group; expiration terminates that invocation and its children. The prior active
pointer is checked before and after attempts; the existing 300-second per-gate command limit also
remains fatal. The existing switch CAS remains mandatory.
Timeout or pointer drift does not switch or restore a runtime. The option does not apply to
bootstrap, layout migration/recovery, or the automatic rollback after a failed post-check.

For the one-time namespace/schema transition, `activate` also accepts
`--migrate-namespace-from <previous-common-directory-name>`,
`--migrate-policy-from <previous-repository-relative-policy-path>`, and repeatable
`--previous-entrypoint /absolute/installed-entry=SHA256`. All three are required together.
Supply the exact governed repository set through `CONTROLLER_ROLLOUT_GOVERNED`, the committed
checkout through `CONTROLLER_ROLLOUT_REPO` (defaults to the script's checkout), and the source URL
through `CONTROLLER_ROLLOUT_GIT_SOURCE`. Stage and activation refuse an unspecified source. The
package index defaults to the normal `uv` configuration; `CONTROLLER_ROLLOUT_INDEX` overrides it.
`--temp-helper /absolute/helper` remains available for sites with a shared temporary-directory
helper; otherwise the script creates and removes its own temporary directory.

For a Skill installation that lags the active runtime, use the source-owned maintenance
entry point without activating another version:

```bash
scripts/controller-activate sync-skill --version <full-active-commit-oid> --dry-run
scripts/controller-activate sync-skill --version <same-full-active-commit-oid>
```

Set `CONTROLLER_ROLLOUT_REPO` and `CONTROLLER_ROLLOUT_GIT_SOURCE` to the governed source as
above. The entry point verifies the active runtime against that exact committed source,
then reads its committed `skills/worktree-lifecycle` payload. Only `SKILL.md` may be replaced;
`agents/openai.yaml` must already match, and unknown paths, symlinks, or installed instructions
that match no committed ancestor of the active version refuse synchronization. The active
pin and installation are checked again under the activation lock. Failed replacement or
verification restores the previous Skill bytes; unknown concurrent drift blocks restoration.
Runtime pointers, sessions, lifecycle metadata and the shared Claude Skill link are preserved.
Dry-run creates no synchronization receipt. Apply writes an identity-only receipt under
`~/.local/state/worktree-controller-rollout/`, including when the Skill is already current;
that no-change case does not replace the installed file.

The transition verifies the previous runtime against its own frozen public contract,
holds the activation and repository locks, and temporarily refuses old entry points.
It moves each unchanged policy to `.agents/worktree-policy.toml` and the existing metadata
directory to `worktree-controller/v1`. Only current writer leases change schema:
`holder.provider` becomes `holder.client_id`; all identities, ownership, timestamps and
expiry remain intact. Directory inodes and historical receipts are preserved. Publication
leases, unsupported unfinished transactions, malformed leases, concurrent mutations and
conflicting layouts block the transition.

The layout gate uses the same terminal-evidence classifier as cleanup and doctor. It also
admits an exact `tracked-source-no-action-recovery/v1` read-only proof; this neither reconciles
the attempt nor authorizes a destructive action. The proof is checked again under the
repository lock. Other live completion evidence tied to old paths still blocks migration.
Only this explicit migration read view validates schema 1 leases through the existing
lease converter in memory; normal runtime parsing still requires schema 2. An unrelated
lease survives the transition, while a lease for the cleanup allocation still blocks it.
`metadata-reference-relocation/v1` pins the old/new roots, unchanged directory device/inode
and each immutable cleanup JSON file's relative path and SHA-256. Its exclusive record
travels with the directory rename. Historical references resolve only through this exact
inventory and digest, using no-follow reads; there is no second metadata authority, alias
symlink, rewritten receipt or guessed legacy namespace. Historical attempt-chain identities
remain unchanged. Missing/changed references or an overlapping old namespace fail closed.

The frozen consumer gate and post-activation checks still run. Failure restores the exact
previous policy, state location, entry points and active pointer. The durable layout receipt
also supports `rollback --layout-receipt <absolute-receipt> --layout-receipt-sha256 <digest>`;
lease renewals are preserved during reverse conversion. Rollback removes only its own
additive relocation record after moving original evidence back unchanged; an interrupted
inverse move retains its reference map for retry. New reconciliation,
action/completion evidence, drift or newly unfinished work blocks rollback without discarding
it. After real client acceptance, call `finish-layout`
with that same receipt and its latest digest to retire the fenced old entry points and empty
old directories. Keep the recovery receipt; it records the retired layout as provenance.

### Started resume attempts (0.9.8)

`cleanup-resume-attempt/v1` records `ATTEMPT_STARTED` separately from immutable action
evidence (`ACTION_STARTED`, `ACTION_COMPLETED`). An incomplete call records the underlying
status, blocker/error codes, safe result/exception summaries and attempt identity before
coordinator diagnostics. Cleanup is an in-process Python call: subprocess exit code is
explicitly null, and no stdout/stderr stream is invented. The CLI returns nonzero for
`RECOVERY_ATTEMPT_INCOMPLETE`. Missing completion never becomes a `None.json` lookup.

`resource-recover --stage reconcile-attempt --recovery-receipt <record.json>
--recovery-receipt-sha256 <sha256> --dry-run` evaluates `started-no-action-recovery/v1`.
Execution additionally requires that dry-run's `--expected-plan-id`. It verifies the exact
full source/resource baseline, hardlink topology, both preservation generations, Git/runtime
authority and immutable original evidence; new journals, completion receipts or destructive
action evidence block this narrow recovery. Absence of a completion receipt alone is insufficient.

Successful reconciliation appends `STARTED_NO_ACTION_RECOVERABLE`, with the assertion
"attempt started, no action proven, safe to supersede". It does not reconstruct a missing
historical refusal. A subsequent explicit `--stage resume` creates a new UUID under
`attempts/`, binds its predecessor reconciliation, revalidates the baseline, and processes
only dist, node_modules and preserved legacy state. Legacy `started.json`, receipts and
reconciliation records are retained unchanged. A failed attempt needs a separate new
reconciliation; it cannot be blindly retried. Any possible partial action remains blocked.
`ATTEMPT_FAILED_BEFORE_ACTION` requires fresh no-action proof; `PARTIAL_ACTION` also covers
uncertainty when that proof fails. Host visibility gaps remain warnings, without a claim of
host-wide absence of references. Automatic release and disposable types are unchanged.

### Exact tracked modified source preservation

`tracked-source-preservation/v1` preserves only unstaged ` M` regular files in an
`ACTIVE` source allocation. The index must match the exact requested HEAD. Staged,
untracked, deleted, renamed, symlink/type-changed, sparse or assume-unchanged source
states are refused. Ignored resources remain outside this receipt's coverage.

The manifest binds allocation/owner/record version, worktree identity, base HEAD/tree,
the complete dirty path set, Git status, original permission mode, byte size and SHA-256.
An independently computed Git tree identifies the base tree plus the exact raw file
overlay. This is not a normalized/staged tree: preservation never changes payload bytes,
the index, HEAD or worktree. File and parent directory identities are checked before and
after copying, including inode, ctime and link count. Runtime authority uses the existing
managed/observed/uncertain blockers and records host visibility gaps without a host-wide
absence claim.

The existing no-follow directory/regular-file primitives and immutable generation root
are reused. Each generation contains `plan.json`, `manifest.json`, `receipt.json` and the
exact file overlay under `tree/`. Files are sealed read-only; their original modes remain
in the manifest for reconstruction. An existing generation is never overwritten, a failed
copy has no success receipt, and verification checks both current source and preserved
bytes against the pinned plan and receipt hash. Unmodified files come from the retained
base HEAD. The contract bounds coverage to 256 modified files, 64 MiB per file and
128 MiB total. Git content filters/encoding and CRLF-expanding checkout configurations
are unsupported; `core.autocrlf=input` does not transform preserved raw bytes.

Credential paths are refused before opening payloads. Recognizable private-key/provider
secret markers also fail closed before that file is copied; diagnostics never include
payloads. This source-only check is not a general secret detector or provider-state
preservation backend. Review the exact selected source paths before executing a plan.

The module CLI is `python -m worktree_controller.source_preservation`: `plan` requires
`--repo`, `--path`, `--allocation-id`, `--owner`, `--expected-record-version`, the full
`--expected-head` and an exclusive `--plan-file` outside the checkout. `copy --plan-file`
creates the generation; `verify --plan-file --receipt-sha256` independently verifies it.
The receipt explicitly records `cleanup_authorized=false`: neither preservation nor its
verification changes lifecycle or relaxes the ordinary dirty/release gates.

`tracked-source-cleanup/v1` is a separate, explicit operation. Its module CLI is
`python -m worktree_controller.source_cleanup plan --plan-file <preservation-plan>
--receipt-sha256 <trusted-sha256>`. Execution requires `execute` and the fresh
`--expected-plan-id`. It restores exactly those tracked files to their bound HEAD blob
bytes and executable mode. The index remains byte- and identity-identical; there is no
reset, stash, staging, commit or implicit release. A modified source inode with multiple
hardlinks is refused, so restoration cannot change an out-of-scope linked file.

Restoration pins each regular inode using `O_PATH | O_NOFOLLOW`, verifies the complete
source identity and digest, and reopens only that descriptor for writing. Every write,
truncate and mode restoration operates on the verified FD. Parent/leaf changes fail
closed; external symlink targets are never opened for writing. Interrupted or short writes
may leave an already-preserved source file partial. The Controller journal and independent
immutable before/after/failure evidence then remain, block release and forbid blind replay.
No automatic rollback or partial-source recovery is introduced by this increment.

After a verified source cleanup completes, a later Git index stat-cache rewrite does not
invalidate archival evidence. Controller 1.2.1 rechecks exact HEAD/tree, every staged mode/blob,
index flags, clean source and the preserved/restored bytes at closeout and release. The current
index identity and bytes must stay stable throughout that verification; the historical action
still proves that restoration itself never changed the index. Old receipts are not rewritten.

An interrupted typed cleanup with every action still PENDING and no action evidence
can use `resource-recover --stage reconcile-typed --transaction-id ... --transaction-sha256 ...`
with a fresh dry-run plan ID. It rechecks the complete in-scope inode/member/content baseline,
resource classification, owner, Git and runtime. Only changes to an already external link-count
remainder and its ctime may be observed; their historical cause remains NOT_PROVEN. Started or
partial actions, unknown receipts, content and inode changes remain blocked. The immutable
`typed-cleanup-no-action/v1` record supersedes the original pending attempt without changing its
journal. A subsequent fresh ordinary cleanup gets a new transaction. Existing approved automatic
release intents use the same reconciliation; a clean directory alone grants no such authority.
A missing completion receipt or an exact immutable pre-action refusal receipt is supported.
The latter must match the original journal/plan/actions, contain no action/runtime checkpoint,
and is pinned by SHA-256 in the recovery record. Its historical result remains unchanged.
Cleanup preflight refusals now retain the underlying blocker code for diagnosis.

Every action checks HEAD, dirty path set/digest, mode, index, parent/file identities,
runtime authority and the immutable preservation generation. Successful source cleanup
records separate completion evidence and formally removes its journal. Completion and
release reverify that generation and the restored source. A task with no task-produced
commit may then use the existing `complete-no-change` lifecycle; its current completion
receipt references the archived source cleanup. This records no historical handoff,
successful validation or publication. Typed cleanup also accepts this narrowly evidenced
`NO_CHANGE_COMPLETED` state; its resource identity, unknown ignored content, runtime and
ordinary release gates remain in force. Other no-change allocations gain no new typed
cleanup permission from this contract.

`controller-transaction-conflict/v1` keeps the repository mutation lock while checking
nonterminal conflicts by exact allocation and overlapping resource scope. The
coordinator identifies its repository/allocation/transaction, worktree, operation
and resource contract. Independent nonterminal resource scopes do not conflict;
unknown identity/lifecycle fails closed. Journal counts are never a conflict predicate.
Every journal identity is validated on each check, but terminal evidence is verified only for
journals in the caller's scope (overlapping resource scope, or the retiring allocation), so large
unrelated historical evidence does not slow an unrelated operation.
Only verified immutable terminal evidence permits a retained journal to coexist with a
new operation, including operations in the same allocation. Neither retention nor
absence alone establishes terminal status. Doctor reports historical terminal attempts
separately from unresolved transactions.

An interrupted `acquire` or `integration-refresh` journal closes only on positive
current-state evidence. The completion shapes require the checkout directory, its
administrative worktree registration, the binding derived from that entry, the record's
task commit and the branch ref to agree exactly with the journal; the no-effect shapes
require every one of those facts to be provably absent, including the target itself.
Half-shapes — an orphan worktree, a dangling detached HEAD, a moved branch, a record
that disagrees, or a residual checkout directory — keep the fence until the operator
restores one of the two complete shapes. The classifier never rewrites or deletes a
journal, reads refs only from the files backend (reftable disables inference), and runs
only while the repository flock is held. Branch retirement's `TRANSACTION_ACTIVE` gate
uses this same pending/terminal semantics as `doctor` and mutation admission.
Dot-prefixed `.tmp` siblings inside `transactions/` are interrupted atomic writes that
never became records: conflict checks, doctor, rollout and activation layout scans
ignore them, and doctor reports them as a non-blocking residue count.

`repo.lock` arbitrates every writer. A mutation against a missing lock stops with
`MISSING_REPOSITORY_LOCK`, and a symlinked, non-regular or unopenable lock stops with
`REPOSITORY_LOCK_INVALID` for readers and writers alike; the lock is opened with
`O_NOFOLLOW` and must be a regular readable-writable file, which is also the contract
the public publication session enforces on behalf of its consumer. `adopt`, `acquire` and `enqueue-integration`
recreate a missing lock as their bootstrap step. Doctor turns both states into
blocking findings carrying the exact path, so recovery is `remove or recreate the
reported path`, after which any writer re-establishes the arbiter.

`tracked-source-no-action-recovery/v1` accepts only a recorded transaction-scope refusal
with empty actions/checks, no before/action evidence, and an exact fresh source, parent,
HEAD, index, preservation-generation and runtime verification. Its module CLI is
`python -m worktree_controller.source_recovery plan --repo <repo> --transaction-id <id>
--receipt-sha256 <trusted-failed-receipt-sha256>`; `reconcile` additionally requires the
fresh `--expected-plan-id`. Reconciliation adds an exclusive immutable record beside the
original plan. The old receipt and journal remain byte-identical. The record proves
`STARTED_NO_ACTION_RECOVERABLE` and binds one new, single-use successor attempt identity;
the original attempt is `SOURCE_ATTEMPT_SUPERSEDED`, not a completed source cleanup.
Fresh installed cleanup planning and execution must reverify the full baseline. Any
possible partial action remains blocked. Structured underlying refusal and conflict
identities survive coordinator/evidence-write errors. Rollout may inspect this state
read-only to admit an installed recovery-capable runtime; it never performs reconciliation.


`tracked-source-progress-recovery/v1` (1.13.2) is a separate append-only closeout for
an interrupted attempt whose recorded actions form a proper restore prefix with a nonempty preserved remainder. Use
`python -m worktree_controller.source_recovery plan-progress` with the same repository,
transaction and failed-receipt SHA options; `reconcile-progress` additionally requires
its fresh `--expected-plan-id`. All completed actions require their exact before/after
chain. An optional last STARTED action must now match its bound HEAD bytes, size and
mode on the original single-link inode; it is recorded as a current observation,
never retroactively completed. Remaining dirty files must exactly match preservation.
Both read-only planning and the locked append reverify authority, runtime, HEAD, index,
parents and the immutable generation. Unknown evidence or partial bytes fail closed.

The exclusive `progress-reconciliation.json` retains the old receipt, journal and
before/after records byte-identical. Shared transaction classification reports
`SOURCE_ATTEMPT_PRESERVED` only after validating this frozen proof. This closes the
historical attempt without source writes, cleanup completion, lifecycle transition,
successor grant or release authority. Future legitimate task edits do not invalidate
historical terminal evidence; current dirty-source release gates remain unchanged.
An unresolved partial attempt still blocks activation: stage the exact recovery-capable
commit, execute its supported module, then run the normal rollout/activation gates.
Older runtimes that cannot validate this marker remain blocked by rollback preflight;
never delete the marker or journal to force rollback compatibility.

### Cleanup hardlinks and re-preservation recovery (0.9.7)

`cleanup-hardlink-topology/v1` records every non-directory inode, all cleanup-scope names,
node identities and opaque content digests when the observed name count equals `st_nlink`.
Same-resource and cross-resource links retain explicit membership. `cleanup-induced-hardlink-mutation/v1`
accepts only the exact link-count decrement for the preceding completed action and its ctime
effect; surviving paths, inode, type, mode, size, mtime, ownership, bytes and symlink literals
remain unchanged. Every action saves exclusive before/after records under `cleanup-actions`.
Deletion continues through verified directory FDs and checks each pinned inode's full identity.

Controller 1.2.0 additionally writes `cleanup-hardlink-topology/v2` when an inode has external
links. It records their exact link-count remainder without searching for, opening or modifying
external names. Only already-authorized in-scope names are unlinked. Every unlink pins the inode
with `O_PATH|O_NOFOLLOW`, verifies its bytes (or symlink literal) before and after the action,
and accepts exactly one link-count decrement plus its ctime effect. Regular bytes are read through
that same FD with `O_NOATIME`; FIFO endpoints remain unopened. The final pinned observation is
retained in `cleanup-induced-hardlink-mutation/v2`, including when no in-scope name survives.
Detected content, identity, ownership, mode, size, mtime or unexpected link-count changes block
further actions and retain partial evidence. This proves the observed unlink boundary; it does
not exclude external writers globally or claim host-wide absence of references. Unknown content
and existing runtime/reference checks retain their own gates.

The v1 encoding and historical evidence remain unchanged, and the current reader supports both
versions. `capabilities.cleanup_hardlink_topology_version=2` is independent of software, metadata
and writer-session versions. Activation and rollback use the target installed runtime's existing
read-only recovery probe; a target unable to validate retained transactions/terminal evidence is
refused. A binary downgrade alone is not a recovery operation.

`re-preservation-recovery/v1` supports exactly a failed legacy cleanup with `backend/.venv`
COMPLETED and `frontend/dist`, `frontend/node_modules`, `.venv` PENDING. It makes no historical
causal assertion: `historical_hardlink_cause=NOT_PROVEN`. A new preservation plan binds the
original journal/receipt hashes, the current complete source tree and all remaining hardlinks.
Current contents and stable identities must still match the earlier preserved state; only the
unattributed nlink/ctime differences may exist before this new baseline is frozen. Subsequent
source or topology changes invalidate the plan. FIFO nodes are preserved without opening them;
symlink targets are never followed and provider files are copied/hashed only as opaque bytes.

Use the installed runtime's `python -I -m worktree_controller.preservation plan` with the existing
allocation/source arguments plus `--recovery-transaction-id`, `--transaction-sha256`, and
`--cleanup-receipt-sha256`; then use its `copy --plan-file` command. The new generation never
overwrites the old one. `worktree-controller resource-recover --stage prepare --dry-run` requires the
same original hashes and the new `--preservation-receipt`/`--preservation-receipt-sha256`.
Executing prepare requires its exact `--expected-plan-id` and writes an immutable recovery
record. `--stage resume --recovery-receipt ... --recovery-receipt-sha256 ...` revalidates it and
uses the ordinary cleanup engine for only the three pending resources. A started execution
cannot blindly replay after interruption. Partial cleanup continues to block allocation release.

Successful recovery writes an independent completion linked to every action and both preserved
generations. The historical transaction file remains unchanged; doctor/release recognize its
formal terminal evidence instead of deleting or rewriting its history. Allocation release is a
separate guarded command. Managed references, observed references and managed uncertainty block;
HOST_VISIBILITY_GAP remains warning/evidence and makes no host-wide absence claim. Unknown ignored
resources remain blocked. This adds no ordinary disposable type, use lease or automatic release.
Older runtimes do not interpret the terminal supplement and continue to block on the retained
historical journal; recovery-aware lifecycle operations require Controller 0.9.7 or later.

To activate that first recovery-aware version, publish and stage it normally, then use the
staged installed runtime to create and verify the new preservation generation. Pass its exact
`--recovery-preservation-receipt` and `--recovery-preservation-receipt-sha256` to
`scripts/controller-activate activate`. The frozen rollout gate asks that staged runtime for
`resource-recover --stage rollout-check --dry-run`: it fully validates the current recovery
baseline without writing recovery metadata. Only this exact recoverable transaction, or a
verified append-only terminal recovery, can remain during activation. All other unfinished
transactions, live mutations, publication leases and incompatible callers still block. Formal
prepare/resume happens through the active installed runtime after activation.

### Checks

Linux, Python 3.12 and an unprivileged user are the supported acceptance environment. Local checks and
`.gitea/workflows/checks.yml` use the same entry point:

```bash
uv run --locked --python 3.12 --no-env-file python scripts/check-project \
  --work-dir /absolute/path/to/a/new/check-output-directory
```

The output directory must not exist. The command retains wheel/sdist artifacts,
isolated installation environments, test fixtures and `pytest.xml` for inspection;
choose a disposable parent and clean it through your environment's normal cleanup
workflow after reviewing the result. Add `--offline` once the locked dependencies,
Python interpreter and build backend are cached.

For an isolated PID namespace, resolve Python 3.12 first and run the check entry point
directly as namespace PID 1:

```bash
wc_checks_python="$(uv python find 3.12)"
unshare --user --map-current-user --pid --fork --mount-proc --kill-child=KILL \
  "$wc_checks_python" scripts/check-project \
  --work-dir /absolute/path/to/a/new/check-output-directory --offline
```

This optional mode requires unprivileged Linux namespaces. As PID 1, the entry point
runs the checks in a child and continuously reaps adopted orphan fixtures. It forwards
SIGINT and SIGTERM to that worker's process group and returns its exit status, using
128 plus the signal number for a signal exit. Command timeouts remain failures; when
namespace init exits, Linux terminates the remaining namespace processes. The interpreter
flags and check arguments are preserved. Outside PID 1 the ordinary checks run directly.

If your local environment provides the optional `tool-temp-dir` companion, create
one owned acceptance root with a separate `tests` or `fixture` token in its prefix:

```bash
set -euo pipefail
wc_checks_root="$(tool-temp-dir create worktree-controller-tests | \
  python3 -c 'import json, sys; print(json.load(sys.stdin)["path"])')"
uv run --locked --python 3.12 --no-env-file python scripts/check-project \
  --work-dir "$wc_checks_root/project"
```

Use a new child such as `"$wc_checks_root/cross-tool"` for optional cross-tool output.
After all checks have exited, review the retained results and preserve any evidence
you need before cleaning the root:

```bash
tool-temp-dir validate-cleanup "$wc_checks_root" --allow-owned-fixture-fifo
tool-temp-dir cleanup "$wc_checks_root" --allow-owned-fixture-fifo
```

The fixture prefix and explicit FIFO option allow native cleanup of the suite's
owned FIFO fixtures under the unchanged nested output layout. Do not move fixture
directories to satisfy cleanup or delete special files by hand. This companion is
optional; ordinary acceptance and CI retain the generic output-directory workflow.

Checks cover Ruff, matching source/package CLI contracts and versions, wheel/sdist
builds, rebuilding a wheel from the sdist, installed console commands, distribution
README/license metadata and the complete self-contained pytest suite. Package smoke
checks run outside the source checkout with isolated Python imports. Tests use fixture
state and do not inherit caller credentials, proxy settings or host integration opt-ins.
Mandatory version, contract, package metadata and tool entry-point stability checks remain
active with Python `-O` or `PYTHONOPTIMIZE=1`.
Git Finalizer consumer acceptance tests remain separately opt-in; their skips are shown
in pytest results. CI runs on the existing `ubuntu-latest` runner with pinned actions
and uv, without publishing packages. The job pins its Ubuntu image, enables Docker's
init process to reap orphan fixtures, and installs GNU Time in that disposable container
when `/usr/bin/time` is missing. No test or recovery guard is skipped. Manual workflow
dispatch can select an existing `source_ref` (commit or tag); the workflow revision and
checked-out source identity remain separate. When the disposable CI container starts as root,
the workflow gives its checkout and job-local Python/cache/output directories to
`nobody:nogroup` and runs acceptance as that user so filesystem permission tests remain effective.

### Optional cross-tool acceptance

After the self-contained checks, run the forty-five acceptance cases against an explicitly
selected installed Controller/Finalizer pair. Both tools and the Finalizer retirement companion
are optional external prerequisites; the ordinary checks and CI do not require them.

Alongside retirement and publication recovery, the installed Controller cases cover direct and
hosted handoff continuation, writes into other checkouts leased by the same process (including
another repository), and refusal of writes into the frozen source or its symlink aliases. They
check CLI exit status and unchanged fixture metadata, using subprocesses whose imports cannot
fall back to this checkout. Unclassified requests, foreign sessions and unleased targets remain
refused. The gate checks `host_handoff_continuation_version` independently of CLI and storage.

Eight installed-runtime cases also exercise real SIGINT while reporting a wait or re-polling
admission, and preserve an already acquired and bound provider for ordinary exit reconciliation.
They observe real procfs thread-count changes during five pool expansions and five contractions,
retaining live file references. An exiting leader with a live thread produces actual vanished
cwd/exe and denied fd observations: completion clears them on a bounded rescan, a persistent live
thread stays uncertain, and permission denial alone stays uncertain without a rescan. Only
scheduling is instrumented; process identities, errors and admission decisions come from the
installed runtime and kernel. The load fixtures require Linux x86_64 or aarch64 and a commit-pinned
Controller with the 1.24.2 behavior. Each worker uses the installed interpreter with isolated imports.

Five installed-CLI cases verify lease release after the host has reaped the holder. The exact
recorded holder and lease ID release only writer entitlement, leaving the allocation and work
unchanged. A missing or wrong lease ID, any changed holder field, a surviving member of the
holder's process group, or another process still using the checkout must refuse without changing
the recorded lease or allocation. Real fixture processes exercise these boundaries and close
through ordinary no-change completion and guarded release once occupancy clears. The gate checks
`exited_holder_release_version` independently of CLI and storage contracts.

Eight installed-tool cases exercise conflict recovery on the original integration intent. A
prepared candidate is formally aborted and released, an intervening fixture integration changes
the same file, and the original intent retries, observes a real merge conflict, retries again and
publishes an exact two-parent resolution through the installed Git Finalizer. Queue position,
frozen source and scope stay unchanged; old validation is cleared and explicit fixture content
and Git review is bound again before publication. Public CLI refusals cover a candidate that was
never aborted and an aborted candidate that was never released, without editing authority files.
Resolution additionally refuses a wrong expected HEAD, an uncommitted candidate edit, a merge
outside frozen scope and a commit with the wrong parent topology. These negative fixtures compare
the complete public intent, candidate and source observations, publication lease and live remote
main before and after refusal. The scope and parent cases deliberately create malformed Git input
inside disposable fixtures; that input is not a Finalizer delivery. A resolved candidate also
refuses a publication lease until fresh content/Git review is prepared, then publishes normally.
All fixture checkouts close through guarded verbs. The gate independently checks
`integration_conflict_resolution_version` and requires version 2 from the matching contract.

Three installed-CLI cases cover release-intent renewal with a real ten-second fixture TTL.
The first expiry renews immediately without changing the frozen allocation. Repeated retention
passes refuse an unrecognizable fixture environment, then report `RENEWAL_BACKOFF` after a second
expiry without creating another active intent, changing the handoff or deleting its payload.
Removing an explicit fixture workspace binding changes the record CAS and renews immediately;
the registered environment and checkout release only through the existing verified publication
and guarded cleanup flow. The fixtures wait for the normal one-minute retention grace and use
public CLI responses throughout, without editing private metadata or patching installed code.
The gate also checks `retention_convergence_version` independently of CLI and storage contracts.

Three installed-tool cases cover main advancing after two independent candidates are prepared.
One publishes through the normal Controller lease and installed Finalizer; the other's lease
request enters `NEEDS_REPREPARE`, clears its old validation and prepared receipt, and leaves its
original intent, queue position, source and scope fixed without granting publication authority.
An `integration-refresh` request with the old record CAS refuses without changing the public
intent, candidate, source, lease or remote main. A current refresh rebuilds that same candidate
on the published main; publication still refuses until fresh fixture content/Git review is bound.
The new prepared receipt then permits normal Finalizer publication, preserving both changes and
closing all published sources and candidates with complete formal release proof. No authority
files are edited. The gate independently checks `integration_publication_execution_version`.

Three installed-tool cases cover the repository's single main publication lease and its real TTL.
Two independent prepared candidates compete: an occupied lease refuses the second candidate
without changing the public allocations, intents, lease or live main. After a two-second lease
expires, its Finalizer request refuses before push; a replacement lease can publish while the
old identity remains fenced without disturbing it. A separate fifteen-second lease uses the
installed Finalizer's existing crash-after-push hook. Once that lease really expires, an already
published candidate refuses replacement and recovers twice with the exact original lease/run/
candidate identity, verified remote main and no repeated push. Normal completion closes the
published source and candidate with complete formal proof. Fixture cleanup settles only its own
remaining lease before guarded abort/release; no clock or private authority files are patched.

Three more installed-tool cases refuse a wrong run on the current publication lease, a dirty
candidate after leasing, and separate fetch/push endpoints on two disposable local bare remotes.
The real Finalizer returns `LEASE_MISMATCH`, `PUBLICATION_EVIDENCE_MISMATCH` and `INVALID_REMOTE`
respectively. Refusals preserve the complete public allocations, intents and lease, all local refs
and remote refs; neither endpoint changes. Each fixture restores only its own file or remote
configuration fault before normal completion and cleanup. The original lease, run, candidate and
validation then publish normally, with complete source and candidate release proof.

Two installed-tool cases recheck remote facts after publication leasing. An independent disposable
clone advances main through the installed Finalizer's normal branch publication path; the old
lease refuses `PUBLICATION_REMOTE_DRIFT` without changing public authority or any refs. Public
lease abort, stale-base detection, refresh and fresh validation then publish both changes on the
original intent with a new lease, without rewinding main. Another case temporarily relocates only
its owned local bare remote. Finalizer refuses `PUBLICATION_REMOTE_UNAVAILABLE`; actual refs at
the relocated fixture and public authority remain unchanged. Restoring the endpoint permits normal
publication with the original lease. Both cases verify complete source/candidate release proof;
fixture failure cleanup restores the endpoint and settles only its own exact lease.

Three installed public-session cases refuse completion before authorization and an authorized
consumer's success claim without a remote push, and disconnect before authorization while an
installed status reader waits for the repository lock. The blocked sessions release the lock
without changing public authority or Git refs. Each case then publishes through the real Finalizer
using its original lease, run and candidate, retaining the intent identity and complete release proof.

```bash
wc_controller_version=$(uv run --locked --python 3.12 --no-env-file python -c \
  'import tomllib; from pathlib import Path; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])')

uv run --locked --python 3.12 --no-env-file python scripts/check-cross-tool \
  --work-dir /absolute/path/to/a/new/cross-tool-output-directory \
  --controller /absolute/path/to/versioned/installed/worktree-controller \
  --controller-version "$wc_controller_version" \
  --git-finalizer /absolute/path/to/git-finalize --finalizer-version 1.8.18 \
  --retirement-companion /absolute/path/to/git-finalize-retirement-plan.py
```

Run the example from this checkout's root. It reads the expected Controller version from
`pyproject.toml`; the selected installed Controller must match. The command verifies both installed
versions and the Controller CLI/storage contracts and selected capabilities before running tests.

To add the twenty real-process rollout cases (seven activation, eight rollback and five automatic restoration), supply
`--rollout-previous-controller /absolute/path/to/previous/installed/worktree-controller`.
Both Controller entries must come from original commit-pinned installations; the activation
fixture verifies their package bytes, receipts and release ancestry against frozen Git commits.
The extended selection requires all sixty-five cases to pass without skips and records both pins.

Run either selection in a PID namespace to isolate process inventory from unrelated host
activity. Prefix the command above with
`unshare --user --map-current-user --mount --pid --fork --mount-proc`; the script uses the existing
project reaper as PID 1. The current UID remains unprivileged. Each case has an independent HOME,
temporary directory and active pointer. The frozen gate and installed CLI responses run normally;
process barriers control transaction creation and completion before or after the staged check.
Fixtures generate journals through the installed CLI and finish interrupted release through its
original request. Cases verify a complete repeated gate, consumer rechecks, permanent recovery
refusal, pointer drift, churn timeout and termination of only the attempt's process group.
Rollback cases use the older target's own frozen gate and installed CLI. They cover successful
rollback, a real mutation-lock refusal followed by a complete gate retry, the older gate's permanent
transaction-set refusal, recovery errors, current consumer minimums, ancestor checks, pointer drift
and timeout cleanup. A rollback acceptance pair must include an older gate that reports transaction
set mismatches as `CONTROLLER_TRANSACTION_UNFINISHED`; it preserves that version's refusal contract.
No host active pointer is changed by these fixtures.

Automatic restoration cases hide only the disposable fixture's Git directory during the new
runtime's post-check, observing the installed CLI's real failure before restoring the directory.
They then verify the previous runtime's exact frozen gate, successful restoration and its post-check,
refusal after a new release-recovery error or consumer capability minimum, fresh review of a changed
published compatibility catalog, and refusal to overwrite another activator's pointer. The previous
runtime must lack `canonical_refresh_version` while the new runtime advertises version 1.

The Controller 1.22.4 / Git Finalizer 1.8.18 wheel pair was verified on 2026-10-13: all seven cases
passed with zero skips, using CLI contract 54, storage layout 4 and continuation capability 2.
The Controller 1.22.3 / Git Finalizer 1.8.18 wheel pair was verified on 2026-10-13: all seven cases
passed with zero skips, using CLI contract 54, storage layout 4 and continuation capability 2.
The Controller 1.22.1 / Git Finalizer 1.8.18 baseline was verified on 2026-10-12: all seven cases
passed with zero skips, using CLI contract 54, storage layout 4 and continuation capability 2.
The historical Controller 1.21.0 / Git Finalizer 1.8.18 baseline was verified on 2026-10-12: all three cases
passed with zero skips, using CLI contract 53 and storage layout 4.
The historical Controller 1.20.4 / Git Finalizer 1.8.18 baseline was verified on 2026-10-12: all three
cases passed with zero skips, using CLI contract 52 and storage layout 4. The Controller 1.20.3 /
Git Finalizer 1.8.18 baseline was verified on 2026-10-11 with the same three passes and zero skips.
Use a versioned installed Controller entry point rather than a HOME-dependent
launcher: fixtures have an isolated HOME and do not inherit caller credentials or proxy settings.
Add `--offline` when dependencies are cached. The output directory must be new.

The cases cover publication interrupted after push, recovery and repeated execution, retirement
proof drift rejection, occupied retirement authorization, and real canonical-transition-backed
local deletion with idempotent completion. They use disposable repositories
and local bare remotes, with the selected installed Controller serving the consumer's public RPCs
and this matching-version checkout creating fixtures. Success requires all forty-five cases to pass
with zero skips. `pytest.xml` and `cross-tool.json` retain the selected versions, contracts, tool
entry-point hashes and test result; entry-point drift during the run also fails the gate.

### CLI performance baseline

Run `uv run --locked python scripts/benchmark-cli --work-dir /absolute/new-output
--controller before=/absolute/pinned/worktree-controller
--controller after=/absolute/pinned/worktree-controller --repeats 7` as one command.
The entry points should belong to isolated installed wheels. Resolve a runtime's versioned
entry point before passing it; home-dependent launchers do not work with the fixture HOME.
The output directory must be new. The script creates only synthetic fixture repositories,
uses canonical metadata serializers and release-plan hashing, and rejects diagnostic findings
or different JSON outputs (apart from `tool_version`). It does not contact remotes.

Each invocation is a fresh process. `first_call` is the first measured call after fixture setup
and version inspection; it is not a storage-cold measurement. Repeated calls use warm OS caches
and bytecode caches isolated under the output directory, avoiding differences from precompiled
installed packages. The script records elapsed wall time, GNU Time peak RSS and stdout bytes
in `results.json`, alongside all samples. Setup and output comparison are outside the timing.
It requires Linux, Python 3.12 and GNU `/usr/bin/time`. Fixture generation measures no writer
durability, and no host cache is flushed.

The 2026-10-01 baseline used Python 3.12.13 on Linux x86_64 with an AMD Ryzen 9 9950X,
seven paired repeats and one live checkout. Each synthetic closed release has an allocation
record, publication receipt, prepared receipt, release plan and release receipt. Publication
receipts remain `REMOTE_VERIFIED` after closure, exercising immutable release-proof joins.
History queries select the shared authority with `--limit 10`. Values below are medians in
milliseconds, shown as **1.18.2 / 1.18.3**:

| Closed releases | status | doctor | lifecycle-history |
| ---: | ---: | ---: | ---: |
| 0 | 61.8 / 62.2 | 60.2 / 62.1 | 62.4 / 62.5 |
| 100 | 74.3 / 73.6 | 72.0 / 74.1 | 77.3 / 75.5 |
| 1,000 | 175.9 / 175.6 | 194.4 / 174.6 | 203.0 / 200.1 |
| 5,000 | 681.6 / 658.0 | 952.0 / 658.3 | 698.5 / 670.6 |

At 5,000 releases, doctor improves by about 31%; its median peak RSS changes from 62.5 to
63.2 MiB. Status and history code is unchanged; their small timing differences are run variation.
All three JSON outputs match across versions after excluding `tool_version`. The 10-entry
history page is about 55 KiB at both 1,000 and 5,000 releases. These are local synthetic
measurements, not portable latency guarantees; all commands still load and validate the full
metadata snapshot, so time and memory continue to grow with history.

The 2026-10-06 comparison of installed 1.19.8, 1.19.9 and the 1.19.10 wheel used the same
fixture, Python 3.12.13 and seven paired repeats. Values below are median milliseconds,
shown as **1.19.8 / 1.19.9 / 1.19.10**:

| Closed releases | status | doctor | lifecycle-history |
| ---: | ---: | ---: | ---: |
| 0 | 100.7 / 103.4 / 108.6 | 152.8 / 172.5 / 136.6 | 118.3 / 112.9 / 110.7 |
| 100 | 150.4 / 151.4 / 128.2 | 99.3 / 110.6 / 103.2 | 101.1 / 102.0 / 108.9 |
| 1,000 | 272.0 / 345.6 / 282.3 | 264.0 / 281.0 / 248.0 | 271.5 / 296.0 / 259.4 |
| 5,000 | 1049.6 / 1227.0 / 909.7 | 849.8 / 1083.9 / 908.7 | 958.2 / 1231.9 / 896.3 |

At 5,000 releases, median peak RSS in MiB was:

| Version | status | doctor | lifecycle-history |
| --- | ---: | ---: | ---: |
| 1.19.8 | 64.7 | 64.7 | 65.3 |
| 1.19.9 | 103.0 | 103.8 | 104.6 |
| 1.19.10 | 64.2 | 65.0 | 65.2 |

The normalized JSON outputs match for all sizes, commands and versions. Profiling confirms
that each query loads one repository snapshot. The 1.19.9 command-scoped decode cache retained
25,001 decoded files at 5,000 releases without reusing them; 1.19.10 runs these three queries
on the uncached read path and preserves full validation. Other commands keep identity-aware
decode reuse within their invocation. Compared with 1.19.9, this run reduces large-query
median latency by 16–27% and peak RSS by about 38%. Small timings and the differences from
1.19.8 vary on the shared host; these measurements do not establish portable latency bounds.

Add `--mode execution-admit --sizes 0 100 1000 --repeats 7` to measure execution admission
with the same versioned installed entry points. Each sample uses a new isolated repository,
HOME and resource ledger, containing the requested number of synthetic lost-exit host
reservations and one actual live writer. The three cases cover immediately available admission,
memory waiting that recovers a finished reservation, and an old run's identity recovery.
The memory case includes one extra blocking reservation. Identity recovery deliberately measures
the expected `EXECUTION_AUTHORITY_CHANGED` refusal after retaining the unfinished task; an
untimed formal resume and admission must then succeed. No task completion is inferred.

This mode starts the selected entry point's Python interpreter and installed `CLI.main` in a
fresh process with fixed memory and PSI probes; Git, metadata, locking and durable writes are
real. Timing includes the fixture worker's bootstrap, but excludes fixture setup and acceptance
postchecks. Bytecode caches are shared only within the output directory, and host caches are
not flushed. Fixtures never use the caller's resource ledger, remotes, credentials or proxies.
Bulk synthetic history is setup, not evidence of writer durability. Results retain entry-point
and package hashes, measured CLI responses, wall time, peak RSS and postchecks for live-writer,
scope, publication, request-idempotency and memory-budget invariants. `status=PASS` is written
only after every sample passes and the installed identities are unchanged.

Unlike query mode, this mode does not require identical JSON across versions: 1.19.2 intentionally
keeps unrelated finished reservations on its available path, while 1.19.1 reconciles them first.
Both states are reported and checked; a lower latency does not count as successful recovery.
From 1.19.6, automatic recovery batches proven bounded host exits into one atomic ledger write.
Each row retains the same target, runtime-chain and process-group checks; repository recovery
still runs separately, outside the host ledger lock. Explicit recovery interfaces are unchanged.
From 1.19.7, `admission-queue --all-repositories --apply` also uses that batch while preserving
the ordered per-request outcomes. A failed batch or an unproven row still uses ordinary diagnostics;
repository-selected and dry-run passes leave host reservations unchanged. Normal host clear and
typed dead-holder recovery retain their separate proof requirements.

Unrelated host process churn can cause a real procfs visibility refusal during old-run recovery.
Such a sample stops the run and retains `recovery-refusals.json` and `refusal-inspect.json`; it
must not be counted as a successful timing. Where unprivileged namespaces are supported, prefix
the benchmark command with `unshare --user --map-current-user --pid --fork --mount-proc` to
isolate the fixture processes while retaining real procfs checks. This uses the existing Linux
`unshare` command, changes only that test process's namespaces, and grants no host privileges.
Results record the PID, mount and user namespace identities.
Use `--admission-cases available` (or either recovery case) to bound a focused experiment.
`--timeout` limits each invocation; a timeout stops the run, terminates its process group and leaves
the output at `RUNNING`, never `PASS`. Preserve that censored observation separately from
completed paired samples before choosing a smaller scale or a longer explicit time budget.

The 2026-10-06 isolated PID-namespace baseline used Python 3.12.13 on Linux x86_64 and
the installed 1.19.1 (`827c71a7`) / 1.19.2 (`094891c2`) packages. Seven paired repeats,
with fixture setup and acceptance outside timing, produced these medians in milliseconds:

| Lost-exit history | Available 1.19.1 / 1.19.2 | Memory recovery 1.19.1 / 1.19.2 | Identity recovery 1.19.1 / 1.19.2 |
| ---: | ---: | ---: | ---: |
| 0 | 95.7 / 101.9 | 108.3 / 164.3 | 183.7 / 170.9 |
| 100 | 1026.7 / 121.4 | 939.4 / 1202.4 | 982.3 / 997.8 |

At 100 entries, immediately available admission improves by about 88%. Waiting pays for an
additional admission check; recovery is not generally faster. All completed samples preserve
the live writer, scope, publication state, request identity and exact memory budget.

The 1,000-entry paired experiment stopped at its 60-second limit: one 1.19.1 available call
completed in 56.4 seconds and the next timed out. There is no completed 1.19.1 paired median
at that scale. A separate 1.19.2 run with a 180-second limit, one repeat plus a first call,
passed all three cases: available 166.5 ms, memory recovery 54,922.9 ms, identity recovery
49,620.3 ms (the repeat samples, not seven-repeat medians). Large recovery backlogs therefore
remain expensive. Host-procfs refusals and the timed-out comparison are retained separately
and are excluded from successful timings. These are synthetic local results, not portable
latency guarantees or evidence that real host procfs visibility will always be clear.

The 2026-10-06 isolated PID-namespace run compared installed 1.19.8, 1.19.9 and the 1.19.10
wheel with seven paired repeats and a 30-second per-invocation limit. All three cases completed
at every size. Median milliseconds are shown as **1.19.8 / 1.19.9 / 1.19.10**:

| Lost-exit history | Available | Memory recovery | Identity recovery |
| ---: | ---: | ---: | ---: |
| 0 | 97.7 / 96.1 / 96.3 | 117.0 / 113.6 / 136.9 | 188.6 / 178.2 / 223.2 |
| 100 | 100.6 / 107.5 / 105.0 | 139.1 / 132.6 / 135.8 | 216.7 / 215.7 / 248.1 |
| 1,000 | 146.3 / 147.8 / 146.7 | 472.4 / 345.0 / 378.4 | 359.8 / 400.6 / 392.8 |

Every measured sample passed the live-writer, scope, publication, request-idempotency and exact
memory-budget checks, including guarded resume after identity recovery. The query-cache change
does not change these paths; their timings vary across the shared-host run and show no consistent
latency improvement. These fixtures contain lost-exit host reservations and a small repository;
they do not measure admission against a large repository metadata history.

For either admission mode, add `--repo-sizes 0 1000 5000` to seed closed repository releases
independently of `--sizes 0 1000`, which still controls lost-exit host reservations. The
Cartesian product measures each axis and their interaction. Release history is seeded after
writer setup and before timing, using the same serializers and plan hashing as query mode.
Every sample also verifies that all closed records and publication/prepared/release proofs
remain unchanged.

Use `--mode session-admit` to measure the complete new-session CLI path: canonical plan,
durable checkout acquisition and execution admission. Its default cases are `available` and
`memory-recovery`; checkout planning must succeed before the memory case reaches execution
waiting. Existing allocations and identity recovery use the existing fallback/step-by-step
path, so `identity-recovery` is accepted only in `execution-admit` mode. Session samples verify
that a repeated start returns fallback with the same allocation and leaves the snapshot and
ledger unchanged. Untimed checks then replay execution admission and bind an actual live
fixture process, requiring its exact holder, writer lease, runtime binding and `WRITE_ALLOWED`
guard. The existing scoped writer and exact memory budget remain acceptance gates.

Add `--profile` for one extra fresh-fixture diagnostic call per version/configuration. These
calls use `cProfile` around installed `CLI.main` and record snapshot-load counts, metadata
decoder counts and the largest cumulative costs in `profile_diagnostic`. They are excluded
from normal latency and RSS medians. Profiles cover the CLI main thread; inventory worker
threads and subprocess internals are excluded. All admission runs persist `status=RUNNING` before the
first sample; a timeout or failed invariant never becomes a passing timing, and fixture
holders are stopped on both success and failure. These remain local synthetic measurements,
with fixed memory/PSI probes, warm storage caches and no caller ledger or credentials.
The tracked fixture tree contains only its policy file, so these measurements cover metadata
and host-history growth, not the checkout cost of a large source tree or executor startup.

The 2026-10-07 complete new-session comparison used installed 1.19.8 (`bc619da`) and
1.19.10 (`7da723c`), Python 3.12.13, isolated PID namespaces, alternating version order,
seven paired repeats and a separate first call. All 192 timed calls passed history, live
writer, scope, publication, fallback, holder/lease, request-identity and memory-budget checks.
Median milliseconds are shown as **1.19.8 / 1.19.10**:

| Closed repository releases | Lost-exit host reservations | Session available | Session memory recovery |
| ---: | ---: | ---: | ---: |
| 0 | 0 | 293 / 384 | 427 / 363 |
| 0 | 1,000 | 472 / 466 | 752 / 697 |
| 1,000 | 0 | 1,341 / 718 | 1,506 / 1,081 |
| 1,000 | 1,000 | 1,502 / 928 | 1,953 / 1,508 |
| 5,000 | 0 | 5,070 / 3,051 | 7,652 / 4,837 |
| 5,000 | 1,000 | 6,406 / 4,554 | 7,623 / 5,400 |

At 5,000 releases, session medians improve by 29–40%; with 1,000 host reservations,
peak RSS medians are 135.7 / 120.1 MiB (available) and 135.5 / 122.9 MiB (memory
recovery). A separate one-repeat profiling pilot at 1,000 releases sees five snapshot loads
in either version, but each immutable prepared/publication/release receipt type is decoded
5,000 / 1,000 times.
Its diagnostic timings are excluded from the seven-repeat table. Empty-history results vary
on this shared host and show no consistent speedup.

A separate seven-repeat `execution-admit` comparison at 5,000 releases and 1,000 host
reservations exposes the single-snapshot cost: available admission takes 1,048 / 1,448 ms
and 72.3 / 118.2 MiB. Memory recovery takes 3,104 / 2,978 ms; identity recovery takes
7,809 / 5,358 ms, including its expected refusal and untimed guarded resume. All 48 timed
calls pass their invariants. The initial execution profiling output was overwritten by an
untimed replay; those diagnostics are excluded. The harness now captures the initial profile
and disables profiling for subsequent acceptance calls.

The subsequent paired 1.19.10 / 1.19.11 wheel comparison uses the same 5,000-release,
1,000-reservation configuration, seven repeats and one separate first call per case. All 48
timed calls and six separate profile calls pass their acceptance checks:

| Standalone execution case | Median milliseconds 1.19.10 / 1.19.11 | Median peak RSS MiB 1.19.10 / 1.19.11 |
| --- | ---: | ---: |
| Available | 1,473 / 976 | 118.7 / 71.5 |
| Memory recovery | 3,274 / 2,646 | 123.2 / 121.8 |
| Identity recovery | 6,561 / 6,445 | 124.4 / 124.4 |

Available admission reduces median latency by 34% and RSS by 40% in this run. Recovery RSS
is similar. Recovery samples vary substantially on the shared host; their median reductions
are not evidence of a reliable recovery speedup. Profiling confirms one snapshot for available
admission, three for memory recovery and eight for identity recovery. The recovery candidate
decodes each unchanged receipt type 10,000 times instead of 5,000 because caching starts after
the first attempt. That extra decoding is the cost of avoiding cache retention on the ordinary
path; profiles are diagnostic and their instrumented timings are excluded from these medians.

The 2026-10-07 complete `session-admit` comparison of installed 1.19.13 (`c59e0cd`) and the
1.19.14 wheel uses Python 3.12.13, isolated PID namespaces, alternating version order, seven
paired repeats and a separate first call. All 192 timed calls and 24 separate profile calls
pass history, live-writer, scope, publication, fallback, holder/lease, request-identity and exact
memory-budget checks. Median milliseconds are shown as **1.19.13 / 1.19.14**:

| Closed repository releases | Lost-exit host reservations | Session available | Session memory recovery |
| ---: | ---: | ---: | ---: |
| 0 | 0 | 233 / 254 | 218 / 215 |
| 0 | 1,000 | 434 / 413 | 678 / 601 |
| 1,000 | 0 | 664 / 494 | 1,065 / 685 |
| 1,000 | 1,000 | 875 / 961 | 2,278 / 1,080 |
| 5,000 | 0 | 3,193 / 1,863 | 4,861 / 3,222 |
| 5,000 | 1,000 | 4,734 / 2,188 | 6,460 / 3,262 |

At 5,000 releases, median latency falls by 34–54% in this run. With 1,000 host reservations,
median peak RSS changes from 120.4 / 99.3 MiB (available) and 122.9 / 102.6 MiB (memory recovery).
Profiling confirms five / three snapshot loads on the available path and seven / five on
memory recovery. Planning reuses the session's initial read-only observation; acquisition still
rechecks current metadata and authority under its exclusive lock, then uses that validated
repository identity. Flat historical metadata files retain the same filename order and validation
while sorting by filename instead of comparing complete `Path` objects.
The unchanged receipt types are still decoded once per command. These are warm-cache synthetic
shared-host measurements; empty and some 1,000-release combinations fluctuate and do not show
a consistent speedup. Profile timings are excluded from the medians, and the fixture still does
not measure a large tracked checkout or executor startup.

The subsequent seven-repeat installed 1.19.14 (`cfd8f3a`) / 1.19.15 wheel comparison focuses
on 5,000 closed repository releases, with zero or 1,000 lost-exit host reservations. It uses
the same Python 3.12.13, isolated PID namespaces, alternating version order and separate first
calls. All 64 timed calls and eight separate profile calls pass the existing history,
live-writer, scope, publication, fallback, holder/lease, request-identity and memory-budget
checks. Medians are shown as **1.19.14 / 1.19.15**:

| Lost-exit host reservations | Session case | Median milliseconds | Median peak RSS MiB |
| ---: | --- | ---: | ---: |
| 0 | Available | 1,572 / 1,133 | 93.9 / 82.6 |
| 0 | Memory recovery | 2,012 / 1,293 | 93.6 / 83.5 |
| 1,000 | Available | 1,674 / 1,307 | 99.3 / 87.6 |
| 1,000 | Memory recovery | 2,258 / 1,531 | 102.4 / 90.7 |

Median latency falls by 22–36% and peak RSS by about 11–12% in this run. Snapshot counts stay
at three (available) and five (memory recovery), and unchanged receipt types are still decoded
once per command. Fresh `os.scandir` enumeration of five flat historical metadata directories
avoids repeated `Path` construction and hashing while preserving filename order and all
schema/identity checks. Directory entries never survive a snapshot load; cached decodes retain
their existing file-identity checks and command-local lifetime.
A separate three-repeat query probe makes 48 calls across empty and 5,000-release histories:
`status`, `doctor` and `lifecycle-history` return matching business payloads in both versions;
small timing differences do not establish a query speedup. The one-repeat admission pilot and
profile diagnostics are excluded from the seven-repeat medians. These remain warm-cache,
synthetic shared-host measurements with fixed pressure/memory probes, rather than latency
bounds or measurements of large tracked checkouts or executor startup.

An installed 1.19.15 (`f309ea5`) / 1.19.16 wheel comparison measures the subsequent ASCII hex
validation change on Python 3.12.13. A compiled full-match pattern replaces Python character
loops for OIDs and SHA-256 digests; exact lengths, lowercase acceptance, whitespace normalization,
optional values and errors remain unchanged. Every snapshot still validates all metadata.
The existing benchmark uses alternating version order, separate first calls and isolated
fixtures. There are 64 timed session calls (seven repeats), 16 immediately available standalone
admission calls (three repeats), 96 query calls (seven repeats), and 16 additional session memory
recovery calls (seven repeats) to recheck a possible regression. All admission safety checks pass;
query business payloads match across versions. Medians below are **1.19.15 / 1.19.16** with
5,000 closed repository releases:

| Command / case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB |
| --- | ---: | ---: | ---: |
| Session / available | 0 | 1,071 / 1,043 | 82.8 / 82.4 |
| Session / memory recovery | 0 | 1,313 / 1,324 | 82.9 / 83.1 |
| Session / available | 1,000 | 1,210 / 1,217 | 87.6 / 87.2 |
| Session / memory recovery | 1,000 | 1,608 / 1,704 | 90.1 / 90.6 |
| Standalone admission / available | 0 | 696 / 635 | 62.6 / 61.3 |
| Standalone admission / available | 1,000 | 742 / 643 | 71.1 / 71.2 |
| Status | 0 | 681 / 645 | 59.0 / 59.2 |
| Doctor | 0 | 648 / 627 | 60.0 / 60.4 |
| Lifecycle history | 0 | 704 / 656 | 60.4 / 60.7 |

Standalone admission improves by 9–13% in this run, with the candidate faster in all six paired
repeat samples. The three large-history queries improve by 3–7%, with the candidate faster in
20 of 21 paired samples. Empty-history query changes are under 10 ms and show no consistent gain.
The first session matrix ranges from 3% faster to 6% slower; the separately repeated 1,000-host
memory case gives 1,769 / 1,652 ms and 91.0 / 90.7 MiB. The initial regression does not repeat,
but these results do not establish a general session speedup. RSS is broadly unchanged.
The one-repeat pilot and eight profile diagnostics are excluded from these medians; later pilot
cases overlap regression tests and are not acceptance timings. Their snapshot counts remain
three / five, with unchanged receipt types decoded once per command. These are synthetic,
warm-cache measurements on a shared host, with fixed memory/pressure probes for admission.
They do not establish portable latency bounds or measure large tracked checkouts/executor startup.

The subsequent installed 1.19.16 (`aea5ee9`) / 1.19.17 wheel comparison isolates compact host
ledger serialization. Python 3.12.13 and the unchanged benchmark use isolated PID namespaces,
alternating version order and separate first calls. All 128 timed calls pass the existing
history, live-writer, scope, publication, holder/lease, request-identity and memory-budget checks:
64 session calls (seven paired repeats), 32 standalone admission calls (three paired repeats),
and 32 zero-host session rechecks (seven paired repeats). With 5,000 closed repository releases,
initial medians are shown as **1.19.16 / 1.19.17**:

| Command / case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB |
| --- | ---: | ---: | ---: |
| Session / available | 0 | 1,652 / 1,791 | 84.1 / 83.8 |
| Session / memory recovery | 0 | 1,848 / 1,771 | 83.0 / 83.1 |
| Session / available | 1,000 | 1,167 / 1,100 | 87.5 / 85.2 |
| Session / memory recovery | 1,000 | 1,546 / 1,436 | 90.9 / 87.6 |
| Standalone admission / available | 0 | 639 / 633 | 61.6 / 61.4 |
| Standalone admission / memory recovery | 0 | 1,257 / 1,262 | 80.6 / 82.1 |
| Standalone admission / available | 1,000 | 700 / 631 | 70.6 / 66.1 |
| Standalone admission / memory recovery | 1,000 | 1,503 / 1,526 | 87.6 / 83.7 |

With 1,000 host reservations, session latency falls by 6–7%, with the candidate faster in six
of seven pairs for each case. Immediately available standalone admission improves by 10%,
with the candidate faster in all three pairs. Standalone memory recovery shows no latency gain;
large-host median peak RSS falls by 2–4.5 MiB across these paths.
The first session matrix includes a 13-second zero-host memory sample. A separate zero-host
recheck gives 989 / 970 ms (available) and 1,310 / 1,280 ms (memory recovery), with peak RSS
82.5 / 82.6 and 83.1 / 83.2 MiB. The initial available-path regression and extreme memory
sample do not repeat; these data do not establish a general zero-host speedup. Keep both trials
separate rather than pooling them.
The 16-call pilot and eight additional profile calls are excluded from formal medians.
The pilot also contains an eight-second memory sample; elevated shared-host I/O and memory
pressure was observed around these anomalies, without establishing their cause. Profile
snapshot counts stay at three / five; the 1,000-host available profile still saves the ledger
four times. Only its JSON encoding changes, preserving all durable save boundaries.
Measurements remain synthetic and warm-cache, with fixed memory/pressure probes and a tiny
tracked checkout. They do not measure executor startup or establish portable latency bounds.

An installed 1.19.17 (`ab50d05`) / 1.19.18 wheel comparison isolates the optional UUID
validation fast path on Python 3.12.13. The unchanged benchmark alternates versions in isolated
PID namespaces and separates first calls from seven-repeat medians. All 192 timed calls pass:
64 session calls, 96 query calls across empty and 5,000-release histories, and 32 session
available-path rechecks. Admission checks preserve history, live writer, scope, publication,
fallback, holder/lease, request identity and memory budgets; query business payloads match.
With 5,000 closed repository releases, initial medians are **1.19.17 / 1.19.18**:

| Command / case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB |
| --- | ---: | ---: | ---: |
| Session / available | 0 | 1,011 / 1,039 | 82.3 / 82.2 |
| Session / memory recovery | 0 | 1,244 / 1,113 | 82.8 / 82.8 |
| Session / available | 1,000 | 1,039 / 1,074 | 85.0 / 85.2 |
| Session / memory recovery | 1,000 | 1,606 / 1,499 | 87.3 / 87.2 |
| Status | 0 | 600 / 550 | 59.4 / 59.4 |
| Doctor | 0 | 583 / 558 | 60.0 / 60.1 |
| Lifecycle history | 0 | 624 / 595 | 60.0 / 59.6 |

Memory-recovery session medians improve by 7–11%, with the candidate faster in seven of seven
zero-host pairs and five of seven 1,000-host pairs. Large-history query medians improve by 4–8%,
with the candidate faster in 18 of 21 pairs. Empty-history queries differ by 1–3 ms without a
consistent gain, and RSS is broadly unchanged.
The initial available sessions are 3% slower. A separate seven-repeat recheck gives
1,042 / 1,033 ms and 82.4 / 81.7 MiB for zero host reservations (three of seven pairs faster),
and 1,121 / 1,077 ms and 84.7 / 84.9 MiB for 1,000 reservations (five of seven pairs faster).
The initial median regression does not repeat; these results do not establish a general
available-session speedup. Keep the initial and recheck trials separate. Outliers include a
6.2-second baseline and 2.1-second candidate in the initial zero-host available trial, and a
1.8-second candidate in the 1,000-host recheck; their cause is not established.
Eight additional profile calls are excluded from medians. Snapshot counts stay at three / five,
and historical decoder counts match across versions. In the zero-host available profile,
85,011 optional UUID checks take 0.193 / 0.091 cumulative seconds; these diagnostic timings
are not ordinary CLI latency. Every identity still receives validation, and noncanonical UUID
forms keep the original validator. Measurements are synthetic and warm-cache on a shared host,
with fixed memory/pressure probes and a tiny tracked checkout. They do not measure executor
startup or establish portable latency bounds.

The 2026-10-10 installed 1.19.22 (`a74ebcb`) / 1.19.23 wheel comparison measures reuse of the
first nonempty-string normalization result on Python 3.12.13. The unchanged benchmark alternates
versions in isolated PID namespaces, with separate first calls and seven paired repeats. All
64 timed session calls and eight separate profile calls pass their existing history, writer,
scope, publication, fallback, holder/lease, request-identity and memory-budget checks. With
5,000 closed repository releases, initial medians are **1.19.22 / 1.19.23**:

| Session case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB | Candidate faster pairs |
| --- | ---: | ---: | ---: | ---: |
| Available | 0 | 935.5 / 911.6 | 82.6 / 82.6 | 3/7 |
| Memory recovery | 0 | 1,139.5 / 1,139.5 | 82.9 / 82.5 | 4/7 |
| Available | 1,000 | 999.1 / 993.8 | 85.4 / 85.2 | 4/7 |
| Memory recovery | 1,000 | 1,381.0 / 1,435.7 | 87.2 / 87.6 | 2/7 |

The initial 1,000-host memory-recovery median is 4% slower. A separate seven-pair recheck
reversing the first-version order gives 1,386.5 / 1,395.7 ms and 87.1 / 87.8 MiB, with the
candidate faster in five of seven pairs. All 16 timed calls and two separate profile calls
pass. Keep the initial and recheck trials separate; these results do not establish a general
session-latency improvement or a repeatable 4% regression. Snapshot counts remain three / five,
and unchanged receipt decoder counts match across versions.

A separate unprofiled function experiment uses eight fixed valid strings, 800,000 calls per
sample and eleven alternating pairs. Median time is 46.0 / 40.9 ms, with the candidate faster
in all eleven pairs (11% lower); 21 absence, type, Unicode-whitespace and NUL cases return the
same text or error. The four initial profiles show 26–30% lower cumulative string-validation
cost at 325,099 / 325,131 calls per command. Profiles are instrumented diagnostics excluded
from CLI medians; the isolated function result is not an end-to-end latency claim.

Two seven-pair query trials each pass all 96 timed calls, with identical business payloads
at zero and 5,000 releases. At 5,000 releases, medians in milliseconds are:

| Command | Initial 1.19.22 / 1.19.23 | Recheck 1.19.22 / 1.19.23 |
| --- | ---: | ---: |
| Status | 492.5 / 482.5 | 477.1 / 479.2 |
| Doctor | 503.6 / 499.6 | 503.3 / 513.4 |
| Lifecycle history | 512.5 / 519.0 | 529.1 / 533.9 |

Initial empty-history doctor and lifecycle-history medians differ by 4–5 ms; the reversed-order
recheck is faster for the candidate in all three empty-history queries. Query gains are not
consistent across trials. RSS differences stay below 1 MiB in these configurations. The measured
benefit is reduced duplicate normalization work; schema validation, errors, snapshot freshness,
decode-cache lifetime and durable writes are unchanged. Measurements remain synthetic and
warm-cache on a shared host, with fixed pressure probes and a tiny tracked checkout, and do not
establish portable latency bounds. The initial profiling pilot is excluded from formal medians.

The 2026-10-10 installed 1.19.23 (`bb0b90f`) / pre-integration 1.19.24 candidate wheel comparison measures command-local
reuse of validated historical mappings. The unchanged benchmark uses Python 3.12.13, isolated
PID namespaces, alternating version order, separate first calls and seven paired repeats.
All 64 initial timed session calls and eight separate profile calls pass the existing history,
writer, scope, publication, fallback, holder/lease, request-identity and budget checks. With
5,000 closed repository releases, medians are **1.19.23 / 1.19.24**:

| Session case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB | Candidate faster pairs |
| --- | ---: | ---: | ---: | ---: |
| Available | 0 | 904.6 / 904.6 | 81.9 / 85.6 | 3/7 |
| Memory recovery | 0 | 1,119.0 / 1,042.9 | 82.4 / 86.0 | 6/7 |
| Available | 1,000 | 969.4 / 1,016.8 | 85.4 / 87.5 | 2/7 |
| Memory recovery | 1,000 | 1,355.8 / 1,281.3 | 86.9 / 90.0 | 6/7 |

The initial 1,000-host available median is 4.9% slower. A separate seven-pair recheck reverses
the first-version order and passes all 32 timed calls and four separate profile calls:

| Session case | Lost-exit host reservations | Median milliseconds | Median peak RSS MiB | Candidate faster pairs |
| --- | ---: | ---: | ---: | ---: |
| Available | 1,000 | 996.4 / 951.7 | 84.1 / 86.9 | 5/7 |
| Memory recovery | 1,000 | 1,379.1 / 1,331.7 | 87.1 / 91.0 | 5/7 |

Keep the trials separate: ordinary admission has no consistent gain or repeated 4.9%
regression. The 1,000-host memory-recovery median is 5.5% / 3.4% lower across the two trials,
with the candidate faster in 11 of their 14 pairs. The measured tradeoff is roughly 3–4 MiB
of additional command-local peak RSS. Snapshot counts remain three / five, and metadata
decoder counts match. The internal file-cache gateway `_decoded` is called 30,007 times
instead of 75,010 / 125,018; unchanged collections reuse validated mappings after fresh
directory enumeration and file-identity checks. This reduces repeated dispatch, filename
validation and mapping construction, without skipping first-read JSON or schema validation.

Query checks pass all 96 initial calls at zero / 5,000 releases and 48 reversed-order recheck
calls at 5,000 releases, with identical business payloads. At 5,000 releases, medians in
milliseconds are:

| Command | Initial 1.19.23 / 1.19.24 | Recheck 1.19.23 / 1.19.24 |
| --- | ---: | ---: |
| Status | 474.1 / 464.2 | 495.4 / 507.2 |
| Doctor | 474.9 / 480.7 | 510.9 / 512.7 |
| Lifecycle history | 491.0 / 498.9 | 530.6 / 512.8 |

These query measurements do not establish a general latency improvement; peak RSS differences
stay below 1 MiB. Results remain synthetic and warm-cache on a shared host, with fixed pressure
probes and a tiny tracked checkout. They do not establish portable latency bounds. The initial
three-pair profiling pilot is excluded from formal medians. Cache lifetime, mutable snapshot
isolation, metadata freshness, authority rechecks, locking and durable writes keep their gates.

The final 2026-10-10 comparison uses installed 1.20.0 (`88094e0`) and the tested 1.20.1 wheel
subsequently published as `db43f68`. It retains the unchanged benchmark, Python 3.12.13,
isolated PID namespaces, alternating version order, separate first calls and seven paired
repeats. With 5,000 closed repository releases and 1,000 lost-exit host reservations, all
32 timed session calls and four separate profile calls pass the existing history, writer,
scope, publication, fallback, holder/lease, request-identity and budget checks. Initial medians
are **1.20.0 / 1.20.1**:

| Session case | Median milliseconds | Median peak RSS MiB | Candidate faster pairs |
| --- | ---: | ---: | ---: |
| Available | 980.5 / 966.1 | 85.3 / 87.9 | 6/7 |
| Memory recovery | 1,413.5 / 1,343.0 | 88.0 / 92.3 | 3/7 |

The initial memory-recovery samples include candidate outliers at 1.676 and 3.002 seconds.
A separate memory-recovery recheck reverses the first-version order; all 16 timed calls
and two separate profile calls pass:

| Session case | Median milliseconds | Median peak RSS MiB | Candidate faster pairs |
| --- | ---: | ---: | ---: |
| Memory recovery | 1,393.3 / 1,373.3 | 88.3 / 92.4 | 5/7 |

Keep the initial and recheck trials separate. Their memory-recovery medians are 5.0% / 1.4%
lower, but the candidate is faster in only eight of the combined 14 pairs. The measured
tradeoff is roughly 4 MiB of additional command-local peak RSS. These noisy samples do not
establish a general session-latency improvement. Profile snapshot counts remain three / five,
historical decoder counts match, and `_decoded` calls fall from 75,010 / 125,018 to 30,007.
Instrumented profile timings are excluded from the latency medians.

Final query checks pass all 48 timed calls at 5,000 releases, with identical business payloads:

| Command | Median milliseconds, 1.20.0 / 1.20.1 | Median peak RSS MiB, 1.20.0 / 1.20.1 |
| --- | ---: | ---: |
| Status | 494.2 / 488.1 | 59.4 / 59.6 |
| Doctor | 497.1 / 476.4 | 61.0 / 60.4 |
| Lifecycle history | 499.2 / 514.5 | 60.5 / 60.4 |

Query results likewise do not establish a general latency improvement. Measurements remain
synthetic and warm-cache on a shared host, with fixed memory/pressure probes and a tiny tracked
checkout; they do not establish portable latency bounds. The earlier 1.19.24 tables describe
the pre-integration candidate and do not replace this final 1.20.1 comparison.

### Writer permission checks

`scripts/benchmark-cli --mode writer-check` measures both installed `writer-session check`
and `writer-session host-check`. The four cases are an active feature write, a read after its
holder's handoff, a refused write to the frozen checkout, and a refused foreign session. The
selected runtime must advertise `host_handoff_continuation_version >= 2`; its CLI/storage
versions, package/entry-point hashes and the benchmark source hash are retained.

For a baseline, run this from the development environment, substituting a versioned installed
Controller entry point and a new output directory under a task-owned temporary root:

```bash
unshare --user --map-current-user --pid --fork --mount-proc --kill-child=KILL \
  .venv/bin/python scripts/benchmark-cli --mode writer-check \
  --work-dir /absolute/new-output \
  --controller current=/absolute/versioned/worktree-controller \
  --sizes 0 1000 5000 --repeats 7
```

`--sizes` counts synthetic closed repository releases (five metadata documents per release).
The default sizes are 0, 1,000 and 5,000. Each call is a fresh installed CLI process descending
from the fixture's actual live lease holder. Setup creates empty feature tasks and pins one
handoff at its unchanged HEAD; setup memory/pressure probes are fixed, while the measured CLI
uses its ordinary process and authority checks. No real repository, host ledger or remote is
modified. `--repo-sizes` remains specific to admission modes. Add `--profile` for one extra
diagnostic process per runtime/case/action/history size, using the selected installed interpreter
and CLI with real locks and holder checks. `profile_diagnostic` records snapshot load counts,
their cumulative time, historical decoder counts and the largest cumulative costs. Its instrumented
latency and RSS are excluded from `first_call`, `samples` and medians.

First calls follow setup and identity probes; repeated calls use warm OS and task-local
bytecode caches. Metadata hashing also warms caches outside the timing. These are synthetic
shared-host measurements, not storage-cold timings or portable latency bounds. Multiple
selected runtimes alternate order within each case. `results.json` retains every sample,
median wall time and peak RSS; `calls/*.json` in each `writer-<size>` fixture retains exact
arguments, exit status, decision and stderr.

Every call verifies the expected grant/refusal, allocation, scope, holder and lease evidence,
unchanged repository/host JSON bytes, and an absent write target. Each timed and diagnostic response
must also match across selected runtimes after excluding only `tool_version`. Cleanup releases the exact
leases, reopens only the empty frozen fixture, and uses Controller `complete-no-change` and
guarded `release` for both checkouts. `fixture-cleanup.json` retains their completion/release
receipts and verifies zero fixture leases or linked checkouts. Check failures/timeouts retain
partial output and still run this cleanup; the overall result becomes `PASS` only after all
samples, cleanup and installed-runtime identity rechecks succeed. The containing fixture
repositories and retained refs remain disposable evidence until their task-owned output root
is cleaned.

The 2026-10-12 baseline uses installed Controller 1.22.2 (`a3111f9`), CLI contract 54,
storage layout 4 and continuation version 2, with Python 3.12.13 on Linux x86_64 in an
isolated PID namespace. All 192 timed calls passed their expected authority and metadata
checks; all six fixture checkouts were released. Seven repeated samples per row give these
direct / hosted median pairs:

| Case | 0 releases, ms | 1,000 releases, ms | 5,000 releases, ms | 5,000 releases, peak RSS MiB |
| --- | ---: | ---: | ---: | ---: |
| Active write | 76.6 / 77.7 | 163.0 / 162.1 | 554.0 / 510.1 | 76.82 / 77.06 |
| Handoff read | 75.1 / 77.8 | 190.1 / 164.0 | 577.3 / 529.3 | 82.41 / 77.00 |
| Frozen write refusal | 90.1 / 90.6 | 187.0 / 165.1 | 616.3 / 564.1 | 82.68 / 76.88 |
| Foreign session refusal | 90.1 / 93.9 | 169.4 / 159.9 | 639.2 / 547.3 | 82.24 / 76.40 |

This is a baseline of one runtime on synthetic warm-cache history. Direct and hosted actions
run in separate blocks; these figures do not establish a paired cross-path performance gain
or an improvement over an earlier release.

The 2026-10-13 comparison uses the installed 1.22.2 runtime and an isolated 1.22.3 wheel with
Python 3.12.13 in a PID namespace. All 384 timed calls and 48 separate profile calls pass the
authority, unchanged-metadata and identical-business-JSON checks; all six fixture checkouts are
released. Runtime order alternates within each of seven pairs, with a separate first call.
Direct `check` medians are shown as **1.22.2 / 1.22.3**:

| Case | 0 releases, ms | 1,000 releases, ms | 5,000 releases, ms | 5,000 releases, peak RSS MiB |
| --- | ---: | ---: | ---: | ---: |
| Active write | 81.0 / 80.3 | 164.8 / 166.8 | 573.9 / 558.3 | 76.85 / 75.89 |
| Read after handoff | 84.1 / 84.7 | 185.4 / 172.2 | 700.1 / 588.7 | 82.22 / 75.59 |
| Frozen write refusal | 83.3 / 83.0 | 197.7 / 185.4 | 712.7 / 624.0 | 82.33 / 76.24 |
| Foreign session refusal | 75.5 / 78.1 | 194.6 / 181.0 | 789.9 / 648.7 | 82.65 / 76.33 |

At 5,000 releases, each of the three continuation/refusal cases is faster in all seven pairs,
with 12–18% lower median latency and about 6 MiB lower peak RSS. Their snapshot loads fall from
two to one and live writer-lease decodes from four to two. Historical decoder counts match;
the redundant history scan/validation is removed rather than cached across calls. The shared
lock covers both the guard and continuation, so Controller authority mutations cannot split
their snapshot. Active checks and every hosted case retain one load. Hosted 5,000-release
medians vary from 0.6% slower to 5.1% faster; empty-history cases and these unchanged controls
do not show a consistent gain. Profile timings are excluded from the table. These remain
warm-cache synthetic measurements on a shared host, not portable latency guarantees or a
full-suite performance claim.

The 2026-10-13 cache comparison uses installed 1.22.3 (`7f6db88`) and an isolated 1.22.4 wheel,
Python 3.12.13, isolated PID namespaces, alternating runtime order and seven paired repeats.
All 384 timed calls and 48 separate profile calls pass the authority, unchanged-metadata and
identical-business-JSON checks; all six fixture checkouts are released. At 5,000 synthetic
closed releases, medians are shown as **1.22.3 / 1.22.4**:

| Case | Command | Median ms | Peak RSS MiB | Faster pairs |
| --- | --- | ---: | ---: | ---: |
| Active write | `check` | 610.0 / 521.3 | 76.27 / 59.00 | 6/7 |
| Active write | `host-check` | 536.0 / 508.2 | 76.32 / 58.67 | 7/7 |
| Read after handoff | `check` | 559.5 / 510.8 | 76.36 / 59.13 | 7/7 |
| Read after handoff | `host-check` | 598.5 / 534.3 | 76.48 / 59.00 | 6/7 |
| Frozen write refusal | `check` | 582.7 / 522.8 | 75.81 / 58.56 | 6/7 |
| Frozen write refusal | `host-check` | 620.8 / 598.3 | 76.27 / 58.65 | 7/7 |
| Foreign session refusal | `check` | 594.6 / 555.2 | 76.12 / 58.57 | 7/7 |
| Foreign session refusal | `host-check` | 658.5 / 562.6 | 76.28 / 58.54 | 6/7 |

The 5,000-release cases use about 17 MiB less peak RSS and have 3.6–14.6% lower median latency.
Every profile retains one snapshot load and identical metadata decoder counts. Single-snapshot
checks stop retaining decoded values, file-identity dictionaries and copies of historical mappings;
the byte reader, full validation and shared-lock decision remain unchanged. At 1,000 releases,
peak RSS falls by roughly 3 MiB, while latency gains vary by case. Empty-history medians range
from 6.7% faster to 5.7% slower, with no consistent RSS change. These are warm-cache synthetic
results on a shared host; they do not establish portable latency guarantees or full-suite performance.
