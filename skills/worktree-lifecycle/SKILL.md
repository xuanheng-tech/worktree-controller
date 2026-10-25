---
name: worktree-lifecycle
description: "编排多 worktree 仓库任务的 authority、预算、handoff、integration 与 release；在写任务需要选择或管理 checkout 时使用。不替代 Git、Controller、Snapshot Runner 或 Git Finalizer 的实现与状态 authority。"
---

# Worktree lifecycle

## Purpose

Use this Skill to route repository work through the existing tools without merging their
responsibilities. Preserve the caller's commit, push, remote, and destructive-operation authority.

## Workflow

1. Load applicable instructions and bounded repository context; use Context Loader when needed.
2. Before repository writes, obtain `worktree-controller status --json` and run `plan` for the proposed
   `task_key`, `authority_key`, module, and role. Read the policy at `repository.policy_path` and
   prefer an existing compatible authority. Interpret `admission_strategy`,
   `checkout_limits_enforced`, resource admission, and count observations separately; do not turn
   observe-only capacity into an allocation veto or deletion authority.
3. After selecting/admitting the checkout, bind/check its writer lease against the real execution
   holder and client/session/run identity, then run `worktree-controller write-guard` with the exact
   path, task, authority, owner, holder identity, and operation. Managed clients use the advertised
   `writer-session` handshake; manual execution uses `writer-lease-acquire`. Never register an
   unrelated PID to simulate a writable session. A blocked decision is final for that attempt:
   choose a valid feature authority or use the explicit recovery flow for drift.
4. Respect the selected admission decision, repository budget, and canonical policy. Do not place
   ordinary feature work in canonical or bypass a capacity refusal. When policy is enabled,
   linked worktree creation and removal must use Controller `acquire`/`release`. If Controller is
   unavailable or metadata is corrupt, use `doctor`/recovery and fail closed rather than raw
   add/remove. Git read-only commands and branch, merge, and commit operations remain outside
   Controller mutation authority; never infer create/remove permission from absence.
   A clean frozen canonical handoff that must move to published main uses the installed
   `canonical-transition` capability with exact old record/branch/HEAD and local/remote main OIDs;
   never release canonical or switch/reset it with raw Git to evade its frozen authority.
   With `canonical_refresh_version` 1, an ACTIVE canonical that only fell behind published main
   (integrations publish from candidates) is advanced with `canonical-refresh`; it fast-forwards a
   clean, idle checkout and otherwise reports the blocker. Never pull or reset canonical by hand.
   With `canonical_main_refresh_version` 1, a handoff frozen on policy `main` or `master` uses
   `canonical-transition`, binding the original frozen HEAD and observed local main HEAD separately. Review
   exact published target ancestry and policy. Partial progress leaves main advanced and metadata
   frozen; retry with exact observed local/remote OIDs, never rewind main. Its
   `CANONICAL_MAIN_REFRESHED` history retains the old handoff without an extra baseline branch or
   fabricated release/retirement evidence.
5. Use `acquire` only with an explicit task, authority, owner, role, branch, and base ref. Feature
   tasks declare repository-relative scope. `RETAINED` work uses `resume` with exact owner and
   record CAS; frozen handoffs use `reopen` or typed recovery, not a new task on the old checkout.
   Do not reassign another owner or broaden a frozen scope implicitly. New integration work uses
   `integration-enqueue` to freeze source handoff identity without a checkout, then
   `integration-claim` when a candidate slot is available. The legacy direct acquire path remains
   available for existing controlled flows. Integration candidates must name their source
   handoff allocations, or resolve retained source evidence with `--source-handoff <commit>`;
   recovery allocations must name one quarantined source and explicitly scope the recovered feature
   paths. The Controller owns allocation metadata and worktree lifecycle, not implementation.
6. Implement and test inside the selected worktree under the actual leased writer. Stop on lease
   loss, release only this writer's exact lease when it ends, and use `park` for unfinished work.
   A missing/expired lease is not proof that another allocation is unused. Register resources while
   the allocation is still `ACTIVE`; record its exact path/type, reproducibility, creator, and
   teardown policy. Never infer disposability from an ignored path.
