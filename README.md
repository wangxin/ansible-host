# ansible-host

[![CI](https://github.com/wangxin/ansible-host/actions/workflows/ci.yml/badge.svg)](https://github.com/wangxin/ansible-host/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/wangxin/ansible-host/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

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

Requires Python 3.10+ and `ansible-core>=2.16,<2.22`. Like Ansible itself, the library runs on POSIX systems (Linux, macOS, WSL) — it is not supported on native Windows.

## Development

The recommended dev workflow uses [`uv`](https://docs.astral.sh/uv/) — it manages the virtualenv directly, sidestepping the `python3-venv` split on Ubuntu and installing dependencies an order of magnitude faster than pip.

```bash
# One-time: install uv (https://docs.astral.sh/uv/getting-started/installation/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# In the repo
uv venv                        # creates .venv with the default Python
uv pip install -e ".[dev]"     # editable install + dev extras
uv run pytest                  # run the test suite
uv run ruff check src tests    # lint
```

To target a specific Python or `ansible-core` version (matches the CI matrix):

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv pip install "ansible-core==2.19.*"
uv run pytest
```

`pip` and a manually-managed venv still work — `uv` is just the convenience.

## Quickstart

### 30-second try — no SSH, no inventory

`AnsibleLocalhost` runs modules in-process on the current machine via Ansible's `local` connection plugin. No inventory file, no SSH, no setup.

```python
from ansible_host import AnsibleLocalhost

host = AnsibleLocalhost()
result = host.ping()
assert result["ping"] == "pong"

result = host.command("uname -a")
print(result["stdout"])
```

### Running against a real host

Drop an inventory file alongside your script:

```ini
# inventory.ini
[switches]
sw-01 ansible_host=10.0.0.1 ansible_user=admin
```

```python
from ansible_host import AnsibleHost

host = AnsibleHost(inventory="inventory.ini", pattern="sw-01")
result = host.shell("show version")
print(result["stdout"])
```

### Multi-host fanout

```python
from ansible_host import AnsibleHosts

hosts = AnsibleHosts(inventory="inventory.ini", pattern="switches")
results = hosts.ping()                       # parallelism via forks=
for hostname, r in results.items():
    print(hostname, r["ping"])

# Container API: index, iterate, len
print(len(hosts), hosts.hostnames)
first = hosts[0]                             # -> AnsibleHost
by_name = hosts["sw-01"]                     # -> AnsibleHost
for h in hosts:
    print(h.hostname)
```

### Dynamic dispatch and task directives

Any Ansible module is callable as a method via `__getattr__` (`host.<module_name>(...)`):

```python
host.copy(src="/etc/hosts", dest="/tmp/hosts.bak")
host.command("rm /tmp/maybe-missing", task_directives={"ignore_errors": True})
host.shell("echo $TOKEN", task_directives={"no_log": True})
```

Common `task_directives`: `ignore_errors`, `no_log`, `when`, `failed_when`, `changed_when`, `become`.

### Batch mode — queue tasks, run them in a single play

```python
with host:
    host.shell("uptime")
    host.shell("df -h")
    host.shell("free -m")
results = host.results
```

### Building a queue across functions

`with host:` is lexically scoped. If you need to assemble a batch across multiple functions, use the explicit form — same machinery, no scope limit:

```python
host.load_module("ansible.builtin.command", args=["uptime"])
host.load_module("ansible.builtin.command", args=["df -h"])
results = host.run_loaded_modules()
```

### Result shapes

|              | single task             | batch (`with` block)                |
| ------------ | ----------------------- | ----------------------------------- |
| single host  | `dict`                  | `list[dict]`                        |
| multi host   | `{hostname: dict}`      | `{hostname: list[dict]}`            |

### Failures

A failing module raises `AnsibleModuleFailed`:

```python
from ansible_host import AnsibleModuleFailed

try:
    host.command("false")
except AnsibleModuleFailed as e:
    print("module failed:", e)

# Or suppress and inspect the result:
result = host.command("false", task_directives={"ignore_errors": True})
assert result["failed"] is True
```

### More examples

See [`tests/test_local_integration.py`](tests/test_local_integration.py) for 30 runnable examples covering ping, command, shell, batch mode, dynamic dispatch, `no_log`, `forks`, multi-host fanout, per-host failure aggregation, and the container protocol.

## Compatibility

This library uses Ansible's internal Python API (`TaskQueueManager`, `InventoryManager`, `VariableManager`, `Play`, `DataLoader`). Those APIs are not officially stable across `ansible-core` releases — expect occasional updates when `ansible-core` introduces breaking internal changes. The current support range is declared in `pyproject.toml` and in the matrix CI.

## Concurrency

`ansible-host` is designed for **sequential use** at the instance level. For parallelism within a single call, use Ansible's native forking via the `forks=` option. Running multiple `AnsibleHost` / `AnsibleHosts` instances concurrently in different threads will race on `ansible.context`'s process-global state.

## Reference implementation

A real-world use of this pattern lives in [sonic-mgmt's `tbng` branch](https://github.com/wangxin/sonic-mgmt/blob/tbng/ansible/testbed/base/ansible_hosts.py) — `ansible-host` is the cleaned-up, packaged version of that code.

## License

Apache License 2.0. See [LICENSE](LICENSE).
