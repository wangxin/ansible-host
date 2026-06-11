"""Tier-1 integration tests using Ansible's `local` connection.

These tests exercise the full execution path (build_task -> TaskQueueManager
-> result parsing -> failure handling) without requiring any SSH listener or
external host. They run the modules in-process on the test runner itself.

If these tests fail, the library is broken for real users; if they pass, the
SSH transport is the only piece left untested (covered by Tier-2 tests when
they are added).
"""

from __future__ import annotations

import json

import pytest

from ansible_host import (
    AnsibleHost,
    AnsibleHosts,
    AnsibleLocalhost,
    AnsibleModuleFailed,
    NoAnsibleHostError,
)


@pytest.fixture
def host():
    """A fresh AnsibleLocalhost per test.

    AnsibleLocalhost defaults to connection: local, so no SSH is involved.
    """
    return AnsibleLocalhost()


def test_ping_returns_pong(host):
    result = host.run_module("ansible.builtin.ping")
    assert isinstance(result, dict)
    assert result.get("ping") == "pong"
    assert result.get("failed") in (False, None)


def test_command_module_returns_stdout(host):
    result = host.run_module(
        "ansible.builtin.command",
        args=["echo hello-from-ansible-host"],
    )
    assert isinstance(result, dict)
    assert result.get("rc") == 0
    assert "hello-from-ansible-host" in result.get("stdout", "")


def test_failing_command_raises(host):
    with pytest.raises(AnsibleModuleFailed):
        host.run_module(
            "ansible.builtin.command",
            args=["false"],
        )


def test_ignore_errors_does_not_raise(host):
    # Use the formal task directive (preferred over the legacy
    # module_ignore_errors kwarg).
    result = host.run_module(
        "ansible.builtin.command",
        args=["false"],
        task_directives={"ignore_errors": True},
    )
    assert isinstance(result, dict)
    assert result.get("failed") is True
    assert result.get("rc") != 0


def test_batch_mode_executes_each_task_exactly_once(host):
    """Regression test for the bug where run_module() in batch mode would
    queue the task AND immediately execute it (missing `return` in the
    batch branch). After the fix, batch tasks are only executed at __exit__.
    """
    with host:
        host.run_module("ansible.builtin.ping")
        host.run_module(
            "ansible.builtin.command",
            args=["echo batch-test"],
        )

    results = host.results
    # Two tasks queued -> two results back.
    assert isinstance(results, list), f"expected list, got {type(results).__name__}"
    assert len(results) == 2, f"expected 2 results, got {len(results)}"

    ping_result, cmd_result = results
    assert ping_result.get("ping") == "pong"
    assert cmd_result.get("rc") == 0
    assert "batch-test" in cmd_result.get("stdout", "")


def test_dynamic_dispatch_via_getattr(host):
    """The __getattr__ shorthand should dispatch to run_module."""
    result = host.ping()
    assert isinstance(result, dict)
    assert result.get("ping") == "pong"


def test_dynamic_dispatch_with_positional_arg(host):
    """`host.command("echo hi")` should pass the positional arg as the
    module's free-form (_raw_params) input."""
    result = host.command("echo dynamic-positional")
    assert isinstance(result, dict)
    assert result.get("rc") == 0
    assert "dynamic-positional" in result.get("stdout", "")


def test_dynamic_dispatch_with_kwargs(host):
    """`host.command(cmd=...)` should pass kwargs as the module parameters."""
    result = host.command(cmd="echo dynamic-kwargs")
    assert isinstance(result, dict)
    assert result.get("rc") == 0
    assert "dynamic-kwargs" in result.get("stdout", "")


def test_dynamic_dispatch_inside_with_block(host):
    """Dynamic dispatch should be deferred too when used inside a with block."""
    with host:
        host.ping()
        host.command("echo from-with-block")

    results = host.results
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0].get("ping") == "pong"
    assert "from-with-block" in results[1].get("stdout", "")


def test_dynamic_dispatch_passes_task_directives(host):
    """task_directives should still be honored via dynamic dispatch."""
    result = host.command("false", task_directives={"ignore_errors": True})
    assert isinstance(result, dict)
    assert result.get("failed") is True
    assert result.get("rc") != 0


def test_load_module_then_run_loaded_modules(host):
    """Explicit load_module + run_loaded_modules sequence (no context manager)."""
    host.load_module("ansible.builtin.ping")
    host.load_module(
        "ansible.builtin.command",
        args=["echo from-loaded"],
    )

    results = host.run_loaded_modules()
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0].get("ping") == "pong"
    assert "from-loaded" in results[1].get("stdout", "")


