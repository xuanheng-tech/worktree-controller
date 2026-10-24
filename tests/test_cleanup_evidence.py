"""Characterize the shared reader's bytes, refusal codes and filesystem races."""

import hashlib
import json
import os
from contextlib import contextmanager

import pytest

from worktree_controller import cleanup_evidence as evidence
from worktree_controller import preservation
from worktree_controller.models import ControllerError


@pytest.fixture
def document(tmp_path):
    directory = tmp_path / "generation"
    directory.mkdir()
    path = directory / "receipt.json"
    path.write_bytes(b'{"value":1}\n')
    return path


@pytest.mark.parametrize("raw", [b'{"value":1}\n', b'{ "value": 1 }\n'])
def test_read_returns_the_exact_raw_digest_without_rewriting(document, raw):
    document.write_bytes(raw)
    before = document.stat()
    expected = hashlib.sha256(raw).hexdigest()

    assert evidence.read(document, expected) == ({"value": 1}, expected)

    assert document.read_bytes() == raw
    after = document.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino, before.st_mtime_ns, before.st_ctime_ns,
    )


def test_read_refuses_a_changed_raw_digest(document):
    with pytest.raises(ControllerError) as error:
        evidence.read(document, "0" * 64)
    assert error.value.code == "HARDLINK_TOPOLOGY_BLOCKED"
    assert str(error.value) == "immutable cleanup evidence bytes changed"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"1", b'"value"'])
def test_read_retains_the_non_object_refusal(document, raw):
    document.write_bytes(raw)
    with pytest.raises(ControllerError) as error:
        evidence.read(document)
    assert error.value.code == "PRESERVATION_COVERAGE_INVALID"
    assert str(error.value) == "invalid preservation document"


def test_read_retains_the_json_parse_error(document):
    document.write_bytes(b'{"broken"')
    with pytest.raises(json.JSONDecodeError):
        evidence.read(document)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_read_refuses_non_regular_entries_without_following_them(document, kind):
    document.unlink()
    if kind == "symlink":
        target = document.with_name("target.json")
        target.write_bytes(b'{"value":1}\n')
        document.symlink_to(target.name)
    elif kind == "directory":
        document.mkdir()
    else:
        os.mkfifo(document)
    with pytest.raises(ControllerError) as error:
        evidence.read(document)
    assert error.value.code == "PRESERVATION_SOURCE_CHANGED"


def test_read_retains_the_64_mib_bound(document):
    with document.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ControllerError) as error:
        evidence.read(document)
    assert error.value.code == "PRESERVATION_COVERAGE_INVALID"
    assert str(error.value) == "preservation evidence exceeds its bound"


def test_read_refuses_replacement_after_reading_identical_bytes(document, monkeypatch):
    original = preservation._regular_fd
    raw = document.read_bytes()

    @contextmanager
    def replace_after_read(*arguments):
        with original(*arguments) as fd:
            yield fd
            document.unlink()
            document.write_bytes(raw)

    monkeypatch.setattr(preservation, "_regular_fd", replace_after_read)
    with pytest.raises(ControllerError) as error:
        evidence.read(document)
    assert error.value.code == "PRESERVATION_SOURCE_CHANGED"


def test_read_refuses_replacement_of_its_directory(document, monkeypatch):
    original = preservation._regular_fd

    @contextmanager
    def replace_directory_after_read(*arguments):
        with original(*arguments) as fd:
            yield fd
        directory = document.parent
        directory.rename(directory.with_name("moved-generation"))
        directory.mkdir()

    monkeypatch.setattr(preservation, "_regular_fd", replace_directory_after_read)
    with pytest.raises(ControllerError) as error:
        evidence.read(document)
    assert error.value.code == "RESOURCE_PLAN_MISMATCH"
