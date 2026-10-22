"""The acceptance entry point preserves caller-owned output paths."""

import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-project"
ROOT = SCRIPT.parents[1]
PYTHON_MODES = ("normal", "flag", "environment")


def copied_project(tmp_path):
    """Keep command fixtures and all possible writes outside the source checkout."""
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "src/worktree_controller").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "cache").mkdir()
    for name in ("check-project", "check-cross-tool"):
        (root / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    for name in ("pyproject.toml", "tool_cli_contract.json", "uv.lock"):
        (root / name).write_bytes((ROOT / name).read_bytes())
    (root / "src/worktree_controller/tool_cli_contract.json").write_bytes(
        (root / "tool_cli_contract.json").read_bytes(),
    )
    uv = root / "bin/uv"
    uv.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
if sys.argv[1:] == ['cache', 'dir']:
    print(root / 'cache')
elif sys.argv[1:2] == ['sync']:
    (root / 'dependencies-touched').touch()
else:
    raise SystemExit(93)
""", encoding="utf-8")
    uv.chmod(0o755)
    return root


def check_environment(root, mode):
    work = root / "environment"
    work.mkdir()
    environment = runpy.run_path(str(SCRIPT))["fixture_environment"](work, str(root / "cache"))
    environment["PATH"] = str(root / "bin") + os.pathsep + environment["PATH"]
    environment["TMPDIR"] = str(root)
    if mode == "environment":
        environment["PYTHONOPTIMIZE"] = "1"
    return environment


def python_command(mode):
    return [sys.executable, *(["-O"] if mode == "flag" else [])]


@pytest.mark.parametrize("mode", PYTHON_MODES)
@pytest.mark.parametrize("mismatch", ["version", "contract", "registry", "artifact"])
def test_source_mismatch_stops_before_dependencies_even_with_optimization(tmp_path, mode, mismatch):
    root = copied_project(tmp_path)
    lock = root / "uv.lock"
    if mismatch == "version":
        contract = json.loads((root / "tool_cli_contract.json").read_text())
        contract["tool_version"] = "0.0.0"
        (root / "tool_cli_contract.json").write_text(json.dumps(contract))
        message = "source version mismatch"
    elif mismatch == "contract":
        (root / "src/worktree_controller/tool_cli_contract.json").write_text("{}")
        message = "source CLI contract mismatch"
    elif mismatch == "registry":
        lock.write_text(lock.read_text().replace(
            "https://pypi.org/simple", "https://mirror.example.invalid/simple", 1))
        message = "uses a non-public registry"
    else:
        lock.write_text(lock.read_text().replace(
            "https://files.pythonhosted.org/packages/",
            "https://mirror.example.invalid/packages/", 1))
        message = "has a non-public artifact URL"
    result = subprocess.run(
        [*python_command(mode), str(root / "scripts/check-project"),
         "--work-dir", str(root / "output"), "--offline"],
        env=check_environment(root, mode), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert not (root / "dependencies-touched").exists()


INSTALLED_SMOKE_DRIVER = """
import json
import runpy
import subprocess
import sys
from pathlib import Path

checks = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
mismatch = sys.argv[3]
project = {'version': 'fixture', 'scripts': {}}
contract = {'contract_version': 43, 'storage_layout_version': 4}

def run(*arguments, **kwargs):
    if '--version' in arguments:
        stdout = 'wrong' if mismatch == 'version' else 'worktree-controller fixture'
    elif 'capabilities' in arguments:
        capability = {'tool_version': 'fixture', 'decision': dict(contract)}
        if mismatch == 'capability-version':
            capability['tool_version'] = 'wrong'
        elif mismatch == 'contract':
            capability['decision']['contract_version'] += 1
        stdout = json.dumps(capability)
    elif 'status' in arguments:
        stdout = json.dumps({'errors': [], 'repository': {'canonical_path': 'wrong'}})
    else:
        stdout = ''
    return subprocess.CompletedProcess(arguments, 0, stdout=stdout)

checks['installed_smoke'].__globals__['run'] = run
checks['installed_smoke'](root / 'fixture.whl', root / 'installation', root, {}, project, contract)
"""


@pytest.mark.parametrize("mode", PYTHON_MODES)
@pytest.mark.parametrize(("mismatch", "message"), [
    ("version", "installed Controller version mismatch"),
    ("capability-version", "installed capability version mismatch"),
    ("contract", "contract_version mismatch"),
    ("repository", "installed Controller repository mismatch"),
])
def test_installed_smoke_rejects_mismatch_with_optimization(tmp_path, mode, mismatch, message):
    root = copied_project(tmp_path)
    result = subprocess.run(
        [*python_command(mode), "-c", INSTALLED_SMOKE_DRIVER, str(SCRIPT), str(root), mismatch],
        env=check_environment(root, mode), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.parametrize("mode", PYTHON_MODES)
def test_embedded_package_smoke_rejects_wrong_version_with_optimization(tmp_path, mode):
    root = copied_project(tmp_path)
    smoke = runpy.run_path(str(SCRIPT))["PACKAGE_SMOKE"]
    fixture = """
import importlib.metadata
import sys
from types import ModuleType, SimpleNamespace
sys.modules['worktree_controller'] = ModuleType('worktree_controller')
sys.modules['worktree_controller.metadata_codec'] = ModuleType('worktree_controller.metadata_codec')
importlib.metadata.distribution = lambda name: SimpleNamespace(version='fixture')
"""
    result = subprocess.run(
        [*python_command(mode), "-c", fixture + smoke, str(root), "expected-fixture"],
        env=check_environment(root, mode), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 1
    assert "distribution version mismatch" in result.stderr


@pytest.mark.parametrize("directory", [False, True])
def test_existing_work_path_is_refused_without_touching_contents(tmp_path, directory):
    destination = tmp_path / "existing"
    if directory:
        destination.mkdir()
        marker = destination / "keep.txt"
    else:
        marker = destination
    marker.write_text("caller-owned data", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--work-dir", str(destination)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "must not already exist" in result.stderr
    assert marker.read_text() == "caller-owned data"
    assert not result.stdout


@pytest.fixture(scope="module")
def pid_namespace():
    unshare = shutil.which("unshare")
    if sys.platform != "linux" or unshare is None:
        pytest.skip("PID namespace acceptance requires Linux unshare")
    command = [unshare, "--user", "--map-current-user", "--pid", "--fork",
               "--mount-proc", "--kill-child=KILL"]
    probe = subprocess.run(
        [*command, sys.executable, "-c", "import os; assert os.getpid() == 1"],
        capture_output=True, text=True, timeout=10,
        env={"PATH": os.defpath, "LANG": "C"},
    )
    if probe.returncode:
        pytest.skip(f"unprivileged PID namespaces unavailable: {probe.stderr.strip()}")
    return command


ARGUMENTS_UV = """
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
worker_pid = os.getppid()
def command_line(pid):
    return [os.fsdecode(argument) for argument in
            Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')[:-1]]
status = Path(f'/proc/{worker_pid}/status').read_text().splitlines()
worker_parent = next(int(line.split()[1]) for line in status if line.startswith('PPid:'))
(root / 'argument-proof.json').write_text(json.dumps({
    'worker_pid': worker_pid, 'worker_parent': worker_parent,
    'init_arguments': command_line(1), 'worker_arguments': command_line(worker_pid),
    'uv_arguments': sys.argv,
}))
(root / 'output with spaces/fixture-evidence').write_text('retain argument evidence')
raise SystemExit(17)
"""


@pytest.mark.parametrize("flag", ["-O", "-I"])
def test_pid_one_entry_preserves_interpreter_flags_and_spaced_arguments(
    tmp_path, pid_namespace, flag,
):
    root = copied_project(tmp_path / "path with spaces")
    (root / "bin/uv").write_text(f"#!{sys.executable}\n" + ARGUMENTS_UV)
    output = root / "output with spaces"
    arguments = [sys.executable, flag, str(root / "scripts/check-project"),
                 "--work-dir", str(output), "--offline"]
    result = subprocess.run(
        [*pid_namespace, *arguments], env=check_environment(root, "normal"),
        capture_output=True, text=True, timeout=15,
    )
    proof = json.loads((root / "argument-proof.json").read_text())
    assert proof["worker_pid"] != 1
    assert proof["worker_parent"] == 1
    assert proof["init_arguments"] == arguments
    assert proof["worker_arguments"] == arguments
    assert proof["uv_arguments"] == [
        str(root / "bin/uv"), "sync", "--locked", "--python", sys.executable, "--offline",
    ]
    assert result.returncode == 1, result.stderr
    assert "Project checks failed:" in result.stderr
    assert "returned non-zero exit status 17" in result.stderr
    assert "+ uv sync" in result.stdout
    assert "Project checks passed" not in result.stdout
    assert (output / "fixture-evidence").read_text() == "retain argument evidence"


ORPHAN_UV = """
import json
import os
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
reader, writer = os.pipe()
middle = os.fork()
if middle == 0:
    os.close(reader)
    orphan = os.fork()
    if orphan == 0:
        os.close(writer)
        while os.getppid() != 1:
            time.sleep(0.001)
        os._exit(0)
    os.write(writer, str(orphan).encode())
    os._exit(0)
os.close(writer)
orphan = int(os.read(reader, 32))
os.close(reader)
os.waitpid(middle, 0)
deadline = time.monotonic() + 3
while Path('/proc', str(orphan)).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
(root / 'orphan-proof.json').write_text(json.dumps({
    'worker_pid': os.getppid(), 'orphan_reaped': not Path('/proc', str(orphan)).exists()
}))
raise SystemExit(17)
"""


def test_pid_one_entry_reaps_orphans_while_the_check_worker_is_running(tmp_path, pid_namespace):
    root = copied_project(tmp_path)
    uv = root / "bin/uv"
    uv.write_text(f"#!{sys.executable}\n" + ORPHAN_UV)
    result = subprocess.run(
        [*pid_namespace, sys.executable, str(root / "scripts/check-project"),
         "--work-dir", str(root / "output"), "--offline"],
        env=check_environment(root, "normal"), capture_output=True, text=True, timeout=15,
    )
    proof = json.loads((root / "orphan-proof.json").read_text())
    assert proof["orphan_reaped"]
    assert proof["worker_pid"] != 1
    assert result.returncode == 1
    assert "Project checks failed" in result.stderr


INTERRUPT_UV = """
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if sys.argv[1:2] != ['sync'] or '--offline' not in sys.argv:
    raise SystemExit(93)
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', '''
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).touch()
while True: time.sleep(0.01)
''', str(root / 'child-ready')])
deadline = time.monotonic() + 10
while not (root / 'child-ready').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('fixture child did not start')
    time.sleep(0.01)
(root / 'sync-ready').write_text(json.dumps({
    'worker_pid': os.getppid(), 'uv_pid': os.getpid(), 'child_pid': child.pid,
}))
while True: time.sleep(0.01)
"""


# Only namespace PID 1 delays Popen's return. The real script and check worker stay intact.
INIT_STARTUP_HOOK = """
import os
import signal
import subprocess
import time
from pathlib import Path

if os.getpid() == 1:
    root = Path(__file__).resolve().parents[1]
    original_popen = subprocess.Popen
    original_signal = signal.signal

    def delayed_popen(*arguments, **keywords):
        child = original_popen(*arguments, **keywords)
        (root / 'init-starting').touch()
        deadline = time.monotonic() + 10
        while not (root / 'allow-init-return').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('fixture startup barrier timed out')
            time.sleep(0.01)
        return child

    def observed_signal(signum, handler):
        if signum in (signal.SIGINT, signal.SIGTERM) and callable(handler):
            def received(value, frame):
                handler(value, frame)
                (root / 'init-signal').write_text(str(value))
            return original_signal(signum, received)
        return original_signal(signum, handler)

    subprocess.Popen = delayed_popen
    signal.signal = observed_signal
"""


def wait_for_entry_marker(process, marker):
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        assert process.poll() is None, f"entry exited before {marker.name}"
        time.sleep(0.01)
    assert marker.exists(), f"entry did not reach {marker.name}"


def entry_processes(process, proof):
    # Observe the owned launcher -> init -> worker -> uv -> child chain from the host.
    owned_pids = [process.pid]
    for _ in range(4):
        parent = owned_pids[-1]
        children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        assert len(children) == 1
        owned_pids.append(int(children[0]))
    for pid, namespace_pid in zip(
        owned_pids[1:], [1, proof["worker_pid"], proof["uv_pid"], proof["child_pid"]],
        strict=True,
    ):
        status = Path(f"/proc/{pid}/status").read_text().splitlines()
        observed = next(line.split()[-1] for line in status if line.startswith("NSpid:"))
        assert int(observed) == namespace_pid
    assert os.getpgid(owned_pids[1]) == process.pid
    assert os.getpgid(owned_pids[2]) == owned_pids[2]
    assert os.getpgid(owned_pids[3]) == owned_pids[2]
    assert os.getpgid(owned_pids[4]) == owned_pids[2]
    return owned_pids


@pytest.mark.parametrize("stage", ["starting", "sync"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_pid_one_entry_preserves_interrupt_status_and_cleans_processes(
    tmp_path, pid_namespace, stage, signum,
):
    root = copied_project(tmp_path)
    (root / "bin/uv").write_text(f"#!{sys.executable}\n" + INTERRUPT_UV)
    environment = check_environment(root, "normal")
    if stage == "starting":
        startup = root / "startup"
        startup.mkdir()
        (startup / "sitecustomize.py").write_text(INIT_STARTUP_HOOK)
        environment["PYTHONPATH"] = str(startup)
    command = [*pid_namespace, sys.executable, str(root / "scripts/check-project"),
               "--work-dir", str(root / "output"), "--offline"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env=environment, start_new_session=True) as process:
        try:
            wait_for_entry_marker(process, root / "sync-ready")
            if stage == "starting":
                wait_for_entry_marker(process, root / "init-starting")
            proof = json.loads((root / "sync-ready").read_text())
            owned_pids = entry_processes(process, proof)
            # Like terminal Ctrl+C, signal only this launcher's isolated process group.
            os.killpg(process.pid, signum)
            if stage == "starting":
                wait_for_entry_marker(process, root / "init-signal")
                assert (root / "init-signal").read_text() == str(int(signum))
                (root / "allow-init-return").touch()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 128 + int(signum), stderr
            assert "Project checks passed" not in stdout
            assert (root / "output").is_dir()
            deadline = time.monotonic() + 5
            while any(Path(f"/proc/{pid}").exists() for pid in owned_pids) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert all(not Path(f"/proc/{pid}").exists() for pid in owned_pids)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


# Keep the entry script unchanged; shorten only its real uv-sync wait after observing the tree.
ENTRY_TIMEOUT_HOOK = """
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if os.getpid() != 1 and Path(sys.argv[0]) == root / 'scripts/check-project':
    class BoundedPopen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if self.args[:2] != ['uv', 'sync'] or timeout != 600:
                return super().communicate(input=input, timeout=timeout)
            (root / 'timeout-armed').write_text(str(timeout))
            deadline = time.monotonic() + 10
            while not (root / 'allow-timeout').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture timeout barrier timed out')
                time.sleep(0.01)
            try:
                return super().communicate(input=input, timeout=0.2)
            except subprocess.TimeoutExpired as error:
                (root / 'timeout-observed').write_text(str(error.timeout))
                raise

    subprocess.Popen = BoundedPopen
"""


def test_pid_one_entry_reports_command_timeout_and_cleans_processes(tmp_path, pid_namespace):
    root = copied_project(tmp_path)
    (root / "bin/uv").write_text(f"#!{sys.executable}\n" + INTERRUPT_UV)
    startup = root / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(ENTRY_TIMEOUT_HOOK)
    environment = check_environment(root, "normal")
    environment["PYTHONPATH"] = str(startup)
    command = [*pid_namespace, sys.executable, str(root / "scripts/check-project"),
               "--work-dir", str(root / "output"), "--offline"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env=environment, start_new_session=True) as process:
        try:
            wait_for_entry_marker(process, root / "sync-ready")
            wait_for_entry_marker(process, root / "timeout-armed")
            assert (root / "timeout-armed").read_text() == "600"
            proof = json.loads((root / "sync-ready").read_text())
            owned_pids = entry_processes(process, proof)
            marker = root / "output/fixture-evidence"
            marker.write_text("retain timeout evidence")
            (root / "allow-timeout").touch()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 1, stderr
            observed_timeout = float((root / "timeout-observed").read_text())
            assert 0 < observed_timeout <= 0.2
            assert "Project checks failed:" in stderr
            assert f"timed out after {observed_timeout} seconds" in stderr
            assert "+ uv sync" in stdout
            assert "Project checks passed" not in stdout
            assert marker.read_text() == "retain timeout evidence"
            deadline = time.monotonic() + 5
            while any(Path(f"/proc/{pid}").exists() for pid in owned_pids) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert all(not Path(f"/proc/{pid}").exists() for pid in owned_pids)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


INIT_DRIVER = """
import runpy
import sys
raise SystemExit(runpy.run_path(sys.argv[1])['namespace_init'](sys.argv[2:]))
"""


@pytest.mark.parametrize(("worker", "code"), [
    ("raise SystemExit(0)", 0),
    ("raise SystemExit(23)", 23),
    ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", 143),
    ("import os, signal; os.kill(os.getpid(), signal.SIGINT)", 130),
])
def test_namespace_init_preserves_worker_exit_status(pid_namespace, worker, code):
    result = subprocess.run(
        [*pid_namespace, sys.executable, "-c", INIT_DRIVER, str(SCRIPT),
         sys.executable, "-c", worker],
        capture_output=True, text=True, timeout=15,
        env={"PATH": os.defpath, "LANG": "C"},
    )
    assert result.returncode == code, result.stderr


SIGNAL_WORKER = """
import signal
import subprocess
import sys
import time
from pathlib import Path
root = Path(sys.argv[1])
signum = int(sys.argv[2])
child_code = '''
import signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def received(signum, frame):
    (root / 'child-signal').write_text(str(signum))
    raise SystemExit(0)
signal.signal(int(sys.argv[2]), received)
(root / 'child-ready').touch()
while True: time.sleep(0.01)
'''
def received(value, frame):
    (root / 'worker-signal').write_text(str(value))
    child.wait(timeout=5)
    raise SystemExit(71)
signal.signal(signum, received)
child = subprocess.Popen([sys.executable, '-c', child_code, str(root), str(signum)])
while not (root / 'child-ready').exists(): time.sleep(0.01)
(root / 'ready').touch()
while True: time.sleep(0.01)
"""


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_namespace_init_forwards_interrupts_to_the_workers_process_group(
    tmp_path, pid_namespace, signum,
):
    command = [*pid_namespace, sys.executable, "-c", INIT_DRIVER, str(SCRIPT),
               sys.executable, "-c", SIGNAL_WORKER, str(tmp_path), str(int(signum))]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env={"PATH": os.defpath, "LANG": "C"}) as process:
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.01)
            assert (tmp_path / "ready").exists()
            children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
            assert len(children) == 1
            os.kill(int(children[0]), signum)
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 71, stderr
            assert (tmp_path / "worker-signal").read_text() == str(int(signum))
            assert (tmp_path / "child-signal").read_text() == str(int(signum))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


TIMEOUT_WORKER = """
import runpy
import subprocess
import sys
checks = runpy.run_path(sys.argv[1])
try:
    checks['run'](sys.executable, '-c',
        'import subprocess, sys, time; '
        'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
        'time.sleep(60)', timeout=0.2)
except subprocess.TimeoutExpired:
    raise SystemExit(73)
raise SystemExit(0)
"""


def test_namespace_init_preserves_timeout_failure_and_closes_descendant_pipes(pid_namespace):
    result = subprocess.run(
        [*pid_namespace, sys.executable, "-c", INIT_DRIVER, str(SCRIPT),
         sys.executable, "-c", TIMEOUT_WORKER, str(SCRIPT)],
        capture_output=True, text=True, timeout=10,
        env={"PATH": os.defpath, "LANG": "C"},
    )
    assert result.returncode == 73, result.stderr
