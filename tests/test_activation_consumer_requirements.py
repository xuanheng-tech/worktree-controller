"""Historical rollout gates cannot erase a current consumer's capability minimums."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests import test_rolling_upgrade
from tests.test_rolling_upgrade import load_activate
from tests.test_rolling_upgrade import toolchain_candidate as toolchain_candidate

CAPABILITY = "host_handoff_continuation_version"


def historical_gate(tmp_path, consumers, *, requirements=None, capabilities=True):
    source = tmp_path / "historical-source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    reported = {str(consumer): {} for consumer in consumers}
    if requirements is not None:
        for value in reported.values():
            value["required_controller_capabilities"] = requirements
    payload = {"decision": "CLIENTS_READY", "blockers": [], "surviving_writers": []}
    if capabilities:
        payload["consumer_capabilities"] = reported
    (scripts / "check-writer-rollout").write_text(
        "import json\nprint(json.dumps(" + repr(payload) + "))\n", encoding="utf-8",
    )
    return source


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    module = load_activate()
    monkeypatch.setattr(module, "GOVERNED", ())
    monkeypatch.setattr(module, "log", lambda _message: None)
    consumer = tmp_path / "consumer"
    calls = []
    real_run = module.run

    def prepare(requirements, available, *, capabilities=True, exit_code=0):
        source = historical_gate(tmp_path, [consumer], requirements=requirements,
                                 capabilities=capabilities)

        def run(command, **options):
            calls.append((command, options))
            if command[-2:] == ["capabilities", "--json"]:
                output = available if isinstance(available, str) else json.dumps(available)
                return subprocess.CompletedProcess(command, exit_code, output, "")
            return real_run(command, **options)

        monkeypatch.setattr(module, "run", run)
        return module, source, consumer, calls

    return prepare


@pytest.mark.parametrize("version", [1, None, True, "2"])
def test_historical_target_gate_cannot_admit_an_insufficient_runtime(preflight, version, monkeypatch):
    module, source, consumer, calls = preflight({CAPABILITY: 2}, {"decision": {CAPABILITY: version}})
    logs = []
    monkeypatch.setattr(module, "log", logs.append)
    with pytest.raises(module.RolloutBlocked) as refusal:
        module.preflight(source, [consumer], "older-runtime")
    assert refusal.value.blockers == [f"CONSUMER_REQUIRES_CAPABILITY:{consumer}:{CAPABILITY}>=2"]
    assert calls[-1][0][0] == module.runtime_path("older-runtime") / "bin/worktree-controller"
    assert any(refusal.value.blockers[0] in message for message in logs)


@pytest.mark.parametrize("version", [2, 3])
def test_historical_target_gate_accepts_a_sufficient_runtime(preflight, version):
    module, source, consumer, calls = preflight({CAPABILITY: 2}, {"decision": {CAPABILITY: version}})
    assert module.preflight(source, [consumer], "target")["decision"] == "CLIENTS_READY"
    assert len(calls) == 2


@pytest.mark.parametrize("required", [[CAPABILITY], {CAPABILITY: True}, {CAPABILITY: 0}])
def test_invalid_consumer_minimums_are_refused_before_querying_the_target(preflight, required):
    module, source, consumer, calls = preflight(required, {"decision": {CAPABILITY: 2}})
    with pytest.raises(module.RolloutBlocked) as refusal:
        module.preflight(source, [consumer], "target")
    assert refusal.value.blockers == [f"CONSUMER_REQUIREMENT_INVALID:{consumer}"]
    assert len(calls) == 1


@pytest.mark.parametrize("required", [None, {}])
def test_consumers_without_minimums_keep_the_existing_gate(preflight, required):
    module, source, consumer, calls = preflight(required, "unused")
    assert module.preflight(source, [consumer], "target")["decision"] == "CLIENTS_READY"
    assert len(calls) == 1


@pytest.mark.parametrize("available,exit_code", [
    ("not json", 0), ({"decision": []}, 0),
    ({"decision": {CAPABILITY: 2}, "errors": ["unavailable"]}, 0),
    ({"decision": {CAPABILITY: 2}}, 2),
])
def test_unverifiable_target_capabilities_refuse_the_switch(preflight, available, exit_code):
    module, source, consumer, _calls = preflight({CAPABILITY: 2}, available, exit_code=exit_code)
    with pytest.raises(module.RolloutBlocked) as refusal:
        module.preflight(source, [consumer], "target")
    assert refusal.value.blockers == [f"CONSUMER_REQUIREMENT_UNVERIFIABLE:{consumer}"]


def test_missing_consumer_projection_does_not_suppress_minimums(preflight):
    module, source, consumer, calls = preflight({CAPABILITY: 2}, "unused", capabilities=False)
    with pytest.raises(module.RolloutBlocked) as refusal:
        module.preflight(source, [consumer], "target")
    assert refusal.value.blockers == [f"CONSUMER_REQUIREMENT_UNVERIFIABLE:{consumer}"]
    assert len(calls) == 1


def test_capability_query_stays_inside_the_preflight_time_budget(preflight):
    module, source, consumer, calls = preflight({CAPABILITY: 2}, {"decision": {CAPABILITY: 2}})
    module.preflight(source, [consumer], "target", timeout=5, process_group=True)
    assert 0 < calls[-1][1]["timeout"] <= 5
    assert calls[-1][1]["process_group"] is True


@pytest.fixture
def deployment(request, monkeypatch):
    module = load_activate()
    preflight = module.preflight
    monkeypatch.setattr(test_rolling_upgrade, "load_activate", lambda **_options: module)
    item = request.getfixturevalue("toolchain_candidate")
    monkeypatch.setattr(module, "preflight", preflight)
    return item


@pytest.mark.parametrize("action", ["rollback", "automatic-restoration"])
def test_rollback_and_automatic_restoration_keep_the_active_pointer(
    deployment, tmp_path, monkeypatch, action,
):
    item = deployment
    module = item.module
    consumer = tmp_path / "consumer"
    source = historical_gate(tmp_path, [consumer], requirements={CAPABILITY: 2})
    monkeypatch.setattr(module, "frozen_source", lambda _work, _oid: source)
    monkeypatch.setattr(module, "GOVERNED", ())
    original_run = module.run

    def run(command, **options):
        if command[-2:] == ["capabilities", "--json"]:
            old = command[0] == item.previous / "bin/worktree-controller"
            decision = {**item.payload["decision"], CAPABILITY: 1 if old else 2}
            return subprocess.CompletedProcess(
                command, 0, json.dumps({**item.payload, "decision": decision}), "",
            )
        return original_run(command, **options)

    monkeypatch.setattr(module, "run", run)
    selected = item.previous if action == "rollback" else item.runtime
    if action == "rollback":
        module.ACTIVE.unlink()
        module.ACTIVE.symlink_to(item.runtime)
        monkeypatch.setattr(module, "verify_toolchain_compatibility", lambda _runtime: None)
    else:
        def failed_post_check(*_args):
            raise module.Abort("initial post-check failed")

        monkeypatch.setattr(module, "post_activation_check", failed_post_check)
    before = module.ACTIVE.resolve()
    monkeypatch.setattr(sys, "argv", ["controller-activate",
        "rollback" if action == "rollback" else "activate",
        "--version", selected.parent.name, "--consumer", str(consumer),
        "--toolchain-compatibility", str(item.catalog)])
    assert module.main() == 2
    # A failed post-check leaves the new compatible runtime active if downgrade is refused.
    assert module.ACTIVE.resolve() == (before if action == "rollback" else item.runtime)