7. At delivery, use `$git-change-delivery` for proportional validation, bounded context/Snapshot
   evidence when required, and Git Finalizer within explicit commit/publication authorization.
   A read-only or small local documentation task does not mechanically require a full audit or
   publication. Controller evidence does not substitute for required delivery evidence.
8. Return module work to the repository control plane with `handoff integration-ready` only when the
   worktree is clean, the commit is resolvable, and required test/Finalizer evidence exists. Use
   `handoff preserved` for rescue/archive state that is recoverable but not integration-ready.
   `HANDOFF_READY` is fenced against new feature writes and bindings; continuing development requires
   explicit `reopen`, which invalidates the previous handoff and release authority. An absorbed or
   published source may require the advertised typed recovery flow; never overwrite frozen evidence
   or reset a drifted checkout to obtain a passing guard. Record the task scope (`acquire --scope`,
   `handoff --scope`, or `handoff --scope-changed` for the exact changed paths of an unscoped
   task); `integration-enqueue` refuses a source without one, and a handoff of unscoped changed
   work warns `MISSING_HANDOFF_SCOPE` — repeat the handoff with `--scope-changed`.
   With `host_handoff_continuation_version` 1 or later, the same session continues after handing
   off its own checkout: push the branch through Git Finalizer, `handoff`, then
   `integration-enqueue` → `integration-claim` → test in the candidate → `integration-validate` →
   `lease-acquire` → Finalizer `--publish-integration-candidate` → `lease-complete` → session end.
   Its per-tool gate passes `--access read|exec|write [--target]` to `host-check` (version 2 also
   to direct `writer-session check`); Controller grants reads, commands, writes outside
   governance, writes into that handoff's candidate and (version 2) writes into other checkouts
   whose lease the same process holds, while the frozen checkout stays write-fenced. Without the
   capability (or with a gate that sends no access class), a handoff of the session's own checkout
   locks its tools: hand off from another session, or stop with the branch pushed and report the
   handoff arguments.
9. The repository-control task owns lineage, integration, publication, and release. Up to the policy
   preparation limit may run non-overlapping intents independently; later overlapping scopes wait in
   `WAITING_SCOPE`. Bind tests and Snapshot identities with `integration-validate`. A prepared
   candidate is write-fenced; use `integration-reopen` before further changes. If publication CAS
   reports `NEEDS_REPREPARE`, run `integration-refresh`, repeat validation, and do not rewrite source
   feature branches. A real conflict moves only its own intent to `ATTENTION_REQUIRED`.
   With capability `integration_retry_version` 1, an aborted unpublished intent whose candidate
   has complete release evidence uses `integration-retry --dry-run` and exact `--expected-plan-id`
   apply. Keep the original ID, source, scope and queue sequence; claim and validate a new attempt.
   A separately enqueued necessary strict source ancestor may be bound with
   `--prerequisite-intent` through the same verified plan. Its scheduling inherits only the failed
   parent's position, without crossing earlier unrelated scope fences. Publish and release that
   prerequisite before retrying the parent. Never use this as arbitrary priority, scope widening,
   source replacement, or reuse of old candidate validation. Unsupported failure states fail closed.
   With `integration_conflict_resolution_version` 1, the same retry plan/CAS also accepts a proved
   pre-allocation `INTEGRATION_CONFLICT_BLOCKED` failure; version 2 also accepts it for an intent
   whose earlier claimed attempt was formally aborted and retried (never abandon and re-enqueue). After retry, an explicit
   `integration-claim --intent-id ID --resolve-conflicts` may allocate a single-source conflict at
   its pinned main base, respecting the original scope order and admission. Merge only the returned
   frozen source in that candidate, resolve its scope explicitly, and use Git Finalizer's resolved
   two-parent merge mode. `integration-resolve` requires exact owner/record/head and Finalizer evidence;
   it accepts only the pinned main/source parents and unchanged frozen scope. It binds a new candidate
   revision without validation authority. Repeat tests, Snapshot and normal `integration-validate`
   before publication. Never invent abort/release proof, replace source commits or bypass queue fences.
   With `integration_lineage_admission_version` 1, the producer can admit an integration task sharing
   its exact frozen source authority while keeping both requests and the original queue identity.
   Use the original `integration-claim`, without changing authority or replacing the intent. An exact
   repeated claim of a clean unchanged `PREPARING_INTEGRATION` candidate observes the same allocation;
   changed owner/path/branch/HEAD or source evidence still blocks. `RESOURCE_WAIT` retains the queued
   request without a candidate, prepared receipt or publication lease. Keep scope and budget gates.
   With `integration_keyed_document_scope_version` 1, Controller may prove distinct QP Nexus (or historical Handbook)
   records from frozen Git commits when the only overlap is the exact known progress document.
   Use the normal claim/reconciler on the original intent; do not edit frozen scopes or queue order.
   Unknown evidence, shared records, code/broad-path overlaps and publishing/recovery/attention
   states retain the file fence. This allows preparation only: main CAS, fresh validation,
   publication leases and all ownership gates remain mandatory.
