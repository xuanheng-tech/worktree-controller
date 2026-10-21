"""One command decodes each unchanged metadata file once; nothing outlives it."""

import json
import os
import runpy
from contextlib import nullcontext
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository
from worktree_controller import store as store_module
from worktree_controller.cli import main
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore, decode_cache


def _store(repository):
    return MetadataStore(inventory(repository.repo).common_dir)


def test_a_command_reuses_unchanged_decodes_and_rereads_replaced_files():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocated = acquire_one(repository)
        allocation = allocated["decision"]["allocation_id"]
        store = _store(repository)
        with decode_cache():
            first = store.load().records[allocation]
            assert store.load().records[allocation] is first
            path = store.records_dir / f"{allocation}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["record_version"] += 1
            store._atomic_json(path, value)
            replaced = store.load().records[allocation]
            assert replaced is not first and replaced.record_version == first.record_version + 1
        assert store_module._DECODE_CACHE is None
        assert store.load().records[allocation] is not replaced


def test_in_place_rewrite_with_new_content_is_never_served_from_the_cache():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocated = acquire_one(repository)
        allocation = allocated["decision"]["allocation_id"]
        store = _store(repository)
        path = store.records_dir / f"{allocation}.json"
        with decode_cache():
            first = store.load().records[allocation]
            value = json.loads(path.read_text(encoding="utf-8"))
            value["record_version"] += 1
            info = os.stat(path)
            path.write_text(json.dumps(value, indent=4), encoding="utf-8")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            assert store.load().records[allocation].record_version == first.record_version + 1


def test_every_cli_invocation_starts_and_ends_without_a_cache(capsys):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        acquire_one(repository)
        assert main(["status", "--repo", str(repository.repo), "--json"]) == 0
        capsys.readouterr()
        assert store_module._DECODE_CACHE is None
        assert main(["plan", "--repo", str(repository.repo), "--task-key", "next",
                     "--authority-key", "next", "--module", "test", "--role", "feature",
                     "--json"]) == 0
        capsys.readouterr()
        assert store_module._DECODE_CACHE is None


@pytest.mark.parametrize("command", ("status", "doctor", "lifecycle-history"))
def test_single_snapshot_queries_still_validate_replaced_metadata(command, capsys):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocated = acquire_one(repository)
        arguments = [command, "--repo", str(repository.repo), "--json"]
        if command == "lifecycle-history":
            arguments += ["--allocation-id", allocated["decision"]["allocation_id"]]
        assert main(arguments) == 0
        assert not json.loads(capsys.readouterr().out)["errors"]
        store = _store(repository)
        path = store.records_dir / f"{allocated['decision']['allocation_id']}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["allocation_id"] = "00000000-0000-0000-0000-000000000001"
        store._atomic_json(path, value)
        assert main(arguments) == (1 if command == "doctor" else 2)
        assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "INVALID_METADATA"
        assert store_module._DECODE_CACHE is None


@pytest.mark.parametrize("command", ("execution-admit", "execution-cancel"))
def test_execution_commands_revalidate_corrupted_metadata_between_invocations(command, capsys):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, allocated = acquire_one(repository)
        arguments = [command, "--repo", str(repository.repo), "--path", str(target),
                     "--owner", "module-owner", "--client-id", "fixture", "--session-id", "fixture",
                     "--run-id", "new", "--json"]
        assert main(arguments) == 0
        assert not json.loads(capsys.readouterr().out)["errors"]
        assert store_module._DECODE_CACHE is None
        store = _store(repository)
        path = store.records_dir / f"{allocated['decision']['allocation_id']}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["allocation_id"] = "00000000-0000-0000-0000-000000000001"
        store._atomic_json(path, value)
        assert main(arguments) == 2
        assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "INVALID_METADATA"
        assert store_module._DECODE_CACHE is None