def test_run_loaded_modules_clears_queue(host):
    """Successive run_loaded_modules calls must not re-execute earlier tasks."""
    host.load_module("ansible.builtin.ping")
    first = host.run_loaded_modules()
    second = host.run_loaded_modules()

    assert isinstance(first, dict)
    assert first.get("ping") == "pong"
    # Second call: queue is empty, returns {} per library contract.
    assert second == {}


def test_unsupported_module_raises(host):
    """Asking for a nonexistent module should raise UnsupportedAnsibleModule."""
    from ansible_host import UnsupportedAnsibleModule

    with pytest.raises(UnsupportedAnsibleModule):
        host.run_module("ansible.builtin.this_module_does_not_exist_xyz")


def test_unsupported_module_via_dynamic_dispatch_raises(host):
    """Same coverage but via the __getattr__ entry point."""
    from ansible_host import UnsupportedAnsibleModule

    with pytest.raises(UnsupportedAnsibleModule):
        host.this_module_does_not_exist_xyz()


def test_explicit_and_dynamic_dispatch_produce_equivalent_results(host):
    """Sanity: both invocation styles should yield the same shape of result."""
    explicit = host.run_module("ansible.builtin.command", args=["echo same"])
    dynamic = host.command("echo same")
    assert explicit.get("rc") == dynamic.get("rc") == 0
    assert "same" in explicit["stdout"]
    assert "same" in dynamic["stdout"]


# -----------------------------------------------------------------------------
# no_log
# -----------------------------------------------------------------------------

SECRET = "super-secret-password-xyz-123"


def test_no_log_true_censors_invocation_args(host):
    """With no_log=True, the result must not leak the sensitive cmd string."""
    result = host.run_module(
        "ansible.builtin.command",
        args=[f"echo {SECRET}"],
        task_directives={"no_log": True},
    )
    # The command still ran (stdout is allowed because the user opted into it),
    # but the args/invocation must be censored so logs don't leak the cmd.
    serialized = json.dumps(result, default=str)
    invocation = result.get("invocation") or {}
    assert SECRET not in json.dumps(invocation, default=str), (
        "no_log=True must censor invocation args, but SECRET was found in: "
        f"{invocation!r}"
    )
    # Ansible marks censored results explicitly.
    assert result.get("censored") or result.get("_ansible_no_log") or (
        "VALUE_SPECIFIED_IN_NO_LOG_PARAMETER" in serialized
    ), f"no_log=True did not produce a censored result. Full result: {serialized}"


def test_no_log_false_includes_invocation_args(host):
    """Negative control: without no_log, the secret leaks somewhere in the
    result (stdout from ``echo`` at minimum). This proves the no_log test
    above is not a false positive (i.e., the secret is genuinely censored
    rather than just absent from the result for unrelated reasons).
    """
    result = host.run_module(
        "ansible.builtin.command",
        args=[f"echo {SECRET}"],
    )
    serialized = json.dumps(result, default=str)
    assert SECRET in serialized, (
        "Without no_log, the secret should be visible somewhere in the result "
        "(typically stdout). Negative control failed -- the no_log positive "
        f"test may be passing for the wrong reason. result={result!r}"
    )


# -----------------------------------------------------------------------------
# forks
# -----------------------------------------------------------------------------

def test_forks_option_accepted_for_single_host():
    """forks=N is meaningless for a single host but must not break execution."""
    host = AnsibleLocalhost(options={"forks": 5})
    result = host.ping()
    assert result.get("ping") == "pong"


@pytest.fixture
def multi_host_inventory(tmp_path):
    """Inventory of 4 'hosts' all backed by the local connection plugin.

    The hostnames are arbitrary labels; ansible_connection=local makes
    each one execute on the test runner itself. This lets us exercise
    multi-host fan-out (and the forks knob) without any network/SSH setup.
    """
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[localpool]\n"
        "node1 ansible_connection=local\n"
        "node2 ansible_connection=local\n"
        "node3 ansible_connection=local\n"
        "node4 ansible_connection=local\n"
    )
    return str(inv)


def _names_from(results: dict) -> set:
    return set(results.keys())


def test_multi_host_fanout_with_forks_default(multi_host_inventory):
    """All 4 hosts should produce a result with default forks."""

    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    results = hosts.run_module("ansible.builtin.ping")
    assert isinstance(results, dict)
    assert _names_from(results) == {"node1", "node2", "node3", "node4"}
    for hostname, res in results.items():
        assert res.get("ping") == "pong", f"{hostname} did not pong: {res!r}"


def test_multi_host_fanout_with_forks_one_runs_sequentially(multi_host_inventory):
    """forks=1 forces sequential execution but must still produce all results."""

    hosts = AnsibleHosts(
        inventory=multi_host_inventory, pattern="all", options={"forks": 1}
    )
    results = hosts.run_module("ansible.builtin.ping")
    assert _names_from(results) == {"node1", "node2", "node3", "node4"}
    for res in results.values():
        assert res.get("ping") == "pong"


