---
name: worktree-lifecycle
description: "编排多 worktree 仓库任务的 authority、预算、handoff、integration 与 release；在写任务需要选择或管理 checkout 时使用。不替代 Git、Controller、Snapshot Runner 或 Git Finalizer 的实现与状态 authority。"
---

# Worktree lifecycle

Route repository work through the existing tools without merging their responsibilities.
Context Loader reads context; Worktree Controller owns allocation, ownership, admission,
leases and lifecycle; Snapshot Runner gathers read-only evidence; Git Finalizer commits and
publishes. Git stays authoritative for worktrees, refs, HEAD and cleanliness. This Skill grants
no commit, push or destructive authority; the caller's task authorization does.

## Normal flow

Pass `--repo <canonical>` when the shell is not inside the repository. Every mutating verb takes
the exact `--expected-record-version` (CAS) read from `status --json`; reread after each step.

1. **Plan.** `worktree-controller status --json`, then `plan --task-key <t> --authority-key <a>
   --module <m> --role <role>`. Prefer an existing compatible authority. Admission is resource
   based; observe-only checkout counts are not a veto and never authorize cleanup.
2. **Acquire.** `acquire --task-key --authority-key --module --role --owner --branch --base-ref
   origin/main [--scope <path>]...`. Declare the narrowest known scope. Ordinary feature work never
   runs in canonical. Unfinished `RETAINED` work uses `resume --path --expected-record-version
   --owner`; a frozen handoff uses `reopen`.
3. **Write under a lease.** Run commands in the checkout as its leased writer. Clients gated by a
   hook use `worktree-session --path <checkout> --client-id <c> --session-id <s> [--run-id <r>]
   <cmd>` from an ungoverned cwd; it binds the lease, runs the command and closes the session.
   Use a fresh `--run-id` per invocation. Manual execution uses `writer-lease-acquire` and
   `write-guard`. Never register an unrelated PID. Stop on lease loss.
4. **Deliver the branch.** Validate, review and commit with `$git-change-delivery`; publish the
   task branch (`--publish-existing-branch` on first push).
5. **Hand off.** `handoff --path --expected-record-version --kind integration-ready --commit <oid>
   --finalizer-record-id <id>`; add `--scope-changed` only for an unscoped task. `preserved` is
   for recoverable work that is not integration-ready. A handoff checkout is write-fenced.
6. **Integrate.** `integration-enqueue --task-key --authority-key --module --owner
   --source-allocation <id>`, then `integration-claim --owner --intent-id <id>`. Test in the
   candidate, then `integration-validate --path <candidate> --expected-record-version
   --owner --validation-evidence-id --tests-receipt-id --snapshot-receipt-id`.
   Overlapping scopes wait in `WAITING_SCOPE`; respect earlier owners.
7. **Publish main.** `lease-acquire --path <candidate> --expected-record-version --owner
   --run-id <run>`, pass the returned lease/run unchanged to Git Finalizer
   `--publish-integration-candidate <oid> --repo <candidate> --lease-id <id> --run-id <run>`,
   then `lease-complete --path --expected-record-version --lease-id --published-commit <oid>
   --finalizer-record-id git-finalizer-integration-<lease>-<oid> --run-id <run>`. Completion
   releases the candidate and normally the source checkout; read `source_release_trigger`.
   A source left `ELIGIBLE_MANUAL` uses `release --dry-run`, then `release` with the same CAS.
8. **Close.** Leave nothing dirty: commit, `park` unfinished work, or end the session. Do not
   hold a checkout "just in case". With `[convergence]` enabled the reconciler closes clean
   idle checkouts and keeps their branches.

Canonical lagging published main is advanced with `canonical-refresh` (dry-run first); never
pull or reset canonical by hand. A frozen canonical handoff moves with `canonical-transition`.

## Refusals, drift and recovery

Controller refusals are specific; act on what they return instead of improvising.

- Read the refusal's `errors`, `recovery_argv` and `next_steps`. For any checkout run
  `recovery-guide --path <checkout>` (or `--intent-id` after release) and execute only the formal
  verb it names, then reobserve. The guide itself grants no authority.
- Before relying on a newer verb, check `capabilities --json` and installed `--help`; software,
  CLI contract, capability and storage versions are independent.
- Plan-based verbs (`release`, `gc-apply`, `resource-clean --typed`, `resource-recover`,
  `integration-retry`, `close-delivered`, `archive-retained`, reconciliations, branch retirement)
  are dry-run first, then applied with the unchanged plan ID and CAS. Drift needs a fresh plan.
- Interrupted transactions resume through their original identity (`release-recovery-inspect`,
  the same intent, lease or run); verify an unknown outcome before retrying and never replay a
  completed action.
- `NEEDS_REPREPARE` → `integration-refresh` and validate again. A real conflict moves only that
  intent to `ATTENTION_REQUIRED`; use `integration-retry` and `integration-claim
  --resolve-conflicts` as advertised. HEAD or scope drift in a candidate → `quarantine`.
- Blocked execution processes: `doctor --namespace-pid <pid>` before blaming metadata. Missing
  process evidence is `UNKNOWN`, not proof of a dead writer.
- `BINDING_IDENTITY_DRIFT`: treat the checkout as unmanaged; its real owner adopts it.

## Invariants

- Controller alone creates and removes linked worktrees: never raw `git worktree add/remove`,
  never edit `<git-common-dir>/worktree-controller/` metadata, journals, locks or the host
  resource ledger, and never hand-delete directories, registrations, branches or tags.
- Release needs exact ownership, a recoverable ref, classified resources and no live lease or
  runtime/editable binding. Age, cleanliness, missing leases or a vanished directory are not
  release evidence; release is not integration, publication or business completion.
- Rows owned by other tasks are reported, not adopted; the same owner string does not prove the
  same session. Dirty work stays with its owner.
- Integration scope is the union of recorded source scopes; out-of-scope commits block
  publication. `preserved` is never promoted implicitly to `integration-ready`.
- The publication lease is repository-global and succeeds only when latest main is an ancestor
  of the exact candidate. Expired or replaced writers reread remote facts and stop mutating.
- Module tasks return control at `integration-ready`, `preserved` or `blocked` and do not
  release unrelated worktrees. Shared daemons and unrelated sessions stay untouched.
- Controller tool updates go through `scripts/controller-activate` (`stage`, `activate`,
  `sync-skill`) with the declared toolchain catalog; finish with the installed
  `tool-skill-sync doctor --repo <checkout> --summary`.