HISTORY_DIRECTORIES = (
    ("records_dir", "records"),
    ("prepared_candidate_receipts_dir", "prepared_candidate_receipts"),
    ("publication_receipts_dir", "publication_receipts"),
    ("release_plans_dir", "release_plans"),
    ("release_receipts_dir", "release_receipts"),
)


@pytest.fixture
def historical_store(tmp_path):
    helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark-cli"))
    repo = helpers["fixture"](tmp_path, 2)
    return MetadataStore(inventory(repo).common_dir)


@pytest.mark.parametrize("directory,field", HISTORY_DIRECTORIES)
@pytest.mark.parametrize("reuse", (False, True))
def test_history_snapshots_do_not_share_mutable_mappings(historical_store, directory, field, reuse):
    store = historical_store
    with decode_cache(reuse=reuse):
        original = getattr(store.load(), field)
        expected = original.copy()
        original.clear()
        second = getattr(store.load(), field)
        assert second == expected
        second.pop(next(iter(second)))
        assert getattr(store.load(), field) == expected


@pytest.mark.parametrize("directory,field", HISTORY_DIRECTORIES)
@pytest.mark.parametrize("reuse", (False, True))
def test_history_revalidates_in_place_edits_without_directory_changes(historical_store, directory, field, reuse):
    store = historical_store
    root = getattr(store, directory)
    path = next(root.glob("*.json"))
    with decode_cache(reuse=reuse):
        assert getattr(store.load(), field)
        before = root.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 999
        path.write_text(json.dumps(value), encoding="utf-8")
        assert root.stat().st_mtime_ns == before.st_mtime_ns
        with pytest.raises(ControllerError) as rejected:
            store.load()
        assert rejected.value.code == "INVALID_METADATA"


@pytest.mark.parametrize("directory,field", HISTORY_DIRECTORIES)
@pytest.mark.parametrize("reuse", (None, False, True))
def test_history_refreshes_removed_added_and_misnamed_files(historical_store, directory, field, reuse):
    store = historical_store
    root = getattr(store, directory)
    path = sorted(root.glob("*.json"), key=lambda path: path.name)[0]
    identity = path.stem
    value = json.loads(path.read_text(encoding="utf-8"))
    with decode_cache(reuse=reuse) if reuse is not None else nullcontext():
        first = getattr(store.load(), field)
        assert list(first) == sorted(first)
        with store.lock(exclusive=True):
            path.unlink()
        assert identity not in getattr(store.load(), field)
        with store.lock(exclusive=True):
            store._atomic_json(path, value)
        restored = getattr(store.load(), field)
        assert restored == first and restored[identity] is not first[identity]
        with store.lock(exclusive=True):
            store._atomic_json(root / ".unexpected.json", value)
        with pytest.raises(ControllerError, match="filename"):
            store.load()


@pytest.mark.parametrize("directory,field", HISTORY_DIRECTORIES)
@pytest.mark.parametrize("reuse", (None, False, True))
def test_history_revalidates_schema_after_atomic_replacement(historical_store, directory, field, reuse):
    store = historical_store
    path = next(getattr(store, directory).glob("*.json"))
    with decode_cache(reuse=reuse) if reuse is not None else nullcontext():
        assert getattr(store.load(), field)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 999
        with store.lock(exclusive=True):
            store._atomic_json(path, value)
        with pytest.raises(ControllerError) as rejected:
            store.load()
        assert rejected.value.code == "INVALID_METADATA"


def test_history_scan_keeps_case_sensitive_suffix_matching(historical_store):
    store = historical_store
    first = store.load()
    for directory, _field in HISTORY_DIRECTORIES:
        root = getattr(store, directory)
        (root / "unrelated.JSON").write_text("invalid", encoding="utf-8")
        (root / ".pending.json.tmp").write_text("invalid", encoding="utf-8")
    assert store.load() == first