10. Main publication requires the repository's single `lease-acquire --run-id ...` authority,
    separately from the checkout's writer lease and the user's commit/push/merge authorization.
    Pass the returned candidate/lease/run identity unchanged to Git Finalizer
    `--publish-integration-candidate`. A consumer of capability
    `integration_publication_execution_version` 1 uses public
    `integration-publication-session/v1`; Controller retains the lock across verify, authorize,
    consumer push and independent completion observation. Consumers never read private lease or
    intent files, and disconnect recovery reuses the same lease/run/candidate. Pass its `record_id` back to
    `lease-complete --run-id ...`. Completion persists the remote receipt, releases the lease, and
    defaults to registered-resource cleanup plus guarded integration-candidate release. Once the
    integration is remote verified and `RELEASED`, Controller may create exact source
    `ReleaseIntentV1` records and reconcile them under repository policy. Do not wait for the
    original chat owner when exact receipt evidence authorizes release. Use `--no-auto-release` only
    for an explicit recovery/debug stop.
    With capability `source_release_summary_version` 1, read each `source_release_trigger.outcomes`
    entry: `ALREADY_RELEASED` carries the verified immutable receipt; pending cleanup, missing proof,
    and live retryable/stale results keep their blocker and existing `recovery_commands`. Do not
    retry release because a completed source has no live handoff binding. On older runtimes, query
    `lifecycle-history` for the exact source allocation and require complete matching proof;
    directory absence and checkout release alone do not prove publication or business completion.
11. If integration HEAD or scope drifts, use `quarantine`; move retained lineage into a governance
   recovery allocation and record `externalize` evidence before releasing the polluted checkout.
   Ordinary feature work must never consume recovery reserve.
12. Every releasable worktree, with or without `.venv`, may run official `release --dry-run`.
    Dry-run and execution use the same plan guards. Automatic source release additionally requires a
    non-expired intent bound to exact HEAD, record CAS, handoff/Finalizer identity, released
    remote-verified integration evidence, no unintegrated result, no active/runtime/editable
    binding, classified resources, and a retained ref. `PRESERVED` is never automatically released.
13. `reconcile receipts --dry-run` plans immutable historical repair; formal reconciliation writes a
    new receipt and never edits the publication receipt. `reconcile release-intents` evaluates only
    existing intents. A capacity-pressure hint may retry eligible/retryable intents but must not scan
    clean worktrees or invent release authority.
14. Agent Workspace consumes `lifecycle-events` using a stable consumer identity and acknowledges
    each event only after its task state is durably committed. `reconcile integration-intents` may
    restore missing events and dispatch existing queued intents; it never invents an intent from a
    clean checkout. Only Controller creates/releases worktrees or grants publication authority.

## Diagnostics and compatibility

With `terminal_preserved_cleanup_version` 1, published integrations and completed
no-change feature/audit tasks may use the existing preservation and `resource-clean --typed`
flow. Require one exact remote-verified, lease-cleared publication or one verified original
no-change completion. Every removed tree needs immutable preservation coverage.
Plans pin the current verified proof baseline; cleanup and release recheck it without
rewriting history, reopening lifecycle or relaxing owner/HEAD/CAS, runtime, lease, retained-ref
and transaction guards. A newly observed legacy digest is not historical hash evidence.
New-format no-change completions may clean afterward only when their frozen prior cleanup
set is empty; existing pre-completion proofs retain their original CAS.


