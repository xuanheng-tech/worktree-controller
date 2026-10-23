# Publishing WC packages

The `Publish release` workflow in `.github/workflows/release.yml` releases an existing stable tag
that is already integrated into public `main`. It does not create tags, commit, merge or push Git
branches. Source delivery continues to use the repository's Controller and Git Finalizer flow.
The software version, CLI contract and storage layout remain independently versioned.

## One-time setup

The repository uses a GitHub environment named **`pypi`**, restricted to workflow runs from **main**.
The environment grants no additional review exemption. Keep any existing platform protections.

In the PyPI project's **Manage → Publishing → Add a new publisher → GitHub** form, register:

| Field | Exact value |
| --- | --- |
| PyPI project | `worktree-controller` |
| Owner | `xuanheng-tech` |
| Repository | `worktree-controller` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The owner completes this authenticated PyPI account step. Do not put a long-lived PyPI token in
GitHub secrets. The workflow uses `uv publish --trusted-publishing always`: an unavailable or
mismatched publisher fails rather than falling back to another credential. Existing local
credentials are not read, copied, revoked or changed by this workflow.

See [PyPI's publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) and
[uv's GitHub integration](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).

## Audit an existing release

Run from an authenticated GitHub CLI with access to the public repository:

```bash
gh workflow run release.yml --repo xuanheng-tech/worktree-controller --ref main \
  -f tag=v1.30.5 -F publish=false
```

This checks and builds the exact tag in a job with read-only repository permissions. Automation
comes from the immutable workflow commit; the payload is checked out separately at its tag.
The workflow records a manifest and checksums and audits the existing GitHub/PyPI inventory.
For an already published release, it also downloads and installs both distributions in fresh
environments and runs the onboarding example shipped in the source archive.

For an unpublished tag, audit reports the missing remote files and skips post-publication
installation. A successful audit is preparation evidence, not proof of a new publication or a
successful OIDC upload. WC's packaged onboarding contract introduced in 1.30.5 is required.

## Publish a real version

After the actual source change and version update have been validated and integrated, create and
publish its exact stable version tag through the normal release process. Then trigger once:

```bash
gh workflow run release.yml --repo xuanheng-tech/worktree-controller --ref main \
  -f tag=vX.Y.Z -F publish=true
```

Replace `vX.Y.Z` with that existing tag. The tag must exactly match `pyproject.toml` and the CLI
contract's software version. A tag outside main, a moved tag, dirty source, unknown file or
checksum mismatch stops the workflow. No force operation or Controller-state edit is used.

The workflow performs these stages:

1. Run `scripts/check-project` on the tag: lint, full default suite, wheel/sdist build, sdist
   rebuild and installed checks. Freeze one authoritative wheel/sdist pair in an immutable run
   artifact. Rebuilding the sdist for validation does not replace that pair.
2. With publishing permissions, recheck the source identity and both remote inventories. Store
   the exact pair and SHA256SUMS in a GitHub draft, upload only missing PyPI files using short-lived
   publishing credentials, verify the registry, and make the matching GitHub release public.
   Building or installing package code never runs in this privileged job.
3. In a separate job without publishing permissions, independently download both PyPI files,
   compare their hashes, install them with fresh caches, verify metadata and all three CLI entry
   points, and run the source archive's onboarding example. A short bounded retry handles a newly
   uploaded version missing from the package index; functional failures are not retried.

## Recover a failed or interrupted run

Inspect the failing step and its real exit code. A nonzero uploader exit remains a failed step,
even if a file reached the server. The GitHub draft and immutable Actions artifact retain the
original bytes. Resolve the stated blocker and use **Re-run failed jobs** in that same run:

```bash
gh run rerun RUN_ID --failed --repo xuanheng-tech/worktree-controller
```

The publish job consumes the successful build job's original artifact name, including its original
run attempt. It checks already uploaded bytes, uploads only missing files, and never uses
`--clobber` or blindly skips an existing PyPI filename. An already complete matching release is
observed without another upload. Unknown assets, yanked files, identity drift and different bytes
remain blocked; do not delete or replace them to make a retry pass.

A repository-wide workflow concurrency group serializes release runs without interrupting an
active publisher. GitHub retains only one pending run per concurrency group, so use failed-job
recovery instead of submitting a queue of duplicate dispatches. Bundles and check evidence have
90-day retention. Once an artifact expires, a new run must build and validate again; if its bytes
differ from existing remote files, it stops and requires review of the retained original evidence.
It never overwrites the old release.

Receipts distinguish a read-only audit, `PUBLISHED_VERIFIED`, and `FRESH_INSTALLS_VERIFIED`.
Skipped tests remain skipped. Local installation or activation of a maintainer's WC runtime is a
separate operation; publishing a package does not update that runtime or authorize a production
release in any repository governed by WC.

## Publication visibility

Successful writes can precede their visibility through GitHub or PyPI queries. After a successful
create, upload or publish command, the publisher polls only the missing observation. All phases
share one five-minute monotonic deadline, including draft, asset, PyPI and public-release visibility.
Each network command retains its own timeout; an in-flight request can finish after that deadline.
Metadata requests ask caches to revalidate. This does not guarantee immediate upstream visibility.

Every poll rechecks the immutable tag and main containment; normal inventory and byte/hash checks
remain active. Command errors, unknown assets, yanked files and identity/checksum drift stop
immediately. Polls never repeat writes. If visibility still times out, the job fails and keeps the
original bundle and current release. Audit destinations and rerun failed jobs in the same run as above.
