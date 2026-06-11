# ansible-host

A thin Python wrapper over Ansible's executor. Lets you run Ansible modules from Python with structured results, batch execution, and a host-object-first API — without going through `pytest-ansible` or `ansible-runner`.

> **Status: alpha (0.1.0a0).** Built from a working internal implementation; API is stable in shape but may shift in details before 1.0.

## Why this exists

Ansible has a great executor and a huge module ecosystem, but the existing Python access paths are awkward for in-test or in-tool use:

| | `pytest-ansible` | `ansible-runner` | `ansible-host` |
| --- | --- | --- | --- |
| Use case | Pytest fixtures | AWX-style managed jobs | In-process programmatic |
| API surface | ~20% of Ansible's runtime | Subprocess + event stream | Typed Python objects |
| Forking, custom callbacks | Limited | Yes (in subprocess) | Yes (native) |
| Per-call overhead | Low | High (subprocess + JSON parsing) | Low (in-process) |
| Returns | Strings | Event stream | Structured Python dicts |

`ansible-host` sits where the other two don't: in-process, low-overhead, structured-result execution that you can drop into any Python codebase that wants Ansible underneath.

## Install

```bash
pip install ansible-host
```

Requires Python 3.10+ and `ansible-core>=2.16,<2.20`. Like Ansible itself, the library runs on POSIX systems (Linux, macOS, WSL) — it is not supported on native Windows.

## Quickstart

```python
from ansible_host import AnsibleHost, AnsibleHosts

# Single host
host = AnsibleHost(inventory="inventory.yml", pattern="vlab-01")
result = host.shell("uptime")
print(result["stdout"])

# Group of hosts (returns dict keyed by hostname)
hosts = AnsibleHosts(inventory="inventory.yml", pattern="vms_1")
results = hosts.ping()
for hostname, r in results.items():
    print(hostname, r["ping"])

# Any Ansible module is callable as a method via __getattr__
host.copy(src="/etc/hosts", dest="/tmp/hosts.bak")
host.shell("ls /tmp", task_directives={"ignore_errors": True})

# Batch mode: queue tasks, run them in a single play
with host:
    host.shell("uptime")
    host.shell("df -h")
    host.shell("free -m")
results = host.results  # list of task results
```

## Compatibility

This library uses Ansible's internal Python API (`TaskQueueManager`, `InventoryManager`, `VariableManager`, `Play`, `DataLoader`). Those APIs are not officially stable across `ansible-core` releases — expect occasional updates when `ansible-core` introduces breaking internal changes. The current support range is declared in `pyproject.toml` and in the matrix CI.

## Concurrency

`ansible-host` is designed for **sequential use** at the instance level. For parallelism within a single call, use Ansible's native forking via the `forks=` option. Running multiple `AnsibleHost` / `AnsibleHosts` instances concurrently in different threads will race on `ansible.context`'s process-global state.

## Reference implementation

A real-world use of this pattern lives in [sonic-mgmt's `tbng` branch](https://github.com/wangxin/sonic-mgmt/blob/tbng/ansible/testbed/base/ansible_hosts.py) — `ansible-host` is the cleaned-up, packaged version of that code.

## License

MIT.