- With `aborted_integration_preserved_cleanup_version` 1, an explicitly aborted unpublished
  integration may use the existing preservation and `resource-clean --typed` flow. Every removed
  tree needs exact immutable preservation coverage; no generated-directory name grants disposal.
  Direct cleanup of `QUARANTINED` remains refused. Its owner first uses the explicit owner/CAS
  `integration-abort`, retaining quarantine and failure history. Require the exact abort receipt,
  HEAD, retained branch, owner/CAS, idle runtime and cleared leases. Cleanup is not release or
  admission cancellation. Normal guarded release settles the old request before a changed-source
  intent can claim; unchanged-source retries retain their existing intent and queue rules.
  Never replace a frozen request, compute private digests, widen policy or force-remove files.

- With `archived_codex_binding_recovery_version` 1, a released writer lease whose
  exact Codex thread was formally archived may use `binding-remove
  --recover-archived-codex-session --dry-run`, followed by the same owner/binding/CAS
  apply. Controller independently verifies the original shared daemon/socket peer,
  archived native session, unloaded status and absence of checkout references, and
  records immutable recovery evidence. Never stop a shared app-server to satisfy a
  dead-PID check. This mode grants no thread-archive permission, writer authority,
  owner transfer or business completion; live/unarchived threads remain protected.

- Before relying on a newer verb or changing runtime, inspect `capabilities --json` and installed
  `--help`. Check software version, CLI contract, each required capability version, and storage
  layout independently. Equal CLI contracts do not prove equal capabilities. Stage, rollout,
  activation, and namespace migration keep their own gates; this Skill never bypasses them.
- At sites using `tool-skill-sync`, declare its owning canonical published catalog for governed
  stage/bootstrap/activate/rollback with `--toolchain-compatibility <absolute-owner-catalog>` or
  `CONTROLLER_ROLLOUT_TOOLCHAIN_COMPATIBILITY`. Deliver required catalog registrations through the
  owner flow before staging/activation. The target gate checks CLI, capability and storage versions;
  a catalog refusal blocks this deployment only. Keep the installed runtime until the gate passes,
  and close deployment with the default installed doctor on the actual checkout.
- `status` warning `BINDING_IDENTITY_DRIFT` means a bound key's checkout is not the Git
  registration its binding was written for (removed and re-created outside the Controller). Treat
  that checkout as unmanaged: never write under the old task or recreate a registration by hand;
  its real owner adopts it explicitly, and the old task resumes from its retained branch.
  With `binding_registration_backfill_version` 1, legacy linked bindings gain that identity only
  through `reconcile binding-registrations --dry-run` and the exact `--expected-plan-id` apply;
  refused bindings keep their legacy meaning and are left to their owner.
- Use Git to resolve the checkout and common dir; linked `.git` entries can be files. Metadata
  belongs at `repository.metadata_path`, not at an assumed checkout-local `.git` directory.
  The namespace suffix `v1` is not the advertised `storage_layout_version`.
- For a blocked execution process, run `doctor --namespace-pid <actual-writer-pid>` and read the
  environment/mount diagnosis before attributing a lock error to corrupt Controller metadata.
  Host write access does not repair an old session's read-only mount. Preserve its task and checkout,
  rebind a compatible authority through Controller, and repeat Git status, writer-session/lease,
  and write-guard checks in the actual execution environment. Diagnostics do not change mounts.
- Missing namespace or process evidence is `UNKNOWN`, not proof of a dead writer. Receipt,
  release-plan, and integration-intent repairs use the advertised typed reconciliation/recovery
  verbs; never edit journals, locks, frozen evidence, or ledger JSON to clear a fence.
- With `release_recovery_inspection_version` 1, query an interrupted release through
  `release-recovery-inspect --transaction-id <exact-uuid> --json`. The producer verifies its
  original journal/ReleasePlan and returns the original request, structured `release_argv`
  and raw evidence digests. Keep the original owner/CAS and publication/intent identities;
  the current released record cannot replace them. Inspection grants no release authority
  and does not preflight execution guards. The original task replays through normal guarded
  `release`, including `--publication-receipt-id` / `--intent-id` when present; no shell
  evaluation, private metadata repair or activation exception follows from this query.

