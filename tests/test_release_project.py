"""Release recovery never substitutes a different tag or overwrites remote bytes."""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release-project"


@pytest.fixture
def release():
    loader = importlib.machinery.SourceFileLoader("wc_release_test", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def save_bundle(release, root, files):
    (root / "dist").mkdir(parents=True)
    manifest = {
        "schema_version": 1, "tag": "v1.30.5", "version": "1.30.5", "commit": "a" * 40,
        "tree": "b" * 40, "contract_version": 60, "storage_layout_version": 5,
        "contract_sha256": "c" * 64, "readme_sha256": "d" * 64,
        "license_sha256": "e" * 64, "project_urls": {}, "files": [],
    }
    for name, data in files.items():
        (root / "dist" / name).write_bytes(data)
        manifest["files"].append({"filename": name, "size": len(data), "sha256": release.digest(data)})
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "SHA256SUMS").write_text("".join(f"{r['sha256']}  {r['filename']}\n" for r in manifest['files']))
    (root / "RELEASE-NOTES.md").write_text("A synthetic fixture release.\n")
    return root, manifest


@pytest.fixture
def bundle(release, tmp_path):
    return save_bundle(release, tmp_path / "bundle", {
        "worktree_controller-1.30.5-py3-none-any.whl": b"synthetic wheel",
        "worktree_controller-1.30.5.tar.gz": b"synthetic source",
    })


def pypi_row(row):
    return {"filename": row["filename"], "size": row["size"], "yanked": False,
            "digests": {"sha256": row["sha256"]}, "url": "https://files.example.invalid/" + row["filename"]}


@pytest.fixture
def remote(release, bundle, monkeypatch):
    root, manifest = bundle
    state = {"release": None, "data": {}, "pypi": [], "mutations": [], "sleeps": [],
             "tag_commit": manifest["commit"], "main_base": manifest["commit"], "now": 0}

    def api(path, missing=False):
        if path.startswith("git/ref/"):
            return {"object": {"type": "commit", "sha": state["tag_commit"]}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": state["main_base"]}}
        if path.startswith("releases/tags/"):
            item = state["release"]
            return item if item and not item["draft"] else None
        if path == "releases?per_page=100&page=1":
            item = state["release"]
            return [dict(item, tag_name=manifest["tag"])] if item else []
        raise AssertionError(path)

    def run(*args, **kwargs):
        args = [str(arg) for arg in args]
        if args[:2] == ["gh", "api"]:
            name = args[2].rsplit("/", 1)[1]
            return subprocess.CompletedProcess(args, 0, state["data"][name], b"")
        state["mutations"].append(args)
        if args[:3] == ["gh", "release", "create"]:
            state["release"] = {"draft": True, "assets": []}
            if state.pop("fail_create", False):
                raise RuntimeError("create outcome unknown")
        elif args[:3] == ["gh", "release", "upload"]:
            assert "--clobber" not in args
            files = args[args.index("--repo") + 2:]
            for path in files:
                path = Path(path)
                assert path.name not in state["data"]
                state["data"][path.name] = path.read_bytes()
                state["release"]["assets"].append({"name": path.name, "id": path.name})
                if state.pop("fail_github", False):
                    raise RuntimeError("interrupted GitHub upload")
            if state.get("drift_after_github"):
                state["tag_commit"] = "f" * 40
        elif args[:2] == ["uv", "publish"]:
            assert args[args.index("--trusted-publishing") + 1] == "always"
            files = [Path(arg).name for arg in args if arg.startswith(str(root))]
            for name in files:
                row = next(row for row in manifest["files"] if row["filename"] == name)
                assert name not in {row["filename"] for row in state["pypi"]}
                state["pypi"].append(pypi_row(row))
                if state.pop("fail_pypi", False):
                    raise RuntimeError("uv exited 2 after partial upload")
        elif args[:3] == ["gh", "release", "edit"]:
            state["release"]["draft"] = False
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release, "read_json", lambda *a, **k: {"urls": state["pypi"]} if state["pypi"] else None)
    def sleep(value):
        state["sleeps"].append(value)
        state["now"] += value

    monkeypatch.setattr(release.time, "sleep", sleep)
    monkeypatch.setattr(release.time, "monotonic", lambda: state["now"])
    return state


