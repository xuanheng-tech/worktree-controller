# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues, pull requests or
discussions.

Instead, report privately so maintainers can triage before any public disclosure:

- Use the **Report a vulnerability** button under the **Security** tab of this repository on
  GitHub (GitHub Security Advisories / private vulnerability reporting). If private reporting is
  not yet enabled on the repository, contact the repository owner privately through the contact
  options shown on the repository's GitHub page.

Please include enough detail to reproduce the issue: the affected version(s), the steps or inputs
that trigger it, and the observed versus expected behavior. Do not include any real credentials,
tokens or private data in your report.

This project does not currently publish a fixed response-time service level agreement. Reports are
handled on a best-effort basis by the maintainers.

## Security model

Worktree Controller is a **local** control plane. Understanding its intended boundaries helps you
assess what is and is not a vulnerability:

- WC runs entirely on the local machine against local Git repositories. It does not run a network
  service, listen on a port, or transmit repository content to a remote server. The only outbound
  network use is Git's own operations (for example `git push` to a remote you configure) and the
  optional companion-tool integrations you install.
- WC stores only non-Git lifecycle facts under the repository's resolved Git common directory
  (`<git-common-dir>/worktree-controller/v1/`). Git remains authoritative for worktrees, paths,
  branches, HEADs, refs, index state and cleanliness.
- Authority mutations serialize on a repository lock and fail closed. Missing, symlinked or
  unusable locks, identity drift, expired leases and incomplete evidence block operations rather
  than granting them. WC never infers create/remove permission from the absence of a record.
- Writer entitlement binds the **real** live process identity (PID/ancestor and client/session/run)
  and the exact operation and target. Registering an unrelated process to simulate a writable
  session is refused by design.
- Runtime scans that gate deletion read same-UID process information from procfs; incomplete
  observation blocks dependent deletion, and unknown content is preserved rather than removed.
- The preservation/cleanup paths verify exact receipts, SHA-256 evidence and hardlink topology
  before any deletion, and refuse to follow directory-name allowlists or to remove content without
  immutable preservation coverage.

`worktree_controller.source_preservation` includes a defensive content scanner that flags common
credential shapes (for example AWS access key IDs and GitHub-style tokens) in tracked source
evidence. This is a protection mechanism, not an indication that the project collects or transmits
secrets.

## Supported versions

Security fixes are applied to the current released line. Version, CLI-contract, capability and
storage-layout versions are independent compatibility requirements; see `README.md` and
`CHANGELOG.md` for the current values. When reporting an issue, please state the exact
`worktree-controller --version` and `capabilities --json` output you observed.

## Scope

The following are expected behavior and not vulnerabilities:

- Operations refused with a fail-closed decision (for example `INVALID_LIFECYCLE`, admission
  refusals, write-guard refusals) when authority, identity or evidence requirements are not met.
- WC requiring a real, live writer process and exact identity matching before permitting writes.
- Git's own transport, credential and remote behavior, which is configured and owned by the caller.