## Governance closeout

With `recovery_guidance_version` 1, use `recovery-guide --path <checkout>` (or the original
`--intent-id` after release) for a read-only explanation and structured next step. Match its
observed owner/allocation/task/authority/HEAD/CAS to the existing Task, execute only an already
authorized formal verb, then reobserve. Clean unchanged `BLOCKED` integrations quarantine before
abort; equal expected/observed HEAD is valid. Scope or source drift still needs its owner.
The guide grants no execution, cleanup, retry, publication or business completion authority.
An already published and released intent reports `PUBLISHED_RELEASED` only with complete matching
immutable publication and release proof; follow its history query instead of retrying it. Missing
or drifted terminal evidence remains blocked, and business Task completion is never inferred.

With `session_closeout_expectations_version` 1, pass every known allocation as repeatable
`--expected-allocation-id` along with the real session selector. Read `allocations`,
`previous_allocations` and `expected_allocations`; require `summary.terminal_complete` in strict
checks. Expected IDs never select mutation targets. Missing observations require complete
immutable release proof; pending live rows publish `recovery_argv` and remain owner work.

With `hosted_session_closeout_check_version` 1, hosted clients may inspect normal-exit preparation
with their verified host selector using `--check`. This check never ends, parks or releases an
allocation; hosted session-ending calls remain refused.

- With `session_closeout_report_version` 1, perform a normal-exit preparation check using the real
  client/session/holder selector and its recorded `--run-id`, covering all of that holder's leased
  checkouts. `worktree-session-end ... --check` is read-only; do not stop at an advisory exit 0 or
  the top-level `SESSION_ENDED`. Read every allocation and `summary.terminal_complete`; strict
  consumers use `--require-complete`. Deliver accepted task-owned files through the authorized
  Finalizer/push/handoff path. For unfinished work, persist the existing Task's checkpoint first,
  classify local artifacts and preserve unknown files. A retained dirty checkout still needs its owner.
- Capability `checkout_owner_action_version` 1 exposes `checkout-owner-action/v1` requests. Match
  the existing Task binding to the exact allocation/owner/task/authority, re-observe HEAD and record
  CAS, then restore that owner through ordinary admission. The request is evidence, not execution
  or discard authority; never create another owner or infer completion from it.
- With `task_checkpoint_park_version` 1, after the writer ends, bind the Task's content-addressed
  checkpoint and exact HEAD using `park --checkpoint-id ... --checkpoint-head ... --waiting-on ...`.
  This is an owner attestation. Keep the dependency in the Task, and use the existing reviewed
  archive-retained/release gates to free an eligible directory while preserving its branch.
  Restore through controlled admission from the verified retained HEAD and reload context before
  a new writer. Unknown work, bindings and uncertain runtime evidence continue to block release.

- Capability `no_change_cleanup_closeout_version` 1 binds no-change completion to its original
  record and exact successful cleanup receipt set. Release verifies the pinned raw completion
  bytes and the exact +1 transition; it never relaxes cleanup CAS. An old `NO_CHANGE_COMPLETED`
  task blocked after completed cleanup uses `reconcile no-change-completion`: recorded owner,
  path/CAS, explicit completion ID/SHA256 and paired cleanup IDs/SHA256, reviewed dry-run and
  unchanged plan ID. The append-only recovery adds one normal history/CAS event, preserves old
  receipts and performs no deletion. Never replace missing prior observations with a claim of
  historical hash evidence; establish and label a new explicit baseline after full verification.

- When `closed_lifecycle_query_version` 1 is advertised, use `lifecycle-history` with one exact
  allocation/authority/task/integration-intent/release-receipt selector. `status` owns live checkout
  observations; `closed_releases` exposes existing immutable release evidence after registration
  disappears. Historical source owner comes from its release plan, not a current allocation or the
  integration publisher. Consumers keep receipt references and validate publication/candidate ancestry
  separately; release alone cannot prove business completion. Incomplete legacy evidence is not a
  reason to invent history, retain a checkout, reopen a writer or bypass release gates.

