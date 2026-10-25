"""Repository-wide nonterminal mutation conflicts; retained evidence is not a lock."""

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from . import cleanup_evidence as e
from .models import ControllerError
from .store import MetadataStore, record_from_dict

CONTRACT = "controller-transaction-conflict/v1"

_OID_LENGTHS = frozenset({40, 64})
_HEX = frozenset("0123456789abcdef")


def is_atomic_temp(name: str) -> bool:
    """True for ``store._atomic_json`` interrupted writes: ``.<target>.<8>.tmp``.

    Such a file never became the authoritative record because its rename into
    place did not complete, so journal and evidence scans skip it instead of
    letting debris fence the whole repository.
    """
    return name.startswith(".") and name.endswith(".tmp")


def _require_flock(store: MetadataStore, *, assume_serialized: bool = False) -> None:
    if not store.flock_held and not assume_serialized:
        raise ControllerError(
            "MISSING_REPOSITORY_LOCK",
            "terminal transaction classification requires the held repository flock; "
            "call it inside store.lock() or pass assume_serialized from a locked caller",
        )


def _is_oid(value: str) -> bool:
    return len(value) in _OID_LENGTHS and _HEX.issuperset(value)


def _files_ref_storage(common_dir: Path) -> bool:
    """False unless refs live in the filesystem layout this classifier reads."""
    try:
        raw = (common_dir / "config").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError):
        return False
    section = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].split(maxsplit=1)[0].lower().strip('"')
        elif "=" in stripped and section == "extensions":
            key, _, value = stripped.partition("=")
            if key.strip().lower() == "refstorage" and "reftable" in value.strip().lower():
                return False
    return True


def _branch_ref_state(common_dir: Path, ref: str) -> tuple[str | None, str]:
    """Filesystem-only ref read: (oid, resolved) | (None, absent) | (None, uncertain).

    Absence is only proven for the files backend when the loose path is truly
    missing and packed-refs was scanned without a hit; a symref, a dangling
    link, an unreadable file, or another ref backend is uncertainty, which
    keeps a journal pending instead of proving a shape nobody observed.
    """
    if not _files_ref_storage(common_dir):
        return None, "uncertain"
    loose = common_dir.joinpath(*ref.split("/"))
    try:
        raw = loose.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raw = None
    except (OSError, UnicodeError):
        return None, "uncertain"
    if raw is not None:
        return (raw, "resolved") if _is_oid(raw) else (None, "uncertain")
    if loose.is_symlink():
        return None, "uncertain"
    try:
        packed = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "absent"
    except (OSError, UnicodeError):
        return None, "uncertain"
    for line in packed.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        oid, _, name = line.partition(" ")
        if name.strip() == ref:
            return (oid, "resolved") if _is_oid(oid) else (None, "uncertain")
    return None, "absent"


def _worktree_registration(common_dir: Path, target: Path) -> tuple[str | None, str | None, str]:
    """Filesystem-only scan of linked-worktree admin entries for ``target``.

    Returns (entry key, HEAD text, state) with state in registered/absent/
    uncertain; two entries claiming one checkout is uncertainty.
    """
    records = common_dir / "worktrees"
    if records.is_symlink():
        return None, None, "uncertain"
    if not records.is_dir():
        return None, None, "absent"
    found: tuple[str, str | None] | None = None
    try:
        entries = sorted(records.iterdir())
    except OSError:
        return None, None, "uncertain"
    for entry in entries:
        try:
            value = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return None, None, "uncertain"
        if value and Path(value).parent == target:
            if found is not None:
                return None, None, "uncertain"
            try:
                head = (entry / "HEAD").read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                head = None
            except (OSError, UnicodeError):
                return None, None, "uncertain"
            found = (entry.name, head)
    if found is None:
        return None, None, "absent"
    return found[0], found[1], "registered"


def _target_state(target: Path) -> str:
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "uncertain"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "directory" if stat.S_ISDIR(info.st_mode) else "present_other"


def _binding_for_key(store: MetadataStore, key: str) -> tuple[str | None, str]:
    """The allocation a worktree key binds: (allocation, present) or state reason."""
    path = store.bindings_dir / f"{key}.json"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "uncertain"
    if not stat.S_ISREG(info.st_mode):
        return None, "uncertain"
    try:
        value = dict(store._read_json(path))
    except ControllerError:
        return None, "uncertain"
    allocation = value.get("allocation_id")
    return (allocation if isinstance(allocation, str) else None), "present"


