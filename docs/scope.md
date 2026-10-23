# Scope and consumer surface (draft for owner decision)

This document records which parts of the public CLI are generic worktree governance and which
exist mainly for one consumer. It is an inventory plus proposed boundaries; nothing here changes
behavior, and every "proposed" line needs an owner decision before code moves.

Snapshot: WC 1.32.3, CLI contract 61, 81 commands, 66 `*_version` capability keys. The public contract
counts were rechecked on 2026-10-23. Host call paths were rechecked against current
consumer source; this remains static evidence, not a runtime trace or proof of exclusive use.
See [consumer boundaries](integration-boundaries.md) for the updated protocol inventory.

## How the inventory was made

- **Agent Workspace (AW):** every `_invoke(...)` call in AW's controller client
  (`backend/src/agent_workspace/infrastructure/worktree_controller.py`), plus argument tuples
  passed through `_run`, bounded dynamic binding helpers, session-retirement and external-execution
  modules. The earlier `_invoke`-only inventory missed `session-bind`. This is not proof that
  dynamic or other consumers do not exist.
- **Git Finalizer (GF):** word match over `git-finalize*` and `tooling/*.py`. Common words
  (`review`, `task`, `release`, `resume`, `status`, `doctor`) can be false positives; the
  retirement and publication entries are confirmed by GF's adapter code.
- **Docs/examples:** word match over `README.md`, `docs/`, `examples/` and the shipped Skill.

## 1. Generic governance core (keep; any client)

Checkout budget, ownership and safe release for any person or agent sharing a repository:

`plan`, `status`, `doctor`, `capabilities`, `acquire`, `adopt`, `park`, `reopen`, `release`,
`release-intent`, `quarantine`, `review`, `handoff`, `resume`, `task`, `complete-no-change`,
`recovery-guide`, `lifecycle-history`, `write-guard`, `writer-lease-acquire`,
`writer-lease-release`, `writer-session`, `lease-acquire`, `lease-complete`, `lease-abort`,
`integration-enqueue`, `integration-claim`, `integration-validate`, `integration-queue`,
`integration-refresh`, `reconcile`.

## 2. Delivery-tool protocols (keep; versioned, consumed by GF)

`branch-retirement-plan`, `-verify`, `-authorize`, `-complete`, `-record`, `-status`,
`-executions`, and `publication-execution-session`. These are the public protocols GF consumes
through versioned capabilities and protocols. This does not relax explicit CLI/storage version
allowlists in the deployment catalog or permit unknown versions. See [boundaries](integration-boundaries.md).

## 3. Host-runtime integration (used by AW today)

| Command | Used by AW | Note |
| --- | --- | --- |
| `host-execution` | yes | Budgets a host process with no repository writer lease; default `--client-id` is `agent-workspace` |
| `execution-admit`, `execution-cancel` | yes | Durable admission of governed executions |
| `admission-queue` | yes | Periodic queue reconciliation |
| `binding-add`, `binding-remove`, `binding-transfer` | yes | Runtime bindings owned by a host |
| `lifecycle-events`, `lifecycle-event-ack` | yes | Event feed with consumer acknowledgement |
| `session-admit`, `session-bind` | yes, confirmed in current source | Capability-negotiated direct/tuple call paths; retain support |
| `reconcile-session` | no direct external AW CLI call found | WC session-end calls its lifecycle function; other external consumers remain unverified |

**Proposed:** keep these in WC only if they are documented as a generic *host runtime protocol*
(any host, explicit `--client-id`, no AW-specific defaults or wording). Otherwise move the
AW-specific policy (memory budgets, default identities) into AW and keep WC's part to admission
and binding records.

## 4. Recovery and maintenance tail

`recover-handoff`, `recover-published`, `recovery-adopt`, `release-recovery-inspect`,
`integration-abort`, `-abandon`, `-reopen`, `-resolve`, `-retry`, `-equivalent`,
`-recovery-matrix`, `close-delivered`, `close-preserved`, `adopt-delivered`, `archive-retained`,
`externalize`, `resource-register`, `resource-clean`, `resource-recover`, `gc-plan`, `gc-apply`,
`reconcile-writers`, `acquire-across`, `acquire-when-free`, `canonical-refresh`,
`canonical-transition`, `namespace-migration-plan`, `-verify`, `-receipts`, `namespace-migrate`.

A static AW/GF inventory cannot establish usage by operators, other hosts or indirect adapters.
These commands remain supported; lack of a literal call is not deprecation evidence.

**Proposed:** classify each as *supported*, *operator-only* or *deprecated* in the CLI contract,
publish a deprecation window for the last group, and stop adding a new top-level command per
recovery case — prefer one `recover` entry with typed subcases.

## Open questions for the owner

1. Is `host-execution` meant for hosts other than AW? If not, should its policy move to AW?
2. Which additional consumers use the session interfaces? AW use of `session-admit` and
   `session-bind` is confirmed; direct external `reconcile-session` usage remains unverified.
3. Which section-4 commands may be deprecated, and on what timeline?

## Current compatibility decision

Keep every existing command supported while these ownership questions are unresolved. Do not
remove host commands, change default identities, relax recovery guards or deprecate commands
from an absence of static matches. New hosts use explicit client/session identities and the
versioned public contracts described in [client integration](client-integration.md). WC remains
the lifecycle and policy authority; consumers retain their business and release approvals.

Effective integration-state interpretation lives in `semantics.py`;
record and intent updates live in `record_updates.py`. These moves preserve CLI contract 61 and
storage layout 5. See [architecture](architecture.md) for module responsibilities.