def delay_visibility(release, state, monkeypatch, phase, seconds):
    api, read_json = release.api, release.read_json

    def observed_api(path, **kwargs):
        value = api(path, **kwargs)
        item = state["release"]
        if not item or state["now"] >= seconds or not path.startswith("releases"):
            return value
        if phase == "draft" and item["draft"]:
            return [] if path.startswith("releases?") else None
        if phase == "assets" and state["data"]:
            if isinstance(value, list):
                return [dict(row, assets=[]) for row in value]
            return dict(value, assets=[]) if value else value
        if phase == "public" and not item["draft"]:
            if isinstance(value, list):
                return [dict(row, draft=True) for row in value]
            return dict(value, draft=True) if value else value
        return value

    def observed_pypi(*args, **kwargs):
        if phase == "pypi" and state["now"] < seconds:
            return None
        return read_json(*args, **kwargs)

    monkeypatch.setattr(release, "api", observed_api)
    monkeypatch.setattr(release, "read_json", observed_pypi)
    return api, read_json


@pytest.mark.parametrize("phase", ["draft", "assets", "pypi", "public"])
def test_delayed_readback_repeats_observations_not_writes(release, bundle, remote, monkeypatch, phase):
    delay = 75 if phase == "pypi" else 15
    delay_visibility(release, remote, monkeypatch, phase, delay)
    assert release.publish(bundle[0])["status"] == "PUBLISHED_VERIFIED"
    assert sum(remote["sleeps"]) == delay
    assert len(remote["mutations"]) == 4
    assert not remote["release"]["draft"]


@pytest.mark.parametrize("phase", ["draft", "assets", "pypi", "public"])
def test_visibility_timeout_preserves_bundle_and_resumes_without_reupload(
    release, bundle, remote, monkeypatch, phase,
):
    root, _ = bundle
    before = {p.name: p.read_bytes() for p in (root / "dist").iterdir()}
    api, read_json = delay_visibility(release, remote, monkeypatch, phase, 600)
    with pytest.raises(RuntimeError, match="retry with the same bundle"):
        release.publish(root)
    assert sum(remote["sleeps"]) == release.PUBLICATION_WAIT_SECONDS
    retained = remote["data"].copy()
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "read_json", read_json)
    assert release.publish(root)["status"] == "PUBLISHED_VERIFIED"
    assert len(remote["mutations"]) == 4
    assert all(remote["data"][name] == data for name, data in retained.items())
    assert before == {p.name: p.read_bytes() for p in (root / "dist").iterdir()}


@pytest.mark.parametrize("phase", ["draft", "pypi"])
def test_identity_drift_during_wait_stops_before_next_write(release, bundle, remote, monkeypatch, phase):
    delay_visibility(release, remote, monkeypatch, phase, 75)
    sleep = release.time.sleep

    def drift(value):
        sleep(value)
        remote["tag_commit"] = "f" * 40

    monkeypatch.setattr(release.time, "sleep", drift)
    with pytest.raises(RuntimeError, match="tag drift"):
        release.publish(bundle[0])
    assert remote["sleeps"] == [5]
    assert remote["release"]["draft"]
    assert not any(args[:3] == ["gh", "release", "edit"] for args in remote["mutations"])
    if phase == "draft":
        assert remote["data"] == {} and remote["pypi"] == []