def _binding_allocation_for_entry(store: MetadataStore, entry_name: str) -> tuple[str | None, str]:
    """Resolve the binding that GitBackend derives from a worktree admin entry."""
    import hashlib

    digest = hashlib.sha256(f"worktrees/{entry_name}".encode()).hexdigest()
    return _binding_for_key(store, f"wt_{digest}")


def _allocation_bound_anywhere(store: MetadataStore, allocation: str) -> str:
    """'bound', 'unbound' or 'uncertain' — full bindings scan, fail-closed."""
    if store.bindings_dir.is_symlink():
        return "uncertain"
    if not store.bindings_dir.is_dir():
        return "unbound"
    try:
        paths = sorted(store.bindings_dir.glob("*.json"))
    except OSError:
        return "uncertain"
    for path in paths:
        try:
            value = dict(store._read_json(path))
        except ControllerError:
            return "uncertain"
        if value.get("allocation_id") == allocation:
            return "bound"
    return "unbound"


def _journal_preconditions(
    store: MetadataStore, transaction: str, command: str, phases: frozenset[str]
) -> dict | None:
    """Journal must exist, match this repository, and describe ``command``."""
    try:
        journal = dict(store._read_json(store.transactions_dir / f"{transaction}.json"))
    except ControllerError:
        return None
    if (
        journal.get("command") != command
        or journal.get("phase") not in phases
        or str(transaction) != journal.get("transaction_id")
        or journal.get("schema_version") not in {1, 2, 3, 4}
        or not journal.get("allocation_id")
        or not isinstance(journal.get("target_path"), str)
        or not Path(journal["target_path"]).is_absolute()
        or not isinstance(journal.get("branch"), str)
        or not journal.get("branch")
        or not isinstance(journal.get("expected_head"), str)
        or not _is_oid(journal["expected_head"])
    ):
        return None
    try:
        if journal.get("repository_id") != str(
            dict(store._read_json(store.root / "repo.json"))["repository_id"]
        ):
            return None
    except (ControllerError, KeyError):
        return None
    return journal


def _record_state(store: MetadataStore, allocation: str, expected_commit: str) -> str:
    """'match' | 'mismatch' | 'absent' | 'uncertain' for the allocation's record."""
    path = store.records_dir / f"{allocation}.json"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "uncertain"
    if not stat.S_ISREG(info.st_mode):
        return "uncertain"
    try:
        record = dict(store._read_json(path))
    except ControllerError:
        return "uncertain"
    if record.get("allocation_id") != allocation:
        return "uncertain"
    return "match" if record.get("task_commit") == expected_commit else "mismatch"


class TransactionConflict(ControllerError):
    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__("INTERRUPTED_TRANSACTION", "nonterminal repository transaction conflicts")
        self.transaction_conflicts = conflicts


def _worktree_administratively_registered(common_dir: Path, target: Path) -> bool:
    """True when Git still lists a linked worktree whose checkout is ``target``."""
    records = common_dir / "worktrees"
    if not records.is_dir():
        return False
    for entry in sorted(records.iterdir()):
        try:
            # The administrative gitdir holds the checkout's own .git pointer, so
            # its parent is the working tree that registered the entry.
            value = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value and Path(value).parent == target:
            return True
    return False


