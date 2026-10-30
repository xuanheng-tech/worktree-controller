# Changelog

## 1.34.0

- Let a released integration branch use the existing plan and CAS retirement flow
  when its preparation was formally aborted and then abandoned, it has no
  publication receipt, and the released commit is a direct ancestor of the
  publication branch. Retryable, drifted, or non-ancestor evidence stays
  blocked by `INTEGRATION_PUBLICATION_NOT_VERIFIED`. Cherry-equivalence is not
  accepted. Advertise `aborted_integration_branch_retirement_version=1`.
  CLI 61 and storage layout 5 stay unchanged.

## 1.33.1

- Align build-resource registration with the existing allocation runtime authority
  used by typed cleanup. Unassociated host permission gaps remain visible evidence
  and warnings; managed references, observed references and uncertain managed or
  scan identity still block both planning and apply. Remove the redundant legacy
  host-wide strict scan. CLI 61, storage 5 and capability versions are unchanged.

## 1.33.0

- Retention dry-run calls the real guarded release preflight before reporting
  `WOULD_RELEASE`; routes needing lifecycle changes report `CLOSEOUT_REQUIRED`.
- Add explicit owner-attested registration of frozen Node project outputs, bound
  to owner, HEAD, record CAS, committed build script/lock and directory identities.
  Registered outputs use the existing typed cleanup transaction, drift checks,
  hardlink proofs and reader-friendly cleanup lock; unknown evidence stays blocked.
- Keep CLI 61 / storage 5; advertise `registered_build_resources_version=1` and
  `retention_release_preflight_version=1`. Existing aborted integration cleanup v2
  remains unchanged.

## Unreleased

## 1.32.18

- Preserve an aborted integration's original proof across verified task-local Python
  environment registration and cleanup. Validate terminal owner/state continuity,
  the unique resource identity, registration/cleanup order and actual resource state.
  Bind new resource events to their exact resource; accept legacy events only under
  the existing single `.venv` contract. Recreated resources and ambiguous histories
  fail closed. Never rewrite the abort receipt or grant release from cleanup alone.
- Advertise aborted-integration preserved cleanup capability 2. Keep CLI 61 and
  storage layout 5; normal preservation, release, admission and publication gates apply.
- Keep directory-only Git ignore semantics when registered environment removal
  completed before its metadata update. A normal same-CAS retry records cleanup
  once; tracking drift, symlinks and foreign resources remain refused.

## 1.32.17

- Reuse positive immutable publication-history ancestry edges within one doctor
  semantic observation. Re-read the formal ref, reflog and graph overrides for each
  checkout; keep base/head checks fresh and retain no cache between requests.
  Preserve CLI 61, capability versions and storage layout 5.

## 1.32.16

- Carry relative paths while walking typed resource trees instead of rebuilding every
  ancestor chain per entry. Sort sibling names directly while retaining the existing
  traversal order and exact metadata fingerprint. Keep both preservation authority
  checks, all content reads, hardlink evidence, runtime guards and refusal conditions.
  CLI 61, capability versions and storage layout 5 remain unchanged.
- Tear down only test-owned FIFO and socket nodes after each test, without following
  directory symlinks or crossing filesystems. Retain regular evidence and directory
  modes; keep host-resource isolation independent of test-level monkeypatch undo.

## 1.32.15

- Observe Git worktrees once per integration-queue or lifecycle-events query. Reuse the
  status observation and its metadata for queue decisions; keep the event outbox on its
  separate guarded read. Preserve full untracked-file observation, diagnostics and
  fail-closed metadata checks. Retain no observation between requests and keep CLI 61,
  capability versions and storage layout 5 unchanged.

## 1.32.14

- Refuse worktree acquisition without a governance policy before creating its
  Controller lock, and recheck the policy under the lock as before. Existing
  incomplete or corrupt metadata remains subject to fail-closed recovery.

## 1.32.13

- Reuse the existing lock-scoped unrelated completion allocation filter in terminal
  no-change cleanup checks. Preserve full relevant-document validation and re-read changed
  files, including in-place changes with restored mtime. Retain no receipt verdict or
  cross-lock cache; keep owner, HEAD, record CAS, lifecycle and preservation gates unchanged.
- Cover the public small-source preservation plan and cross-consumer scope reuse. An
  isolated eight-receipt fixture reduces unrelated document reads from 64 to 16 while
  retaining both authority observations. First-read, tree verification and retirement
  costs remain separate. Keep CLI contract 61, capability versions and storage layout 5.

## 1.32.12

- Add `repository_lock_admission_version` 1. Cooperating readers and writers enter through
  the existing metadata directory before taking `repo.lock`; an admitted writer prevents
  later readers from continually bypassing it. Nested shared reads in the same thread remain
  safe. Keep `repo.lock` as the metadata mutation authority and retain CLI 61 / storage 5.
- Keep typed-cleanup history readable even with a queued writer. Mark active cleanup using
  a flock on the existing cleanup-actions directory only after its shared repository lock is
  held. Kernel locks release on process exit; interrupted operations retain their existing
  journal recovery requirements.
  Older runtimes retain exclusion but do not participate in the new admission ordering or marker.
- During one held flock, reuse only the unchanged allocation scope of unrelated completion
  documents. Recheck FD-bound inode/ancestor identities, including ctime, and decode changed
  files again. Relevant receipts, byte hashes, action chains and preservation trees are always
  fully verified. Retain no receipt verdict or scope mapping after the flock ends.
- This prevents a reproduced reader-bypass pattern and reduces repeated unrelated receipt
  decoding; it does not promise strict FIFO, fixed wait times or faster payload verification.

## 1.32.11

- Recover an already removed, RELEASED-recorded feature/audit release when its own retained
  METADATA_WRITTEN acquire journal would otherwise require the not-yet-written release receipt.
  Require the original release plan, exact allocation/path/branch/base lineage, acquired and
  released history, +1 record CAS, retained HEAD and absent checkout/binding/writer lease.
  Preserve the acquire journal; only the completed immutable release receipt makes it terminal.
- Keep other transactions, ambiguous predecessors, request/owner/intent drift, changed history,
  retained refs and incomplete release phases blocked. Keep the original guarded release CLI,
  CLI contract 61, capability versions and storage layout 5.

## 1.32.10

- Keep closed lifecycle history available during typed resource cleanup. Hold the existing
  repository lock shared to exclude metadata writers and serialize cleanup on the existing
  transactions directory. Preserve every owner/CAS, preservation, hardlink, runtime, action,
  journal and completion check, including interruption and repeated-call refusals.
- Keep doctor filesystem/journal diagnostics coherent and refuse activation while the cleanup
  gate is held, including its preflight and post-scan boundaries. Introduce no new persistent
  path, query cache, public command, capability or storage schema; retain CLI 61 / storage 5.
- Document that missing historical release plans require trusted original evidence; existing
  publication reconciliation and interrupted-release replay cannot synthesize that evidence.

## 1.32.9

- Avoid full Git worktree inventory for exact release-receipt queries and other lifecycle
  selectors with no matching live checkout binding. Retain canonical Git identity, the shared
  repository lock, full metadata validation and all immutable evidence/conflict checks.
- Keep live projections on the existing inventory path. Preserve query output, CLI contract 61,
  capability versions and storage layout 5; introduce no cache, service or storage format.
- Correct the history-handover fixture's read-only check to allow filesystem access-time updates
  while retaining symlink identity, target, ownership, size and nanosecond modification/change checks.

## 1.32.8

- Place shared bounded, FD-bound evidence document reads in `cleanup_evidence`, updating
  preservation, recovery and migration consumers. Retain raw-byte hashes, file and ancestor
  identity checks, size limits and refusal codes; remove four private cross-module imports.
- Separate ordinary installed upgrade acceptance from the two historical restoration cases,
  requiring declared staging provenance and matching CLI/capability prerequisites before running.
- Preserve staged source bindings in disposable fixtures and cover wrong-source and identity-drift
  refusal. Keep every selected case mandatory; record unrequested cohorts without counting passes.
- Test/document decision-aware consumers. Keep CLI contract 61, capability versions and
  storage layout 5 unchanged.
- Scope the installed exit/rescan fixture's process enumeration to its real child so unrelated
  protected host processes cannot decide the result. Retain real procfs and refusal checks.

## 1.32.7

- Hold one shared repository lock through doctor snapshot diagnostics and refresh Git inventory
  after acquiring it. Guarded release/refresh cannot invalidate a checkout while its semantic
  or transaction evidence is being read.
- Cover a real concurrent guarded release, mutation before lock acquisition, and genuinely
  missing commits through both API and CLI, retaining commit/ref evidence and refusal codes.
- Update the maintenance baseline after the 1.32.6 delivery and keep CLI contract 61,
  capability versions and storage layout 5 unchanged.

## 1.32.6

- Report uncertain runtime visibility as `RUNTIME_SCAN_UNCERTAIN`, separately from positive
  `RUNTIME_OCCUPIED` evidence. Both retain runtime bindings and execution reservations.
- Isolate parent-side procfs in the command-exit regression, retaining real command/lease
  execution. Verify repeated refusal, public CLI exit codes, later ordinary reconciliation,
  idempotency and preservation of commits, untracked files and generated output without retries.
- Share allocation-to-integration-intent lookup in `semantics`, retaining lifecycle aliases
  and identity checks. Reduce private cross-module imports from 78 to 76.
- Keep CLI contract 61, capability versions and storage layout 5 unchanged.

## 1.32.5

- Poll successful publication writes through one shared five-minute visibility budget.
  Revalidate cached GitHub/PyPI metadata, preserve exact bundle recovery and stop immediately
  on command failures, identity drift, unknown assets or checksum mismatches.
- Move pure integration scope hashing and overlap queries into `semantics`; retain lifecycle
  aliases and reduce private cross-module imports from 80 to 78 without changing scope rules.
- Keep CLI contract 61, capability versions and storage layout 5 unchanged.

## 1.32.4

- Share pure release-intent selection and renewal-backoff queries in `release_intents`;
  retain lifecycle clocks, locks, mutations, aliases and history. Reduce private cross-module
  imports from 82 to 80 without changing release or recovery behavior.
- Keep publication lease-reconciliation queries in the existing shared semantics module,
  preserving terminal-cleanup proof checks and the private-import regression gate.
- Correct the consumer inventory, document existing WC/GF public boundaries and record the
  staged maintenance roadmap. Existing session and recovery commands remain supported.
- Keep CLI contract 61, capability versions and storage layout 5 unchanged.

## 1.32.3

- Allow exact preserved-tree cleanup for published integrations with verified producer
  publication proof and completed no-change feature/audit tasks with verified
  original completion evidence. Pin proof in the cleanup plan and recheck it at release;
  retain frozen lifecycle, owner/CAS, runtime, lease, ref and transaction guards.
- Require preservation for every removed tree on these terminal compatibility paths.
  New-format no-change completion with prior cleanup keeps its original exact-CAS contract.
- Add `terminal_preserved_cleanup_version` 1; CLI 61 and storage layout 5 remain unchanged.

## 1.32.2

- Share history-preserving record and intent updates through `record_updates`, and interpret
  effective integration state through `semantics`. Preserve lifecycle aliases and existing
  terminal-evidence refusal rules while reducing cross-module private imports from 93 to 82.
- Handle disappearing `/proc` children and asynchronous signal delivery in session-recovery
  and rollout-test helpers; retain the same live-process checks and verify eventual child exit.
- Keep CLI contract 61 and storage layout 5 unchanged.

## 1.32.1

- Preserve keyed integration preparation for the QP Nexus progress document and its exact
  merge profile. Historical Handbook commits remain readable under their original profile;
  moves, broad overlaps, shared records and ambiguous evidence retain the file fence.


## 1.32.0

- Add a versioned exported-release history handover and receipt-bound rollback to the existing
  maintenance entry. Require exact baseline payloads, live published release identities, unchanged
  storage and Skill formats, current consumer gates and a compare-and-swap plan. Preserve interrupted
  evidence and distinguish restoration after failure from successful activation.
- Bind staged runtimes to their frozen primary source through `controller-runtime-source/v1` so
  optional maintenance checkers can verify public release histories without private source copies.
- Keep ordinary `--dry-run` refusal while adding explicit history preparation plans. CLI 61 and
  storage 5 remain unchanged; no Controller lifecycle, admission or write-guard rules are relaxed.

- Document the public AI client integration boundary and managed-upgrade history constraints.
- Add an offline two-process continuation example: preserve one original task and its unknown
  files, verify fresh writer identities, inspect both checkouts at closeout, and retry an
  incomplete no-change release without repeating its completion. No runtime contract changes.

## 1.31.0

- Add task-oriented list/show/start/resume/finish/recover entrypoints over existing Controller
  authority. Resolve task paths and record versions without a new task database or lifecycle.
- Support original-task checkpoint pause/resume, exact Finalizer handoff and guarded no-change
  release. Refuse live or stale recorded writers and allocation replacement during resolution.
- Report partial no-change release with its completed prefix and nonzero exit; retries preserve
  completion evidence. Recovery guidance now directs completed no-change tasks to release guards.
- Add CLI 61 and capability `task_workflow_version` 1; existing low-level interfaces and storage
  layout 5 remain compatible. No dependencies were added.

## 1.30.6

- Add a complete local change example with public Finalizer and Snapshot Runner, genuine review
  receipts, strict checkout closeout, and pause/resume of the original integration request.
- Document Ubuntu's venv prerequisite and the explicit Snapshot evidence required for integration;
  keep the optional maintenance checker separate from standalone WC requirements.

- Add a manually triggered, resumable package release workflow using PyPI Trusted Publishing.
  Freeze checked distributions once, retain a matching GitHub draft before PyPI upload, reject
  remote identity/content drift, and verify public downloads and fresh installations separately.
- Keep build/test, publishing and installed-package execution in separate permission stages.
- CLI contract 60, storage layout 5 and lifecycle behavior are unchanged.

## 1.30.5

- Add public repository, documentation, issue tracker and changelog links to package metadata.
- Shorten the README entry point and retain the full technical material in `docs/reference.md`.
- Add a self-contained, offline onboarding example with real admission, a live writer lease,
  an expected owner-mismatch refusal, read-only diagnosis and verified no-change closeout.
  It uses a new isolated repository and needs no companion delivery tools or credentials.
- Run the example against both the installed wheel and the wheel rebuilt from the sdist.
- Lifecycle behavior, CLI contract 60 and storage layout 5 remain unchanged.

## 1.30.4

- The read-only rollout gate (`scripts/check-writer-rollout`) no longer treats a live
  Controller invocation as an activation blocker. The pinned entry point resolves the active
  symlink once per invocation and activation repoints it with an atomic replace, so a command
  already running finishes on the version it started and only later invocations see the target.
  Live invocations are now reported as `live_controller_invocations` inventory instead of a
  `LIVE_CONTROLLER_INVOCATION` blocker; an unobservable process still blocks
  (`PROCESS_SCAN_UNAVAILABLE`), fail closed. This restores the gate's documented intent to
  block on compatibility and genuinely in-flight work, never on a process count.
