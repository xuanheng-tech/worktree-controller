# Architecture and maintenance boundaries

The Controller adds authority and lifecycle to Git facts. It does not replace Git, the caller's
test/review evidence, or Git Finalizer publication. See [operations](operations.md) for the normal
development and recovery paths; exact public interfaces live in the [contract](../tool_cli_contract.json).

## Responsibilities

| Area | Existing implementation | Invariant |
| --- | --- | --- |
| CLI parsing and routing | `cli.py`, command modules | Dispatch explicit operations; do not duplicate authority decisions |
| Git observations and mutations | `git_backend.py` | Git owns registrations, refs, paths, HEAD and cleanliness |
| State and durable evidence | `models.py`, `store.py`, `metadata_codec.py`, `record_updates.py` | Validate stored identities; serialize mutations under the repository lock; every record or intent update bumps its version and appends history |
| Shared identity and semantic checks | `semantics.py` | Consume the caller's snapshot; do not acquire authority or write metadata |
| Budget admission | `resource_admission.py` | Preserve durable request identity and host reservations across retries |
| Writer entitlement | `writer_lease.py`, `writer_session.py`, `governance.py` | Bind the real process identity and exact operation/target |
| Lifecycle orchestration | `lifecycle.py` and focused recovery modules | Compose existing guards and append durable transition evidence |
| Environment and resource inspection | `environment.py`, `disposable.py`, preservation modules | Incomplete observation blocks dependent deletion; unknown content is retained |
| Publication execution | `publication_execution.py` and Finalizer protocol | Exact candidate, validation, lease and current remote main must agree |
| Idle-work maintenance | `convergence.py`, `reconciler.py` | Only delegated authority closes eligible unused allocations |

This is a responsibility map, not a claim of an acyclic module graph. Lifecycle orchestration
still imports and is imported by focused modules, including deferred imports. New code should use
the narrow existing owner of a guard instead of importing lifecycle merely to look up a record.

`semantics.record_for`, `managed_record` and `assert_record_version` are internal shared helpers.
They preserve binding lookup, Git registration checks and allocation/version CAS errors. They do
not refresh a snapshot or establish a transaction. A mutation caller must retain its existing
repository lock and apply the relevant role, owner, lifecycle, resource and runtime checks.
Compatibility aliases in lifecycle/controller keep existing internal call sites usable; new
consumers should import the shared helpers directly.

## Durable state and recovery

Resolve the repository's actual Git common directory; a linked checkout's `.git` can be a file.
Controller Git commands drop inherited repository selection (`GIT_DIR`, `GIT_INDEX_FILE` and the
other variables in `git_backend.REPOSITORY_SELECTION_ENVIRONMENT`). A worktree key is derived
from the Git administrative directory name, which Git reuses after a removal; a linked binding
therefore also carries a `worktree-registration/v1` token that must match the marker inside that
administrative directory. `MetadataStore` withdraws a mismatched binding for every consumer and
reports `BINDING_IDENTITY_DRIFT`; new binding writers pass the registration to `write_record`.
Bindings that predate registrations gain one only through `registration_backfill`, which ties
them to the live registration on matching Git and record evidence and writes nothing else.
Every directory under the metadata root is a `MetadataStore` attribute declared in the layout
contract; a listing failure other than absence fails closed.
The `worktree-controller/v1` namespace suffix is not its storage layout version. Repository
metadata and host admission reservations have separate locks and identities. Never infer an
allocation, completed cleanup, publication or release from a missing path or a clean Git status.

Before changing a transition, follow its ordinary path and its interrupted path: persisted request,
allocation, frozen handoff, integration preparation, validation, publication and release receipts.
Use typed recovery entries for partial operations. Preserve the original request and evidence;
an error explanation must not silently substitute a new intent or relax the original guard.

## Validation and future changes

Start with regressions for the observed failure and its unchanged refusal boundary. For shared
authority checks, cover both successful callers and the error taxonomy. Use `check-shards` for
the parallel integration gate and `check-project` for the complete package/release gate. Build
and installed smoke results are distinct from a live external client's behavior.

History growth should be measured with `scripts/benchmark-cli` before changing storage. Compare
pinned executables on the same synthetic fixtures, alternating repeated calls, and retain the
history sizes, timing, peak RSS and cache limitations. A faster lookup must not skip fresh evidence
validation or introduce a second authoritative state store. Indexing or additional caching needs
a demonstrated bottleneck and explicit invalidation/recovery tests.

Runtime scans that gate deletion read every same-UID process. Positive references and read
failures other than permission denial block; a permission-denied same-UID process without a
verified holder association is a host visibility gap (for example a non-dumpable service
manager), reported by typed cleanup and tolerated by release. Both scans require the checkout to
keep one path and inode for the whole scan. The strict scan, which also refuses visibility gaps,
is for consumers that must prove complete visibility.

Further lifecycle extraction should follow one cohesive transition at a time, keeping its lock
scope and immutable evidence together. Avoid moving mutation helpers that depend on lifecycle's
clock, transaction state or caller-specific guards merely to reduce a line count. Public CLI,
capability and storage changes require their own version and compatibility review.
