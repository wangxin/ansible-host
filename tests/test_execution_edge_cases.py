"""Tier-1 edge-case coverage for error contracts, failure aggregation,
unreachable hosts, the legacy ``module_ignore_errors`` kwarg, string-arg
guidance, fact gathering, and the result-collector's verbosity logging.

These pin down branches that the existing smoke/integration/logging suites
do not exercise. Everything runs over Ansible's ``local`` connection (or a
deliberately-refused SSH connection for the unreachable case), so no external
host is required.
"""

from __future__ import annotations

import pytest
from ansible.errors import AnsibleError

from ansible_host import (
    AnsibleHost,
    AnsibleHosts,
    AnsibleHostsBase,
    AnsibleLocalhost,
    AnsibleModuleFailed,
    MultipleAnsibleHostsError,
    NoAnsibleHostError,
    NoTasksError,
)


@pytest.fixture
def multi_host_inventory(tmp_path):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[localpool]\n"
        "node1 ansible_connection=local\n"
        "node2 ansible_connection=local\n"
    )
    return str(inv)


# ---------------------------------------------------------------------------
# Constructor validation / cardinality errors
# ---------------------------------------------------------------------------


def test_missing_inventory_file_raises(tmp_path):
    missing = str(tmp_path / "does-not-exist.ini")
    with pytest.raises(AnsibleError):
        AnsibleHost(inventory=missing, pattern="whatever")


def test_ansible_host_zero_match_raises(multi_host_inventory):
    with pytest.raises(NoAnsibleHostError):
        AnsibleHost(inventory=multi_host_inventory, pattern="ghost")


def test_ansible_host_multiple_match_raises(multi_host_inventory):
    with pytest.raises(MultipleAnsibleHostsError):
        AnsibleHost(inventory=multi_host_inventory, pattern="all")


# ---------------------------------------------------------------------------
# Multi-task / multi-host failure aggregation (_check_failed_results lists)
# ---------------------------------------------------------------------------


def test_single_host_multiple_tasks_failure_raises():
    """A failing task inside a multi-task batch must raise (list branch)."""
    host = AnsibleLocalhost()
    host.load_module("ansible.builtin.ping")
    host.load_module("ansible.builtin.command", args=["false"])
    with pytest.raises(AnsibleModuleFailed):
        host.run_loaded_modules()


def test_single_host_multiple_tasks_ignore_errors_no_raise():
    """With ignore_errors on the failing task, the batch returns all results."""
    host = AnsibleLocalhost()
    host.load_module("ansible.builtin.ping")
    host.load_module(
        "ansible.builtin.command",
        args=["false"],
        task_directives={"ignore_errors": True},
    )
    results = host.run_loaded_modules()
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0].get("ping") == "pong"
    assert results[1].get("failed") is True