- Closing is automatic when policy enables `[convergence]`: the delegated reconciler (timer and every
  `acquire`) closes clean, unused checkouts through the guarded verbs below and releases them; it keeps
  branches and never touches dirty, detached, leased, bound, or `ACTIVE` work. An agent therefore only
  has to leave its checkout clean and end its session (`worktree-session-end`, `park`, or `handoff`;
  a client launched from canonical ends its leased checkouts with the session selector of
  `session_end_selector_version` 1 and reads each per-allocation decision, not the exit code);
  it must not hold a checkout "just in case" or delete one to meet a count. Take a writer lease when
  the client supports it: a clean `ACTIVE` checkout without lease, process, or Git activity is
  treated as a finished session and parked after `idle_hours_max` (pressure never shortens it). Commit or discard
  uncommitted work before ending: dirty checkouts are reported as `ATTENTION_REQUIRED` and stay until
  their owner acts. Use `reconcile retention --actor <any>` (dry-run) to see what will close and why:
  `reason`/`outcome_counts` summarize the real blockers, dirty rows carry `owner_action`, released
  rows carry the `receipt_id`, and `last_applied_pass` shows how the latest applied (for example
  detached admission) pass ended; that report is an observation, not release evidence.
- With `retention_convergence_version` 2, published hotfix handoffs also converge, and policy
  `release_unintegrated_handoffs = true` lets an idle integration-ready feature/audit/hotfix handoff leave
  before integration once its frozen identities, local branch and remote-tracking branch equal
  HEAD. So push the handoff branch through Git Finalizer; do not keep a checkout to wait for
  integration. `integration-enqueue` then resolves the released source from its retained branch
  and fails closed if that branch moved. Release never means integrated or published.
- At task start, a dry-run `ATTENTION_REQUIRED` row owned by the current task is that task's to
  resolve. Rows of other tasks are reported, not adopted: the same owner string does not prove the
  same session, and convergence never commits, stashes, resets, or discards for anyone.
- There is no fixed checkout ceiling to plan around. Admission is resource based; idle thresholds
  shrink as the population grows beyond `live + standing_reserve`.
- `gc-plan` inventories Git registrations with allocation, path, branch, lifecycle, owner, lease,
  dirtiness, bindings, resources, and blockers. Keep active work; block dirty, unknown, or ambiguous
  targets. Age, a clean status, absent leases, and capacity observations do not authorize release.
- For clean `RETAINED` work, distinguish published content (`close-delivered`, then
  `RELEASE_READY`) from unfinished recoverable content (`archive-retained`, then `ARCHIVED`).
  Both retained closeouts require the advertised capabilities,
  exact owner-release authority and record CAS, a reviewed dry-run plan, and its unchanged plan ID.
  They do not declare business completion, change commits, or implicitly delete refs.
- Verified archived `PRESERVED` work keeps its existing `close-preserved` / guarded release flow.
  A legacy `PRESERVED` feature/audit checkout without frozen handoff or archive evidence may use
  explicit `close-delivered` from Controller 1.13.4, after its reviewed plan proves exact HEAD,
  original task lineage and live publication containment with owner/CAS and resource guards.
  This preserves original evidence without inventing a Finalizer record or business completion;
  it does not make preserved work eligible for automatic convergence.
- When `published_handoff_resource_registration_version` 1 is advertised, a published
  `HANDOFF_READY` hotfix may classify its existing reproducible on-release root `.venv` through
  `resource-register` with exact owner/CAS and `--creator handoff-owner-attestation:<owner>`.
  This attests current owner classification, not the original creation command. Controller checks
  exact frozen task/handoff/Finalizer identity, retained branch/base ancestry, live publication,
  no leases/bindings/open integration or transaction obligations, and existing runtime/venv
  preflight. Only the new resource history event receives proof; source files and frozen handoff
  evidence remain unchanged. Existing cleanup/release guards and automatic convergence roles apply.
- Apply a reviewed target's exact `gc-plan` identity through `gc-apply`, or use normal
  `release --dry-run` / `release`. Revalidate current HEAD, lifecycle, owner, active use, retained refs,
  resources, ignored content, and bindings under the release lock. Drift requires a fresh plan;
  interrupted release resumes through the existing release transaction identity. With
  `gc_apply_replay_version` 1, a target already released by exactly the reviewed plan returns
  its receipt as `ALREADY_RELEASED`; do not treat a vanished directory alone as success.
