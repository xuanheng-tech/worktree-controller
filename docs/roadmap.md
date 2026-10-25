# Maintenance roadmap

Execution baseline: 2026-10-25. This is the WC portion of the approved four-tool maintenance
direction. Each stage needs its own tested deliverable; later ideas are not current behavior.

## Current baseline

WC 1.32.6 completed the runtime-visibility fix and the previously planned allocation-to-intent
query extraction. The 1.32.8 maintenance release places shared bounded, FD-bound document reads
in the existing cleanup evidence module, removing four private imports (76 to 72). Raw-byte
hashes, file/ancestor checks, limits and refusal codes stay unchanged; mutation and crash hooks
retain their original owners. Release acceptance covers the exact installed candidate, ordinary
1.32.7 upgrades and historical 1.30.4 restoration. Publication and local activation remain
separate verified steps. The 1.32.9 patch skips unrelated live Git inventory for closed lifecycle selectors while
retaining full locked metadata and immutable evidence validation. The 1.32.10 patch keeps closed history readable during typed cleanup while preserving writer,
cleanup, diagnostic and activation serialization. Software version 1.32.11 retains
CLI contract 61, the existing capability versions and storage layout 5, and serializes doctor
snapshot reads with normal Controller mutations. Diagnostics refresh Git inventory after taking
the shared repository lock, then retain it through semantic, transaction and retirement checks.
The regression uses a real guarded release and verifies the retained commit and branch; a truly
missing commit still refuses through both the API and CLI.

The 1.32.12 patch adds cooperating reader/writer entry ordering and an active-cleanup marker
on existing directories, while retaining CLI 61 / storage 5. Repeated cleanup checks reuse only
unchanged unrelated receipt scopes inside one flock. Late-reader bypass and repeated decoding
are independently reproduced; long-held checks, mixed old runtimes and exact site latency
remain separate observations, not a claim that every historical delay has been explained.

The original intermittent commit-resolution report did not retain enough per-command evidence
to establish its exact cause. The isolated concurrency defect is reproducible; that does not
prove the historical report had the same cause. Site-specific owner actions remain separate
from WC repairs and package deployment.

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
| WC structure: delivered | Pure release-intent, scope and allocation-to-intent queries; shared FD-bound evidence reader | Existing behavior retained; private-import baseline 72; CLI 61/storage 5 unchanged; reader byte/race characterizations | Preserve clocks, mutation and crash-hook ownership; verify the planned maintenance release |
| Diagnostic stability: delivered | One coherent doctor snapshot across guarded lifecycle changes | 1.32.7 released and installed; real concurrent release, fresh inventory and negative API/CLI controls | Retain missing-commit and ownership refusals; preserve original incident uncertainty |
| Upgrade acceptance: delivered | Ordinary and historical cohorts with declared staging provenance | Matching installed pins, all selected cases pass; explicit prerequisite refusals and source/identity negative controls | Never infer provenance from origin, skip a selected failure or relax activation gates |
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

The allocation-to-intent maintenance task and the 1.32.7 delivery are complete. The real
diagnostic-stability task verified checkpoint pause, fresh-executor resume, Finalizer handoff,
integration and release. Use normal maintenance tasks for further observations; preserve pending
results instead of interpreting an advisory success as completion. Batch test-only maintenance
on main without republishing or activating an unchanged runtime.
The released publication visibility window keeps exact bytes,
identities, failed-command outcomes and same-bundle recovery mandatory. CL/SR shared quality checks
have passed their hosted parallel and final matrices.
