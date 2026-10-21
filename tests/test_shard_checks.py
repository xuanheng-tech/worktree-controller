"""Exercise the actual shell gate with real pytest collection and isolated fixtures."""

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-shards"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    for name in ("scripts", "tests", ".venv/bin", "bin", "outputs", "cache"):
        (root / name).mkdir(parents=True)
    (root / "scripts/check-shards").write_bytes(SCRIPT.read_bytes())
    for name, body in {
        ".venv/bin/python": f"exec {shlex.quote(sys.executable)} \"$@\"",
        ".venv/bin/ruff": "exit 0",
        "bin/uv": (
            f"if [ \"$1\" = sync ]; then printf '%s\\n' \"$*\" >> {shlex.quote(str(root / 'uv-sync'))}; "
            f"if [ -e {shlex.quote(str(root / 'stale-environment'))} ]; then "
            "echo 'Would install 1 package'; exit 1; fi; exit 0; fi\n"
            f"printf '%s\\n' {shlex.quote(str(root / 'cache'))}"
        ),
    }.items():
        path = root / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    return root


def environment(root):
    return {
        "PATH": f"{root / 'bin'}:{os.defpath}",
        "HOME": str(root), "SHARD_ROOT": str(root / "outputs"),
        "PYTHONDONTWRITEBYTECODE": "1",
        # These must not affect either collection or execution.
        "PYTEST_ADDOPTS": "-k nonexistent_test", "PYTHONPATH": "/not-a-real-module-path",
    }


def healthy_tests(root, count=4):
    for number in range(count):
        (root / "tests" / f"test_good{number}.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "assert os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'\n"
            "assert 'PYTEST_ADDOPTS' not in os.environ\n"
            "assert 'PYTHONPATH' not in os.environ\n"
            "def test_good():\n"
            f"    with Path('ran-{number}').open('a') as stream: stream.write('once\\n')\n"
        )


def run_gate(root, shards="4"):
    return subprocess.run(
        ["bash", str(root / "scripts/check-shards"), shards],
        env=environment(root), capture_output=True, text=True, timeout=30, check=False,
    )


def test_collection_error_blocks_all_shards_and_preserves_diagnostic(project):
    healthy_tests(project)
    (project / "tests/test_broken.py").write_text("raise RuntimeError('collection failed fixture')\n")
    result = run_gate(project)
    assert result.returncode != 0
    assert "collection failed fixture" in result.stderr
    assert not list(project.glob("ran-*"))
    outputs = list((project / "outputs").iterdir())
    assert len(outputs) == 1
    assert "collection failed fixture" in (outputs[0] / "collection.log").read_text()


@pytest.mark.parametrize("count", [1, 4, 7])
def test_each_file_runs_once_even_with_empty_shards(project, count):
    healthy_tests(project, count)
    result = run_gate(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list(project.glob("ran-*"))) == count
    assert all(path.read_text() == "once\n" for path in project.glob("ran-*"))
    assert not list((project / "outputs").iterdir())


def test_locked_dependency_check_runs_before_collection(project):
    healthy_tests(project)
    result = run_gate(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / "uv-sync").read_text() == (
        "sync --locked --check --no-install-project --inexact\n"
    )


def test_stale_environment_blocks_before_collection(project):
    healthy_tests(project)
    (project / "stale-environment").touch()
    result = run_gate(project)
    assert result.returncode != 0
    assert "does not match uv.lock" in result.stderr
    assert not list(project.glob("ran-*"))
    outputs = list((project / "outputs").iterdir())
    assert len(outputs) == 1
    assert not (outputs[0] / "collection.log").exists()


@pytest.mark.parametrize("value", ["0", "-1", "65", "x", "01", "999999999999999999999999"])
def test_invalid_shard_count_fails_before_collection(project, value):
    result = run_gate(project, value)
    assert result.returncode == 2
    assert "SHARDS must" in result.stderr
    assert not list((project / "outputs").iterdir())


def test_test_failure_preserves_worker_log(project):
    (project / "tests/test_bad.py").write_text("def test_bad():\n    assert False, 'worker failure fixture'\n")
    result = run_gate(project)
    assert result.returncode != 0
    outputs = list((project / "outputs").iterdir())
    assert len(outputs) == 1
    assert "worker failure fixture" in (outputs[0] / "0.log").read_text()


def test_changed_collection_between_discovery_and_execution_is_refused(project):
    (project / "tests/test_drift.py").write_text(
        "import os\n"
        "def test_always(): pass\n"
        "if '/collection/' in os.environ['HOME']:\n"
        "    def test_disappears(): pass\n"
    )
    result = run_gate(project)
    assert result.returncode != 0
    assert "collected 2, executed 1" in result.stderr


def test_interrupt_terminates_worker_descendant(project):
    (project / "tests/test_wait.py").write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "def test_wait():\n"
        "    child = subprocess.Popen(['sleep', '60'])\n"
        "    Path('child-pid').write_text(str(child.pid))\n"
        "    child.wait()\n"
    )
    with subprocess.Popen(
        ["bash", str(project / "scripts/check-shards"), "1"], env=environment(project),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            marker = project / "child-pid"
            while not marker.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            assert marker.exists()
            child_pid = int(marker.read_text())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)
            assert process.returncode == 143
            child = Path(f"/proc/{child_pid}/stat")
            deadline = time.monotonic() + 3
            while child.exists() and time.monotonic() < deadline:
                try:
                    if child.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                        break
                except FileNotFoundError:
                    break
                time.sleep(0.02)
            else:
                assert not child.exists(), "worker descendant survived interruption"
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
