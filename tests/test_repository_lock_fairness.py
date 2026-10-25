"""New readers must not bypass an admitted writer; cleanup history stays readable."""

import fcntl
import multiprocessing
import os
import signal
from pathlib import Path

import pytest

from worktree_controller.store import MetadataStore


def _participant(common, kind, connection):
    store = MetadataStore(Path(common))
    original = fcntl.flock
    identity = os.stat(store.lock_file)

    def announce(descriptor, operation):
        opened = os.fstat(descriptor)
        if operation == fcntl.LOCK_EX and (opened.st_dev, opened.st_ino) == (
            identity.st_dev, identity.st_ino
        ):
            connection.send("writer-requesting")
        return original(descriptor, operation)

    writer = kind in {"writer", "waiting-crash"}
    if writer:
        fcntl.flock = announce
    if kind == "waiting-crash":
        signal.signal(signal.SIGALRM, lambda *_: os._exit(97))
        signal.alarm(1)
    try:
        if kind == "legacy":
            with store.lock_file.open("rb") as stream:
                original(stream.fileno(), fcntl.LOCK_EX)
                connection.send("entered")
                assert connection.recv() == "release"
        else:
            if kind == "late-reader":
                connection.send("reader-ready")
            lock = store.cleanup_lock() if kind == "cleanup" else store.lock(exclusive=writer)
            with lock:
                connection.send("entered")
                if kind == "recursive":
                    assert connection.recv() == "nested"
                    with MetadataStore(Path(common)).lock(exclusive=False):
                        connection.send("nested-entered")
                command = connection.recv()
                if command == "crash":
                    os._exit(97)
                assert command == "release"
        connection.send("finished")
    finally:
        fcntl.flock = original
        connection.close()


@pytest.fixture
def lock_store(tmp_path):
    store = MetadataStore(tmp_path / "repo.git")
    store.root.mkdir(parents=True)
    store.lock_file.touch(mode=0o600)
    return store


@pytest.fixture
def participants(lock_store):
    children = []

    def start(kind):
        parent, child = multiprocessing.get_context("spawn").Pipe()
        process = multiprocessing.get_context("spawn").Process(
            target=_participant,
            args=(str(lock_store.root.parents[1]), kind, child),
        )
        process.start()
        child.close()
        children.append((process, parent))
        return parent

    yield start
    for process, connection in children:
        if process.is_alive():
            process.terminate()
        process.join(10)
        connection.close()


def receive(connection, expected):
    assert connection.poll(10), f"lock participant did not reach {expected}"
    assert connection.recv() == expected


def finish(connection):
    connection.send("release")
    receive(connection, "finished")


@pytest.mark.parametrize("reader_count", [1, 8])
def test_later_shared_reader_waits_behind_an_admitted_writer(lock_store, participants, reader_count):
    with lock_store.lock(exclusive=False):
        writer = participants("writer")
        receive(writer, "writer-requesting")
        readers = [participants("late-reader") for _ in range(reader_count)]
        for reader in readers:
            receive(reader, "reader-ready")
        assert not any(reader.poll(0.2) for reader in readers), "a later reader bypassed the writer"
    receive(writer, "entered")
    assert not any(reader.poll(0.1) for reader in readers)
    finish(writer)
    for reader in readers:
        receive(reader, "entered")
        finish(reader)


def test_nested_shared_reads_do_not_deadlock_behind_a_waiting_writer(participants):
    reader = participants("recursive")
    receive(reader, "entered")
    writer = participants("writer")
    receive(writer, "writer-requesting")
    reader.send("nested")
    receive(reader, "nested-entered")
    finish(reader)
    receive(writer, "entered")
    finish(writer)


def test_cleanup_still_admits_history_reads_with_a_waiting_writer(lock_store, participants):
    with lock_store.cleanup_lock():
        writer = participants("writer")
        receive(writer, "writer-requesting")
        reader = participants("reader")
        receive(reader, "entered")
        finish(reader)
        assert not writer.poll(0.2)
    receive(writer, "entered")
    finish(writer)


def test_legacy_exclusive_holder_still_excludes_new_readers(lock_store, participants):
    legacy = participants("legacy")
    receive(legacy, "entered")
    reader = participants("reader")
    assert not reader.poll(0.5)
    finish(legacy)
    receive(reader, "entered")
    finish(reader)


def test_body_failure_releases_entry_and_repository_locks(lock_store):
    failure = ValueError("fixture body failure")
    with pytest.raises(ValueError) as observed, lock_store.lock(exclusive=True):
        raise failure
    assert observed.value is failure
    with lock_store.lock(exclusive=False):
        assert lock_store.flock_held


@pytest.mark.parametrize("kind", ["writer", "cleanup"])
def test_crashed_holder_releases_its_locks_without_state_repair(lock_store, participants, kind):
    holder = participants(kind)
    if kind == "writer":
        receive(holder, "writer-requesting")
    receive(holder, "entered")
    holder.send("crash")
    assert holder.poll(10)
    with pytest.raises(EOFError):
        holder.recv()
    with lock_store.lock(exclusive=True):
        assert lock_store.flock_held


def test_waiting_writer_crash_does_not_leave_a_stale_entry_gate(lock_store, participants):
    with lock_store.lock(exclusive=False):
        writer = participants("waiting-crash")
        receive(writer, "writer-requesting")
        assert writer.poll(10)
        with pytest.raises(EOFError):
            writer.recv()
        reader = participants("reader")
        receive(reader, "entered")
        finish(reader)
