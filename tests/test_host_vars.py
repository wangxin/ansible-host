"""Tests for the variable-access API.

Covers the variable getters/properties that were previously untested:

  AnsibleHostsBase / AnsibleHosts (host-keyed):
    - get_host_var(hostname, var_name, default)
    - get_visible_var(hostname, var_name, default)
    - extra_vars (property)

  AnsibleHost / AnsibleLocalhost (single-host):
    - get_host_var(var_name, default)
    - get_visible_var(var_name, default)
    - update_extra_vars(extra_vars)
    - host_vars (property)
    - visible_vars (property)

The two layers being pinned down here are:
  * host_vars  -> only variables defined directly on the inventory host
                  (raw, NOT templated).
  * visible_vars -> the fully-resolved set a host can see (host + group +
                    extra vars) with Jinja2 templates rendered to native types.
"""

from __future__ import annotations

import pytest

from ansible_host import (
    AnsibleHost,
    AnsibleHosts,
    AnsibleLocalhost,
)

# A YAML inventory lets us declare host vars, group vars, a shared var (to
# exercise host > group precedence) and a Jinja2 template in one file.
_INVENTORY_YAML = """\
all:
  children:
    web:
      hosts:
        node1:
          ansible_connection: local
          host_only_var: from_host
          shared_var: from_host
          base_number: 21
          templated_var: "{{ base_number * 2 }}"
        node2:
          ansible_connection: local
          host_only_var: from_host2
      vars:
        group_only_var: from_group
        shared_var: from_group
"""


@pytest.fixture
def inventory(tmp_path):
    inv = tmp_path / "inventory.yml"
    inv.write_text(_INVENTORY_YAML)
    return str(inv)


# ---------------------------------------------------------------------------
# AnsibleHost (single host) — get_host_var / get_visible_var
# ---------------------------------------------------------------------------


@pytest.fixture
def node1(inventory):
    return AnsibleHost(inventory=inventory, pattern="node1")


def test_get_host_var_returns_host_defined_var(node1):
    assert node1.get_host_var("host_only_var") == "from_host"


def test_get_host_var_ignores_group_var(node1):
    """get_host_var sees only host-level vars, never group vars."""
    assert node1.get_host_var("group_only_var") is None
    assert node1.get_host_var("group_only_var", "fallback") == "fallback"


def test_get_host_var_missing_returns_default(node1):
    assert node1.get_host_var("does_not_exist") is None
    assert node1.get_host_var("does_not_exist", "the-default") == "the-default"


def test_get_host_var_returns_raw_unrendered_template(node1):
    """host vars are returned verbatim — templates are NOT rendered here."""
    assert node1.get_host_var("templated_var") == "{{ base_number * 2 }}"


def test_get_visible_var_returns_group_var(node1):
    assert node1.get_visible_var("group_only_var") == "from_group"


def test_get_visible_var_returns_host_var(node1):
    assert node1.get_visible_var("host_only_var") == "from_host"


def test_get_visible_var_host_beats_group(node1):
    """When the same name is defined at host and group level, host wins."""
    assert node1.get_visible_var("shared_var") == "from_host"


def test_get_visible_var_renders_template_to_native_type(node1):
    """visible_vars resolves the Jinja2 template (unlike get_host_var, which
    returns it raw). The rendered scalar is 42; ansible-core renders it as a
    native int on >=2.19 and as the string "42" on older versions, so accept
    either as long as it is genuinely rendered (not the raw template)."""
    rendered = node1.get_visible_var("templated_var")
    assert rendered != "{{ base_number * 2 }}"
    assert rendered in (42, "42")
    assert str(rendered) == "42"


def test_get_visible_var_missing_returns_default(node1):
    assert node1.get_visible_var("nope") is None
    assert node1.get_visible_var("nope", "d") == "d"


# ---------------------------------------------------------------------------
# AnsibleHost — host_vars / visible_vars properties
# ---------------------------------------------------------------------------


def test_host_vars_property_contains_only_host_scope(node1):
    hv = node1.host_vars
    assert isinstance(hv, dict)
    assert hv.get("host_only_var") == "from_host"
    # group var must not leak into the host-only view
    assert "group_only_var" not in hv


def test_visible_vars_property_contains_host_and_group(node1):
    vv = node1.visible_vars
    assert isinstance(vv, dict)
    assert vv.get("host_only_var") == "from_host"
    assert vv.get("group_only_var") == "from_group"
    assert vv.get("shared_var") == "from_host"


# ---------------------------------------------------------------------------
# AnsibleHost — extra_vars / update_extra_vars
# ---------------------------------------------------------------------------