@pytest.mark.parametrize("kind", ("directory", "broken-symlink"))
@pytest.mark.parametrize("reuse", (None, False, True))
def test_matching_nonregular_history_entries_are_not_silently_filtered(historical_store, kind, reuse):
    store = historical_store
    path = store.records_dir / ".invalid.json"
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(store.records_dir / "missing")
    with decode_cache(reuse=reuse) if reuse is not None else nullcontext(), pytest.raises(ControllerError) as rejected:
        store.load()
    assert rejected.value.code == "INVALID_METADATA"
    assert str(path) in str(rejected.value)


@pytest.mark.parametrize("reuse", (None, False, True))
def test_history_observes_replacement_after_directory_enumeration(historical_store, monkeypatch, reuse):
    store = historical_store
    path = sorted(store.records_dir.glob("*.json"), key=lambda path: path.name)[0]
    scan = store_module._json_entries
    with decode_cache(reuse=reuse) if reuse is not None else nullcontext():
        first = store.load().records[path.stem]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["record_version"] += 1

        def replace_after_scan(directory):
            entries = scan(directory)
            if directory == store.records_dir:
                store._atomic_json(path, value)
            return entries

        monkeypatch.setattr(store_module, "_json_entries", replace_after_scan)
        replaced = store.load().records[path.stem]
        assert replaced is not first and replaced.record_version == first.record_version + 1


@pytest.mark.parametrize("input_kind", ("path", "entry"))
@pytest.mark.parametrize("payload", (
    b'{"value": 1}',
    '{"message": "\u6c49\u5b57 Caf\u00e9", "escaped": "\\n"}\r\n'.encode(),
    b'{\r"items": [true, null, 1.25]\r}\r',
    b'{"duplicate": 1, "duplicate": 2, "escaped": "\\ud800"}',
    b'{"nonfinite": Infinity}',
    b'[]',
    b'null',
    b'"text"',
    b'',
    b'{"truncated":',
    b'{"invalid_utf8": "\xff"}',
    b'\xef\xbb\xbf{}',
))
def test_uncached_reads_preserve_utf8_json_and_error_contract(tmp_path, input_kind, payload):
    store = MetadataStore(tmp_path)
    path = tmp_path / "metadata.json"
    path.write_bytes(payload)
    with os.scandir(tmp_path) as entries:
        entry = next(item for item in entries if item.name == path.name)
    source = path if input_kind == "path" else entry
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as original:
        with pytest.raises(ControllerError) as rejected:
            store._decoded(source, dict)
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == f"cannot read {path}: {original}"
        assert type(rejected.value.__cause__) is type(original)
        assert str(rejected.value.__cause__) == str(original)
    else:
        if isinstance(expected, dict):
            assert store._decoded(source, dict) == expected
        else:
            with pytest.raises(ControllerError) as rejected:
                store._decoded(source, dict)
            assert rejected.value.code == "INVALID_METADATA"
            assert str(rejected.value) == f"metadata must be an object: {path}"
            assert rejected.value.__cause__ is None


@pytest.mark.parametrize("input_kind", ("path", "entry"))
@pytest.mark.parametrize("kind", ("missing", "directory", "broken-symlink"))
def test_uncached_read_failures_preserve_os_error_and_filename(tmp_path, input_kind, kind):
    store = MetadataStore(tmp_path)
    path = tmp_path / "metadata.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "broken-symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_text("{}", encoding="utf-8")
    with os.scandir(tmp_path) as entries:
        entry = next(item for item in entries if item.name == path.name)
    if kind == "missing":
        path.unlink()
    source = path if input_kind == "path" else entry
    with pytest.raises(OSError) as original:
        path.read_text(encoding="utf-8")
    with pytest.raises(ControllerError) as rejected:
        store._decoded(source, dict)
    assert rejected.value.code == "INVALID_METADATA"
    assert str(rejected.value) == f"cannot read {path}: {original.value}"
    assert type(rejected.value.__cause__) is type(original.value)
    assert str(rejected.value.__cause__) == str(original.value)