def test_multi_host_fanout_with_forks_high(multi_host_inventory):
    """forks=10 (> host count) must not break; all hosts still produce results."""

    hosts = AnsibleHosts(
        inventory=multi_host_inventory, pattern="all", options={"forks": 10}
    )
    results = hosts.run_module("ansible.builtin.ping")
    assert _names_from(results) == {"node1", "node2", "node3", "node4"}
    for res in results.values():
        assert res.get("ping") == "pong"

# ============================================================================
# AnsibleHosts container protocol + multi-host contract
# ----------------------------------------------------------------------------
# Cover the API surface of AnsibleHosts that the fanout tests do NOT exercise:
# - __len__, __getitem__ (int + str + bad key), __iter__
# - hosts_count / hostnames properties
# - pattern matching (subset, empty -> NoAnsibleHostError)
# - _make_single_host regression (returns AnsibleHost that actually executes)
# - per-host failure aggregation with ignore_errors
# - __getattr__ rejects dunder names on AnsibleHosts (parallel to base smoke)
# ============================================================================


def test_hosts_len_count_and_names_agree(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    assert len(hosts) == 4
    assert hosts.hosts_count == 4
    assert set(hosts.hostnames) == {"node1", "node2", "node3", "node4"}


def test_hosts_pattern_selects_subset(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="node1:node2")
    assert len(hosts) == 2
    assert set(hosts.hostnames) == {"node1", "node2"}


def test_hosts_pattern_matching_none_raises(multi_host_inventory):
    with pytest.raises(NoAnsibleHostError):
        AnsibleHosts(inventory=multi_host_inventory, pattern="does-not-exist")


def test_hosts_getitem_by_int_returns_executable_single_host(multi_host_inventory):
    """Regression: hosts[0] must reuse parsed inventory AND actually run a module.

    This is the test that pins down the _make_single_host fast-path: if it
    forgets to copy any field (loader / im / vm / options / _batch_mode / ...),
    the resulting AnsibleHost will look fine until you try to .run_module()
    on it.
    """
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    first = hosts[0]
    assert isinstance(first, AnsibleHost)
    assert first.hostname == "node1"

    # Single-host result must be unwrapped (no hostname key).
    result = first.run_module("ansible.builtin.ping")
    assert isinstance(result, dict)
    assert result.get("ping") == "pong"


def test_hosts_getitem_by_string_matches_int_index(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    by_int = hosts[0]
    by_str = hosts["node1"]
    assert by_int.hostname == by_str.hostname == "node1"
    assert type(by_int) is type(by_str) is AnsibleHost


def test_hosts_getitem_bad_keys_raise(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    with pytest.raises(IndexError):
        hosts[99]
    with pytest.raises(KeyError):
        hosts["no-such-node"]
    with pytest.raises(TypeError):
        hosts[1.5]  # type: ignore[index]


def test_hosts_iteration_yields_one_ansible_host_per_member(multi_host_inventory):
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    seen = list(hosts)
    assert len(seen) == 4
    assert all(isinstance(h, AnsibleHost) for h in seen)
    assert [h.hostname for h in seen] == hosts.hostnames


def test_hosts_dunder_attribute_does_not_become_module(multi_host_inventory):
    """AnsibleHostsBase.__getattr__ must NOT treat dunder probes as module names.

    Without this guard, copy/pickle/repr/debuggers probing for things like
    __deepcopy__ would build an Ansible task named '__deepcopy__' and blow up
    with UnsupportedAnsibleModule the first time anyone copies a hosts object.
    """
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    with pytest.raises(AttributeError):
        hosts.__deepcopy__  # noqa: B018  -- attribute access is the test


def test_hosts_failure_does_not_drop_others_from_results(multi_host_inventory):
    """Per-host result aggregation: when run_module fails, every host still
    appears in the results dict (failures are aggregated, not short-circuited).

    We make all 4 hosts fail (running ``false``) with ignore_errors=True so the
    call returns instead of raising. The contract being pinned is "the results
    dict has one entry per matched host even on failure paths" -- the bug this
    guards against would be silently dropping failed hosts from the dict.
    """
    hosts = AnsibleHosts(inventory=multi_host_inventory, pattern="all")
    results = hosts.run_module(
        "ansible.builtin.command",
        args=["false"],
        task_directives={"ignore_errors": True},
    )
    assert isinstance(results, dict)
    assert set(results.keys()) == {"node1", "node2", "node3", "node4"}
    for name, res in results.items():
        assert res.get("failed") is True, f"{name} should be failed: {res!r}"
        assert res.get("rc") == 1, f"{name} should have rc=1: {res!r}"
