"""Tier-2 integration tests over a REAL SSH transport.

Unlike the Tier-1 ``local`` tests, these exercise Ansible's ssh connection
plugin end-to-end: connection options, remote module copy/exec, privilege
escalation (become) over ssh, multi-host fan-out, and result parsing from a
genuinely remote (containerized) host. This is the piece the Tier-1 suite
explicitly leaves untested.

All tests carry the ``ssh`` marker and auto-skip unless an SSH target is
advertised via environment variables (set by the docker-compose-backed CI job
in ``tests/ssh/``, or any sshd you point them at):

    AH_SSH_HOST   (default: 127.0.0.1)
    AH_SSH_PORT   (default: 2222)
    AH_SSH_USER   (default: ansible)
    AH_SSH_KEY    (required: path to the private key authorized on the target)

If ``AH_SSH_KEY`` is unset/missing or the target port is not accepting
connections, the whole module is skipped, so the default ``pytest`` run (and
Windows/WSL local development) stays green without any SSH infrastructure.
"""

from __future__ import annotations

import os
import socket

import pytest

from ansible_host import AnsibleHost, AnsibleHosts, AnsibleModuleFailed

pytestmark = pytest.mark.ssh

SSH_HOST = os.environ.get("AH_SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("AH_SSH_PORT", "2222"))
SSH_USER = os.environ.get("AH_SSH_USER", "ansible")
SSH_KEY = os.environ.get("AH_SSH_KEY")

# Loopback/containerized targets have ephemeral host keys; don't let host-key
# checking (or a polluted known_hosts) break the connection.
_COMMON_ARGS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _require_ssh_target() -> None:
    if not SSH_KEY:
        pytest.skip("AH_SSH_KEY not set; no SSH target configured")
    if not os.path.exists(SSH_KEY):
        pytest.skip(f"AH_SSH_KEY points to a missing file: {SSH_KEY}")
    if not _port_open(SSH_HOST, SSH_PORT):
        pytest.skip(f"No SSH listener on {SSH_HOST}:{SSH_PORT}")


def _host_line(alias: str) -> str:
    return (
        f"{alias} ansible_host={SSH_HOST} ansible_port={SSH_PORT} "
        f"ansible_user={SSH_USER} ansible_connection=ssh "
        f"ansible_ssh_private_key_file={SSH_KEY} "
        f"ansible_ssh_common_args='{_COMMON_ARGS}'\n"
    )


@pytest.fixture
def ssh_inventory(tmp_path):
    _require_ssh_target()
    inv = tmp_path / "ssh_hosts.ini"
    inv.write_text(_host_line("target"))
    return str(inv)


@pytest.fixture
def ssh_host(ssh_inventory):
    return AnsibleHost(inventory=ssh_inventory, pattern="target", options={"timeout": 10})


def test_ssh_ping_pong(ssh_host):
    result = ssh_host.run_module("ansible.builtin.ping")
    assert result.get("ping") == "pong"


def test_ssh_command_returns_remote_stdout(ssh_host):
    result = ssh_host.run_module("ansible.builtin.command", args=["echo hello-over-ssh"])
    assert result.get("rc") == 0
    assert "hello-over-ssh" in result.get("stdout", "")


def test_ssh_become_escalates_to_root(ssh_host):
    """become over ssh must run the task as root (passwordless sudo target)."""
    result = ssh_host.run_module(
        "ansible.builtin.command",
        args=["id -un"],
        task_directives={"become": True},
    )
    assert result.get("rc") == 0
    assert result.get("stdout", "").strip() == "root"


def test_ssh_failing_command_raises(ssh_host):
    with pytest.raises(AnsibleModuleFailed):
        ssh_host.run_module("ansible.builtin.command", args=["false"])


def test_ssh_gather_real_remote_facts(ssh_host):
    """Run setup directly to confirm real facts are collected over ssh."""
    result = ssh_host.run_module("ansible.builtin.setup")
    assert "ansible_facts" in result
    assert result["ansible_facts"].get("ansible_system") == "Linux"


def test_ssh_multi_host_fanout(tmp_path):
    """Two inventory aliases pointing at the same target exercise ssh fan-out
    and the multi-host result-aggregation path over a real connection."""
    _require_ssh_target()
    inv = tmp_path / "ssh_multi.ini"
    inv.write_text(_host_line("node_a") + _host_line("node_b"))
    hosts = AnsibleHosts(inventory=str(inv), pattern="all", options={"timeout": 10})

    results = hosts.run_module("ansible.builtin.ping")
    assert set(results.keys()) == {"node_a", "node_b"}
    for res in results.values():
        assert res.get("ping") == "pong"
