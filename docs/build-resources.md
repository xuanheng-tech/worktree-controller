# Explicit build resources

Controller 1.33.0 advertises `registered_build_resources_version=1` with CLI 61
and storage 5. This adds an owner attestation for existing outputs in a frozen
Node project. It does not execute a build, prove reproducibility by rebuilding,
or authorize research evidence deletion. The recorded owner must first confirm
that the exact outputs contain only rebuildable material and no unique evidence.

The initial contract accepts immediate `.astro`, `.generated`, `dist` and
`node_modules` children of an explicit project. Names alone grant no permission:
registration requires the original owner, exact HEAD and record CAS, a committed
`package.json` with the named nonempty script, matching `package-lock.json`,
Git-ignored and untracked outputs, real directory ancestors and idle runtime.
Project paths under `research`, `experiments` or `evidence` are refused.
Other build systems, absent outputs and other paths require the existing exact
preservation flow. A recipe is recorded, never executed by Controller.

Registration is allowed for `HANDOFF_READY` feature/audit/hotfix and eligible
`RETAINED` sources after writers end. It is not allowed for an integration
candidate, including aborted/published candidates: their exact preservation
requirements remain unchanged. Registered root `.venv` must first finish its
existing supported cleanup. Bindings, pending transactions, stale owner/CAS,
head drift, missing retained refs and runtime references remain blockers.

## One checkout, then a batch owned by the maintenance task

Read current status and capabilities. Substitute the observed values; no example
CAS or HEAD is authority for another checkout. First review the registration plan:

```sh
worktree-controller build-resource-register --repo "$repo" --path "$checkout" \
  --owner "$owner" --expected-head "$head" --expected-record-version "$cas" \
  --project apps/qp_docs --recipe build --attest-rebuildable \
  --resource-path apps/qp_docs/.astro --resource-path apps/qp_docs/.generated \
  --resource-path apps/qp_docs/dist --resource-path apps/qp_docs/node_modules \
  --dry-run --json
```

List only the existing, reviewed outputs. Apply the exact same request, replacing
`--dry-run` with `--expected-plan-id <returned-plan-id>`. Registration is one
append-only allocation-history event and increments CAS once. It preserves the
frozen source and Finalizer identity; it invalidates an old release intent rather
than silently carrying old release authority forward. It does not remove files.

Use the returned CAS for the normal typed plan and reviewed apply:

```sh
worktree-controller resource-clean --repo "$repo" --path "$checkout" \
  --owner "$owner" --expected-record-version "$new_cas" --typed --dry-run --json
worktree-controller resource-clean --repo "$repo" --path "$checkout" \
  --owner "$owner" --expected-record-version "$new_cas" --typed \
  --expected-plan-id "$cleanup_plan_id" --json
worktree-controller release --repo "$repo" --path "$checkout" \
  --owner-release "$owner" --expected-record-version "$new_cas" --dry-run --json
```

Inspect the complete `cleanup_paths` list: typed cleanup can also include existing
recognized resources. Unknown ignored paths block the transaction. Only a clean
plan is executable. Verify `TYPED_DISPOSABLE_CLEANED` and the formal receipt before
the separate guarded release. Release keeps the retained branch and is not an
integration or publication receipt. The QP maintenance owner performs the live
single-checkout canary and decides whether to proceed with its exact batch.

From 1.33.1, registration uses the same `allocation-runtime-authority/v1` runtime
classification as typed cleanup. Its response includes fresh `runtime_evidence`
and `HOST_VISIBILITY_GAP` warnings for unassociated host permission gaps. This is
not proof of host-wide absence of references. Managed or observed references and
managed/scan uncertainties still block; apply reobserves runtime even after a
successful plan. It does not run the additional legacy host-wide strict scan.

## Integrity and recovery

Cleanup rechecks the registered input identities, all resource content and the
hardlink topology at existing action boundaries. It uses pinned directory FDs,
before/after action evidence, exact external-survivor checks and the existing
transaction journal. Registration does not permit direct `rm`, `git clean`, or
the legacy `resource-clean --resource-path` remover. Changed root inodes, changed
HEAD/record, replaced inputs, escaping symlinks and concurrent drift fail closed.
One registration covers the allocation's complete reviewed output set. Later
record changes invalidate it; do not append another registration to refresh stale
authority. Preserve failed transaction evidence and use the existing advertised
recovery flow; unsupported partial states remain blocked.

With `build_preservation_recovery_version=1`, typed cleanup verifies existing
preservation generations before deleting any registered output. The only accepted
CAS bridge is the unchanged allocation/owner/HEAD immediately before its one
`BUILD_RESOURCES_REGISTERED` event. Original source/root anchors and preserved
bytes must match. The plan freezes the verified generation hashes; each completed
action supplies its original before/after topology proof. Old receipts are never
rewritten or promoted to the new CAS.

An interrupted typed transaction with an exact completed prefix and an untouched
pending suffix uses the existing prefix recovery engine:

```sh
worktree-controller resource-recover --repo "$repo" --stage prepare-multiple \
  --path "$checkout" --owner "$owner" --expected-record-version "$new_cas" \
  --transaction-id "$transaction_id" --transaction-sha256 "$journal_sha256" \
  --cleanup-receipt-sha256 "$failed_receipt_sha256" --dry-run --json
```

Review the remaining paths, then repeat with `--expected-plan-id` instead of
`--dry-run`. Use the returned `recovery_receipt.path` and `.sha256` with
`resource-recover --stage resume --recovery-receipt PATH
--recovery-receipt-sha256 SHA --dry-run --json`, then the same request without
`--dry-run`. Require `MULTIPLE_RECOVERY_CLEANED` and its completion receipt.
Recovery preserves the old journal, failed receipt and completed action proofs,
adds a verified baseline, and executes only the pending suffix. An old 1.33.1 plan
without generation bindings is accepted only after full verification against its
original registration and action topology; the new baseline is explicitly a new
observation. Started/uncertain actions, changed bytes, recreated completed paths,
unknown outputs or identity drift remain refused. A started recovery never
blindly replays; a completed one may only reconcile its exact terminal receipt.
This grants no release or batch authority. CLI 61 and storage 5 remain unchanged.
Version 1.33.1 cannot verify this combined generation/action lineage and continues
to refuse it. Recovery and subsequent release require the advertised capability;
any runtime rollback must still pass the normal transaction compatibility gate.

The performance benefit is eliminating separate preservation plan/copy/verify
cycles for every explicitly rebuildable output. The shared-lock typed cleanup
still scans and hashes resources and can remain expensive for large trees. No
cross-call verdict cache or validation exemption is introduced. Older runtimes
can read the history event but cannot classify these outputs; they continue to
refuse unknown ignored content. Do not alternate runtimes during cleanup.

## Retention preview

`retention_release_preflight_version=1` changes the preview meaning:
`WOULD_RELEASE` requires the exact release dry-run to succeed at observation time.
Its decision is attached as `release_preflight`. `CLOSEOUT_REQUIRED` means a park,
archive or other closeout must occur before release eligibility can be evaluated;
it is not counted in `would_release`. Neither preview is a reservation: execution
rechecks all guards, and concurrent changes can still block release.