def release_evidence_terminal(store: MetadataStore, transaction: str) -> bool:
    """True when a retained resource-clean journal is superseded by exact release evidence.

    A cleanup whose own journal never finished can still be overtaken by a later
    release that did: the allocation is RELEASED, exactly one receipt binds that
    release to this allocation, target and record version, and the resource is
    physically gone.  That is the same immutable evidence the admission reconciler
    already demands, so the journal stops fencing without being rewritten; any
    missing or contradictory piece leaves it pending.  Only the three directories
    that carry that evidence are read, so this stays cheap inside pending().
    """
    try:
        journal = dict(store._read_json(store.transactions_dir / f"{transaction}.json"))
    except ControllerError:
        return False
    if (journal.get("command") != "resource-clean" or journal.get("phase") != "PREPARED"
            or str(transaction) != journal.get("transaction_id")
            or not journal.get("allocation_id")
            or not isinstance(journal.get("target_path"), str)):
        return False
    target = Path(journal["target_path"])
    if target.exists() or target.is_symlink():
        return False
    allocation = str(journal["allocation_id"])
    record_path = store.records_dir / f"{allocation}.json"
    if (store.writer_leases_dir / f"{allocation}.json").exists() or not record_path.is_file():
        return False
    try:
        record = dict(store._read_json(record_path))
        repository_id = str(dict(store._read_json(store.repo_file))["repository_id"])
    except (ControllerError, KeyError):
        return False
    if (record.get("allocation_id") != allocation or record.get("lifecycle") != "RELEASED"
            or journal.get("repository_id") != repository_id):
        return False
    if _worktree_administratively_registered(store.root.parents[1], target):
        return False
    matches = 0
    for path in sorted(store.release_receipts_dir.glob("*.json")) if store.release_receipts_dir.is_dir() else ():
        try:
            receipt = dict(store._read_json(path))
        except ControllerError:
            continue
        if (receipt.get("repository_id"), receipt.get("allocation_id"), receipt.get("target_path"),
                receipt.get("released_record_version"), receipt.get("result")) == (
                repository_id, allocation, str(target), record.get("record_version"), "RELEASED"):
            matches += 1
    return matches == 1


def _acquire_handoff_head(store: MetadataStore, journal: dict) -> str | None:
    """A later frozen handoff can positively prove a completed acquisition."""
    if journal["phase"] != "METADATA_WRITTEN":
        return None
    try:
        record = record_from_dict(
            store._read_json(store.records_dir / f"{journal['allocation_id']}.json")
        )
        first = record.history[0]
        head = record.handoff_commit
        if (
            record.allocation_id != journal["allocation_id"]
            or record.role not in {"feature", "audit"}
            or record.lifecycle not in {"HANDOFF_READY", "PUBLISHED", "RELEASED"}
            or record.base_commit != journal["expected_head"]
            or not head
            or head == journal["expected_head"]
            or record.task_commit != head
            or record.finalizer_commit != head
            or not record.finalizer_record_id
            or first.get("event") != "ACQUIRED"
            or first.get("from") is not None
            or first.get("to") != "ACTIVE"
            or first.get("at") != record.created_at
            or not any(event.get("event") == "HANDOFF_INTEGRATION_READY"
                       and event.get("to") == "HANDOFF_READY" for event in record.history)
        ):
            return None
        from .git_backend import is_ancestor

        return head if is_ancestor(store.root.parents[1], record.base_commit, head) else None
    except (ControllerError, OSError, ValueError, KeyError, IndexError):
        return None


def _acquire_retirement_evidence(store: MetadataStore, journal: dict, release: dict) -> bool:
    """An exact retirement preserves release proof after its retained ref disappears.

    A still-open authorization also admits settlement of that very deletion. It
    does not settle the authorization, which keeps its independent rollout fence.
    """
    from .branch_retirement import _load_authorization, _load_plan

    for path in sorted(store.branch_retirement_plans_dir.glob("*.json")):
        try:
            plan = _load_plan(store, path.stem)
            identity = {
                "repository_id": journal["repository_id"],
                "allocation_id": journal["allocation_id"],
                "branch": journal["branch"],
                "expected_oid": release["released_head"],
            }
            if (
                any(plan.get(key) != value for key, value in identity.items())
                or plan.get("release_receipt_id") != release.get("receipt_id")
                or plan.get("eligible") is not True
                or plan.get("blockers") != []
            ):
                continue
            receipt_path = store.branch_retirement_receipts_dir / f"{path.stem}-local.json"
            if receipt_path.is_file():
                receipt = dict(store._read_json(receipt_path))
                if (
                    receipt.get("schema_version") == 2
                    and receipt.get("plan_id") == path.stem
                    and receipt.get("receipt_id")
                    == hashlib.sha256(f"{path.stem}:local".encode()).hexdigest()
                    and receipt.get("operation") == "local"
                    and receipt.get("branch") == identity["branch"]
                    and receipt.get("expected_oid") == identity["expected_oid"]
                    and receipt.get("expected_integrated_oid")
                    == plan.get("expected_integrated_oid")
                    and receipt.get("provenance") == "AUTHORIZED_EXECUTION"
                    and receipt.get("finalizer_result") == "REF_REMOVED_AT_EXPECTED_OID"
                    and receipt.get("observed_ref") == f"refs/heads/{identity['branch']}"
                    and receipt.get("observed_at_settle") is None
                ):
                    return True
            for grant_path in sorted(store.branch_retirement_authorizations_dir.glob("*.json")):
                grant = _load_authorization(store, grant_path.stem)
                if (
                    grant.get("plan_id") == path.stem
                    and all(grant.get(key) == value for key, value in identity.items())
                    and grant.get("operation") == "local"
                    and grant.get("ref") == f"refs/heads/{identity['branch']}"
                    and grant.get("expected_integrated_oid") == plan.get("expected_integrated_oid")
                    and grant.get("controller_state_digest") == plan.get("controller_state_digest")
                    and grant.get("minimum_revalidation", {}).get("ref_state") == "PRESENT_EXPECTED"
                ):
                    return True
        except (ControllerError, OSError, KeyError, TypeError, ValueError):
            continue
    return False


