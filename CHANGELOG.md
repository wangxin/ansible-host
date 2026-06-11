# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `__version__` is now derived from installed package metadata via
  `importlib.metadata` instead of a hardcoded string, so it stays in sync
  with `pyproject.toml` automatically.
- `Operating System` classifier widened from `POSIX :: Linux` to `POSIX`
  to match the documented support range (Linux, macOS, WSL).
- Added `Changelog` URL to `[project.urls]` (linked from PyPI sidebar).

## [0.1.0a0] — 2026-06-11

Initial alpha release.

### Added
- `AnsibleHost`, `AnsibleHosts`, `AnsibleLocalhost` classes for in-process
  Ansible module execution with structured results.
- Dynamic dispatch: any Ansible module is callable as a method
  (e.g. `host.ping()`, `host.command("uname -a")`).
- Batch execution via context manager (`with host:`) and via explicit
  `load_module` / `run_loaded_modules`.
- Container protocol on `AnsibleHosts` (`len`, `[]` by int or hostname,
  iteration).
- Per-host failure aggregation via `task_directives={"ignore_errors": True}`.
- Verbosity-controlled debug logging via the `ansible_host` logger and the
  `ANSIBLE_HOST_VERBOSITY` environment variable.
- Inline `_JsonResultsCallback` for result collection without a separate
  plugin file.
- Tier-1 integration test suite (30 tests) using Ansible's `local`
  connection — no SSH or Docker required.
- Tier-A logging/verbosity tests guarding `display.verbosity` save/restore
  and the verbosity ladder contract.

### Supported
- Python 3.10, 3.11, 3.12, 3.13
- ansible-core 2.16, 2.17, 2.18, 2.19, 2.20, 2.21
- POSIX systems (Linux, macOS, WSL); not supported on native Windows.

[Unreleased]: https://github.com/wangxin/ansible-host/compare/v0.1.0a0...HEAD
[0.1.0a0]: https://github.com/wangxin/ansible-host/releases/tag/v0.1.0a0