- Only registered, task-owned, reproducible resources may be torn down through supported Controller
  cleanup. Surviving external editable/runtime bindings and unknown ignored evidence block release.
  Do not hand-remove directories, registrations, branches, tags, or metadata to meet a count target.

- Capability `multiple_preservation_cleanup_version` 1 adds paired repeatable
  `--additional-preservation-receipt` / `--additional-preservation-receipt-sha256` to the primary
  exact-preservation cleanup pair. Every source keeps its own verified proof and hardlink identity;
  all allocation/resource/runtime gates remain mandatory. Single-input plans retain their shape.
  Capability `multiple_preservation_recovery_version` 1 adds `resource-recover --stage
  prepare-multiple` with exact original transaction ID, journal and failed-receipt SHA-256,
  recorded owner, path/CAS, reviewed dry-run and unchanged plan ID. It accepts only a fully
  evidenced nonempty `COMPLETED` prefix followed by nonempty never-started `PENDING` actions.
  `resume` binds its immutable recovery record and runs only the pending suffix through the existing guarded
  executor. The complete original proof set stays pinned; preserve every journal, receipt and
  generation. A started successor cannot be replayed, and unsupported partial shapes still block
  release/rollout. Only a verified append-only composite completion closes the original journal;
  never downgrade the proof set into legacy single-receipt recovery.
- With `typed_cleanup_verify_boundary_version` 1, a typed `resource-clean` refused by
  `HARDLINK_TOPOLOGY_BLOCKED` is closed with `resource-recover --stage reconcile-typed --dry-run`
  and the exact `--expected-plan-id` apply, including the pre-1.28.0 shape with one
  `FAILED_OR_PARTIAL` first action and only its `0000-before.json`. Then run a fresh cleanup;
  never edit the journal or remove the resource by hand.
  The recovery record freezes the original allocation owner/CAS/history and every verified
  official generation's metadata references and raw SHA-256, including older superseded copies.
  Historical terminal verification rechecks that complete unchanged set. An absent checkout
  requires exact official release plan, receipt and frozen allocation history; absence alone
  never bypasses source-root or ancestor guards for live preparation and deletion.

- Capability `preservation_supersession_version` 1 relates older ignored-resource generations
  only through the same exact allocation/owner and a verified formal frozen handoff, reopen and
  new frozen handoff lineage. The source-relative path, original checkout-root inode and the
  source-parent ancestor chain must stay unchanged. A source leaf or directory inode may be
  rebuilt only across that formal lineage, with a new exact verified baseline and actual completed
  action. Old generations are retained and checked; source absence alone is never completion
  evidence.

- Capabilities `retained_typed_cleanup_version` 1 and `preservation_leaf_version` 1 add explicit
  exact owner/CAS preservation and cleanup for clean `RETAINED` feature/audit allocations and
  selected ignored regular-file or literal symlink leaves. Use installed help and the existing
  primary/additional receipt pairs; no cleanup authority comes from a copy alone. Verify original
  ancestor pins and keep all lease, runtime, binding, ref and transaction gates. Do not follow a
  symlink target or treat a leaf's tracked parent as disposable. `archive-retained` freezes actual
  completed receipt IDs, paths and raw SHA; release rechecks those bytes and only the exact
  verified archive transition (`after CAS = before CAS + 1`). Partial cleanup remains blocked through its original
  journal and receipt; use only the advertised exact multi recovery shape, and never replay a
  completed action or downgrade a multi proof set.

- Branch retirement closes only the operations selected by the authorized task. Global
  `branch-retirement-status` counts local/remote receipts; `PARTIAL` with `pending_operations`
  containing remote can remain after a local-only candidate retirement. Keep the original status
  and receipts, and report an unselected remote action separately with its actual ref observation.
  Never invent a receipt or delete an extra ref to complete the global count. For each selected
  operation, use existing `branch-retirement-verify --plan-id <exact-plan> --operation local|remote`
  and require matching plan/operation/receipt, `RETIREMENT_OPERATION_COMPLETE`, current
  `minimum_revalidation.ref_state = ABSENT`, and empty blockers together. The code preserves
  historical completion even when a ref reappears or governance moves; failed observations leave
  closeout unresolved. Remote state is a local tracking observation: additionally use Finalizer's
  live remote verification or exact `git ls-remote --exit-code <remote> <full-ref>` (0 with the exact
  returned ref means present, 2 with no match absent; unexpected output or other errors unknown).
  Report a surviving remote ref, and keep selected
  remote retirement open. `ABSENT_UNATTESTED` without a receipt proves no deletion history.
  Repeated read-only verification grants no mutation authority; release, publication and business
  completion remain independently evidenced.