def _acquire_task_progress(store: MetadataStore, journal: dict, head: str) -> str | None:
    """Prove creation survived task progress, without asserting task completion."""
    if journal["phase"] != "METADATA_WRITTEN" or not isinstance(head, str) or not _is_oid(head):
        return None
    try:
        record = record_from_dict(
            store._read_json(store.records_dir / f"{journal['allocation_id']}.json")
        )
        first = record.history[0]
        if (
            record.allocation_id != journal["allocation_id"]
            or record.role not in {"feature", "audit"}
            or record.lifecycle not in {"ACTIVE", "RETAINED", "ARCHIVED", "RELEASED"}
            or record.base_commit != journal["expected_head"]
            or record.task_commit != journal["expected_head"]
            or record.handoff_commit is not None
            or record.finalizer_commit is not None
            or record.finalizer_record_id is not None
            or first.get("event") != "ACQUIRED"
            or first.get("from") is not None
            or first.get("to") != "ACTIVE"
            or first.get("at") != record.created_at
        ):
            return None
        from .git_backend import is_ancestor

        if not is_ancestor(store.root.parents[1], journal["expected_head"], head):
            return None
        if record.lifecycle == "ARCHIVED":
            from .delivered_closeout import _archive_receipt_binding

            _archive_receipt_binding(
                store, record, journal["repository_id"],
                target=Path(journal["target_path"]), branch=journal["branch"], head=head,
            )
        return record.lifecycle
    except (ControllerError, OSError, ValueError, KeyError, IndexError):
        return None


def acquire_precedes_removed_release(store: MetadataStore, transaction: str, release: dict) -> bool:
    """Permit receipt recovery, never terminal classification, for this exact predecessor.

    A retained acquire cannot become terminal until the release receipt exists. Only a
    release already recorded at its exact +1 CAS may finish that receipt in its presence.
    Both journals stay authoritative; ordinary mutations and rollout still see them pending.
    """
    _require_flock(store)
    acquire = _journal_preconditions(
        store, transaction, "acquire", frozenset({"METADATA_WRITTEN"})
    )
    if acquire is None or release.get("phase") != "RELEASED_RECORDED":
        return False
    identity = ("repository_id", "allocation_id", "target_path", "branch")
    if any(acquire.get(key) != release.get(key) for key in identity):
        return False
    plan_id = release.get("release_plan_id")
    if not isinstance(plan_id, str):
        return False
    snapshot = store._load_unlocked()
    plan = snapshot.release_plans.get(plan_id)
    record = snapshot.records.get(acquire["allocation_id"])
    if plan is None or record is None or not record.history:
        return False
    if any(release.get(key) != getattr(plan, key) for key in (
        *identity, "expected_head", "expected_record_version",
    )):
        return False
    first, last = record.history[0], record.history[-1]
    if (
        record.role not in {"feature", "audit"}
        or record.role != plan.role
        or record.authority_key != plan.authority_key
        or record.base_commit != acquire["expected_head"]
        or record.task_commit != plan.expected_head
        or record.lifecycle != "RELEASED"
        or record.owner != "unassigned"
        or record.record_version != plan.expected_record_version + 1
        or first.get("event") != "ACQUIRED"
        or first.get("from") is not None
        or first.get("to") != "ACTIVE"
        or first.get("at") != record.created_at
        or last.get("event") not in {"RELEASED", "RELEASE_RECOVERED"}
        or last.get("from") != plan.lifecycle
        or last.get("to") != "RELEASED"
        or _target_state(Path(plan.target_path)) != "absent"
        or _worktree_registration(store.root.parents[1], Path(plan.target_path))[2] != "absent"
        or _allocation_bound_anywhere(store, plan.allocation_id) != "unbound"
        or (store.writer_leases_dir / f"{plan.allocation_id}.json").exists()
        or _branch_ref_state(store.root.parents[1], f"refs/heads/{plan.branch}")
        != (plan.expected_head, "resolved")
    ):
        return False
    from .git_backend import is_ancestor

    return is_ancestor(store.root.parents[1], acquire["expected_head"], plan.expected_head)