def test_multi_host_multiple_tasks_failure_raises(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    hosts.load_module("ansible.builtin.ping")
    hosts.load_module("ansible.builtin.command", args=["false"])
    with pytest.raises(AnsibleModuleFailed):
        hosts.run_loaded_modules()


def test_multi_host_multiple_tasks_ignore_errors_no_raise(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    hosts.load_module("ansible.builtin.ping")
    hosts.load_module(
        "ansible.builtin.command",
        args=["false"],
        task_directives={"ignore_errors": True},
    )
    results = hosts.run_loaded_modules()
    assert set(results.keys()) == {"node1", "node2"}
    for per_host in results.values():
        assert isinstance(per_host, list)
        assert len(per_host) == 2
        assert per_host[0].get("ping") == "pong"
        assert per_host[1].get("failed") is True


# ---------------------------------------------------------------------------
# Unreachable host (v2_runner_on_unreachable)
# ---------------------------------------------------------------------------


@pytest.fixture
def unreachable_host(tmp_path):
    inv = tmp_path / "dead.ini"
    # 127.0.0.1:1 is a closed port -> ssh fails instantly (connection refused).
    inv.write_text(
        "deadhost ansible_host=127.0.0.1 ansible_port=1 "
        "ansible_connection=ssh ansible_user=nobody\n"
    )
    return AnsibleHost(inventory=str(inv), pattern="deadhost", options={"timeout": 2})


def test_unreachable_host_marked_not_reachable(unreachable_host):
    """An unreachable host must surface reachable=False / unreachable=True
    without being silently dropped. ignore_errors lets us inspect the result
    instead of having the library raise on the failure."""
    result = unreachable_host.run_module(
        "ansible.builtin.ping",
        task_directives={"ignore_errors": True},
    )
    assert isinstance(result, dict)
    assert result.get("reachable") is False
    assert result.get("unreachable") is True
    assert result.get("failed") is True


def test_unreachable_host_raises_without_ignore(unreachable_host):
    with pytest.raises(AnsibleModuleFailed):
        unreachable_host.run_module("ansible.builtin.ping")


# ---------------------------------------------------------------------------
# NoTasksError
# ---------------------------------------------------------------------------


def test_empty_batch_block_raises_no_tasks():
    host = AnsibleLocalhost()
    with pytest.raises(NoTasksError):
        with host:
            pass  # nothing loaded -> __exit__ runs an empty task list


# ---------------------------------------------------------------------------
# Legacy module_ignore_errors kwarg (build_task)
# ---------------------------------------------------------------------------


def test_build_task_module_ignore_errors_true_sets_ignore_errors():
    task = AnsibleHostsBase.build_task(
        "ansible.builtin.command",
        args=["false"],
        kwargs={"module_ignore_errors": True},
    )
    assert task.get("ignore_errors") is True
    # The legacy kwarg must be stripped from the module args.
    assert "module_ignore_errors" not in task["action"]["args"]


def test_build_task_module_ignore_errors_non_bool_is_ignored():
    """Only the literal bool True opts in; truthy non-bool (e.g. 1) must not."""
    task = AnsibleHostsBase.build_task(
        "ansible.builtin.command",
        args=["false"],
        kwargs={"module_ignore_errors": 1},
    )
    assert "ignore_errors" not in task


def test_module_ignore_errors_kwarg_suppresses_raise():
    host = AnsibleLocalhost()
    result = host.run_module(
        "ansible.builtin.command",
        args=["false"],
        kwargs={"module_ignore_errors": True},
    )
    assert result.get("failed") is True
    assert result.get("rc") != 0


# ---------------------------------------------------------------------------
# run_module string-args friendly error
# ---------------------------------------------------------------------------


def test_string_args_produces_helpful_error():
    host = AnsibleLocalhost()
    # The library re-raises the underlying ansible error type (which varies by
    # ansible-core version: AnsibleModuleFailed vs AnsibleParserError) with a
    # helpful note appended. Pin the appended guidance, not the concrete type.
    with pytest.raises(AnsibleError) as excinfo:
        # args must be a list; passing a string is the mistake being guided.
        host.run_module("ansible.builtin.ping", args="x")
    assert "must be a list" in str(excinfo.value)


# ---------------------------------------------------------------------------
# gather_facts=True
# ---------------------------------------------------------------------------


def test_gather_facts_true_still_runs_module():
    """gather_facts=True runs an extra implicit setup task, so the single-task
    return is a list ([setup_result, ping_result]); the ping must still pong."""
    host = AnsibleLocalhost()
    result = host.run_module("ansible.builtin.ping", gather_facts=True)
    assert isinstance(result, list)
    assert any(item.get("ping") == "pong" for item in result)


# ---------------------------------------------------------------------------
# Result-collector verbosity logging (_JsonResultsCallback._log_res)
# ---------------------------------------------------------------------------


def test_log_res_emits_when_ansible_verbosity_nonzero(capsys, monkeypatch):
    """``_log_res`` is gated on Ansible's own display.verbosity (which ``_run``
    sets from C.DEFAULT_VERBOSITY), independent of ANSIBLE_HOST_VERBOSITY. It is
    NOT dead code: raising the Ansible default makes the collector emit a
    per-host line. Pin that contract so the branch can't silently rot."""
    monkeypatch.setattr("ansible.constants.DEFAULT_VERBOSITY", 2, raising=False)
    host = AnsibleLocalhost()
    host.run_module("ansible.builtin.ping")
    captured = capsys.readouterr()
    assert "[localhost] =>" in (captured.err + captured.out)
