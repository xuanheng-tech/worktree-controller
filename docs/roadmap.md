# Maintenance roadmap

Execution baseline: 2026-10-23. This is the WC portion of the approved four-tool maintenance
direction. Each stage needs its own tested deliverable; later ideas are not current behavior.

## Current baseline

WC 1.32.5 retains CLI contract 61 and storage layout 5. The scope and architecture documents
are already on main. Previous extractions reduced private cross-module imports from 93 to
82 and then 80; pure scope hashing/overlap queries lowered it to 78. This maintenance batch
moves allocation-to-intent lookup into `semantics`, reducing the baseline to 76 while retaining
its duplicate-identity refusal and lifecycle alias. Mutation helpers, clocks and lock scope stay
with their existing owner. Batch this source maintenance into the next planned release; it does
not declare a new package release or change the installed 1.32.5 runtime.

GF 1.9.8 already consumes SR's public read API and WC's versioned publication/retirement
protocols. CL 1.3.10 and SR 2.5.12 already use main on both hosting platforms. These completed
items are not prerequisites still waiting to happen.

[Consumer boundaries](integration-boundaries.md) correct the earlier AW session-call inventory.
Remaining evidence gaps are external consumer usage, installed-host behavior beyond the existing
acceptance gates, and historical temporary-file impact. No historical evidence is cleaned up.

## Sequence and acceptance

| Stage | Deliverable | Exit evidence | Stop or rollback boundary |
| --- | --- | --- | --- |
| Now: boundaries | Current protocol and consumer inventory, including legacy adapters | Named owner per interface, source references, explicit unknowns; no changes to supported commands | Conflicting evidence keeps the interface supported |
| Now: WC structure | One cohesive pure release-intent query extraction | Full integration gate; existing renewal/cancellation/recovery behavior; CLI 61/storage 5 unchanged | Revert the isolated extraction if behavior differs |
| Shared quality checks: delivered | CL/SR adopt release-tools with a reviewed full commit pin | Both hosted parallel pilots and final three-version checks passed; SR retains its package check | Restore the pinned original consumer workflow; PyPI/Gitea flows remain owned by consumers |
| Next 2–6 weeks | Further WC extraction only where a shared responsibility is clear; measure maintenance burden and failures | One behavior-preserving batch at a time, consumer gates, shrinking private-import baseline | Keep injected clocks, transaction and crash hooks with their owner until an explicit migration is tested |
| Following 6–12 weeks | Evaluate moving manifests, compatibility catalog and shared Skills from GF to a dedicated toolchain owner | Full ownership inventory, staged installer/migration, exactly one active writer, historical pin and rollback tests | No second simultaneous Skill authority; no ownership switch on file-copy evidence |
| Following 3–6 months | Evaluate a staged GF Python core migration | Characterization of exit/status/ref effects, isolated old/new comparisons, interruption recovery and real consumer gates | No dual production writes; preserve scanner refusals and existing installation until replacement passes |

The time ranges express ordering, not release deadlines. Batch internal maintenance with a
planned software release instead of increasing the CLI contract for every refactor. A public
behavior change still requires explicit contract/capability review and compatibility updates.

## Shared workflow pilot boundary

Start with CL and retain its Python matrix and `just check`; only after hosted equivalence is
proved adopt SR with its additional package check. Pin all workflow dependencies to reviewed
commits. Keep upload jobs and their Trusted Publisher/environment in each repository's top-level
release workflow. First publication of a new public repository requires its own authorization.

Do not introduce a shared runtime Python dependency into CL/SR, merge the tools, relax SR's
Python floor, move host-specific defaults without migration, or replace existing WC/GF protocols.
GitHub-first release consolidation and a future toolchain repository remain design decisions,
not permission to disable existing Gitea delivery or platform controls.

The current maintenance task moves allocation-to-intent lookup to `semantics`, lowering the
private-import baseline from 78 to 76. Use its real task to observe checkpoint pause, fresh-executor
resume, Finalizer handoff, integration and release; preserve pending results instead of interpreting
an advisory success as completion. The released publication visibility window keeps exact bytes,
identities, failed-command outcomes and same-bundle recovery mandatory. CL/SR shared quality checks
have passed their hosted parallel and final matrices.