def acquire_evidence_terminal(store: MetadataStore, transaction: str) -> str | None:
    """Close an interrupted acquire only on positive current-state proof.

    The completion shape is the whole effect this journal's allocation could
    have produced: the target checkout directory exists and is administratively
    registered, the binding under that worktree key names this allocation, the
    record names this allocation with the acquired base as its task commit,
    and the branch still exactly equals that base. It is admitted from
    WORKTREE_ADDED or METADATA_WRITTEN, which covers the post-write record
    step racing the journal phase. The no-effect shape is the total absence of
    every one of those facts (plus the target itself), admitted from PREPARED
    or from a WORKTREE_ADDED crash whose orphan the operator has fully cleaned
    up. Any half-shape or unreadable element stays pending and fail-closed.
    A later frozen handoff can advance that commit only with its original
    ACQUIRED identity, unchanged base, matching frozen commits and ancestry.
    An unfrozen ACTIVE/RETAINED feature or audit may advance its attached branch
    with the original ACQUIRED identity, unchanged task/base and ancestry. This
    proves acquisition only; its lifecycle and unfinished task remain untouched.
    ARCHIVED progress additionally binds its immutable archive receipt. RELEASED
    progress still requires exact release, absence and retained-ref/retirement proof.
    A later guarded release supersedes METADATA_WRITTEN only when its exact
    receipt, released record, retained branch (or exact retirement), and absent
    checkout all agree.
    """
    journal = _journal_preconditions(
        store, transaction, "acquire", frozenset({"PREPARED", "WORKTREE_ADDED", "METADATA_WRITTEN"})
    )
    if journal is None:
        return None
    common_dir = store.root.parents[1]
    target = Path(journal["target_path"])
    allocation = str(journal["allocation_id"])
    branch_oid, branch_state = _branch_ref_state(common_dir, f"refs/heads/{journal['branch']}")
    key, admin_head, registration = _worktree_registration(common_dir, target)
    target_state = _target_state(target)
    expected_head = journal["expected_head"]
    handoff_head = None
    if branch_state in {"resolved", "absent"} and branch_oid != expected_head:
        handoff_head = _acquire_handoff_head(store, journal)
        if handoff_head is not None:
            expected_head = handoff_head
    record_state = _record_state(store, allocation, expected_head)
    if (
        journal["phase"] in {"WORKTREE_ADDED", "METADATA_WRITTEN"}
        and registration == "registered"
        and target_state == "directory"
    ):
        binding_allocation, binding_state = _binding_allocation_for_entry(store, str(key))
        if (
            binding_state == "present"
            and binding_allocation == allocation
            and branch_state == "resolved"
            and record_state == "match"
        ):
            attached = admin_head == f"ref: refs/heads/{journal['branch']}"
            if branch_oid == expected_head and (handoff_head is None or attached):
                return "ACQUIRE_HANDOFF_SUPERSEDED" if handoff_head else "ACQUIRE_EVIDENCE_SUPERSEDED"
            if (handoff_head is None and attached
                    and _acquire_task_progress(store, journal, branch_oid)
                    in {"ACTIVE", "RETAINED", "ARCHIVED"}):
                return "ACQUIRE_EVIDENCE_SUPERSEDED"
    if (
        journal["phase"] == "METADATA_WRITTEN"
        and registration == "absent"
        and target_state == "absent"
        and branch_state in {"absent", "resolved"}
        and record_state == "match"
        and _allocation_bound_anywhere(store, allocation) == "unbound"
        and not (store.writer_leases_dir / f"{allocation}.json").exists()
    ):
        try:
            record = dict(store._read_json(store.records_dir / f"{allocation}.json"))
        except ControllerError:
            record = {}
        if record.get("lifecycle") == "RELEASED":
            matches = []
            for path in sorted(store.release_receipts_dir.glob("*.json")):
                try:
                    receipt = dict(store._read_json(path))
                except ControllerError:
                    continue
                if receipt.get("allocation_id") != allocation:
                    continue
                released_head = receipt.get("released_head")
                progressed = (
                    handoff_head is None
                    and _acquire_task_progress(store, journal, released_head) == "RELEASED"
                )
                if (
                    receipt.get("repository_id") == journal["repository_id"]
                    and receipt.get("target_path") == str(target)
                    and receipt.get("branch") == journal["branch"]
                    and (released_head == expected_head or progressed)
                    and (branch_state == "absent" or branch_oid == released_head)
                    and receipt.get("released_record_version") == record.get("record_version")
                    and receipt.get("result") == "RELEASED"
                ):
                    matches.append(receipt)
                else:
                    return None
            if len(matches) == 1 and (
                branch_state == "resolved" or _acquire_retirement_evidence(store, journal, matches[0])
            ):
                return "ACQUIRE_RELEASE_SUPERSEDED"
    if (
        journal["phase"] in {"PREPARED", "WORKTREE_ADDED"}
        and registration == "absent"
        and branch_state == "absent"
        and target_state == "absent"
        and record_state == "absent"
        and _allocation_bound_anywhere(store, allocation) == "unbound"
    ):
        return "ACQUIRE_NO_EFFECT_SUPERSEDED"
    return None


