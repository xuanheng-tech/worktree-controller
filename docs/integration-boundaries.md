# Consumer and protocol boundaries

Inventory verified on 2026-10-23 against WC 1.32.3 (CLI 61, storage 5) and GF 1.9.7.
This is a source inventory, not an observation of every installed host or external user.
The public [CLI contract](../tool_cli_contract.json) remains authoritative.
[Scope](scope.md) records the conservative decision to keep existing commands supported.

## Existing delivery protocols

| Flow | Producer authority | Consumer responsibility | Required evidence and refusal |
| --- | --- | --- | --- |
| Integration publication | WC `integration-publication-session/v1`; repository lock spans verify, authorize, push observation and completion | GF publishes the exact authorized candidate with a non-force push and independently observes the remote | Repository/allocation, lease/run, candidate, expected main, record version and validation identity must match. Missing capability, drift or incomplete completion blocks. After uncertain transport, inspect remote facts and re-enter the same identity. |
| Branch retirement | WC `branch-retirement-execution/v1` and `retirement-execution-result/v1` | GF performs only the plan-authorized exact-OID ref operation | The public plan, verify, authorize and complete results bind the operation and ancestry proof. Missing refs alone do not establish a completed retirement; retain recovery identity. |
| Sensitive source review | GF owns its review-file schema | Treat the four WC linkage fields as opaque identities | No derivation of private authority digests or parsing WC metadata to construct authority. |
| Review evidence | SR owns `snapshot-runner read`, evidence-read schema 1 and stored-era validation | GF's explicit initial-publish mode consumes public fields | Unknown schema, partial output, gaps or identity mismatch blocks; GF does not parse SR storage or hard-code its security epoch. |
| Deployment health | The published toolchain catalog, currently owned by GF | `tool-skill-sync doctor` verifies installed capabilities and the frozen runtime contract | Explicit CLI/storage allowlists and required capability versions all apply. Source availability or equal CLI numbers alone do not prove compatibility. |

These protocols already exist. The next step is preserving and testing them, not designing a
replacement message bus or accepting unknown versions. Protocol/capability versioning does not
relax the deployment catalog's explicit CLI/storage compatibility checks.

GF is independently usable for its core Git operations. WC/SR adapters run only when their
corresponding operation is requested. GF's
[adapter inventory and regression matrix](https://github.com/xuanheng-tech/git-finalizer/blob/828c35a3bc3d60c5ef25f1fb5221a086e749c0b5/docs/integration-boundaries.md)
is the consumer-owned authority. WC owns lifecycle; a GF execution receipt still needs the
formal `lease-complete` flow before lifecycle completion.

Two bounded legacy couplings remain: GF checks WC namespace directory **existence** to report
a migration blocker, and recognizes exact public contract descriptions in its scanner. Neither
is general private-state parsing. Replace the existence probe only after a producer-owned
public unmanaged/legacy/migrated verdict exists and all negative controls pass. Keep the narrow
scanner recognition until an agreed producer/consumer contract change replaces it; never widen
the scanner or silently fall back to private files.

## Host consumers

A fresh inspection of AW's current controller client confirms the following source call paths:

| Surface | Evidence classification | Ownership decision |
| --- | --- | --- |
| `host-execution`, `execution-admit/cancel`, `admission-queue` | Direct calls | WC governs admission and resource claims; AW keeps business scheduling and host policy. |
| `session-admit` | Direct `_invoke` call after capability negotiation | Supported. The earlier audit's absence claim is obsolete. |
| `session-bind` | Argument tuple passed through `_run`, rather than a literal `_invoke` call | Supported. An `_invoke`-only search misses this consumer. |
| `binding-add/remove/transfer` | Transfer is direct; add/remove are dynamically composed by a bounded helper | Supported; explicit client/session identity and existing lease checks remain required. |
| `lifecycle-events`, `lifecycle-event-ack` | Direct calls | WC owns event identity; AW acknowledges after its own durable update. |
| `reconcile-session` | No direct external AW CLI call found in this inspection; WC's shipped session-end adapter calls the lifecycle function | Supported. No proof of exclusive use or safe deprecation. |

Also observed: plan/acquire/resume/status/capabilities, writer lease acquire/release and write
guard; integration queue/refresh/validate, publication lease acquire/complete, reconciliation,
release-intent and lifecycle-history. Static literals are evidence of a call path, not runtime
frequency or a complete list of dynamically constructed commands.

No AW identity default, resource limit, QP policy or business approval changes in this inventory.
Any future host-policy relocation needs consumer-owned changes, staged compatibility, migration
and rollback. Recovery commands remain supported until all consumers have an evidence-backed
migration and a published deprecation window.

## Regression and change gates

- WC: public CLI/API contract tests plus the affected lifecycle/recovery suites, and the complete
  integration gate for shared changes. A pure helper extraction preserves clocks, locks, request
  identity, immutable history, refusal codes, retry timing and aliases.
- GF: its existing boundary tests, public-protocol negative controls and required real WC/SR
  release gate. A consumer stub is not a substitute for real producer acceptance.
- Host: installed capability checks and the consumer's existing acceptance command before
  activation. Source inspection is not installed-runtime or live-business acceptance.
- Preserve old receipts and explicit version allowlists. Do not count an expected nonzero refusal
  as functional success, and do not infer business completion from a clean checkout.
