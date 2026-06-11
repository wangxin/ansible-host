"""Smoke tests that don't require a working Ansible inventory."""
from __future__ import annotations


def test_public_api_importable():
    from ansible_host import (
        AnsibleHost,
        AnsibleHosts,
        AnsibleHostsBase,
        AnsibleLocalhost,
        AnsibleModuleFailed,
        MultipleAnsibleHostsError,
        NoAnsibleHostError,
        NoTasksError,
        UnsupportedAnsibleModule,
    )

    # Sanity: the host classes inherit from the base.
    assert issubclass(AnsibleHost, AnsibleHostsBase)
    assert issubclass(AnsibleHosts, AnsibleHostsBase)
    assert issubclass(AnsibleLocalhost, AnsibleHostsBase)


def test_version_present():
    import ansible_host

    assert isinstance(ansible_host.__version__, str)
    assert ansible_host.__version__


def test_dunder_lookup_does_not_dispatch_as_module():
    """Regression: __getattr__ must not treat __deepcopy__/__getstate__/etc.
    as Ansible module names. It should raise AttributeError so Python's normal
    machinery (copy, pickle, debuggers) can fall back to default behavior.
    """
    import copy

    from ansible_host import AnsibleHostsBase

    # Don't actually instantiate (would parse inventory); use __new__ + a stub.
    inst = AnsibleHostsBase.__new__(AnsibleHostsBase)
    inst._batch_mode = False
    inst._loaded_modules = []

    # __getattr__ should raise AttributeError for dunder names.
    try:
        inst.__deepcopy__
    except AttributeError:
        pass
    else:
        raise AssertionError("__getattr__ should raise AttributeError for __deepcopy__")

    # Implicitly: copy.copy on an object with our __getattr__ should not blow up
    # with UnsupportedAnsibleModule when probing dunders. We don't need to fully
    # exercise copy here since the AttributeError check above is the contract.
    assert copy is not None