- `CONTROLLER_MUTATION_IN_FLIGHT` now fences only an exclusive repository-lock holder (a genuine
  mutation). The probe takes a shared `flock`, so the shared-lock accounting and observation that
  `admission-queue --apply` performs since 1.28.1 -- which leave repository metadata unchanged --
  no longer masquerade as an in-flight mutation. Together these let `controller-activate` find a
  ready window despite a host that runs `admission-queue --all-repositories --apply` continuously,
  which previously exhausted `--wait-ready-seconds` and aborted activation.
- A schema/layout cutover keeps its own independent fence: `LayoutMigration` takes the exclusive
  repository lock and performs its own live-invocation check, so relaxing the shared read-only
  gate does not weaken layout migration. Genuine mutations still hold the exclusive lock and
  still block an ordinary activation.
- CLI contract remains 60 and storage layout remains 5.

## 1.30.3

- Combine the published terminal recovery guidance with the deployed reopen lineage.
- Accept runtime occupancy during `reopen` only through the exact current writer lease,
  including allocation, owner, repository, path, live holder, session, run and expiry checks.
  A client name alone does not establish ownership. Preserve all bindings and normal write gates.
- CLI contract remains 60 and storage layout remains 5.

## 1.30.2

- Explain already published and released integration intents through the existing immutable
  lifecycle query. Require matching allocation, owner, task, authority, candidate and verified
  publication/release proof before reporting `PUBLISHED_RELEASED`; offer history inspection only.
- Keep missing or drifted terminal evidence blocked, retain normal recovery for aborted unpublished
  intents, and preserve the guide's read-only/no-business-completion boundary.
- CLI contract 60, storage layout 5 and both 1.30.0 capability versions remain compatible.

## 1.30.1

- Reverify successful retained-source cleanup at its original CAS after a complete
  same-owner park/resume history and a same-HEAD formal integration-ready handoff.
  Keep exact receipt, post-scan, action chain, preserved generation and original root
  checks; reject missing history, unknown transitions, identity/HEAD drift and reappeared
  resources. No old receipt or allocation history is rewritten.
- CLI contract remains 60 and storage layout remains 5.

## 1.30.0

- Add read-only `recovery-guide` with current owner/allocation/HEAD/CAS, explicit blockers and
  structured next-step arguments through the existing quarantine, abort, release and original
  intent retry contracts. It grants no execution or publication authority.
- Add optional expected allocation IDs to session closeout. Missing observations require complete
  immutable release proof; they cannot silently turn an empty selector into a successful closeout.
- Recheck frozen handoff/Finalizer identity, selected allocation/CAS and writer identity during
  preparation. Keep historical inspection failures visible per allocation and reject replacement
  writers when observing prior checkpoint/handoff closeout.
- Additive capabilities `recovery_guidance_version` 1 and `session_closeout_expectations_version` 1;
  CLI contract remains 60 and storage layout remains 5. Existing clients remain compatible;
  consumers must inspect installed capabilities before using these additions.

## 1.29.1

- Stop repeatedly observing vanished historical repositories during global admission maintenance,
  using fresh same-filesystem absence proof. Share that proof only with the exact terminal
  execution; keep live historical repositories enrolled for approved release-intent maintenance.
- Reconcile an explicitly abandoned integration's matching unattempted checkout request to
  `CANCELLED`, preserving the terminal event and failure reason. Verify owner/source identity,
  absent checkout/ref, transaction fences and exact ledger CAS; uncertainty remains blocked.
  This does not complete the Task or alter its source evidence.
- CLI contract remains 60; storage layout remains 5.

## 1.29.0

- Add `aborted_integration_preserved_cleanup_version` 1 to the existing preservation and
  `resource-clean --typed` flow. An unpublished `INTEGRATION_ABORTED` candidate can preserve
  every ignored tree, execute exact receipt-bound cleanup, and use normal guarded release.
  No Astro path allowlist, admission cancellation or automatic quarantine recovery is added.
- Pin new abort receipts by ID and raw SHA-256 in their history event. Keep single exact legacy
  aborts compatible; missing, changed or ambiguous evidence refuses cleanup. Recheck the abort
  when verifying completed cleanup. Original source, failure, branch and quarantine evidence stay.
- Keep changed-source admission blocked until the old allocation's formal release settles its
  durable budget. Document the Phase 1F recovery and its separate QP owner boundary.
- CLI contract 60; storage layout remains 5. This local candidate is not a published release.

## 1.28.0

- Typed `resource-clean` verifies hardlink topology before marking an action `STARTED`. A
  topology refusal no longer leaves `FAILED_OR_PARTIAL` action evidence that no recovery accepts.
- `resource-recover --stage reconcile-typed` and `rollout-check` accept the exact refusal left by
  earlier runtimes (first action `FAILED_OR_PARTIAL`, `HARDLINK_TOPOLOGY_BLOCKED`, only its
  `0000-before.json` bound to the plan topology) when the whole resource tree and in-scope
  topology are unchanged; the BEFORE receipt is bound into the reconciliation record.
- Accept ctime-only drift on inodes with links outside the scope before and after (net-zero
  external link churn); in-scope-only inodes still require exact identity. Capability
  `typed_cleanup_verify_boundary_version` 1, CLI contract 59; storage layout stays 5.

## 1.27.0

- Add `reconcile binding-registrations` (dry-run, then `--expected-plan-id`) to give linked
  bindings written before 1.26.0 their `worktree-registration/v1` identity when the live Git
  registration still matches the allocation (record present, `gitdir` pointer, no stale marker,
  registration no later than 900 s after `created_at`, base commit in history). Other bindings
  are reported with typed blockers and keep their legacy meaning. Capability
  `binding_registration_backfill_version` 1, CLI contract 58; storage layout stays 5.
- The native transport test binds its Unix socket in a short private directory, so a deep pytest
  basetemp no longer fails with `AF_UNIX path too long` or leaves sockets behind.

## 1.26.0

- Bind each new linked allocation to its exact Git registration. `acquire` and `adopt` write a
  `worktree-registration/v1` token into the worktree's Git administrative directory and under
  `binding-registrations/`. A registration that reuses a removed worktree's name no longer
  inherits the old allocation: the binding is withdrawn, `status` warns `BINDING_IDENTITY_DRIFT`,
  and write-guard, writer leases and release treat the checkout as unmanaged. Orphan bindings
  (administrative directory gone) and bindings written by earlier releases keep their meaning.