def test_readback_requests_revalidate_caches(release, monkeypatch):
    def urlopen(request, **kwargs):
        assert dict((k.lower(), v) for k, v in request.header_items())["cache-control"] == "no-cache"
        return io.BytesIO(b"{}")

    def run(*args, **kwargs):
        assert args[-2:] == ("-H", "Cache-Control: no-cache")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(release.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(release, "run", run)
    assert release.read_json("https://pypi.org/example") == {}
    assert release.api("releases") == {}


def test_visibility_phases_share_one_wait_budget(release, bundle, remote, monkeypatch):
    delay_visibility(release, remote, monkeypatch, "draft", 75)
    delay_visibility(release, remote, monkeypatch, "pypi", 600)
    with pytest.raises(RuntimeError, match="PyPI propagation"):
        release.publish(bundle[0])
    assert sum(remote["sleeps"]) == release.PUBLICATION_WAIT_SECONDS
    assert len(remote["mutations"]) == 3
    assert remote["release"]["draft"]


def test_successful_publish_then_replay_makes_no_further_mutation(release, bundle, remote):
    root, _ = bundle
    assert release.publish(root)["status"] == "PUBLISHED_VERIFIED"
    assert len(remote["mutations"]) == 4
    original = remote["data"].copy()
    replay = release.publish(root)
    assert replay["pypi_uploaded"] == []
    assert len(remote["mutations"]) == 4 and remote["data"] == original


def test_existing_draft_hidden_from_tag_endpoint_is_reused(release, bundle, remote):
    remote["release"] = {"draft": True, "assets": []}
    assert release.api("releases/tags/v1.30.5", missing=True) is None
    assert release.publish(bundle[0])["status"] == "PUBLISHED_VERIFIED"
    assert not any(args[:3] == ["gh", "release", "create"] for args in remote["mutations"])


def test_draft_lookup_paginates_without_treating_an_older_draft_as_absent(release, monkeypatch):
    calls = []
    draft = {"tag_name": "v1.30.5", "draft": True, "assets": []}

    def api(path, missing=False):
        calls.append(path)
        if path.startswith("releases/tags/"):
            return None
        if path.endswith("page=1"):
            return [{"tag_name": f"v2.0.{i}"} for i in range(100)]
        return [draft]

    monkeypatch.setattr(release, "api", api)
    assert release.release_by_tag("v1.30.5") == draft
    assert calls[-1] == "releases?per_page=100&page=2"


@pytest.mark.parametrize("failure", ["fail_create", "fail_github", "fail_pypi"])
def test_interrupted_publish_resumes_from_exact_remote_inventory(release, bundle, remote, failure):
    root, _ = bundle
    remote[failure] = True
    with pytest.raises(RuntimeError):
        release.publish(root)
    assert remote["release"]["draft"]
    retained = remote["data"].copy()
    result = release.publish(root)
    assert result["status"] == "PUBLISHED_VERIFIED"
    assert all(remote["data"][name] == data for name, data in retained.items())
    if failure == "fail_pypi":
        uploads = [args for args in remote["mutations"] if args[:2] == ["uv", "publish"]]
        assert len([arg for arg in uploads[-1] if arg.startswith(str(root))]) == 1


@pytest.mark.parametrize("field", ["tag_commit", "main_base"])
def test_identity_drift_refuses_before_any_mutation(release, bundle, remote, field):
    remote[field] = "f" * 40
    with pytest.raises(RuntimeError, match="drift|contained"):
        release.publish(bundle[0])
    assert remote["mutations"] == []


def test_tag_drift_after_freezing_draft_refuses_pypi_upload(release, bundle, remote):
    remote["drift_after_github"] = True
    with pytest.raises(RuntimeError, match="tag drift"):
        release.publish(bundle[0])
    assert not remote["pypi"] and remote["release"]["draft"]
    assert not any(args[:2] == ["uv", "publish"] for args in remote["mutations"])


@pytest.mark.parametrize("drift", ["sha", "size", "yanked", "unknown"])
def test_pypi_drift_preserves_remote_and_refuses_github_creation(release, bundle, remote, drift):
    row = pypi_row(bundle[1]["files"][0])
    if drift == "sha":
        row["digests"]["sha256"] = "f" * 64
    elif drift == "size":
        row["size"] += 1
    elif drift == "yanked":
        row["yanked"] = True
    else:
        row["filename"] = "unknown-owner-file.whl"
    remote["pypi"] = [row]
    with pytest.raises(RuntimeError, match="PyPI"):
        release.publish(bundle[0])
    assert remote["mutations"] == [] and remote["pypi"] == [row]


@pytest.mark.parametrize("unknown", [False, True])
def test_github_drift_is_never_overwritten(release, bundle, remote, unknown):
    name = "unknown-owner-evidence.txt" if unknown else bundle[1]["files"][0]["filename"]
    remote["release"] = {"draft": True, "assets": [{"id": name, "name": name}]}
    remote["data"][name] = b"keep original bytes"
    with pytest.raises(RuntimeError, match="GitHub"):
        release.publish(bundle[0])
    assert remote["mutations"] == [] and remote["data"][name] == b"keep original bytes"


def test_audit_is_read_only_and_reports_missing_files(release, bundle, remote):
    observed = release.audit(bundle[0])
    assert observed["mutation_performed"] is False
    assert observed["github"]["exists"] is False and len(observed["pypi_missing"]) == 2
    assert remote["mutations"] == []


@pytest.mark.parametrize("drift", ["bytes", "unknown", "sums", "tag", "duplicate"])
def test_local_bundle_drift_is_rejected(release, bundle, drift):
    root, manifest = bundle
    if drift == "bytes":
        (root / "dist" / manifest["files"][0]["filename"]).write_bytes(b"changed")
    elif drift == "unknown":
        (root / "dist/keep.txt").write_text("unknown")
    elif drift == "sums":
        (root / "SHA256SUMS").write_text("changed")
    else:
        if drift == "tag":
            manifest["tag"] = "v1.30.6"
        else:
            manifest["files"].append(manifest["files"][0])
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        release.load_bundle(root)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_only_http_404_is_treated_as_absent(release, monkeypatch, status):
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org/example", status, "fixture", {}, None)
    monkeypatch.setattr(release.urllib.request, "urlopen", unavailable)
    if status == 404:
        assert release.read_json("https://pypi.org/example", missing=True) is None
    else:
        with pytest.raises(urllib.error.HTTPError):
            release.read_json("https://pypi.org/example", missing=True)


@pytest.mark.parametrize("optimize", [False, True])
def test_invalid_cli_tag_fails_without_touching_existing_work(tmp_path, optimize):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("caller data")
    result = subprocess.run([sys.executable, *(["-O"] if optimize else []), str(SCRIPT), "prepare",
                             "--bundle", str(tmp_path / "bundle"), "--source", str(tmp_path),
                             "--tag", "../main", "--work-dir", str(tmp_path)],
                            env={"PATH": os.defpath, "HOME": str(tmp_path)},
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 1 and "exact stable tag" in result.stderr
    assert sentinel.read_text() == "caller data" and not (tmp_path / "bundle").exists()


def verification_fixture(release, tmp_path, monkeypatch):
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        data = b"# synthetic example; never executed by this unit test\n"
        member = tarfile.TarInfo("worktree_controller-1.30.5/examples/quickstart.py")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    files = {"worktree_controller-1.30.5-py3-none-any.whl": b"synthetic wheel",
             "worktree_controller-1.30.5.tar.gz": source.getvalue()}
    root, manifest = save_bundle(release, tmp_path / "bundle", files)
    monkeypatch.setattr(release, "read_json", lambda *a, **k: {"urls": [pypi_row(row) for row in manifest["files"]]})
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda url, **k: io.BytesIO(files[url.rsplit('/', 1)[1]]))
    return root, manifest


def test_fresh_installs_retry_only_index_propagation(release, tmp_path, monkeypatch):
    root, manifest = verification_fixture(release, tmp_path, monkeypatch)
    installs, environments, sleeps = [], [], []
    monkeypatch.setenv("GH_TOKEN", "synthetic-fixture-value")

    def run(*arguments, env=None, **kwargs):
        environments.append(env)
        if arguments[:3] == ("uv", "pip", "install"):
            installs.append(arguments)
            if len(installs) == 1:
                return subprocess.CompletedProcess(arguments, 1, "", "there is no version of worktree-controller==1.30.5")
        output = ""
        if "capabilities" in arguments:
            output = json.dumps({"tool_version": manifest["version"], "decision": {
                name: manifest[name] for name in ["contract_version", "storage_layout_version"]}})
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release.time, "sleep", lambda value: sleeps.append(value))
    assert release.verify(root, tmp_path / "verify")["status"] == "FRESH_INSTALLS_VERIFIED"
    assert len(installs) == 3 and sleeps == [10]
    assert all("GH_TOKEN" not in env for env in environments)


def test_fresh_install_functional_failure_is_not_retried(release, tmp_path, monkeypatch):
    root, _ = verification_fixture(release, tmp_path, monkeypatch)
    calls = []

    def run(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ("uv", "pip", "install"):
            return subprocess.CompletedProcess(arguments, 1, "", "build backend failure")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release.time, "sleep", lambda value: pytest.fail("functional errors must not retry"))
    with pytest.raises(RuntimeError, match="build backend failure"):
        release.verify(root, tmp_path / "verify")
    assert sum(args[:3] == ("uv", "pip", "install") for args in calls) == 1


def test_download_drift_is_rejected_before_installing(release, tmp_path, monkeypatch):
    root, _ = verification_fixture(release, tmp_path, monkeypatch)
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"different"))
    monkeypatch.setattr(release, "run", lambda *a, **k: pytest.fail("must not execute corrupt bytes"))
    with pytest.raises(RuntimeError, match="downloaded PyPI artifact drift"):
        release.verify(root, tmp_path / "verify")