def test_constructor_hostvars_populate_extra_vars(inventory):
    host = AnsibleHost(
        inventory=inventory, pattern="node1", hostvars={"injected": "value"}
    )
    assert host.extra_vars.get("injected") == "value"
    assert host.get_visible_var("injected") == "value"


def test_extra_vars_override_host_var_in_visible_vars(inventory):
    """extra_vars sit at the top of Ansible's precedence, so they win even
    over a host-defined var of the same name."""
    host = AnsibleHost(
        inventory=inventory, pattern="node1", hostvars={"shared_var": "from_extra"}
    )
    assert host.get_visible_var("shared_var") == "from_extra"
    # ...but the raw host-scope value is untouched.
    assert host.get_host_var("shared_var") == "from_host"


def test_update_extra_vars_adds_and_is_visible(node1):
    node1.update_extra_vars({"runtime_var": "set-later"})
    assert node1.extra_vars.get("runtime_var") == "set-later"
    assert node1.get_visible_var("runtime_var") == "set-later"


def test_update_extra_vars_overrides_existing_in_visible_vars(node1):
    node1.update_extra_vars({"group_only_var": "overridden"})
    assert node1.get_visible_var("group_only_var") == "overridden"


# ---------------------------------------------------------------------------
# AnsibleHosts (multi-host, host-keyed API)
# ---------------------------------------------------------------------------


@pytest.fixture
def hosts(inventory):
    return AnsibleHosts(inventory=inventory, pattern="web")


def test_hosts_get_host_var_is_per_host(hosts):
    assert hosts.get_host_var("node1", "host_only_var") == "from_host"
    assert hosts.get_host_var("node2", "host_only_var") == "from_host2"


def test_hosts_get_visible_var_resolves_group_var(hosts):
    assert hosts.get_visible_var("node1", "group_only_var") == "from_group"
    assert hosts.get_visible_var("node2", "group_only_var") == "from_group"


def test_hosts_get_host_var_default(hosts):
    assert hosts.get_host_var("node1", "missing", "d") == "d"


def test_hosts_get_host_var_unknown_host_raises(hosts):
    with pytest.raises(KeyError):
        hosts.get_host_var("ghost", "host_only_var")


def test_hosts_get_visible_var_unknown_host_raises(hosts):
    with pytest.raises(KeyError):
        hosts.get_visible_var("ghost", "group_only_var")


def test_hosts_extra_vars_reflect_constructor_hostvars(inventory):
    hosts = AnsibleHosts(
        inventory=inventory, pattern="web", hostvars={"injected": "v"}
    )
    assert hosts.extra_vars.get("injected") == "v"


# ---------------------------------------------------------------------------
# AnsibleLocalhost
# ---------------------------------------------------------------------------


def test_localhost_extra_vars_via_constructor():
    host = AnsibleLocalhost(hostvars={"injected": "value"})
    assert host.extra_vars.get("injected") == "value"
    assert host.get_visible_var("injected") == "value"


def test_localhost_update_extra_vars_is_visible():
    host = AnsibleLocalhost()
    host.update_extra_vars({"runtime_var": "set-later"})
    assert host.extra_vars.get("runtime_var") == "set-later"
    assert host.get_visible_var("runtime_var") == "set-later"


def test_localhost_var_getters_default_and_property_types():
    host = AnsibleLocalhost()
    assert host.get_host_var("missing", "d") == "d"
    assert host.get_visible_var("missing", "d") == "d"
    assert isinstance(host.host_vars, dict)
    assert isinstance(host.visible_vars, dict)


# ---------------------------------------------------------------------------
# Regression: extra_vars must NOT leak across independent host instances
# ---------------------------------------------------------------------------
#
# Ansible's ``load_extra_vars`` memoizes its dict on a function attribute and
# ``VariableManager`` stores it by reference. Mutating ``vm.extra_vars`` in
# place therefore used to pollute every VariableManager created afterwards in
# the same process. Each host must own its own extra_vars; one host's mutation
# must never be visible to another.


def test_update_extra_vars_does_not_leak_to_other_instance():
    first = AnsibleLocalhost()
    first.update_extra_vars({"leaky_var": "from_first"})

    second = AnsibleLocalhost()
    assert "leaky_var" not in second.extra_vars
    assert second.get_visible_var("leaky_var") is None


def test_constructor_hostvars_do_not_leak_to_other_instance(inventory):
    first = AnsibleHost(
        inventory=inventory, pattern="node1", hostvars={"leaky_ctor_var": "from_first"}
    )
    assert first.get_visible_var("leaky_ctor_var") == "from_first"

    second = AnsibleHost(inventory=inventory, pattern="node1")
    assert "leaky_ctor_var" not in second.extra_vars
    assert second.get_visible_var("leaky_ctor_var") is None