def integration_refresh_terminal(store: MetadataStore, transaction: str) -> str | None:
    """Close an interrupted refresh on ref/record state, never on phase alone.

    A detach that still dangles at the candidate, a worktree whose admin HEAD
    no longer names its branch, a moved branch, or a record that disagrees with
    the journal is a real effect only the operator can resolve, so every such
    half-shape keeps the fence. The no-effect shape requires the checkout
    present and attached with branch and record still at the journal's old
    head; the completion shape requires branch and record both advanced to the
    recorded candidate with the worktree reattached.
    """
    journal = _journal_preconditions(
        store,
        transaction,
        "integration-refresh",
        frozenset({"REFRESH_PREPARED", "DETACHED_AT_NEW_HEAD", "BRANCH_UPDATED"}),
    )
    if journal is None:
        return None
    new_head = journal.get("new_head")
    if not isinstance(new_head, str) or not _is_oid(new_head):
        return None
    if not isinstance(journal.get("expected_record_version"), int):
        return None
    common_dir = store.root.parents[1]
    target = Path(journal["target_path"])
    allocation = str(journal["allocation_id"])
    ref = f"refs/heads/{journal['branch']}"
    _key, head, registration = _worktree_registration(common_dir, target)
    if registration != "registered" or _target_state(target) != "directory":
        return None
    if head != f"ref: {ref}":
        return None
    branch_oid, branch_state = _branch_ref_state(common_dir, ref)
    if branch_state != "resolved":
        return None
    if (
        journal["phase"] == "BRANCH_UPDATED"
        and branch_oid == new_head
        and _record_state(store, allocation, new_head) == "match"
    ):
        return "REFRESH_EVIDENCE_SUPERSEDED"
    if (
        journal["phase"] in {"REFRESH_PREPARED", "DETACHED_AT_NEW_HEAD"}
        and branch_oid == journal["expected_head"]
        and _record_state(store, allocation, journal["expected_head"]) == "match"
    ):
        return "REFRESH_NO_EFFECT_SUPERSEDED"
    return None