- Ignore inherited Git repository selection (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_COMMON_DIR`, object-directory, namespace and related variables) in every Controller Git
  command, so a call from a Git hook cannot observe or mutate another repository.
- Fail closed when a metadata directory cannot be listed (`FILESYSTEM_PERMISSION` or
  `INVALID_METADATA`) instead of treating it as empty.
- Refuse a release runtime scan whose checkout path or inode changes during the scan. Same-UID
  processes with denied procfs entries remain host visibility gaps, as in typed cleanup.
- Storage layout 5: declare the `cleanup-actions`, `cleanup-recoveries`,
  `native-session-recoveries` and `session-closeouts` directories already written by v4 runtimes,
  and add `binding-registrations`. The layout contract test now also rejects undeclared
  `store.root` joins. Closeout observations whose exact holder is provably gone are pruned when
  another observation is written. CLI contract 57.
- `scripts/check-shards` refuses to run when `.venv` does not match the locked dependencies.

## 1.25.1

- Permit hosted children to inspect all checkouts selected by their verified host identity using
  read-only session preparation. Ending a parent's allocation remains refused.

## 1.25.0

- Add read-only session preparation and strict closeout checks. Match the exact recorded run,
  enumerate every leased checkout, and report pending owner work separately from process exit or
  lease release. The launcher uses its session selector even after its starting checkout leaves.
- Expose stable owner-action requests with allocation, owner, HEAD and record CAS for existing Task
  consumers. Retain the human convergence explanation and preserve unknown files.
- Bind owner-attested Task checkpoint references and dependency reasons to guarded parking. Clean
  waiting work can use the existing reviewed archive/release path while its branch stays retained.
  No automatic commit, discard, business-completion claim or relaxed release guard.

## 1.24.8

- Add bounded advisory process diagnostics to live-process `canonical-refresh` refusals: kernel
  names, parent identities, matched paths, omission counts and normal session-closeout/retry hints.
  JSON and text output share the same evidence; command arguments and environments are not read.
- Reuse the original process-path scan without rereading descriptors or mappings. Missing,
  unreadable, malformed or oversized identity metadata stays unknown and keeps the original
  refusal. No process/session mutation or relaxed refresh guard; CLI contract 56 and storage 4 stay.

## 1.24.7

- Preserve completed acquisition evidence when ordinary task commits are followed by formal
  archival and guarded release. Bind live archival to its immutable receipt; require the original
  acquisition/task/base identity and ancestry. Closed progress still needs exact release and
  absence evidence plus the matching retained ref or official retirement proof.
- Reuse metadata-only archive identity validation in transaction classification, retaining full
  preserved-payload checks at actual archive release. No journal rewrite, inferred task completion,
  publication authority or changed writer/resource guard. CLI contract 56 and storage layout 4 stay.

## 1.24.6

- Recognize a completed feature/audit acquisition after ordinary unfrozen task commits. Require
  the original acquisition history, unchanged task/base identity, exact registration and binding,
  an attached branch and proven base ancestry. ACTIVE/RETAINED tasks keep their lifecycle,
  ownership, work and immutable journal; creation proof grants no handoff, publication or release.
  Frozen handoff, incomplete acquisition and identity-drift guards remain unchanged.
  CLI contract 56 and storage layout 4 are unchanged.

## 1.24.5

- Defer transaction and publication-lease inspection while a repository mutation lock is busy.
  Keep the rollout blocked and retry the complete gate; an in-flight command's intermediate
  journal no longer becomes a permanent unsupported-recovery diagnosis. Once idle, all existing
  transaction, recovery and publication checks remain mandatory. CLI contract 56 and storage
  layout 4 are unchanged.

## 1.24.4

- Fail the sharded gate on collection errors, invalid shard counts and collection/execution count
  drift. Use the same isolated environment for discovery and workers, skip empty shards, terminate
  worker process groups on interruption and retain failed logs.
- Block release when an editable-reference candidate is oversized, a symlink, a special file or
  cannot be enumerated/read. Bounded nonblocking reads no longer turn incomplete scans into a
  false absence of dependencies; dry-run and execution keep the same guard.
- Share record binding, managed-registration lookup and CAS checks through the existing semantics
  module without changing their errors or callers' lock scope. Add operational and architectural
  navigation, and explain original-request recovery when main advances during resource wait.
  CLI contract 56 and storage layout 4 are unchanged.
- Correct frozen canonical guidance to use `canonical-transition`. Keep the standalone retirement
  wheel fixture independent of registry caches for unrelated native-transport dependencies, matching
  the existing package smoke installation; deployment still installs declared runtime dependencies.

- Add three installed public publication-session checks: completion before authorization, a consumer
  success claim without a remote push, and disconnect before authorization with a waiting status
  reader. Refusals release the repository lock and preserve public authority and fixture refs;
  the original lease/run/candidate then publish and close out normally. The native core/extended
  gates require all 45/65 cases without skips and reject omission of any new case from JUnit.
  Runtime behavior and contracts are unchanged.

- Add two installed Controller/Finalizer checks after publication leasing: an independent external
  writer advancing main and a temporarily unavailable local bare remote. Refusals preserve public
  authority and actual fixture refs. Public abort/refresh and fresh validation recover remote drift
  on the same intent with a new lease; endpoint restoration reuses the original lease. Both publish
  normally and close source/candidate allocations with complete proof. The native core/extended
  gates require all 42/62 cases without skips and reject omission of either new case from JUnit.
  Runtime behavior and contracts are unchanged.

- Add three installed Controller/Finalizer publication refusal cases: a wrong run on the current
  lease, a dirty leased candidate, and distinct fetch/push endpoints. Refusals preserve public
  authority and all fixture Git refs, including both local bare remotes. Restoring the owned fault
  permits normal publication with the original lease/run/candidate and unchanged validation;
  source and candidate close with complete release proof. The native core and extended gates
  require these cases with zero skips, and reject omission of each new case from JUnit.
  Runtime behavior and contracts are unchanged.

- Add three installed-tool main publication lease acceptance cases: live-lease exclusion,
  real expiry fencing the old publisher before and after lease replacement, and a completed push
  recovering its original expired lease without repeating the push. Public refusals preserve the
  current authority and live main; the installed Finalizer supplies real publication/recovery
  records and guarded completion closes published fixtures. Real two/fifteen-second TTLs and
  the existing crash-after-push hook exercise the installed tools without private-state edits.
  The native core and extended gates require these cases with zero skips; extra JUnit
  observations also refuse the gate. Runtime behavior and contracts are unchanged.

- Add three installed-tool publication reprepare acceptance cases. A second independent prepared
  candidate publishes through Controller/Finalizer; the stale candidate clears its old validation
  and enters `NEEDS_REPREPARE` without a lease. Refresh refuses an old record CAS without changing
  public state, then a current refresh requires fresh validation before publication on the same
  intent, queue position, source and scope. Both changes survive and published fixtures close with
  complete release proof. The native gates require these cases with zero skips and independently
  check publication execution capability 1. Runtime and contracts
  are unchanged.

- Add five installed-tool conflict-resolution refusal cases: wrong expected HEAD, a dirty
  candidate, a merge outside frozen scope, invalid parent topology, and publication before fresh
  candidate validation. Refusals preserve the complete public intent, candidate and source
  observations, publication lease and live remote main. Fresh validation still permits normal
  Finalizer publication; fixtures verify formal guarded release. The native core and extended
  gates require these cases to pass with zero skips. Runtime behavior and contracts are unchanged.

- Add three installed-CLI release-intent renewal acceptance cases: immediate first-expiry renewal,
  unchanged retention failures backing off without new active intents or checkout changes, and
  record-CAS change after clearing a fixture binding renewing and releasing through verified
  publication and guarded environment cleanup. Real fixture TTL and retention grace exercise
  public CLI paths without private-state edits. The native core and extended gates require these
  cases to pass with zero skips and independently check retention capability 2.
  Runtime behavior and contracts are unchanged.

- Add three installed Controller/Finalizer conflict-retry acceptance cases. A formally aborted
  and released preparation can retry on the same intent after a real intervening integration,
  resolve a later conflict, bind fresh fixture content/Git evidence and publish through the public
  lease protocol; queue position, source and scope stay fixed. Retry refuses an un-aborted
  candidate or an aborted candidate without release. The native core and extended gates require
  these cases to pass with zero skips and independently check conflict capability 2.
  Runtime behavior and contracts are unchanged.

- Add five installed-CLI acceptance cases for releasing a reaped holder's writer lease: exact
  identity and lease ID, missing or wrong lease ID, changed holder fields, surviving process-group
  members, and another process still using the checkout. Refusals retain the lease and allocation;
  completed fixtures close through guarded no-change release. The native gates independently check
  `exited_holder_release_version`. Runtime behavior and contracts are unchanged.

- Add eight installed-runtime load and interrupt acceptance cases to the native cross-tool
  gate: real SIGINT during waiting reports and admission re-polls, preservation of an already
  bound provider, growing and contracting thread pools, transient and persistent exiting
  process scans, and persistent permission denial. Runtime behavior and contracts are unchanged.

## 1.24.2

- A waiting `writer-session` now cancels its queued execution request on Ctrl-C at any point of
  the wait, including while it reports the wait or re-polls admission. Before, an interrupt that
  missed the sleep escaped with a traceback and left the request for reconciliation. A request
  admitted meanwhile still refuses cancellation and stays for reconciliation.
- The runtime occupancy scan rescans a process that is exiting during the scan: its cwd/exe are
  already gone and, without its mm, its fd directory denies access. Such a process used to be
  reported as scan-uncertain at once, so binding removal, budget reconciliation and release could
  refuse spuriously on a busy host. A live process that keeps failing, or one that only denies
  access, is still uncertain.
- Process identity no longer treats a resizing thread pool as a different process. A busy
  multi-threaded process of the same user (a web server, a Provider runtime) changed its thread
  count between the two reads of every scan attempt and was reported as `PID_IDENTITY_CHANGED`.
  Becoming single-threaded (an exec from a non-leader thread keeps PID and start time) or
  multi-threaded still counts as a change.
- Add five real-process automatic-restoration acceptance cases using installed runtime pins and
  the previous release's frozen gate. An actual installed post-check failure exercises successful
  restoration, new recovery errors, changed consumer minimums, a changed published compatibility
  catalog and concurrent pointer drift. The extended cross-tool selection requires all 27 cases
  with zero skips. CLI contract 56 and storage layout 4 are unchanged.

## 1.24.1

- Apply the release-intent renewal backoff in retention convergence before it creates an intent
  to classify a frozen handoff's unregistered `.venv`. A handoff whose venv classification keeps
  failing the same way (for example `PROJECT_IDENTITY_MISMATCH`) no longer adds a new intent on
  every pass; the row reports `RENEWAL_BACKOFF` with the last blocker, and a changed record or
  HEAD proceeds at once. CLI contract 56 and storage layout 4 are unchanged.

## 1.24.0

- Release an exited holder's writer lease from its exact recorded identity and lease ID when no
  member of its process group survives and no other process still uses the checkout. A host whose
  runtime already reaped the session no longer leaves the lease stranded until the next reconciler
  pass; other mismatches still refuse.
  Advertise `exited_holder_release_version` 1.
- Let an intent that was claimed, formally aborted and retried resolve a later pre-allocation
  conflict: retry eligibility judges the attempt after the last `PREPARATION_RETRY_QUEUED`, earlier
  claimed attempts must have been aborted, and only released earlier candidates may remain.
  Advertise `integration_conflict_resolution_version` 2.
- Back off renewal of a stale release intent whose record and HEAD are unchanged: the first
  expiry still renews at once, each repeat waits the intent TTL doubled per repeat (capped at a
  day) and is reported as `deferred: RENEWAL_BACKOFF`. Changed work renews immediately. A blocked
  handoff checkout no longer adds a new intent on every hourly reconciliation pass.
- Add `scripts/check-shards [N]`, isolated parallel test shards plus ruff, and
  `.agents/integration.toml` so Agent Workspace's `just integrate` uses it. `scripts/check-project`
  remains the release gate. CLI contract 56; storage layout stays 4.
- Add eight real-process rollback acceptance cases against the older target's frozen gate and
  installed CLI, covering successful rollback, real mutation-lock retries, legacy transaction
  refusal, recovery errors, consumer minimums, ancestry, pointer drift and timeout cleanup.
  The optional two-runtime selection now requires all twenty-two cases to pass without skips;
  default acceptance, runtime behavior and version contracts are unchanged.

## 1.23.0

- Add `canonical-refresh [--dry-run]`: fast-forward an ACTIVE, clean, idle canonical checkout on the
  publication branch to the fetched published main. Writer leases, runtime bindings, unfinished
  transactions, live processes using the checkout (other than the caller's own ancestry), divergence
  and an ungovernable published policy all refuse; a frozen canonical keeps `canonical-transition`.
  Controller metadata is unchanged. Advertise `canonical_refresh_version` 1 and CLI contract 55;
  storage layout stays 4.
- Compare the held-checkout continuation write with a plain continuation check instead of a fixed
  decode count, so the test keeps proving snapshot reuse after direct checks load only once.
  Test-only change.

- Add seven optional real-process rollout acceptance cases using two commit-pinned installed
  runtimes, CLI-generated transaction journals and deterministic scheduling barriers. Cover complete
  gate retries after transaction creation/completion, refusal on recovery errors and pointer drift,
  bounded churn and process-group timeout cleanup. Select them with `check-cross-tool
  --rollout-previous-controller`; require all fourteen selected cases to pass without skips and
  retain the default seven-case gate. Reuse the project PID-namespace reaper for isolated execution.
  Runtime behavior, software version 1.22.5, CLI contract 54 and storage layout 4 are unchanged.

## 1.22.5

- Recheck transaction membership after the staged read-only rollout check, including an initially
  empty set. Refuse that attempt when membership changes; opt-in readiness waiting retries the
  complete frozen gate only after a successful staged check matching an observed boundary.
  Failed recovery, malformed or contradictory results and unproved set mismatches still stop
  immediately. Default refusal, wait budget, pointer CAS and recovery authority are unchanged.
  CLI contract 54, storage layout 4 and continuation capability 2 stay unchanged.

## 1.22.4

- Disable command-local metadata reuse for direct and hosted writer permission checks that load
  one snapshot per repository. Retain byte decoding, exact JSON error details, full historical
  validation and the existing shared-lock authority decision. Other commands retain their caches.
  CLI contract 54, storage layout 4 and continuation capability 2 are unchanged.
  Seven alternating installed-runtime pairs at 5,000 synthetic closed releases show about 17 MiB
  lower peak RSS and 3.6–14.6% lower median latency across the eight direct/hosted cases. Smaller
  histories have mixed latency results; these local warm-cache measurements are not portable
  guarantees or a full-suite performance claim.

## 1.22.3

- Keep direct writer entitlement and handoff continuation on one shared-lock metadata snapshot.
  Remove the second load on continuation/refusal paths while serializing concurrent Controller
  lease/lifecycle changes through the decision. Fresh-call validation and refusal semantics remain
  unchanged; add real-flock lease-revocation and direct authority-fault regressions. CLI contract 54
  and storage layout 4 unchanged. In seven alternating installed-runtime pairs at 5,000 synthetic
  closed releases, the three continuation/refusal medians improve by 12–18%, with about 6 MiB lower
  peak RSS. This is a local benchmark result, not a portable or full-suite performance claim.
- Add separate writer-check profile diagnostics for snapshot loads, cumulative costs and decoder
  counts, excluded from timed samples and medians. Require identical business JSON across installed
  runtimes and diagnostics, apart from `tool_version`.

- Add `scripts/benchmark-cli --mode writer-check` for installed direct and hosted permission
  checks. Cover active writes, reads after handoff, frozen writes and foreign sessions, with
  separate first calls, repeated latency/RSS samples and full authority/metadata verification.
  Retain call outputs and source/runtime hashes, then close each fixture through guarded
  Controller completion and release, including on check failure or timeout. Runtime behavior,
  CLI contract 54 and storage layout 4 are unchanged.

## 1.22.2

- Compare runtime paths at component boundaries, normalizing only repeated slashes and `.`
  components instead of calling `commonpath` for each root and mapping. Preserve deleted-file
  references, lexical `..` rules, scan scope, limits and fresh release checks. Five alternating
  pairs of three host-namespace acceptance cases drop from median 18.90 to 14.04 seconds (26%).
  This is a local representative-case result, not a full-suite latency claim. CLI contract 54
  and storage layout 4 unchanged.
- Give the executor-session test children an explicit default SIGINT handler. A suite launched
  as a background job of a non-interactive shell inherits SIGINT ignored, so the competing-lease
  cancellation test could not interrupt its waiting session and timed out. Test-only change.

## 1.22.1

- Decode repository metadata once per entitlement check. `host-check` and `writer-session check`
  pass their locked inventory and snapshot to the write guard, and governance discovery skips its
  separate validation load when the caller loads the same metadata itself. A continuation write
  into a checkout of the same repository reuses that snapshot. Decisions and fail-closed handling
  of unreadable metadata are unchanged; the median hook check fell from about 0.47 s to 0.31 s on
  a repository with about 20,000 metadata records. CLI contract 54 and storage layout 4 unchanged.

## 1.22.0

- Accept `--access read|exec|write [--target]` on `writer-session check` as well as `host-check`,
  so a direct (non-hosted) holder also keeps its own frozen handoff's Controller flow.
- Let a frozen holder write into another checkout whose writer lease the same live process holds;
  that checkout's ordinary feature-write guard still decides.
- Evaluate entitlement checks without the full `status()` scan, removing about 0.5 s from every
  per-tool host gate call. Decisions are unchanged.
- Add `handoff --scope-changed`, recording the exact paths changed since the publication base for an
  unscoped feature task. An integration-ready handoff of unscoped changed work returns warning
  `MISSING_HANDOFF_SCOPE`, since `integration-enqueue` would refuse it.
- Let a rollout consumer report `required_controller_capabilities`; activation and rollback refuse a
  target runtime that does not advertise each declared minimum.
- Advertise `host_handoff_continuation_version` 2 and CLI contract 54; storage layout stays 4.

## 1.21.0

- Let the exact live holder of a frozen handoff continue its own Controller flow. With
  `writer-session host-check --access read|exec|write [--target <absolute-path>]`, the holder of
  the current lease on its `HANDOFF_READY`/`PRESERVED` feature allocation receives
  `CONTINUATION_GRANTED` for reads, commands, writes outside governance and writes into an
  integration candidate of that allocation. Per-tool host gates no longer lock a session out of
  `integration-enqueue`/`integration-claim` after it hands off its checkout. The frozen checkout,
  other checkouts, unclassified checks, new bindings and writer sessions keep their refusal;
  missing leases, foreign holders, HEAD drift and malformed requests fail closed. Advertise
  `host_handoff_continuation_version` 1 and CLI contract 53; storage layout stays 4.

## 1.20.4

- Align the optional cross-tool example with the verified Controller 1.20.3 / Git Finalizer
  1.8.18 baseline: all three cases passed with zero skips on CLI contract 52 and storage layout 4.
- Revalidate completed branch retirement across ordinary integrated-main fast-forwards,
  preserving the original plan and receipt. Require a matching operation receipt,
  absent ref, unchanged governance and original-main ancestry; fail closed on
  ref reappearance, receipt mismatch or real drift. Pending retirement CAS remains exact.
- Preserve the public historical `RETIREMENT_OPERATION_COMPLETE` marker when revalidation
  has blockers; current ref and blocker evidence still prevent closeout.
- Verify the real PID 1 check entry preserves `-O` and `-I`, script and output paths
  containing spaces, and `--offline`. Observe init/worker command lines in the PID
  namespace and confirm dependency failure retains output evidence.
- Exercise command timeout through the real PID 1 project-check entry, using a
  fixture-only shorter wait. Verify failure diagnostics and exit status, retained
  output evidence, closed descendant pipes and cleanup of the observed process tree.
- Exercise the real PID 1 project-check entry with SIGINT/SIGTERM during worker startup
  and dependency synchronization. Verify signal exit status, retained check outputs and
  cleanup of signal-ignoring descendants in an isolated launcher process group.
- When `scripts/check-project` runs as PID 1 in a disposable Linux namespace, supervise
  its check worker and reap adopted orphan fixtures while checks are running. Forward
  SIGINT/SIGTERM to the worker's process group and preserve failure, timeout and signal
  exit statuses. Document the direct-interpreter namespace entry and cover it with real
  orphan, exit, interrupt and timeout acceptance cases.

## 1.20.3

- Pin the selected allocation and original record CAS when ending a leased session.
  Refuse replacement allocations and resumed records instead of reconciling their newer identity.
- Check for a writer lease inside the terminal parking lock, preserving a new writer before its
  runtime binding is written. Keep explicit owner parking and advisory exit status unchanged.
  CLI contract 52 and storage layout 4 remain unchanged.

## 1.20.2

- Resolve a stable canonical repository context for `release` and `lease-complete`, so a
  `--repo` entry inside the removed checkout cannot interrupt receipt completion or source
  release acknowledgement. Preserve release guards, CAS and exact recovery identity.
- Add real CLI regressions for canonical, checkout-root and checkout-subdirectory entries,
  proving complete release receipts, cleared transactions, retained refs and idempotent replay.
  CLI contract 52 and storage layout 4 remain unchanged.

## 1.19.22

- Add read-only `release-recovery-inspect` for one exact interrupted release transaction.
  Verify its journal and persisted ReleasePlan, then expose the original guarded request,
  structured release arguments and raw evidence digests. Preserve the original owner/CAS;
  missing or inconsistent evidence is refused. Inspection grants no execution authority.
- Allow `release --publication-receipt-id` to pass the original identity to the existing
  guarded release engine. CLI contract 52 adds `release_recovery_inspection_version` 1;
  storage layout 4, recovery guards and rollout activation gates are unchanged.

## 1.19.20

- Read uncached historical metadata directly through its fresh directory entry, avoiding a
  per-file `Path` construction. Preserve UTF-8 text and newline handling, JSON object checks,
  error codes/messages/causes, and every schema, filename and identity validation.
- Keep fresh scans on each snapshot load, command-local identity-aware decode reuse,
  CLI contract 51, storage layout 4, writer guards and recovery behavior unchanged.

## 1.19.18

- Validate lowercase hyphenated optional metadata UUIDs with one compiled ASCII pattern,
  avoiding UUID object construction for that common form. All other inputs keep the original
  standard-library validation, accepted legacy formats, stripped original text and errors;
  UUID versions and variants remain unrestricted.
- Keep every schema and identity check, snapshot freshness, command-local decode reuse,
  CLI contract 51, storage layout 4, writer guards and recovery behavior unchanged.

## 1.19.17

- Save the host resource ledger as sorted-key compact UTF-8 JSON instead of indented JSON.
  Existing indented ledgers and older readers remain compatible; request and holder digests,
  save boundaries, locks, file permissions, file fsync, atomic replacement and directory fsync
  are unchanged. Repository metadata and immutable receipt encoding are unchanged.
- Keep CLI contract 51, storage layout 4, host ledger contract v1, writer guards and recovery
  behavior unchanged.

## 1.19.16

- Validate commit OIDs and SHA-256 digests with one compiled ASCII hex pattern instead of
  per-character Python loops. Keep exact lengths, lowercase-only acceptance, outer whitespace
  normalization, optional/required values, NUL rejection and existing error codes/messages.
- Every metadata snapshot still performs its full schema and identity checks. CLI contract 51,
  storage layout 4, command-local decode reuse, writer guards and recovery behavior are unchanged.

## 1.19.15

- Enumerate the five flat historical metadata directories with a fresh `os.scandir` on each
  snapshot load. Sort by filename and use path strings for command-local decode-cache keys,
  avoiding repeated `Path` construction and hashing. Entries are never cached across loads.
- Preserve atomic-replacement detection, filename/schema/identity validation, matched directory
  and symlink handling, and command-local cache lifetime. CLI contract 51, storage layout 4,
  writer guards and recovery behavior are unchanged.

## 1.19.14

- Complete new-session admission reuses its initial read-only inventory/snapshot for planning.
  Checkout acquisition still rechecks current authority and metadata under its exclusive lock,
  then uses that validated repository identity without loading the same snapshot again.
- Sort flat historical record and release-proof files by filename, preserving their order and
  every decode/identity check while avoiding repeated `Path` comparisons.
  CLI contract 51, storage layout 4, writer guards and recovery behavior are unchanged.

## 1.19.13

- Governed deployments can declare the published owner toolchain catalog through
  `--toolchain-compatibility` or `CONTROLLER_ROLLOUT_TOOLCHAIN_COMPATIBILITY`. Stage and runtime
  switches check the target's reviewed CLI, capability and storage bindings before activation;
  catalog/read/publication failures preserve the prior active pointer. Recheck at the switch
  covers concurrent catalog changes, bootstrap, rollback and automatic restoration.
- Keep catalog contents in the existing owner source. The source-owned lifecycle Skill requires
  its declaration at sites using `tool-skill-sync`; standalone sites retain independent deployment.
  Package CLI contract 51, storage layout 4 and existing rollout/refusal/retry guards are unchanged.

## 1.19.12

- Classify refused rollout transactions by command and cleanup contract. Interrupted release
  directs callers to the original guarded release request instead of requesting a preservation
  baseline. Unknown transactions remain blocked; all supported cleanup and terminal checks stay.
- Preserve transaction ID, command, phase, concrete error and a recovery entry hint
  through the staged check, frozen gate and activation log. Failed staged checks retain an
  explicit diagnostic. CLI contract 51 adds `rollout_transaction_diagnostics_version` 1;
  storage layout 4, activation/refusal guards and retry eligibility are unchanged.

## 1.19.11

- Keep immediately available `execution-admit` and `execution-cancel` on the uncached
  single-snapshot path. Standalone admission enables command-local decode reuse only after
  its first attempt needs exit recovery; composite session admission keeps its existing
  cache. Recovery may decode unchanged history twice rather than once. Full validation,
  holder/scope/budget guards, CLI contract 50 and storage layout 4 are unchanged.
- Extend `scripts/benchmark-cli` with full new-session admission, independent repository/host
  history sizes, untimed live-holder binding and fallback checks, and separate profiling
  diagnostics that retain the initial call instead of an untimed replay. Interrupted runs
  retain `RUNNING` evidence and reap their fixture holders.

## 1.19.10

- Keep `status`, `doctor` and `lifecycle-history` on the uncached metadata read path. Each
  query loads one repository snapshot, so retaining all decoded files increased latency and
  peak memory without reuse. Other CLI commands retain the command-scoped decode cache from
  1.19.9. Full metadata validation, CLI contract 50 and storage layout 4 are unchanged.

## 1.19.9

- One CLI command decodes each unchanged metadata file once. A command such as `session-admit`
  loads the repository snapshot several times; within that command a file whose (device, inode,
  size, mtime) identity is unchanged reuses its earlier frozen decode, and an atomically replaced
  file is decoded again. Nothing is cached across commands, and direct library callers are
  unaffected. On a repository with about 6,500 metadata files a repeated load drops from about
  0.29 s to 0.09 s. CLI contract 50 and storage layout 4 are unchanged.

## 1.19.8

- Clarify operation-scoped branch retirement closeout in the README and source-owned Skill.
  Global status counts local/remote receipts; a local-only candidate may remain `PARTIAL`
  after its selected operation completes. Closeout checks the existing per-operation receipt,
  current absent ref and empty blockers together, and verifies actual remote refs separately.
  Synthetic cases cover unpublished/published remotes, recreated refs, occupancy and failed
  observations. Retirement implementation, CLI contract 50 and storage layout 4 are unchanged.

## 1.19.7

- Explicit `admission-queue --all-repositories --apply` batches proven exited host executions
  into one atomic ledger write, retaining the existing ordered per-request results. Live,
  leased, foreign or uncertain rows keep their budgets and ordinary diagnostics; batch failures
  leave independent repository recovery available outside the host lock. Repository-selected
  and dry-run passes retain their scope. CLI contract 50 and storage layout 4 are unchanged.

## 1.19.6

- Pre-admission recovery settles proven exited host executions in one locked, atomic ledger write,
  instead of rereading and rewriting the full history per reservation. Each row still requires its
  exact target identity, bounded scope, runtime binding chain and exited process group; live or
  uncertain rows keep their budgets. Repository recovery remains outside the host lock, and normal
  host clear and typed dead-holder recovery keep their existing behavior. CLI contract 50 and
  storage layout 4 are unchanged.

## 1.19.5

- Fix: 1.19.4 could settle the checkout budget of a repository that still exists. Its absence check
  used `os.path.lexists`, which reports a permission (EACCES) or I/O (EIO) failure as absence. Only
  an explicit ENOENT now counts; every other failed observation keeps the reservation. Each of the
  repository, common directory and checkout path must also reach its own existing, non-symlink
  ancestor directory on the reserved filesystem. Rows already settled carry their recorded evidence
  unchanged. CLI contract 50 and storage layout 4 are unchanged.

## 1.19.4

- A checkout budget whose repository has been deleted no longer stays reserved forever. The
  all-repositories `admission-queue --apply` pass settles it only on positive absence of the
  repository, its common directory and the checkout path, observed on the filesystem the budget was
  reserved on, and records that `vanished-repository/v1` evidence on the row. Anything still
  present keeps the reservation. CLI contract 50 and storage layout 4 are unchanged.

## 1.19.3

- Transaction conflict checks and branch-retirement status still validate every journal identity,
  but verify terminal evidence only for journals in their own scope. Unrelated large historical
  recovery evidence no longer costs every check its full re-verification; a retirement status that
  took 31-46 s under load now takes under a second.
- An execution admitted but never bound to a holder now settles with its allocation's formal
  release. Reservations left behind by earlier releases are settled by `admission-queue --apply`
  and the pre-admission reconcile pass, only with that allocation's unique release receipt and the
  existing Git, path and journal checks; a held allocation keeps its in-flight reservation.
  CLI contract 50 and storage layout 4 are unchanged.

## 1.19.2

- Execution admission reconciles finished executions only when that can change its answer: a
  request that would wait, or that meets an earlier recorded execution of the same task, reconciles
  and retries once with the same durable request. An immediately available admission no longer
  waits on recovering unrelated finished sessions. Cancellation is unchanged. CLI contract 50 and
  storage layout 4 are unchanged.

## 1.19.1

- Admit a frozen feature-to-integration lineage that shares its source authority without relaxing
  unrelated or same-task request drift. Keep the original intent, source, scope, queue order and
  source reservation, and advertise `integration_lineage_admission_version` 1.
- Repeated exact claims observe the unchanged preparing candidate without another allocation,
  reservation or lifecycle event. Resource waits retain the queued intent and no longer report a
  candidate as claimed; scope fences and publication authority remain separate.
- Refresh a canonical handoff frozen on policy `main` or `master` through `canonical-transition`,
  retaining its original handoff history and Finalizer evidence without an extra baseline branch.
- Validate the immutable published policy before the checked-out branch moves. Preserve local
  ignored evidence, refuse unpublished/divergent main and keep all existing owner/CAS, lease,
  binding, transaction and intent gates.
- Recover exact partial Git progress without rewinding main; activate the canonical record only
  after exact remote verification. Advertise `canonical_main_refresh_version` 1 and CLI contract 50;
  the legacy transition contract, retirement plan schema 1 and storage layout 4 stay unchanged.

## 1.19.0

- Add `session-admit` and `session-bind` for executor hosts. They run the existing plan, acquire
  and execution-admit steps, and the writer-lease, holder-aware write-guard and runtime-binding
  steps, each in one process with the same guards, journals, receipts and record-version CAS,
  without attaching a repository status to every intermediate step. Uncommon starts return
  `SESSION_ADMISSION_FALLBACK` unchanged; a bind that fails after its lease releases that lease.
- Start the admission-triggered retention pass after the acquisition instead of before it, and skip
  it while a pass runs or within 300 seconds of the last one, so admissions no longer queue behind it.
- Pre-admission execution reconciliation skips reservations whose exact holder is still running
  instead of scanning and locking their repositories.
- Advertise `session_admission_version` 1 and CLI contract 49; storage layout stays 4.

## 1.18.15

- Allow explicit retirement of the old baseline ref proved by one complete canonical transition
  history event. Bind its original owner, Finalizer, HEAD and publication ancestry in the plan;
  preserve canonical registration and the protections for ordinary unregistered refs.
- Keep expected-OID execution and all occupancy, recovery and obligation gates. Missing, ambiguous
  or drifted transition evidence fails closed; completion remains independent and idempotent.
- Advertise `canonical_transition_retirement_version` 1, retirement contract 5 and CLI contract 48;
  plan schema 1 and storage layout 4 remain unchanged.

## 1.18.14

- Read each process's cwd, descriptors and mappings once per writer reconciliation pass instead of
  once per checkout. The pass runs under the exclusive repository lock and is started by every
  `acquire`, so repositories with many checkouts held that lock for seconds. Per-checkout outcomes
  are unchanged; `paths_in_use` keeps reading fresh. CLI contract 47 and storage layout 4 unchanged.
- Refresh the optional cross-tool example to the Controller 1.18.13 / Git Finalizer 1.8.15
  baseline verified on 2026-10-05: all 49 focused conflict-recovery, retry, Git-inventory and
  contract tests passed; all three installed cross-tool acceptance cases passed without skips.

## 1.18.13

- Observe per-worktree Git status (inventory) and index/status (state fingerprint) with a
  bounded pool of 8 concurrent read-only Git processes. Results keep registration order, and the
  first failure in registration order is still the one reported. Every command that inventories a
  repository with many checkouts becomes faster; outputs, CLI contract 47 and storage layout 4
  are unchanged.

## 1.18.12

- Retry pre-allocation integration conflicts through the original owner/plan/CAS and frozen intent.
- Add explicit single-source conflict claim and exact two-parent merge resolution binding; preserve
  queue/scope/ancestry, source commits, Finalizer and fresh candidate validation/publication gates.
- Recognize a recorded unallocated conflict as pending work rather than missing-allocation corruption.
- Advertise `integration_conflict_resolution_version` 1 and CLI contract 47; storage layout stays 4.

- Document optional task-owned acceptance roots and native FIFO-fixture cleanup
  without relocating test output; update the installed cross-tool example to 1.18.11.

## 1.18.11

- Add plan/CAS `integration-retry` for an aborted candidate with complete release evidence.
  Retain the original intent, frozen source/scope, enqueue sequence and historical failure receipts;
  a new attempt clears all prior candidate validation and uses normal preparation/publication.
- Admit one separately evidenced strict source-ancestor prerequisite under the failed parent's
  scheduling position, preserving both original enqueue identities and independent scope checks.
  Earlier unrelated scope fences remain mandatory; formal prerequisite publication/release must
  precede the parent retry. Reject drift, active candidates and already published results.
- Advertise `integration_retry_version` 1 and CLI contract 46; storage layout stays 4.

## 1.18.10

- Add opt-in `--wait-ready-seconds` to ordinary activation and rollback. Retry only temporary
  Controller invocations and mutation-lock occupancy through the complete frozen gate within
  one monotonic budget. Stop on permanent/unknown blockers, timeout or active-pointer drift;
  terminate only the attempt's own process group. Default refusal, switch CAS, post-check and
  automatic rollback gates remain unchanged. CLI contract 45 and storage layout 4 stay unchanged.

## 1.18.9

- Add exact typed preservation and cleanup for clean retained allocations and ignored regular-file
  or literal-symlink leaves, preserving owner/CAS, ancestor, resource and runtime guards.
- Preserve multiple ignored-resource generations across verified frozen handoff/reopen lineage.
  Recover only a proved completed cleanup prefix followed by never-started pending actions, with
  unchanged original evidence and append-only composite completion.
- Reuse the already-held repository store throughout cleanup and release validation instead of
  acquiring a second store under the repository lock. CLI contract 45; storage layout 4 unchanged.

## 1.18.8

- Freeze the original record and exact successful resource-cleanup receipts when a no-change
  task completes. Release verifies the write-once completion's raw SHA256 and its exact +1
  transition before reusing the cleanup's original CAS. Add an explicit append-only
  `reconcile no-change-completion` recovery for legacy completed audits; bind trusted receipt
  identities and digests without rewriting evidence or repeating cleanup. CLI contract 44;
  storage layout 4 remains unchanged.

## 1.18.7

- Calculate publication-to-release duration from the immutable candidate release timestamp.
  Repeating an already closed publication no longer counts time after release. Report an unknown
  duration when the candidate release timestamp is not unique; preserve historical receipts and
  existing release authority. CLI contract 43 and storage layout 4 remain unchanged.

## 1.18.6

- Keep a released acquisition terminal after exact authorized retirement of its retained branch.
  Bind the retirement plan and observed local deletion to the original allocation and release
  receipt. Support settlement of the same authorized deletion without clearing its independent
  authorization fence; missing or contradictory proof remains blocking. Preserve historical
  journal bytes. CLI contract 43 and storage layout 4 remain unchanged.

## 1.18.5

- Report per-source release outcomes during publication closeout. A previously released source
  returns `ALREADY_RELEASED` and its receipt only after complete, unique lifecycle evidence matches
  the frozen source and publication identities; duplicate closeout does not create release evidence.
- Keep pending cleanup, manual release, retryable blockers, missing or conflicting proof, and real
  stale intents explicit, with existing recovery commands. Add capability
  `source_release_summary_version` 1; CLI contract 43 and storage layout 4 remain unchanged.

## 1.18.4

- Recognize an exact typed publication reconciliation when a published integration candidate was
  released by its owner without a publication link in the original release plan. Preserve original
  receipts, require a unique acknowledgement of the released record version, and retain all
  publication identity, ancestry, occupancy and CAS guards.
- Bind release reconciliation receipts into retirement plan state so changed or missing proof
  invalidates execution. CLI contract 43 and storage layout 4 remain unchanged.

## 1.18.3

- Index the exact allocation, released record version and publication receipt identity once per
  doctor snapshot, replacing a repeated scan of all release receipts. Preserve metadata validation,
  missing-proof diagnostics and reconciliation behavior.
- Add a reproducible CLI benchmark with isolated on-disk fixtures, fresh processes, task-local
  bytecode caches, peak RSS, output sizes and cross-version JSON equivalence checks. Record the
  measured baseline and limits in the README.
- Treat ESRCH while reading an already-open process mapping stream as a vanished process in the
  existing best-effort runtime scan. Preserve other I/O error handling and strict scan boundaries.
- CLI contract stays 43; storage layout 4 and writer-session versions are unchanged.

- Replace assertion-based acceptance guards with explicit failures, including isolated package
  smoke checks, so Python optimization cannot disable version, contract, metadata or entry-point
  drift validation. Add rejection regressions for ordinary, `-O` and `PYTHONOPTIMIZE=1` execution
  using fixture tools without host dependencies.
- Add a separate optional cross-tool acceptance entry point with explicit installed-version pins,
  isolated fixtures, CLI/storage contract checks and a three-pass, zero-skip requirement. Retain
  JUnit and version/hash evidence while keeping the ordinary suite independent of host tools.
- Allow the publication interruption/recovery acceptance case to select an installed Controller
  for its public RPCs. Verify the 1.18.3 Controller and 1.8.9 Finalizer pair without changing runtime
  behavior, CLI contract 43 or storage layout 4.

## 1.18.2

- Resolve a pending or stale feature/audit release intent during branch retirement only when its
  frozen identity matches the exact completed release plan and receipt. Keep historical records
  unchanged and retain all ancestry, ref, lease, binding and transaction guards.
- CLI contract stays 43; storage layout 4 and writer-session versions are unchanged.
- Add one Linux/Python 3.12 acceptance entry point for local use and Gitea Actions: locked dev
  dependencies, Ruff, CLI contract checks, wheel/sdist builds, isolated installations and the
  complete self-contained test suite. Preserve outputs in an explicitly selected fresh directory.
- Run CI acceptance as an unprivileged user, including in root-started job containers, so
  read-only filesystem checks exercise normal permission enforcement.
- Include the README in distribution metadata and verify the packaged license and contract,
  including a wheel rebuilt from the sdist outside the source checkout.

## 1.18.1

- Separate pure metadata validation and serialization from repository locking and atomic disk
  storage, retaining the existing store imports and persisted schemas.
- Reuse receipt association indexes for closed lifecycle queries and serialize only the returned
  page while retaining conflict checks for every matching release.
- Remove test-owned socket paths after special-node acceptance checks so successful tests leave
  temporary trees eligible for guarded cleanup.

## 1.18.0

- Preserve interrupted acquire journals on uncertain Git failures. Archive positively verified
  no-effect attempts in resource-request history before clearing their journals and make those
  reservations retryable; interrupted settlement resumes through the existing admission queue.
  Advertise `acquire_no_effect_recovery_version=1` and prevent switching to a runtime without that
  capability while archived proof still needs settlement.
- Disable Git replacement objects consistently for authority, tree and ancestry evidence. Parse
  rename/copy source paths as part of their status record and accept Unicode public ref names.
- Reconcile finished executions before standalone admission using the existing runtime guards;
  retain live or uncertain bindings. Tests inject resource samples inside executor subprocesses
  and use a bounded local HTTP stall instead of an external blackhole address.
- Add gated first-runtime `controller-activate bootstrap` with absent-pointer CAS and guarded
  failure restoration. Resolve rollout metadata and separate-directory canonical inventory through
  Git rather than assuming a checkout-local `.git` directory.
- Return CLI filesystem failures as stable JSON errors and use one fixed UTC+08:00 timestamp
  authority without relying on OS timezone data. Share terminal integration states and remove
  obsolete arithmetic coupling between observe-only count declarations.
- CLI contract stays 43; storage layout 4 and writer-session versions are unchanged.

## 1.17.1

- Recognize an integration intent's explicit terminal abandonment when checking branch retirement.
  Reuse the existing semantic classifier; unexplained attention states and surviving allocation
  links remain blocked. Retain the original intent and immutable abandonment evidence.

## 1.17.0

- Allow retirement of a released integration candidate only when its release plan links to the
  exact remote-verified publication receipt. Repository, allocation, candidate, Finalizer and
  policy target identities must match. Missing or mismatched proof blocks both planning and
  completion with `INTEGRATION_PUBLICATION_NOT_VERIFIED`.
- Preserve ancestry, protected-ref, checkout, binding, lease, intent, transaction and exact-OID
  guards. Local and remote retirement continue through the existing authorization/CAS/completion
  protocol; release never implies branch deletion.
- Branch-retirement contract v4 and CLI contract 43 declare the new eligibility and blocker.
  Plan schema 1, execution contract v1, storage layout 4 and writer-session versions stay unchanged.

## 1.16.2

- A frozen hotfix handoff now follows the same convergence routes as feature/audit, including
  `release_unintegrated_handoffs`; the hotfix-only "published first" hold is removed. Its
  unregistered `.venv` still waits for the owner's attestation.

## 1.16.1

- Allow guarded canonical transition to a policy-selected `master` as well as `main`, retaining
  exact handoff, owner/CAS, ancestry, live remote, occupied branch and rollback checks.

## 1.16.0

- `worktree-session-end --client-id --session-id --holder ancestor:<program>` ends a client
  session that leased linked checkouts outside its launch directory: it releases only the writer
  leases whose recorded holder is exactly that live session and routes each allocation through
  the unchanged reconcile-session. Hosted children, partial identities and `--owner` are refused;
  a repeated notification is a no-op.
- `gc-apply` returns the existing receipt (`ALREADY_RELEASED`) when the exact reviewed
  ReleasePlan already completed, for example through a background pass; a release from any other
  plan stays `GC_PLAN_DRIFT` and names its receipt.
- Capabilities `session_end_selector_version` 1 and `gc_apply_replay_version` 1; CLI contract
  stays 42, storage layout 4.

## 1.15.0

- Retention convergence v2. RELEASED findings now reference the real `release_receipt_id` and
  `release_plan_id`; summaries and `outcome_counts` explain what actually kept checkouts (in use,
  owner action, blocked, below threshold) instead of always blaming idle time. Dirty rows carry
  change counts and an owner recovery entry; nothing uncommitted is ever touched.
- Published hotfix `HANDOFF_READY` checkouts converge through `release-published-handoff`; an
  unregistered hotfix `.venv` stays blocked for its owner's attestation.
- Optional `[convergence] release_unintegrated_handoffs` (default false) releases an idle,
  integration-ready feature/audit handoff whose frozen identities, local branch and
  remote-tracking branch all equal HEAD before integration. `integration-enqueue` resolves such a
  released source's branch from its release receipt while that branch still equals the handoff.
- Applying passes record RUNNING/COMPLETED/FAILED beside the pass lock; dry runs return it as
  `last_applied_pass`, so a detached admission pass is observable. CLI contract stays 42;
  `retention_convergence_version` becomes 2; storage layout stays 4.

## 1.14.1

- Add a guarded canonical transition from a frozen handoff to an exact remote `main` commit.
  Preserve the old branch and evidence, advance only an ancestral local `main`, and rebind the
  canonical record to a new active control-plane identity. Reject dirty, leased, bound, occupied,
  divergent or unsupported target states; exact partial Git progress can be resumed.
- Advance CLI contract to 42 with `canonical_transition_version` 1; storage layout stays at 4.

## 1.14.0

- Add provider-neutral `integration-publication-session/v1`: Controller validates its own lease,
  allocation, intent and prepared receipt, then holds its existing lock across public verify,
  authorize, consumer push and independent completion observation. Consumers no longer need
  private metadata paths or copied intent rules.
- Bound JSON frames and idle waits; release the lock on disconnect and recover already-published
  candidates without a second push, including expired-lease recovery. Lifecycle receipt persistence
  and checkout release remain with `lease-complete`.
- Retry an interrupted candidate auto-release from its pinned plan when the checkout is already
  removed, restoring the missing release receipt before acknowledging publication completion.
- Advance CLI contract to 41, preserve storage layout 4, and exercise the real Finalizer consumer.
- Make contract tests independent of a maintainer's untracked checkout policy by using an explicit
  temporary policy fixture, so clean source checkouts reproduce the same checks.

## 1.13.8

- Allow explicit schema v1 policies to queue frozen integration intents for the existing manual
  claim, validation, publication-lease and Finalizer path. Repositories without a policy still
  refuse queue creation; schema v1 gains no automatic continuation or release setting.
- Add `retained_typed_cleanup_version` 1 for exact owner/CAS preservation and explicit typed
  cleanup on clean `RETAINED` feature/audit allocations. Keep retained refs, lineage, lease,
  runtime, binding, integration and transaction guards; cleanup does not declare task completion.
- Add `preservation_leaf_version` 1 for ignored regular-file and literal symlink leaves alongside
  directories. Bind original ancestor identities, preserve link text without following the target,
  and remove only the verified leaf while retaining its tracked parent and siblings. Fail closed
  on unsupported callbacks and environment path races.
- Reuse the existing single/multiple preservation receipt contracts. Freeze each actual completed
  cleanup receipt's ID, path and raw SHA in archive evidence; guarded release accepts only the
  verified archive transition from the exact preceding record CAS to CAS + 1. Changed or partial
  evidence remains blocked, and interrupted multi cleanup still has no replay path.
- Keep CLI contract 40 and storage layout 4; retain the previously delivered capabilities.

## 1.13.7

- Add capability `published_handoff_resource_registration_version` 1 for owner/CAS classification
  of an existing reproducible root `.venv` on a published `HANDOFF_READY` hotfix. Verify exact
  frozen task/handoff/Finalizer identity, live publication, branch/base ancestry, runtime clearance
  and terminal transaction scope. Append owner-attested publication/environment proof to the new
  resource event without changing source files or frozen handoff evidence. Existing cleanup and
  release gates and automatic convergence roles remain unchanged.
- Honor existing validated effective terminal integration evidence in archive/delivered closeout
  and retention convergence, including formal abandonment without a physical candidate. Keep
  open obligations and frozen source ancestry guards; do not rewrite historical intents.
- Add capability `multiple_preservation_cleanup_version` 1 and CLI contract 40 for paired repeatable
  additional exact preservation receipts. Bind every source proof to the cleanup plan and each
  completed action, verify source-specific hardlink topology, and recheck every generation before
  release. Single-input plans and receipts remain unchanged. The distinct multi contract blocks
  unsupported interrupted recovery and rollout while preserving all original evidence.
- Preserve the legacy `PRESERVED` delivered closeout capability and storage layout 4.

## 1.13.4

- Extend the owner-reviewed `close-delivered` path to legacy `PRESERVED` feature/audit checkouts
  already contained in live publication and lacking frozen handoff/Finalizer evidence. Bind the
  original lifecycle, exact HEAD, branch, owner, record and publication to the existing plan and
  immutable receipt; retain business-completion and physical-release boundaries. Verified source
  archives keep their existing flow. Uncleaned resources, active use, ignored content and drift
  fail closed; preserved allocations remain excluded from automatic convergence.
- Preserve CLI contract 39, delivered closeout protocol 1 and storage layout 4.
- Describe authorization tokens as typed sensitive fields in the public contract instead of
  credential-like string literals; runtime response fields and secret scanning stay unchanged.

## 1.13.3

- A dormant `ACTIVE` lane (no lease, process, binding or Git activity) now always waits the full
  `idle_hours_max` before convergence parks it. Population pressure still shortens the threshold
  for already-retained unfinished work, but it had also shortened it for `ACTIVE` lanes, so a
  development line resumed the next day without a writer lease could be archived after a few hours
  on a crowded repository. Branches were never at risk; the checkout was.

## 1.13.1

- Runtime scanning rescans a process whose `/proc` fd/cwd entries vanished while its identity
  was unchanged (a task mid-exit, before it becomes a zombie). The scan previously reported
  `RUNTIME_SCAN_UNCERTAIN`, which on a busy host could stop a release after `.venv` teardown
  and leave an interrupted transaction. Rescans stay within the existing bounded attempts; a
  live process that stays unreadable still fails closed.

## 1.13.0

- Add read-only `lifecycle-history` (`closed-lifecycle/v1`), keyed by allocation, authority, task,
  integration intent or immutable release receipt. Retain the existing live `status` boundary and
  expose owner, checkout identity, exact release evidence and existing publication/validation/Finalizer
  references after a checkout disappears. Missing legacy receipts remain explicitly incomplete.
- Reuse metadata and immutable receipts without a database, daemon, history writer or migration.
  CLI contract 39 adds query capability 1; storage layout 4 and metadata schema 4 are unchanged.
- Preserve the original entry-point mode during layout rollback even under a restrictive caller umask.

## 1.12.3

- Convergence can close a published frozen handoff that carries an unregistered `.venv`. The
  release refuses unclassified ignored content, and a `HANDOFF_READY` allocation only accepts
  resource classification behind a pending release intent, so convergence first creates that
  intent (trigger `capacity-pressure`, bound to the exact handoff/Finalizer identity) and then
  registers the environment. The handoff freeze itself is unchanged.

## 1.12.2

- `acquire` no longer runs convergence inline. An inline pass released checkouts under the
  caller's lifetime; a caller killed by its tool timeout interrupted a release transaction
  after `.venv` teardown (recovered through the existing release recovery). The pass now
  runs detached in its own session, and applying passes are serialized per repository by a
  non-blocking host lock that reports `CONVERGENCE_BUSY`.

## 1.12.1

- Writer reconciliation also sweeps a lease on a non-ACTIVE, unreleased allocation once its
  holder and process group are provably gone. Such a lease grants nothing (resuming requires
  a new lease) but made retention convergence treat the checkout as in use forever. Live
  holders keep their lease; the allocation itself is never changed.

## 1.12.0

- Add delegated retention convergence (`reconcile retention`, policy schema 8 `[convergence]`).
  Admission never counted checkouts, but every exit from `RETAINED` was a manual, per-target,
  owner-signed command, so retained checkouts of finished agent sessions accumulated without
  bound. The delegated reconciler now composes the existing guarded verbs on clean, unused
  checkouts: published work closes through `close-delivered`, no-change work through
  `complete-no-change`, published frozen handoffs release directly, and unfinished work is
  archived on its exact branch once it has idled past a dynamic threshold. A clean `ACTIVE`
  allocation without lease, process, binding or Git activity for that threshold (clients that
  never take writer leases) is parked first, as its session would have done on exit. Dirty, detached,
  leased, bound or in-use checkouts are only reported.
- The idle threshold follows population pressure instead of a fixed count: `idle_hours_max`
  while registered checkouts stay within `live + standing_reserve`, shrinking with the square
  of the overshoot to `idle_hours_min`. Nothing refuses admission; `acquire` runs a
  best-effort pass first and reports it as `retention_convergence`.
- Fix: `close-delivered` rejected any checkout with a Python environment, because an
  unregistered `.venv` was unclassified ignored content and a registered one had to be
  cleaned first. Closeout and its release now accept a registered reproducible `.venv`, which
  the unchanged release teardown preflight removes.
- Fix: a `RETAINED` checkout may register its task-local `.venv`; previously this required
  reopening a writer (and, for handoffs, re-freezing evidence) only to classify a rebuildable
  environment.
- CLI contract 38 adds `retention_convergence_version` 1 and `retention-convergence/v1`.
  Storage layout 4 and metadata schema 4 are unchanged; convergence writes only the existing
  closeout, archive, completion and release receipts.

## 1.11.2

- Allow `archive-retained` to close an unfinished source checkout referenced only by a
  released historical integration intent when its frozen handoff remains on the branch.
  Active intents and unrelated handoffs still block; `close-delivered` remains strict.
- Keep the archived branch, task evidence, and publication claims unchanged.

## 1.11.1

- Add owner/CAS recovery for a frozen source advanced to already-published history and for a
  clean detached published integration. Preserve the original frozen and publication evidence;
  retain all source files and create an exact retained ref before the existing release flow.
- Publish additive CLI contract 37 and recovery capability 1; keep storage layout 4 unchanged.

## 1.11.0

- Continue from the active 1.9.4 runtime and combine its guarded `archive-retained`
  flow with the local v1.10.0 candidate's namespace migration recovery and storage
  layout v4 declaration. The v1.10.0 tag remains an unactivated candidate.
- Publish CLI contract 36 with separate software, capability-set and storage-layout
  version checks during release validation and installed-runtime activation.

## 1.10.0 (local candidate, never activated)

- Close the 1.8.0 namespace migration runtime audit defects while keeping
  `namespace-migration/v1` and migration capability 1. A `PREPARED`
  receipt pins document digests, lease conversions and policy source for exact CAS
  replay, including after post-switch Controller writes. Converted leases publish complete,
  interrupted scratch does not change plan identity, and leftover legacy paths block
  relocation evidence. Plan and verify expose unsettled recovery; ten fault points cover
  conversion, rewrite, policy and settlement windows.
- Recheck Governance v1.1 doctor environment classification, lifecycle observation and
  read-only gc-plan without changing their released capability versions. Retain the
  already active 1.9.2 registration-prune and 1.9.3 delivered-closeout paths. Retirement,
  execution and release contracts remain pinned.
- Advance storage layout from v3 to v4 solely to declare the existing
  `registration-prune-receipts/` directory. It is write-once terminal and audit evidence,
  included in namespace migration content digests and moves; no data relocation occurs.
- Include the CLI contract in the installed package and source distribution. Remove
  site-specific rollout defaults; deployment source and governed repositories are
  explicit, and a standard temporary-directory fallback removes the private helper
  requirement. No production activation or worktree GC was part of this candidate.
- Add the Apache License 2.0 text and SPDX package metadata for public distribution.

## 1.9.4

- Add reviewed `archive-retained` for a clean, owner-held `RETAINED` feature or audit
  whose unfinished commits remain on its exact local branch. The dry-run plan binds
  the owner, actor, HEAD and metadata; leases, runtime use, integration obligations,
  unknown ignored content and unsafe resources block the transition. The immutable
  receipt explicitly asserts no task completion, integration readiness or publication.
- `ARCHIVED` keeps the task authority fenced until the existing guarded `gc-plan` /
  `gc-apply` release removes only its physical checkout. The branch and prior task
  fields stay intact. `reopen` returns an unreleased archive to `RETAINED`; doctor
  rejects changed archive evidence. CLI contract 35 adds `retained_archive_version` 1.

## 1.9.3

- Add reviewed `close-delivered` for clean, attached `RETAINED` feature/audit checkouts.
  The checkout HEAD and historical commit identities must be contained in the live
  publication branch; leases, runtime use, unclassified content, resources, frozen
  handoffs and integration obligations block closeout. An exact dry-run plan binds
  the recorded owner's release authority and named governance actor.
- Closeout records immutable evidence and moves only lifecycle metadata to
  `RELEASE_READY`, preserving original task fields and making no business-completion
  claim. Physical deletion remains the existing `release`/`gc-apply` flow, which
  verifies the receipt and rechecks publication and runtime facts. Partial metadata
  closeout retries retain the first receipt. Doctor rejects missing or drifted
  closeout evidence. CLI contract 34 advertises `delivered_closeout_version` 1.
- Recognize a completed historical acquire after a later frozen handoff when the
  original acquisition, unchanged base, ancestry, allocation binding and exact
  task/handoff/Finalizer HEAD agree. Subsequent guarded release still requires its
  exact receipt. Original journals remain unchanged; partial or drifted evidence
  continues to block rollout.

## 1.9.2

- Allow an owner-reviewed `gc-plan`/`gc-apply` to remove the Git registration of a
  physically missing, detached, no-change `RETAINED` audit. The audit must have no
  task commit, lease, resource, external binding or integration intent, and its HEAD
  must remain reachable from the publication branch. The Controller uses a plan ID,
  a scoped non-force Git removal, a recoverable transaction and an exact receipt;
  ordinary checkout release still uses the existing ReleasePlan/ReleaseReceipt path.

## 1.9.1

- Recognize an interrupted acquire as historically terminal when the same allocation was
  subsequently released through Controller, its exact release receipt and retained branch match,
  and the checkout and binding are absent. The original journal remains immutable; missing or
  conflicting evidence still fails closed.

## 1.9.0

- Add Governance v1.1 observation for long-running multi-Agent checkouts. `status` and
  read-only `gc-plan` report owner, lifecycle, session, writer lease, lease-renewal evidence,
  stale/orphan findings, candidate reason, environment health and blocking facts. These
  observations do not change lifecycle state, release eligibility or resource admission.
- Separate Controller authority/lock faults from filesystem permission failures and sandbox
  namespace/mount mismatches in `doctor`. Probe repo, `.git`, Git common dir and lock path in
  the caller and, when selected, another process namespace; unverified evidence is `UNKNOWN`.
  Environment evidence takes precedence over lock attribution. The diagnostics do not repair
  mounts or infer the sandbox source.
- Retain the existing single-target `gc-apply` route through guarded `release`, its ReleasePlan
  and ReleaseReceipt. No automatic GC, new deletion channel, storage migration or persisted
  lifecycle state is introduced. Governance capability 2, doctor mount capability 2, CLI
  contract 33 and `worktree-governance/v2` cover the expanded read-only plan; the existing
  `gc-apply` result remains `worktree-governance/v1`. Storage layout 3, metadata schema 4,
  retirement and execution contracts remain unchanged.

## 1.8.0

- Publish the legacy namespace migration as a formal, idempotent, per-repository verb:
  `namespace-migration-plan`, `namespace-migrate`, `namespace-migration-receipts` and
  `namespace-migration-verify` move
  `<git-common-dir>/codex-worktree/v1` to `<git-common-dir>/worktree-controller/v1`
  (new contract section `namespace-migration/v1`, new capability
  `namespace_migration_version` 1, contract version 31, storage layout version 3). A
  caller never copies, edits or deletes namespace JSON: nothing in the runtime
  discovers a namespace, so an unmigrated repository keeps reading as unmanaged.
- Pin the five shapes the migration can meet, and make each one a decision rather than
  a guess: legacy-only content moves (`NAMESPACE_MIGRATION_REQUIRED`); a repository with
  no legacy namespace is a no-op that writes nothing (`NAMESPACE_ALREADY_CURRENT`);
  equivalent content in both namespaces converges by clearing the redundant legacy
  copies (`NAMESPACE_CONVERGENCE_REQUIRED`); differing content in both fails closed
  (`NAMESPACE_CONFLICT`); and legacy evidence the Controller cannot trust - a non-JSON
  document, a symlink, an unreadable record, a missing `repo.lock`, no `repo.json` in
  either namespace, an unfinished journal - fails closed before any write. A converged
  namespace must also be readable: a destination writer lease still holding the
  historical `holder.provider` schema is rewritten into schema 2 rather than kept,
  because byte-equality alone would have stranded the repository behind a parser that
  refuses it.
- Fence the transition on the destination `repo.lock` rather than on the caller's word.
  `adopt`, `acquire` and `integration-enqueue` bootstrap metadata under that same lock,
  so creating and exclusively holding it for the whole transition is what stops a
  concurrent verb minting a second repository identity beside the tree being moved; the
  new conflict test proves the bootstrapped identity is then refused, not merged. The
  legacy lock is taken without waiting, because a caller holding it is an activation in
  flight and blocking there would invert that flow's lock order; the migration fails
  closed with `NAMESPACE_BUSY` instead. Read-only verbs never queue.
- Make the plan identity a compare-and-set that survives its own execution. The plan id
  digests the merged content of both namespaces, so a move does not change it; it is
  re-derived inside the fence, it is the receipt filename, and an interrupted migration
  therefore replays under the same id and reports the original receipt instead of
  starting a second migration. `repo.json` is the only file that makes a namespace
  authoritative, so it lands last by rename and is re-digests before and after the
  switch.
- Treat interruption as a supported state. A write-once `PREPARED` receipt is filed
  before anything destructive; each document moves only while its planned bytes still
  match; the legacy namespace is removed only when demonstrably empty
  (`NAMESPACE_RESIDUE` otherwise); and the frozen
  `metadata-reference-relocation/v1` record is written only after the old root is gone,
  which is when the reference resolver requires it. Five fault points
  (`namespace_receipt_prepared`, `namespace_entries_moved`, `namespace_switched`,
  `namespace_record_written`, `namespace_receipt_settled`) are each crashed in a
  subprocess and replayed to the same finished state, and receipts stay `0400`
  documents no later run rewrites.
- Gate the published contract against the implementation, so a declared-but-unenforced
  value fails: scenario codes, the identity-field tuple, the refusal codes the module can
  actually raise, the result codes it can report, the four verbs and the five fault
  points are all compared to `namespace_migration.py`, and the capability value is
  checked against the live `capabilities` payload.
- Keep the released surface intact. No retirement semantics changed: plans, grants and
  receipts move byte for byte, `branch-retirement/v1..v3` labels and the
  `branch_retirement_*` capabilities are untouched, and a test walks a released branch's
  persisted plan through the move and still resolves it. Layout version 3 is additive
  (`namespace-migrations` only), and the historical namespace name stays an explicit
  caller argument that is validated against the released names.

## 1.7.0

- Close the retirement execution window with a versioned grant protocol:
  `branch-retirement-authorize`, `branch-retirement-complete` and
  `branch-retirement-executions {list,inspect,abandon}`
  (`branch-retirement-execution/v1`, new capability
  `branch_retirement_execution_version` 1, contract version 30,
  `branch-retirement/v3`). A grant is minted while the Controller holds `repo.lock`
  exclusively, is single-use per plan and operation, is content-addressed over an
  enumerated identity tuple with the execution token excluded, and binds both the
  `controller_state_digest` and an `observed` vector of the literal Git facts that
  digest cannot see (watched ref to OID, checked-out paths, upstream ref and
  target, classification, blockers). `complete` re-reads refs, re-derives the
  digest and re-evaluates governance inside its own exclusive section, and files a
  receipt only for the one authorized delta: the target ref moving from its
  expected OID to absent.
- Remove the self-attested completion sink. `branch-retirement-record` previously
  turned any well-shaped consumer JSON into a governance receipt without calling
  `_evaluate`, without reading a ref and without any lock-internal validation. It
  now refuses a completion whose ref the Controller can still see
  (`EXECUTION_OBSERVATION_MISMATCH`), refuses an authority or occupancy change
  after the plan (`EXECUTION_GOVERNANCE_MOVED` over the published
  `authority_blockers`), and refuses an operation holding a grant
  (`RETIREMENT_EXECUTION_ACTIVE`). Receipts carry `provenance`:
  `AUTHORIZED_EXECUTION`, `CONTROLLER_OBSERVED`, or `CONSUMER_CLAIM` for history
  filed before this contract. Reproduced on the released tree and re-run here:
  `verify EXECUTABLE` then a plain `git worktree add` then `git update-ref -d`
  (rc 0, orphaning a live checkout, because `update-ref` has no occupancy guard)
  then `record` returned `BRANCH_RETIREMENT_RESULT_RECORDED` with a receipt on
  disk at 1.6.0, and now returns
  `REFUSED EXECUTION_GOVERNANCE_MOVED | authority changed after the plan:
  BRANCH_CHECKED_OUT` with no receipt file.
- Never launder an unobserved deletion. `abandon` requires the execution token,
  writes immutable `<authorization-id>-abandoned.json` evidence through the
  existing write-once evidence idiom, and never files a receipt: holder liveness
  is reported by `inspect` as information only, because a flock dies with the
  process while the mutation may never have run.
- Keep the fence honest and narrow. `RETIREMENT_AUTHORIZATION_ACTIVE` is computed
  at the verb layer for the granted operation only, so a local grant leaves
  `branch-retirement-verify --operation remote`, `-status` and re-planning on
  their released payloads; `verify` keeps its four verdicts and its eight
  `minimum_revalidation` names. Grants live in a new
  `branch-retirement-authorizations` directory that is deliberately outside the
  state digest, so a grant can never invalidate the plan it authorizes.
- Make the execution surface provider-neutral. Core speaks
  `retirement-execution-result/v1` (`retirement_execution.py`) and knows nothing
  about a consumer product: released argument-array generation and summary parsing
  moved to `retirement_adapters.py`, `plan` now publishes a neutral `execution`
  recipe alongside the released `finalizer` array, and only `CAS_DELETE` is
  grantable while `ATTEST_ABSENT` remains a translated legacy claim.
- Make the published contract consumable and gated. `branch_retirement_contract`
  v3 publishes the authorization identity fields, canonicalization, token and
  scope rules, binding, settlement, abandonment, authority blockers, outcomes,
  kinds, fault points and the `not_claimed` limits. New gates compare the running
  implementation against the published lists, capabilities **values** (not just
  key presence) against the contract, and the storage layout against
  `MetadataStore` introspection instead of a hand-copied tuple, so an
  undocumented directory can no longer ship silently.
- Give the grant TTL the lease doctrine already states: expiry means the claim needs
  verification, never that the work is done. An expired grant still fences occupancy,
  still has to be settled or abandoned with its token, and a settlement that lands
  after expiry is recorded as `grant_expired_at_settle` instead of being refused, so
  a slow remote compare-and-delete can still be attested honestly.
- Publish the fence scope instead of overstating it: the allocation half is enforced
  at `MetadataStore.write_record`, the single chokepoint every new binding passes
  through (gated by an introspection test that fails if a second binding writer or a
  removed guard appears), while the branch half relies on `acquire`'s existing
  `BRANCH_EXISTS` rule plus the settlement observation vector, because a metadata
  record carries no branch name.
- Prove the contract against a real consumer without making the default suite depend on
  one: `tests/test_branch_retirement_git_finalizer_acceptance.py` drives the shipped
  `git-finalize` retirement path (dry-run) and its companion verifier against a plan
  from this tree, asserting acceptance, drift rejection and the
  `branch_retirement_version == plan.schema_version` pin, and is skipped unless
  `WORKTREE_CONTROLLER_GIT_FINALIZER` and `WORKTREE_CONTROLLER_GIT_FINALIZER_COMPANION`
  name an installation. Both cases pass here, and the companion verifier still accepts
  while a grant is outstanding.
- Gate the newly published vocabulary rather than merely listing it: every
  `compatibility_error_codes` entry must be raisable in the source,
  `authority_blockers` must equal the implementation set, `result_contracts` and
  `document_contracts` are now declared and cross-checked, capabilities **values**
  (not only key presence) must equal the contract, and consumer wire codes are held
  out of core by a shrink-only allowlist.
- `storage_layout.version` 1 to 2: exactly one additive directory
  (`branch-retirement-authorizations`); every v1 name, file identity and record
  schema is unchanged and nothing relocates. `branch_retirement_version` stays 1
  because the deployed consumer compares it against `plan.schema_version`; the new
  surface is detected through `branch_retirement_execution_version` and
  `contract_version` 30, so persisted schema-1 plans and released receipts keep
  verifying.


## 1.6.0

- Publish the branch retirement contract as `branch-retirement/v2` (contract version 29, new
  `branch_retirement_verify_version` capability): the plan identity field list, its canonical JSON
  encoding, the `controller_state_digest` recipe (authority directories and files, the relevance
  filter, the separator order and the `.agents/worktree-policy.toml` byte component), the
  blocker-to-verdict policy and the minimum revalidation set are now declared where consumers can
  pin them, so a consumer no longer mirrors Controller governance code.
- Add `branch-retirement-verify --plan-id <sha256> --operation {local,remote}`: the Controller-owned
  pre-mutation verdict for one operation. Under the shared repository lock it returns
  `RETIREMENT_EXECUTABLE` with the exact CAS fields (`plan_id`, `branch`, `expected_oid`,
  `expected_integrated_oid`, `integrated_ref`, observed ref state, `controller_state_digest`),
  `RETIREMENT_STALE` when the branch OID or Controller/policy authority moved, `RETIREMENT_BLOCKED`
  for checkout, writer/runtime, intent, transaction, classification or `ABSENT_UNATTESTED`
  occupancy, and `RETIREMENT_OPERATION_COMPLETE` once an immutable receipt exists.
- Keep retirement plans and receipts out of the state digest so recording the local result never
  invalidates the remote half of the same plan, and treat an unattested absent ref as blocked until
  a Finalizer summary proves the idempotent already-absent path.
- Keep `branch_retirement_version` pinned to the persisted plan schema instead of raising it with the
  contract surface: the deployed Git Finalizer retirement verifier compares that capability against
  `plan.schema_version`, so raising it alone rejects every still-valid plan. Detection of this
  release moves to `branch_retirement_verify_version` and `contract_version`.
- Persisted retirement plans stay at schema 1 and the existing `branch-retirement-plan`, `-status`
  and `-record` result payloads are unchanged, so 1.5.0 consumers keep working; verify is additive.

## 1.5.0

- Refuse unbounded host execution scope at the producer: `host-execution
  --action admit` now stops with `INVALID_REQUEST` when the resolved target is
  the home root or one of its ancestors (including symlink aliases), mirroring
  the recovery-side refusal, so home-scoped reservations cannot be minted again.
- Add the read-only `host-execution --action inspect` contract: per uid-owned
  execution reservation it publishes the canonical holder bytes and their
  SHA-256 (the formally documented sorted-key compact JSON representation), a
  live dead-holder observation, target observation (present/absent/identity
  changed), accounted-memory effect, and the terminal-evidence template that a
  typed clear must present — recovery callers no longer read the private ledger
  layout, and inspection writes nothing. Host execution admission capability is
  2; contract version 27.
- Release orphaned host reservations through typed authority: `clear
  --recover-dead-holder` now also settles a reservation whose target is
  provably absent, recording `target_observation: "ABSENT"` in the single
  immutable `host-recoveries/<request-id>.json` receipt; every other action
  against a missing target fails closed with `RESOURCE_IDENTITY_CHANGED`
  instead of raising an unhandled error. Replaying identical recovery authority
  after a crash between evidence write and ledger settle reuses the original
  immutable receipt; differing bytes remain refused.
- Make the repository lock authoritative for every writer: an exclusive request
  against a missing, symlinked, non-regular, or unopenable `repo.lock` stops with
  `MISSING_REPOSITORY_LOCK` / `REPOSITORY_LOCK_INVALID` instead of proceeding
  without serialization, the lock is opened with `O_NOFOLLOW`, and `doctor`
  reports both states as blocking findings with the file path. Only a genuinely
  missing lock keeps the shared read path open so `doctor` can still diagnose;
  adopt, acquire, and enqueue-integration remain the bootstrap paths that
  recreate the arbiter.
- Classify interrupted `acquire` and `integration-refresh` journals from positive
  current-state evidence only: the completion shapes (`ACQUIRE_EVIDENCE_SUPERSEDED`,
  `REFRESH_EVIDENCE_SUPERSEDED`) require record, binding, worktree registration,
  checkout directory, and branch ref to agree with the journal, and the
  no-effect shapes require the total absence of those facts. Half-shapes (orphan
  worktree, dangling detach, moved branch, mismatched record, residual checkout
  directory) stay fenced and fail closed; reftable refs storage or unreadable
  evidence disables filesystem inference. Journals are never rewritten or
  deleted to lift a fence, classification runs only under a held (or explicitly
  delegated) repository flock, and branch-retirement `TRANSACTION_ACTIVE` now
  follows the same pending/terminal semantics as `doctor` and admission.
- Debris from interrupted atomic writes (dot-prefixed `.tmp` siblings inside
  `transactions/`) no longer fences conflicts, `doctor`, rollout checks,
  activation layout migration, or the frozen activation gate; `doctor` reports
  them as a non-blocking count.

## 1.4.0

- Release a historical host execution reservation whose own recorded runtime identity proves it
  is dead, but only through an explicitly typed call that presents that reservation's holder
  digest plus matching terminal evidence, and records an immutable recovery receipt first. The
  unbounded home-root refusal, live holders, process groups, writer leases, recycled-PID
  ambiguity, and unknown boot identity all stay fail-closed.
- Let a retained `resource-clean` journal stop fencing when exact immutable release evidence
  overtakes it: the allocation is RELEASED, one receipt binds this repository, allocation,
  target and record version, and the resource is neither present nor administratively
  registered. Journals, phases and receipts are never rewritten, and any missing or
  contradictory piece leaves the journal pending.

## 1.3.1

- Recover a terminated host execution reservation by its exact runtime identity instead of
  treating unrelated activity on the same repository or canonical path as its occupancy. Live
  holders, process groups, writer leases, uncertain identities, and unbounded home-root targets
  remain blocking.

## 1.3.0

- Add released-branch retirement planning with explicit integration ancestry, exact local/upstream
  OIDs, protected/recovery exclusions, and allocation, writer, runtime, transaction, and intent
  occupancy gates.
- Bind Git Finalizer execution to an immutable Controller-state digest and record local and remote
  retirement results independently for idempotent partial completion.
- Classify cherry-equivalent, merge-only, unique/mixed, and historical unregistered refs as
  report-only in the first contract version; Controller never deletes Git refs.

## 1.1.1

- Close exact archived source as PRESERVED without claiming business completion; recheck archival and closeout receipts during cleanup and release.


## 1.1.0

- Replace checkout/writer quantity rejection with shared filesystem and execution
  budgets, atomic cross-repository reservations and durable no-writer waiting.
- Track actual process occupancy through existing runtime bindings, including safe
  descendant transfer and host executions without a repository writer lease.
- Settle checkout budgets at formal release, renew stale intents through current
  authorization checks, and keep independent nonterminal resource scopes separate.
- Route receipt-authorized nested build/environment cleanup through the existing
  typed coordinator before a fresh release plan; unknown content still blocks.
- Negotiate v1/v2 consumers by behavior and reject rollback that cannot preserve
  current resource requests; retain exact preservation and append-only recovery.
- Enable receipt-authorized feature closure and periodic reconciliation for this
  repository without enabling automatic integration or preserved-work release.

## 1.0.1

- Classify retained terminal journals by exact evidence while keeping repository-wide
  nonterminal mutation conflicts fail closed.
- Reconcile tracked-source attempts refused before action through append-only evidence
  and a new single-use attempt identity; preserve the original refusal and receipts.
- Bind immutable cleanup references across the explicit 1.0 layout migration; admit only
  verified terminal or exact no-action state and refuse rollback after new cleanup evidence.

## 1.0.0

- Replace provider-named executables, package and repository namespaces with the
  neutral Controller, session launcher and explicit client/session/run contracts.
- Remove executor adapter families and the retired advisory compatibility shim.
- Use current task ownership for reconciler separation and apply the same consumer,
  provenance and pointer-CAS gates to activation and rollback.
- Preserve 0.9.7–0.9.9 cleanup recovery, tracked-source preservation, immutable
  receipts, task ownership and release gates.
- Add an explicit, fenced policy/state layout transition to governed activation,
  preserving writer identities and historical receipts with exact rollback and delayed
  entry-point retirement after client acceptance.

## 0.9.8 - 2026-09-13

- Preserve resume refusals and exceptions before coordinator diagnostics; absent completion
  receipts return an explicit incomplete result without constructing a receipt path.
- Distinguish coordinator attempts from destructive actions. An explicit append-only
  no-action reconciliation requires unchanged resources, topology, preservation and old
  evidence plus clear runtime guards, then permits a new attempt identity without replaying
  completed backend cleanup. Old started records remain intact.

## 0.9.7 - 2026-09-12

- Record complete cleanup-scope hardlink topology before destructive actions. Unknown external
  links and topology drift block cleanup; exact recorded unlink effects preserve nlink/ctime
  verification across resource boundaries. Each action retains immutable before/after evidence.
- Recover the narrow legacy backend-completed transaction shape by fully re-preserving current
  state in a new generation. Historical causality remains NOT_PROVEN. Recovery, execution and
  completion records are append-only; the old journal and receipts remain byte-identical and
  completed backend cleanup is never replayed.
- Let the versioned staged runtime verify an exact new preservation baseline for activation;
  retain all other rollout blockers. Terminal journals require verified append-only completion.

## 0.9.6 - 2026-09-12

- Preserve the complete legacy `.venv/agent-workspace` state in an immutable generation with
  source/destination manifests and byte digests. Regular files remain opaque, symlinks are not
  followed, and FIFOs are preserved as filesystem nodes; sockets and devices remain blocked.
- Add exact preservation receipt cleanup, separate from ordinary disposable types. Bind the
  receipt SHA-256, allocation, source and generation identities; recheck both trees before
  FD-bound deletion and retain the generation through the separate guarded allocation release.
- Record cleanup action progress, failures, post-scans and runtime visibility warnings in
  immutable evidence. Partial failures retain the transaction and prevent release.

## 0.9.4 - 2026-09-10

- Upgrade the Controller without draining provider sessions. A running Codex, Claude or Agent
  Workspace session is no longer evidence of danger, so the rollout gate stopped counting
  processes and started checking whether every effective caller can keep working across the
  switch. It now blocks only on real unsafe conditions: an old or missing adapter, an entry
  point that is not version pinned, a target version that is not staged, a Controller command
  currently running, a Controller mutation inside its critical section, an incompatible Agent
  Workspace capability, an Agent Workspace writer session admitted under the old contract, and
  a recorded writer identity that a surviving session would need but that cannot be
  reconstructed. Live providers and surviving writers are reported as inventory instead.
- Separate installing from activating. `scripts/controller-activate` stages one more versioned
  runtime beside the ones already installed, changing nothing about who is in charge, then
  takes a short lock only around an atomic repoint of a single symlink. `scripts/codex-worktree-runtime`
  is the pinning entry point: it resolves the active runtime once per invocation, so a command
  already running finishes on the version it started with and one invocation can never mix two
  versions. No daemon, no database, no second authority.
- Adapt the one legacy request shape that a session admitted before activation still sends.
  `codex-worktree-writer-lease release <path> <holder>` carries no provider and no session
  identity, which the strict contract rejects, and the lease would leak. The adapter now
  reconstructs the strict request from the lease the Controller already recorded, and only
  after the Controller agrees that the holder the caller names is that recorded holder. A
  release that names no holder, names a different one, or cannot resolve it is refused.
- Stop partitioning ordinary worktrees into two pools that cannot lend to each other. Feature and
  audit checkouts and retained checkouts were separate physical sub-quotas, so parking could fail
  with `BLOCK_RETAINED_SLOT` even though it creates no worktree at all, and a repository could be
  refused with capacity to spare. They now share one bounded pool of `feature_or_audit + retained`
  checkouts, exactly the total policy already approved, while writer concurrency stays separately
  bounded by `feature_or_audit`. Parking and automatic retention take no capacity and can no longer
  be refused for want of it; a genuinely full pool refuses with `BLOCK_ORDINARY_POOL` and says so.
  Policy schema 1 through 4, which have no retained pool, are unchanged.
- Give status, plan, acquire, resume and the reconciler one capacity model. `capacity_accounting`
  counts six things that were previously conflated: registered worktrees, writer entitlements, live
  executions, legacy allocations reserved conservatively because no lease proves them idle, runtime
  bindings, and cleanup-pending work. Every plan decision now reports the numbers it decided on.
- Admit one task that needs several repositories all at once or not at all. `acquire_across`
  preflights every repository, acquires in one deterministic order keyed by Git identity, and gives
  back what the attempt created if a later repository refuses; it is limited to ordinary work,
  because that is what it can give back. A refusal names the repository, and a rollback names the
  branches it left behind.
- Let a waiter wait a bounded time instead of racing. `acquire_when_free` retries the ordinary
  guarded acquisition until a deadline, holding no worktree, lease or reservation while it waits, so
  two waiters for one slot resolve to one admission and one explicit `WAIT_EXPIRED`. A refusal that
  waiting cannot change is raised immediately and never queues behind anything.
- Finish the automatic terminal release instead of ending it at the first unclassified file.
  A task's own reproducible environment is ignored content until someone classifies it, and
  `IGNORED_CONTENT` was not a retryable release blocker, so the release intent that a verified
  publication had just earned went straight to `STALE`. Nothing retried it, the classification that
  would have cleared it was refused after handoff, and registering it later cancelled the very
  intent that was waiting for it. One finished task then held a feature slot forever, and admission
  reported the repository as full. Unclassified content is now retryable, the classification is
  reachable from the state the flow actually reaches, and registering a resource carries the intent
  to the new record version instead of discarding its evidence.
- Report a publication and its cleanup separately. `lease-complete` raised out of the whole call
  when the candidate checkout could not be cleaned, so a verified remote publication read as a
  failure. It now returns `PUBLICATION_COMPLETED` with a `cleanup` result, still runs the source
  trigger so its answer says what the sources are waiting on, and a published candidate may classify
  what it created so the pending cleanup can finish. Only a recoverable blocker becomes a pending
  cleanup; drift, identity and unknown-work failures still stop the call.
- Give capacity pressure the same convergence as ordinary completion. Admission previously retried
  only writer retention, which reclaims a dead writer but never releases finished work. On
  `BLOCK_FEATURE_SLOT` it now makes one bounded pass, eligible terminal releases first and writer
  retention second, then retries acquisition once. A metadata-only check decides whether a release
  pass is worth starting, so the exclusive repository lock is never held across a scan with nothing
  to converge. Live writers, external bindings and unverified legacy allocations still refuse.
- Stop ending a release intent because a scan could not finish. `RUNTIME_SCAN_ERROR`,
  `RUNTIME_SCAN_LIMIT`, `ENVIRONMENT_SCAN_ERROR` and `ENVIRONMENT_SCAN_LIMIT` describe the machine at
  that moment, not the allocation, and a busy host makes them likeliest exactly when capacity is
  under pressure. They are retryable like the other environment blockers.
- Read the rollout gate's writer inventory against allocation lifecycle. The gate scanned the
  lease directory alone, so a lease left by an allocation that had already ended still read as
  a surviving writer and, for a provider that re-acquires during a session, still refused the
  switch. That could not clear: the mechanism that removes such a lease ships in the version
  the gate was refusing to activate, and a repository that does not delegate reconciliation has
  no other way to converge. A lease whose allocation is released or gone is now reported under
  `stranded_entitlement` as inventory and never blocks; a lease belonging to a live allocation
  is unchanged.
- Stop leaving writer entitlement behind when an allocation finishes. A lease is only ever
  granted to an ACTIVE allocation, but `park` and `release` left the lease file in place, so a
  released task kept a recorded writer whose holder process was often still alive. Everything
  that reads the lease directory saw it: the rollout gate reported surviving writers for tasks
  that no longer existed and refused to activate a new runtime. `park` and `release` now drop
  the entitlement they end, and `reconcile-writers` reports and clears entitlement whose
  allocation is released or gone, so existing residue converges without a metadata edit. The
  sweep is terminal-only on purpose: a parked or frozen allocation can still return to ACTIVE
  and is someone's unfinished lane, so it is reported by liveness rules as before and never
  swept.
- Read the temporary-directory helper's reply as the JSON record it is. `controller-activate`
  treated the whole reply as a path, which created a directory tree named after the JSON
  instead of using the directory the helper had made. Found while staging the first real
  rollout.
- Add `codex-worktree writer-session describe <path> [holder]`, the read-only projection that
  reconstruction reads. It reports the governed target, the recorded lease identity and whether
  a named selector is that holder. It grants nothing, mutates nothing, and a host-managed
  context cannot turn it into an admission.

## 0.8.4 - 2026-09-08

- Report the same capacity arithmetic in a preview as in a real pass. `writers_after` and
  `retained_before` were derived from committed reclamations, so a dry run reported
  `writers 11 -> 11` for a pass that would free five slots. A preview whose numbers differ from
  reality is worse than no preview. Both now derive from what the pass decided, and `would_reclaim`
  reports the count a preview would apply while `reclaimed` stays the count actually written.

## 0.8.3 - 2026-09-08

- Let a dry run preview the legacy classification. `--adopt-legacy` was applied only when a pass was
  already mutating, so the read-only report showed `LEGACY_UNVERIFIED` for allocations the real pass
  would reclaim - no preview for the one operation that most needs one. The preview now classifies
  exactly as the real pass does, still writes nothing, and honours the bounded retained pool, so the
  reported before/after capacity is what actually happens.

## 0.8.2 - 2026-09-07

- Add `reconcile-writers --only TASK_KEY` (repeatable) so convergence can be restricted to named
  allocations. A one-time legacy classification is rarely safe for a whole repository at once: the
  allocations that can be positively tied to a live session, or that show recent work, have to be
  excluded by name rather than swept up with the rest. Unnamed allocations are still inspected and
  reported, and are left exactly as found.

## 0.8.1 - 2026-09-07

- Count writer and retained occupancy from registered checkouts rather than from every historical
  metadata record, so `reconcile-writers` reports real capacity instead of including long-released
  tasks. Report `legacy_unverified` alongside `reclaimed` and `adopted` so a one-time classification
  can be planned from the read-only output.

## 0.8.0 - 2026-09-07

- Separate temporary write entitlement from durable task identity. An allocation records who owns
  a task and preserves its work; a **writer lease** records which live process may currently write
  through it. Holder identity is `(boot id, pid, kernel start ticks)`, optionally strengthened by a
  provider session id, so a recycled pid can never satisfy a lease written for its predecessor and
  a lease can never survive a reboot. Adds `writer-lease-acquire` and `writer-lease-release`.
- Add `reconcile-writers`, one shared reconciliation engine used by every trigger. It answers a
  single question - can this writer entitlement be proven inactive right now - and a proven-dead
  writer only ever moves `ACTIVE` to `RETAINED`. It never completes a task, publishes, finalizes,
  deletes a checkout, or touches uncommitted work. Read-only by default; `--apply` converges.
- Reclamation evidence, in the order it is checked: role, lifecycle, external bindings, publication
  lease, integration intent, recovery state and worktree drift all block first, so a blocked
  allocation reports the real obstacle. Then a live process anywhere in the checkout outranks the
  lease record, a surviving child of a dead wrapper keeps the slot, and lease expiry means the
  claim needs verification rather than that the work finished.
- Preserve pre-lease allocations fail-closed: absence of a lease is never treated as evidence of
  inactivity. `--adopt-legacy` performs the one-time classification, and only when policy allows it.
- Delegate convergence through policy schema v6 `[reconciler]` to a distinct system actor that can
  never be a task owner. Repositories below schema v6 keep the previous behaviour exactly, so a
  newer Controller can be installed before any policy is migrated.
- Run one bounded reconciliation pass with exactly one retry inside `acquire`, and only when the
  writer pool is what blocked it. `plan` stays read-only, and integration, emergency, recovery and
  physical limits refuse exactly as before.

## Unreleased

- Repair omitted committed source-task paths through explicit owner-reviewed scope additions.
  Bind amendments to clean exact HEAD and record CAS, retain before/after scope and review reason,
  and reject scope broadening beyond the actual task diff or frozen integration evidence.

## 0.7.4 - 2026-09-07

- Report capacity the way admission already grants it. `acquire` counts feature/audit *writers*
  (`lifecycle != "RETAINED"`), but the read-only surface still counted every registered checkout:
  `plan` raised `BLOCK_DRIFT` from `FEATURE_CAPACITY_VIOLATION`, and `normal_slots_remaining`
  charged retained checkouts to the normal budget. A repository that parked a task and admitted its
  replacement therefore read as out of capacity while `acquire` was still granting slots. The
  enforcing path was always correct, so nothing was wrongly admitted, but the documented
  `status`/`plan` step reported a repository as full when it was not.
- Add `RETAINED_CAPACITY_VIOLATION` so the bounded retained pool is policed like every other role
  pool, and report `retained_limit`, `retained_used` and `retained_slots_remaining` so retention is
  visible rather than inferred. Policies below schema 5 cannot hold a retained allocation, so their
  counts and warnings are unchanged.

## 0.7.3 - 2026-09-07

- Recognize the explicit `PREPARATION_ABANDONED` event as effective `ABANDONED` in
  diagnostics and integration reconciliation. A formally abandoned preparation no longer
  fails `doctor` or recreates attention events; an unexplained missing allocation remains
  blocked. Stored intent schema, source evidence and failure history are preserved.
- Make repeated `integration-abandon` calls preserve the original terminal event and reason
  without adding history or changing source allocations.
- Allow a delivered hotfix to use the existing owner- and identity-checked `reopen` flow so
  its owner can register an overlooked reproducible environment and hand off again. Unknown
  ignored content, teardown guards and retained branch requirements remain unchanged.

## 0.7.2 - 2026-09-07

- Add `codex-worktree-session-end`, a provider-neutral entry point a direct Codex or Claude CLI
  session (or an editor hook) calls on exit. It resolves the allocation that owns a working path
  and routes it through `reconcile-session`, so an unfinished task is retained and never treated
  as complete. It refuses canonical, missing metadata, detached, locked, prunable, externally
  bound, integration-bound, non-feature/audit and non-terminal cases, and it stays advisory at the
  process boundary so a session exit is never turned into a failure.
- Add `adopt-delivered`, a recorded ownership transfer for an allocation whose deliverable is
  already contained in the publication branch. It requires `HANDOFF_READY`/`PRESERVED`, handoff and
  Finalizer evidence, handoff commit equal to HEAD and contained in the publication branch, a clean
  worktree, no external binding, no registered disposable resource, no publication lease and no
  integration intent, under a record-version check. Unfinished, dirty, bound or uncertain
  allocations stay refused, so the ownership gate is unchanged for live work.
- Add `integration-abandon`, which terminates an intent whose preparation is already gone so it
  stops holding a scope claim, preserving its sources and failure reason. An intent that holds no
  allocation and records a blocker no longer fences later intents, which unblocks a queue after a
  crashed or aborted preparation.
- Prefer a live allocation over a released one when both carry the same handoff commit, so a source
  released before integration still resolves while a live source is unambiguous.
- Add `reconcile release-plans`, which retires a spent release plan only when its allocation is
  `RELEASED` and a durable release receipt exists. This repairs a repository whose plan carries a
  lifecycle an older reader rejects, without hand-editing metadata.

## 0.7.1 - 2026-09-07

- Add `reconcile-session`, the terminal reconciliation entry point a direct Codex/Claude CLI
  session uses on exit and the one Agent Workspace already calls on task-terminal and
  session-archive. Stopping is never completion: an unfinished task is only moved to `RETAINED`,
  preserving its checkout, work and branch; a task that already carries handoff evidence keeps the
  existing release-intent path. A disconnected terminal, exhausted quota, missing PID or idle
  session never implies completion.
- Add `complete-no-change` for audits and investigations that finish without producing a commit.
  It records a real completion receipt only when owner, record version, clean worktree, absence of
  any task-produced commit, absence of an untracked result, no integration or publication
  obligation, no external runtime binding and no unresolved recovery state are all proven. No
  Finalizer commit is fabricated and no receipt is borrowed.
- Add `integration-abort` so a terminally failed or owner-aborted integration preparation can
  release its temporary integration checkout. The source allocation, its branch and its handoff
  evidence are untouched, and the candidate oid, validation evidence, provenance and failure
  reason are preserved on an abort receipt and on the intent.
- `NO_CHANGE_COMPLETED` and `INTEGRATION_ABORTED` are releasable terminal states that carry their
  own completion receipt instead of Finalizer evidence; every other release gate is unchanged.
- Parking now fails closed on registered external or runtime bindings.
- `park`, `reconcile-session`, `complete-no-change` and `integration-abort` all require policy
  schema v5. A repository on schema v1-v4 can therefore never store `RETAINED`,
  `NO_CHANGE_COMPLETED` or `INTEGRATION_ABORTED`, so an older controller never meets an unknown
  lifecycle. Metadata schema stays 4 and record shape is unchanged. Migration order is explicit:
  install this release first, then raise a repository policy to schema v5.

## 0.7.0 - 2026-09-07

- Separate feature/audit **writer** admission from **retained** occupancy. Feature and audit slots
  now count allocations that are actually admitted to write; `RETAINED` allocations no longer
  consume a writer slot, so a repository holding legitimately unfinished work can still admit new
  concurrent tasks.
- Add policy schema v5 with a bounded `[slots] retained` pool. Retained checkouts keep costing a
  physical worktree (`normal_limit + emergency + recovery_reserve + retained == hard_limit`), so
  their cost stays visible and cannot grow without bound. Schema v1-v4 keep retention disabled and
  behave exactly as before.
- Add `park` and `resume`. `park` is an explicit decision by the recorded owner under a record
  version check; it never deletes, cleans, commits, or publishes anything, so uncommitted work,
  the checkout and the branch are preserved exactly. `resume` returns a retained allocation to
  writer status under normal admission, preserving task, authority, owner and allocation identity.
- Parking refuses canonical, non-feature/audit roles, non-`ACTIVE` lifecycles, a mismatched owner,
  a stale record version, a held publication lease, a bound integration intent, and a full retained
  pool. Release, handoff, ownership, single-writer, external-binding and recovery gates are
  unchanged.

## 0.6.0 - 2026-09-02

- Add `IntegrationIntentV2` with durable FIFO ordering, frozen source branch/Finalizer/Snapshot and
  scope identities, three independent preparation slots, explicit scope fencing, and one unchanged
  main-publication lease.
- Persist immutable prepared-candidate receipts and require an exact `origin/main` CAS before
  publication; stale candidates enter `NEEDS_REPREPARE` and must replay and revalidate.
- Add a durable idempotent lifecycle event outbox, explicit integration reopen fencing, the formal
  recovery phase matrix, capacity-pressure reconciliation of existing intents only, and immutable
  terminal reconciliation for historical intent drift.
- Continue reading policy schema v1-v3 and integration intent v1 records; add policy schema v4 for
  preparation capacity and Agent Workspace continuation without changing release safety boundaries.

## 0.5.1 - 2026-09-02

- Resume an interrupted registered-resource release from `VENV_REMOVED` or `PREPARED` when its
  immutable plan, transaction journal, teardown receipt, allocation CAS, retained branch, recovery
  lineage, and live worktree/reference guards still match.
- Add a read-only `RELEASE_RECOVERY_PLAN_READY` preflight for post-teardown transactions while
  keeping missing, altered, or ordinary unjournaled resource teardown fail-closed.

## 0.5.0 - 2026-09-02

- Unify ordinary dry-run and execution under one `ReleasePlan` guard path with exact HEAD and
  record-version CAS, immutable plan/release receipts, retained branches, and idempotent recovery.
- Add explicit disposable-resource contracts; `.venv` registration is fenced to `ACTIVE`, while
  unknown ignored content and unregistered teardown remain fail-closed.
- Add immutable publication-receipt reconciliation and distinguish safe historical drift as
  non-blocking `DEGRADED` doctor state.
- Add `ReleaseIntentV1`, policy v3 eligibility, targeted event/capacity reconciliation, integration
  ancestry or reviewed equivalent replay evidence, and no clean-worktree discovery or timer janitor.
- Add handoff write fencing, explicit `reopen`, active binding CAS, and automatic intent
  cancellation on record, HEAD, handoff, resource, or binding identity change.
- Close the remote-verified integration trigger after either automatic or later manual candidate
  release; Agent Workspace remains an event/binding adapter and never removes a worktree directly.

## 0.4.3 - 2026-09-01

- Add a persistent frozen-handoff integration intent queue that does not consume a worktree until a
  policy candidate slot is claimed, while retaining policy schema v2 and metadata schema v2.
- Construct and refresh exact candidate revisions from immutable source handoff OIDs plus live main
  without using the canonical checkout; stale independent candidates invalidate old validation and
  revalidate, while a semantic conflict blocks only its own intent.
- Bind publication leases to repository, target ref, holder/run, expected main, candidate, validation
  evidence, acquisition, and expiry; serialize exact Git Finalizer publication through the existing
  repository lock and recover from live remote fact without duplicate mutation.
- Persist publication receipts and metrics, release the lease before cleanup, and automatically run
  registered reproducible `.venv` teardown plus guarded candidate release after remote verification.
  Unknown content, HEAD drift, external/runtime bindings, or unsafe resource identity remain blocked.
- Freeze validated and published integration checkouts against feature drift; `PUBLISHED` candidates
  need no returning chat owner for receipt-authorized guarded release.

## 0.4.2 - 2026-09-01

- Add a read-only `write-guard` entry boundary that binds task, authority, owner, allocation role,
  lifecycle, and declared operation before repository writes.
- Refuse feature implementation in integration allocations, including after `HANDOFF_READY`, and
  route pre-existing integration drift to explicit quarantine/recovery without blocking recovery.

## 0.4.1 - 2026-09-01

- Correct the standard `CACHEDIR.TAG` signature used by reproducible `.venv` teardown validation.

## 0.4.0 - 2026-09-01

- Add `release --teardown-reproducible-venv` with a read-only preflight mode for the exact
  worktree-root `.venv`.
- Require clean releasable state, matching task/handoff/Finalizer/HEAD identity, reproducible
  project markers, no external or cross-environment binding, and no visible runtime reference.
- Persist size, lock, Python, and environment identity receipts and resume an interrupted teardown
  before the normal guarded worktree release transaction.
- Keep all other ignored content blocked and retain the released branch.

## 0.3.0 - 2026-09-01

- Separate two integration-candidate slots from the single main-publication lease.
- Require task, handoff, Finalizer, source-lineage, and path-scope consistency before an integration
  candidate can hand off or acquire publication authority.
- Add explicit `QUARANTINED` and `RECOVERY_REQUIRED` lifecycles with recovery allocation and
  externalization evidence.
- Reserve governance-only recovery capacity independently from feature and emergency capacity.
- Add registered, identity-checked cleanup for task-local Python `.venv` resources.
- Refresh the configured remote main branch and recheck ancestry at publication lease acquisition
  and completion; Git Finalizer remains responsible for the actual push.
- Continue reading policy and metadata schema v1 while writing metadata schema v2.

## 0.2.0 - 2026-08-23

- Add versioned `status`, `plan`, `adopt`, and `doctor` Shadow Mode interfaces.
- Add CAS- and journal-guarded `acquire`, `review`, `handoff`, and `release` lifecycle operations.
- Keep Git authoritative for live worktree/ref/index facts and store only lifecycle metadata under
  the dynamically resolved Git common directory.
- Fail closed on budget, duplicate authority, canonical targeting, ignored evidence, editable
  environment references, external bindings, drift, interrupted transactions, and unsafe release.
