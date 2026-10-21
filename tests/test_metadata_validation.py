"""Metadata identities preserve acceptance, text normalization and errors."""

import uuid

import pytest

from worktree_controller.metadata_codec import (
    _optional_oid,
    _optional_sha256,
    _optional_uuid,
    _required_oid,
    _required_sha256,
)
from worktree_controller.models import ControllerError


@pytest.mark.parametrize(
    "validate,length,invalid_message",
    (
        (_required_oid, 40, "identity must be a full SHA-1 commit OID"),
        (_optional_oid, 40, "identity must be a full SHA-1 commit OID"),
        (_required_sha256, 64, "identity must be a SHA-256 digest"),
        (_optional_sha256, 64, "identity must be a SHA-256 digest"),
    ),
)
def test_hex_identity_acceptance_and_error_contract(validate, length, invalid_message):
    valid = ("0123456789abcdef" * 4)[:length]
    for value in (valid, "0" * length, "f" * length):
        assert validate(value, "identity") == value
        assert validate(f" \t\r\n\u2003{value}\u2003\n\r\t ", "identity") == value

    for value in (False, True, 0, 1, b"a" * length, [], {}, "", " \t\n\u2003"):
        with pytest.raises(ControllerError) as rejected:
            validate(value, "identity")
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == "identity must be a non-empty string"

    for value in ("\x00", valid + "\x00", "\x00" + valid):
        with pytest.raises(ControllerError) as rejected:
            validate(value, "identity")
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == "identity contains a NUL byte"

    for value in (valid[:-1], valid + "a", "a" * 100_000):
        with pytest.raises(ControllerError) as rejected:
            validate(value, "identity")
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == invalid_message

    # Check every ASCII character at every position, including embedded whitespace.
    # Unicode digits/letters resembling hex remain invalid after outer whitespace is stripped.
    for character in (*map(chr, range(1, 128)), "\uff10", "\uff41", "\u0660", "\u00e9", "\u200b"):
        if character in "0123456789abcdef":
            continue
        for position in range(length):
            value = valid[:position] + character + valid[position + 1:]
            with pytest.raises(ControllerError) as rejected:
                validate(value, "identity")
            assert rejected.value.code == "INVALID_METADATA"
            assert str(rejected.value) == invalid_message


@pytest.mark.parametrize("validate", (_optional_oid, _optional_sha256))
def test_optional_hex_identity_preserves_absence(validate):
    assert validate(None, "identity") is None


@pytest.mark.parametrize(
    "validate,message",
    (
        (_required_oid, "identity is required"),
        (_required_sha256, "identity must be a non-empty string"),
    ),
)
def test_required_hex_identity_preserves_absence_error(validate, message):
    with pytest.raises(ControllerError) as rejected:
        validate(None, "identity")
    assert rejected.value.code == "INVALID_METADATA"
    assert str(rejected.value) == message


@pytest.mark.parametrize(
    "value",
    (
        "01234567-89ab-cdef-0123-456789abcdef",
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "01234567-89AB-CDEF-0123-456789ABCDEF",
        "01234567-89ab-CdEf-0123-456789aBcDeF",
        "0123456789abcdef0123456789abcdef",
        "{01234567-89ab-cdef-0123-456789abcdef}",
        "urn:uuid:01234567-89ab-cdef-0123-456789abcdef",
        "urn:uuid:{01234567-89ab-cdef-0123-456789abcdef}",
        "{{01234567-89ab-cdef-0123-456789abcdef}}",
        "0123456789ab-cdef0123-456789abcdef",
        "\u0660" * 32,
        "\uff10" * 32,
    ),
)
def test_optional_uuid_preserves_legacy_forms_and_text(value):
    uuid.UUID(value)
    assert _optional_uuid(value, "identity") == value
    assert _optional_uuid(f" \t\r\n\u2003{value}\u2003\n\r\t ", "identity") == value


def test_optional_uuid_preserves_absence_and_string_errors():
    assert _optional_uuid(None, "identity") is None
    for value in (False, True, 0, 1, b"0" * 32, [], {}, "", " \t\n\u2003"):
        with pytest.raises(ControllerError) as rejected:
            _optional_uuid(value, "identity")
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == "identity must be a non-empty string"
        assert rejected.value.__cause__ is None
    for value in ("\x00", "00000000-0000-0000-0000-000000000000\x00"):
        with pytest.raises(ControllerError) as rejected:
            _optional_uuid(value, "identity")
        assert rejected.value.code == "INVALID_METADATA"
        assert str(rejected.value) == "identity contains a NUL byte"
        assert rejected.value.__cause__ is None


def test_optional_uuid_matches_stdlib_for_near_matches():
    valid = "01234567-89ab-cdef-0123-456789abcdef"
    values = [valid[:-1], valid + "a", "a" * 100_000, "URN:UUID:" + valid]
    values.extend(
        valid[:position] + character + valid[position + 1:]
        for character in (*map(chr, range(1, 128)), "\uff10", "\uff41", "\u0660", "\u00e9", "\u200b")
        for position in range(len(valid))
    )
    for value in values:
        text = value.strip()
        try:
            uuid.UUID(text)
        except ValueError as original:
            with pytest.raises(ControllerError) as rejected:
                _optional_uuid(value, "identity")
            assert rejected.value.code == "INVALID_METADATA"
            assert str(rejected.value) == "identity must be a UUID"
            assert type(rejected.value.__cause__) is ValueError
            assert str(rejected.value.__cause__) == str(original)
        else:
            assert _optional_uuid(value, "identity") == text