def registration_prune_terminal(store: MetadataStore, transaction: str) -> bool:
    """A missing audit registration is terminal only with exact receipt and metadata."""
    path = store.transactions_dir / f"{transaction}.json"
    try:
        journal = dict(store._read_json(path))
    except ControllerError:
        return False
    if journal.get("command") != "registration-prune":
        return False
    allocation = journal.get("allocation_id")
    target = journal.get("target_path")
    key = journal.get("worktree_key")
    version = journal.get("expected_record_version")
    if (
        journal.get("transaction_id") != transaction
        or not isinstance(allocation, str)
        or not isinstance(target, str)
        or not Path(target).is_absolute()
        or not isinstance(key, str)
        or not isinstance(version, int)
        or not isinstance(journal.get("admin_path"), str)
        or not isinstance(journal.get("preserving_oid"), str)
        or not _is_oid(journal["preserving_oid"])
        or journal.get("phase") not in {"PREPARED", "WORKTREE_REMOVED", "RELEASED_RECORDED"}
    ):
        return False
    common = store.root.parents[1]
    if (
        _target_state(Path(target)) != "absent"
        or _target_state(Path(journal["admin_path"])) != "absent"
        or _worktree_registration(common, Path(target))[2] != "absent"
        or _binding_for_key(store, key)[1] != "absent"
        or _allocation_bound_anywhere(store, allocation) != "unbound"
    ):
        return False
    try:
        record = dict(store._read_json(store.records_dir / f"{allocation}.json"))
        receipt = dict(
            store._read_json(store.registration_prune_receipts_dir / f"{transaction}.json")
        )
    except ControllerError:
        return False
    return (
        record.get("allocation_id") == allocation
        and record.get("lifecycle") == "RELEASED"
        and record.get("owner") == "unassigned"
        and record.get("record_version") == version + 1
        and receipt.get("contract") == "registration-prune/v1"
        and receipt.get("receipt_id") == transaction
        and receipt.get("repository_id") == journal.get("repository_id")
        and receipt.get("allocation_id") == allocation
        and receipt.get("target_path") == target
        and receipt.get("worktree_key") == key
        and receipt.get("expected_head") == journal.get("expected_head")
        and receipt.get("expected_record_version") == version
        and receipt.get("released_record_version") == version + 1
        and receipt.get("preserving_ref") == journal.get("preserving_ref")
        and receipt.get("preserving_oid") == journal.get("preserving_oid")
        and receipt.get("gc_plan_id") == journal.get("gc_plan_id")
        and receipt.get("result") == "REGISTRATION_PRUNED"
    )


def terminal_state(
    store: MetadataStore,
    transaction: str,
    *,
    verify_generations: bool = False,
    assume_serialized: bool = False,
) -> str | None:
    """Only supported, exact immutable terminal evidence closes a retained journal.

    Classification is authority: it may run only while the caller actually
    holds the repository flock (or explicitly declares it is already serialized
    by a locked caller), so no reader can classify a journal that a live writer
    is still mid-flight updating.
    """
    _require_flock(store, assume_serialized=assume_serialized)
    from .cleanup_attempts import typed_terminal
    from .cleanup_recovery import completed
    from .source_recovery import terminal_state as source_terminal_state

    try:
        if typed_terminal(store, transaction):
            return "TYPED_ATTEMPT_SUPERSEDED"
        if completed(store, transaction, verify_generations=verify_generations):
            return "RECOVERY_CLEANED"
        source_state = source_terminal_state(
            store, transaction, verify_generation=verify_generations
        )
        if source_state is not None:
            return source_state
        if release_evidence_terminal(store, transaction):
            return "RELEASE_EVIDENCE_SUPERSEDED"
        acquire_state = acquire_evidence_terminal(store, transaction)
        if acquire_state is not None:
            return acquire_state
        refresh_state = integration_refresh_terminal(store, transaction)
        if refresh_state is not None:
            return refresh_state
        if registration_prune_terminal(store, transaction):
            return "REGISTRATION_PRUNE_EVIDENCE_SUPERSEDED"
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ControllerError(
            "INVALID_TRANSACTION", "terminal transaction evidence is invalid or unavailable"
        ) from exc
    return None


def terminal(
    store: MetadataStore,
    transaction: str,
    *,
    verify_generations: bool = False,
    assume_serialized: bool = False,
) -> bool:
    return (
        terminal_state(
            store, transaction, verify_generations=verify_generations,
            assume_serialized=assume_serialized,
        )
        is not None
    )