- Capability `canonical_transition_retirement_version` 1 lets `branch-retirement-plan --branch`
  prove an old baseline ref through one exact canonical transition history event. Review its
  `CANONICAL_TRANSITION_BACKED` registration and event evidence, then use the ordinary Finalizer
  plan-bound retirement flow. Canonical remains registered; never fabricate a release receipt or
  release canonical. Ordinary unregistered refs remain report-only, and missing, ambiguous or
  drifted history, occupancy, leases, runtime bindings and unresolved obligations still fail closed.

## Invariants

- `task_key` identifies the bounded task; `authority_key` identifies the capability ownership.
  Duplicate ACTIVE feature authority is a blocker, while a matching existing authority is a reuse
  candidate.
- An admission refusal cannot be bypassed by another path, role, or runtime. Emergency capacity
  requires the explicit emergency/hotfix role; recovery capacity requires a recovery/preservation
  role. Observe-only count limits do not override resource admission or confer cleanup permission.
- Integration-candidate capacity does not grant main-publication authority. A publication lease is
  repository-global, binds repository/target/holder/run/expected-main/candidate/validation identity,
  and succeeds only when latest main is an ancestor of the exact candidate HEAD. Expired or replaced
  writers must reread remote fact and cannot continue mutation.
- Queue entries do not consume physical worktrees. Prepared receipt/base/tree/scope changes
  invalidate publication eligibility; `READY_TO_PUBLISH` requires explicit reopen before writes.
- Integration scope is the union of recorded source handoff scopes. Novel non-merge commits or
  paths outside that scope require quarantine/recovery and block handoff/publication.
- Cleanliness is Git state, not ownership, integration readiness, or release eligibility.
- HEAD, record, resource, or binding drift after plan/intent creation fails closed. Runtime/editable
  blockers may retry only after the corresponding clearance event; identity drift makes the intent
  stale instead of retryable.
- Controller authority mutations serialize on the repository `repo.lock`, and the Git
  Finalizer retirement path serializes on the same lock. A missing, symlinked, or
  unusable lock stops all mutations fail closed (`REPOSITORY_LOCK_MISSING` /
  `REPOSITORY_LOCK_INVALID`); never hand-edit `<git-common-dir>/worktree-controller/v1`
  or the host resource ledger to clear a fence. Interrupted journals close only through
  published terminal evidence or the typed verbs. Storage layout is an independently versioned
  contract for external consumers, not something inferred from the namespace directory name.
- With `repository_lock_admission_version` 1, cooperating readers cannot bypass a writer
  already admitted to the entry gate. Same-thread nested reads and history reads during active
  typed cleanup retain their compatible access; doctor keeps its filesystem/journal fence.
  Existing calls from older runtimes preserve `repo.lock` exclusion but do not join the new
  ordering or active marker. This promises neither strict FIFO nor bounded wait times. Preserve
  the original request and let its owner finish; never remove a lock, stop another owner or
  change authority to clear contention. Inspect each source-release outcome after publication.
- Host resource recovery is typed: `host-execution --action inspect` publishes the
  canonical holder digest and required terminal-evidence shape, and only `clear
  --recover-dead-holder` may release a provably dead or orphaned reservation. A
  home-root or ancestor admission is refused at the producer; dead holders never
  justify manual ledger edits.
- `preserved` cannot be promoted implicitly to `integration-ready`.
- Module tasks return repository control at `integration-ready`, `preserved`, or `blocked`; they do
  not self-merge, self-publish, or release unrelated worktrees.
- Context Loader reads context; Worktree Controller manages lifecycle; Snapshot Runner gathers
  read-only evidence; Git Finalizer commits and publishes. No tool acquires another tool's mutation
  authority.
