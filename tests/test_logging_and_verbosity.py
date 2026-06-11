"""Tier-A tests for the ``ansible_host`` library's logging + verbosity.

These tests pin down the contract that the verbosity ladder and the
``display.verbosity`` save/restore are not silently broken. They use
pytest's ``caplog`` and ``monkeypatch`` so no Ansible internals are
mocked.
"""

from __future__ import annotations

import logging

import pytest
from ansible.utils.display import Display

from ansible_host import AnsibleLocalhost

LOGGER_NAME = "ansible_host"
ENV_VAR = "ANSIBLE_HOST_VERBOSITY"


@pytest.fixture
def host():
    return AnsibleLocalhost()


@pytest.fixture(autouse=True)
def _clear_verbosity_env(monkeypatch):
    """Strip any user-set ANSIBLE_HOST_VERBOSITY so each test sees a clean env."""
    monkeypatch.delenv(ENV_VAR, raising=False)


def _ansible_host_records(caplog):
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def test_display_verbosity_is_restored_after_run(host):
    """``_run`` mutates ``display.verbosity`` (ansible-core global state) but
    MUST restore it on exit. If this regresses, every script using the library
    will have its global Ansible verbosity silently stomped on the first call.
    """
    sentinel = 99
    display = Display()
    original = display.verbosity
    try:
        display.verbosity = sentinel
        host.run_module("ansible.builtin.ping")
        assert display.verbosity == sentinel, (
            f"display.verbosity was not restored: expected {sentinel}, "
            f"got {display.verbosity}"
        )
    finally:
        display.verbosity = original


def test_default_verbosity_emits_compact_json(host, caplog):
    """With no env var and no options.verbosity, the library defaults to
    verbosity=2, which logs the per-task action + args/kwargs/directives.
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        host.run_module("ansible.builtin.ping")

    records = _ansible_host_records(caplog)
    assert records, (
        f"Default verbosity (=2) should emit ansible_host log records, "
        f"got none. All records: {caplog.records!r}"
    )
    # At v=2 the pre-run log line includes the module name + 'args=' marker.
    joined = "\n".join(r.getMessage() for r in records)
    assert "ansible.builtin.ping" in joined
    assert "args=" in joined, (
        f"v=2 should log args=/kwargs=/task_directives=; got: {joined!r}"
    )


def test_verbosity_zero_emits_no_library_logs(host, caplog):
    """options={'verbosity': 0} must suppress all ansible_host log output,
    including the pre-run task line and the post-run results line.
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        host.run_module(
            "ansible.builtin.ping",
            options={"verbosity": 0},
        )

    records = _ansible_host_records(caplog)
    assert not records, (
        f"verbosity=0 should emit no ansible_host log records, got "
        f"{len(records)}: {[r.getMessage() for r in records]!r}"
    )


def test_options_verbosity_overrides_env_var(host, caplog, monkeypatch):
    """options={'verbosity': N} wins over ANSIBLE_HOST_VERBOSITY. We set the
    env to 0 (would suppress everything) and pass options=3 (indented JSON).
    The post-run line at v=3 indents the results dict, so we look for the
    4-space indent that ``json.dumps(..., indent=4)`` produces.
    """
    monkeypatch.setenv(ENV_VAR, "0")

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        host.run_module(
            "ansible.builtin.ping",
            options={"verbosity": 3},
        )

    records = _ansible_host_records(caplog)
    assert records, (
        "options.verbosity=3 should override env=0 and emit records; got none."
    )
    joined = "\n".join(r.getMessage() for r in records)
    # indent=4 -> newline + 4 spaces appears in indented JSON output.
    assert "\n    " in joined, (
        f"v=3 should emit indented JSON (indent=4) in the results line; "
        f"got: {joined!r}"
    )