def pending(
    store: MetadataStore,
    *,
    assume_serialized: bool = False,
    relevant: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Every journal identity is validated; terminal evidence only for relevant ones.

    Terminal classification can re-verify large historical evidence, so a caller
    that only consumes some journals names them with ``relevant`` instead of
    filtering afterwards. The result equals the unfiltered result filtered by it.
    """
    _require_flock(store, assume_serialized=assume_serialized)
    if not store.transactions_dir.exists():
        return []
    repository = store._read_json(store.root / "repo.json")["repository_id"]
    result = []
    for path in sorted(store.transactions_dir.iterdir()):
        if is_atomic_temp(path.name):
            continue
        try:
            value, _ = e.read(path)
            if (
                path.suffix != ".json"
                or str(UUID(path.stem)) != path.stem
                or value.get("transaction_id") != path.stem
                or value.get("repository_id") != repository
                or not value.get("allocation_id")
                or not isinstance(value.get("target_path"), str)
                or not Path(value["target_path"]).is_absolute()
                or value.get("command")
                not in {
                    "acquire",
                    "release",
                    "integration-refresh",
                    "resource-clean",
                    "registration-prune",
                }
                or value.get("schema_version") not in {1, 2, 3, 4}
                or value.get("phase")
                not in {
                    "PREPARED",
                    "TEARDOWN_PREPARED",
                    "VENV_REMOVAL_STARTED",
                    "VENV_REMOVED",
                    "WORKTREE_ADDED",
                    "METADATA_WRITTEN",
                    "WORKTREE_REMOVED",
                    "RELEASED_RECORDED",
                    "REFRESH_PREPARED",
                    "DETACHED_AT_NEW_HEAD",
                    "BRANCH_UPDATED",
                }
            ):
                raise ValueError("unknown transaction identity or lifecycle")
        except (OSError, ValueError, TypeError, KeyError, ControllerError) as exc:
            raise TransactionConflict(
                [{"journal": str(path), "lifecycle": "UNKNOWN", "reason": "INVALID_TRANSACTION"}]
            ) from exc
        if relevant is not None and not relevant(value):
            continue
        if not terminal(store, path.stem, assume_serialized=assume_serialized):
            result.append(value)
    return result


def conflicts(
    store: MetadataStore,
    *,
    allocation: str | None,
    target: Path,
    assume_serialized: bool = False,
) -> list[dict]:
    """Persisted uncertainty fences its actual resource scope, not the repository.

    The repository lock still serializes each mutation. Unknown journal identity
    remains repository-wide through pending(); validated terminal evidence is not
    an occupancy. Paths are compared without following potentially changed links.
    """
    def overlaps(row: dict) -> bool:
        other = Path(row["target_path"])
        return (row["allocation_id"] == allocation or target == other
                or target in other.parents or other in target.parents)

    return pending(store, assume_serialized=assume_serialized, relevant=overlaps)


def check_source(
    store: MetadataStore,
    allocation: str,
    target: Path,
    current: dict | None,
    *,
    assume_serialized: bool = False,
) -> None:
    """Admit only the exact current attempt within the affected resource scope."""
    transactions = conflicts(
        store, allocation=allocation, target=target, assume_serialized=assume_serialized
    )
    if current is not None:
        expected = store.transactions_dir / f"{current['transaction_id']}.json"
        actual, _ = e.read(expected)
        if (
            actual != current
            or current.get("allocation_id") != allocation
            or current.get("target_path") != str(target)
            or current.get("command") != "resource-clean"
            or current.get("cleanup_plan", {}).get("contract") != "tracked-source-cleanup/v1"
            or current not in transactions
        ):
            raise TransactionConflict(
                [{"journal": str(expected), "reason": "CURRENT_IDENTITY_MISMATCH"}]
            )
    blocked = [
        {
            "repository_id": row["repository_id"],
            "allocation_id": row["allocation_id"],
            "transaction_id": row["transaction_id"],
            "target_path": row["target_path"],
            "operation": row["command"],
            "resource_scope": row.get("cleanup_plan", {}).get("contract"),
            "lifecycle": "NONTERMINAL",
            "phase": row["phase"],
        }
        for row in transactions
        if row != current
    ]
    if blocked:
        raise TransactionConflict(blocked)