@pytest.mark.parametrize("input_kind", ("path", "entry"))
@pytest.mark.parametrize("reuse", (False, True))
@pytest.mark.parametrize("payload", (
    '{"message": "汉字 Café", "escaped": "\\n"}\r\n'.encode(),
    b'{"duplicate": 1, "duplicate": 2, "escaped": "\\ud800"}',
    b'{"nonfinite": Infinity}',
    b'{\r\n"value":\r\n}',
    b'{"invalid_utf8": "' + b'x' * 9000 + b'\xff"}',
    b'\xef\xbb\xbf{}',
    b'[]',
))
def test_cli_readers_preserve_byte_decoding_and_error_details(tmp_path, input_kind, reuse, payload):
    store = MetadataStore(tmp_path)
    path = tmp_path / "metadata.json"
    path.write_bytes(payload)
    with os.scandir(tmp_path) as entries:
        entry = next(item for item in entries if item.name == path.name)
    source = path if input_kind == "path" else entry
    with decode_cache(reuse=reuse):
        try:
            expected = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as original:
            with pytest.raises(ControllerError) as rejected:
                store._decoded(source, dict)
            assert rejected.value.code == "INVALID_METADATA"
            assert str(rejected.value) == f"cannot read {path}: {original}"
            assert type(rejected.value.__cause__) is type(original)
            assert str(rejected.value.__cause__) == str(original)
        else:
            if isinstance(expected, dict):
                assert store._decoded(source, dict) == expected
            else:
                with pytest.raises(ControllerError) as rejected:
                    store._decoded(source, dict)
                assert rejected.value.code == "INVALID_METADATA"
                assert str(rejected.value) == f"metadata must be an object: {path}"
                assert rejected.value.__cause__ is None


@pytest.mark.parametrize("reuse", (False, True))
def test_cli_reader_keeps_descriptor_identity_failure_closed(tmp_path, monkeypatch, reuse):
    path = tmp_path / "metadata.json"
    path.write_text("{}", encoding="utf-8")
    failure = OSError("descriptor identity unavailable")

    def refuse(descriptor):
        raise failure

    monkeypatch.setattr(store_module.os, "fstat", refuse)
    with decode_cache(reuse=reuse), pytest.raises(ControllerError) as rejected:
        MetadataStore(tmp_path)._decoded(path, dict)
    assert rejected.value.code == "INVALID_METADATA"
    assert str(rejected.value) == f"cannot read {path}: {failure}"
    assert rejected.value.__cause__ is failure


def test_single_snapshot_reader_retains_no_decoded_values(historical_store):
    with decode_cache(reuse=False):
        first, second = historical_store.load(), historical_store.load()
        assert first == second
        for _directory, field in HISTORY_DIRECTORIES:
            before, after = getattr(first, field), getattr(second, field)
            assert all(after[key] is not value for key, value in before.items())
        assert store_module._DECODE_CACHE is None and store_module._HISTORY_CACHE is None
    assert not store_module._BYTE_READS


@pytest.mark.parametrize("outer_reuse", (False, True))
def test_nested_reader_restores_reuse_and_reader_mode_after_error(tmp_path, outer_reuse):
    store = MetadataStore(tmp_path)
    path = tmp_path / "metadata.json"
    path.write_text('{"value": 1}', encoding="utf-8")
    with decode_cache(reuse=outer_reuse):
        first = store._decoded(path, dict)
        with pytest.raises(ControllerError), decode_cache(reuse=not outer_reuse):
            nested = store._decoded(path, dict)
            assert nested == first and nested is not first
            path.write_bytes(b'{\r\n"value":\r\n}')
            store._decoded(path, dict)
        with pytest.raises(ControllerError, match=r"char 13"):
            store._decoded(path, dict)
        path.write_text('{"value": 2}', encoding="utf-8")
        replaced = store._decoded(path, dict)
        assert replaced == {"value": 2}
        again = store._decoded(path, dict)
        assert (again is replaced) == outer_reuse
    assert store_module._DECODE_CACHE is None and store_module._HISTORY_CACHE is None
    assert not store_module._BYTE_READS
