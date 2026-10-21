# Contributing to Worktree Controller

Thank you for your interest in improving Worktree Controller (WC). This document explains how to
set up a development environment, run the checks, and propose changes.

## Scope and responsibilities

WC is the control plane for Git worktree budget, allocation authority, ownership,
integration-candidate evidence, serialized main-publication authority, recovery and guarded
checkout release. It does **not** replace Git or the other delivery tools, and contributions must
preserve those boundaries:

- **Git** stays authoritative for registered worktrees, paths, branches, HEADs, refs, index state
  and cleanliness.
- **Worktree Controller** is the authority for task-checkout lifecycle, ownership, admission and
  policy.
- **Context Loader** reads context; **Snapshot Runner** gathers read-only review evidence;
  **Git Finalizer** commits and publishes. No tool acquires another tool's mutation authority.

See [docs/architecture.md](docs/architecture.md) for module ownership and
[docs/operations.md](docs/operations.md) for the development, handoff, integration, recovery and
release flows.

## Supported platform

The supported runtime is **Linux** with **Python 3.12** (`>=3.12,<3.13`), **Git** and a readable
`/proc` (procfs). Writer identity, runtime evidence and repository locking depend on Linux process
interfaces and POSIX `flock`. Pull requests that change platform or Python-version support need a
matching port plus tests and must update the declared `requires-python` and classifiers.

## Development setup

WC uses [uv](https://docs.astral.sh/uv/) for a locked, reproducible environment:

```bash
git clone <repository-url> wc && cd wc
uv sync --locked --python 3.12
```

## Running checks

The project has two gate scripts. Always give them a **new** output directory; they refuse a
pre-existing one so prior evidence is preserved.

- Fast integration gate (sharded). `SHARDS` is 1–64:

  ```bash
  bash scripts/check-shards [SHARDS]
  ```

- Complete release gate: lint, build, wheel + sdist packaging, installed smoke checks and the full
  default test suite. It requires Linux, Python 3.12 and an **unprivileged** user (it verifies file
  permissions):

  ```bash
  uv run --locked --python 3.12 --no-env-file python scripts/check-project \
    --work-dir /path/to/a/new/project-check
  ```

`check-project` sets its own pytest `--basetemp`. If you run `pytest` directly, always pass an
explicit new `--basetemp`; do not run a bare `pytest`.

Linting uses [ruff](https://docs.astral.sh/ruff/) (line length 100, target `py312`, rules
`E,F,W,I,UP,B,SIM`); the configuration lives in `pyproject.toml`.

## Cross-tool acceptance

`scripts/check-cross-tool` validates WC against exact installed versions of the companion tools.
Run `scripts/check-cross-tool --help` for the current arguments. Gates whose environment is not
available should be recorded as skipped rather than claimed as passing.

## Proposing changes

1. Open an issue describing the problem or proposal before starting substantial work, so the
   approach can be agreed early.
2. Keep changes minimal and focused. Public CLI, capability and storage-layout changes each need
   their own version and compatibility review; do not bundle a lifecycle-protocol bump into an
   unrelated documentation or CI change.
3. Add or update tests. Start with a regression for the observed behavior and its unchanged refusal
   boundary; for shared authority checks, cover both the successful path and the error taxonomy.
4. Update `tool_cli_contract.json` **and** its packaged copy
   `src/worktree_controller/tool_cli_contract.json` together; they must stay byte-identical.
5. Update `README.md`, `CHANGELOG.md` and `docs/` where behavior, interfaces or version/compatibility
   history change.
6. Run the relevant checks locally and report the real results.

Please do not commit credentials, private service details, business data or internal audit content.
The test suite uses synthetic repositories and `@example.invalid` identities; keep it that way.

## License

By contributing, you agree your contributions will be licensed under the
[Apache License 2.0](LICENSE).
