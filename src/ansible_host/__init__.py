"""ansible-host: a thin Python wrapper over Ansible's executor.

Exposes typed ``AnsibleHost`` / ``AnsibleHosts`` / ``AnsibleLocalhost`` classes that
let you run Ansible modules from Python without going through ``pytest-ansible`` or
``ansible-runner``. Use it when you want structured results, real forking, batch
execution, and a Python-native API over Ansible's ``TaskQueueManager``.

Public API:

    from ansible_host import AnsibleHost, AnsibleHosts, AnsibleLocalhost

See the README for usage and compatibility notes.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

import ansible
from ansible import constants as C
from ansible import context
from ansible.errors import AnsibleError
from ansible.executor.task_queue_manager import TaskQueueManager
from ansible.inventory.manager import InventoryManager
from ansible.module_utils.common.collections import ImmutableDict
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.play import Play
from ansible.plugins.callback import CallbackBase
from ansible.plugins.loader import init_plugin_loader, module_loader
from ansible.utils.display import Display
from ansible.vars.hostvars import HostVars
from ansible.vars.manager import VariableManager

display = Display()
init_plugin_loader()

try:
    __version__ = _pkg_version("ansible-host")
except PackageNotFoundError:
    # Source checkout without installed metadata (e.g., running from a tarball
    # without ``pip install -e .``). Falls back to an inert marker.
    __version__ = "0.0.0+unknown"

# Logger for the ansible-host library. Users can configure verbosity by
# attaching handlers to this logger (logging.getLogger("ansible_host")).
logger = logging.getLogger("ansible_host")


class _JsonResultsCallback(CallbackBase):
    """Internal result collector for AnsibleHostsBase._run.

    Not registered via Ansible's plugin loader: it is instantiated directly
    by _run and attached to the TaskQueueManager's _callback_plugins list.
    The leading underscore marks this as package-private; downstream users
    should rely on the structured results returned by run_module() / .results
    rather than instantiating this class themselves.

    Derived from the json_results callback in sonic-net/sonic-mgmt (Apache 2.0).
    """

    CALLBACK_VERSION = 2.0
    # Deliberately NOT 'stdout' — Ansible's built-in `null` stdout callback
    # handles terminal output (silently); this collector is purely a result sink.
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "ansible_host._json_results"

    TASK_FIELDS = (
        "action",
        "become",
        "become_method",
        "become_user",
        "connection",
        "ignore_errors",
        "ignore_unreachable",
        "register",
        "retries",
        "timeout",
    )

    def __init__(self):
        super().__init__()
        self._results: dict[str, list[dict]] = {}

    def _get_module_name(self, result):
        if ansible.__version__ >= "2.19.0":
            return result.task
        return result.task_name

    def _get_task_fields(self, result):
        return {
            field: result._task_fields[field]
            for field in self.TASK_FIELDS
            if field in result._task_fields
        }

    def _log_res(self, hostname, module_name, res):
        if display.verbosity == 0:
            return
        if display.verbosity == 1:
            log_func = display.v
            brief = json.dumps(
                {"module_name": module_name, "reachable": res["reachable"], "failed": res["failed"]},
                default=str,
            )
            msg = f"[{hostname}] => {brief}"
        elif display.verbosity == 2:
            log_func = display.vv
            msg = f"[{hostname}] => {json.dumps(res, default=str)}"
        elif display.verbosity == 3:
            log_func = display.vvv
            msg = f"[{hostname}] => {json.dumps(res, indent=4, default=str)}"
        else:
            log_func = display.vvvv
            msg = f"[{hostname}] => {json.dumps(res, indent=4, default=str)}"
        log_func(msg)

    def _record(self, result, *, reachable, failed):
        hostname = str(result._host.get_name())
        module_name = self._get_module_name(result)
        self._results.setdefault(hostname, [])
        res = dict(hostname=hostname, reachable=reachable, failed=failed)
        # deepcopy preserves non-serializable values (e.g. Task instances in
        # _task_fields on ansible-core 2.19+); the prior json round-trip lost
        # or crashed on those.
        res.update(copy.deepcopy(result._result))
        if "invocation" in res and isinstance(res["invocation"], dict):
            res["invocation"]["module_name"] = module_name
        res["_task_fields"] = self._get_task_fields(result)
        self._log_res(hostname, module_name, res)
        self._results[hostname].append(res)

    def v2_runner_on_ok(self, result):
        self._record(result, reachable=True, failed=False)

    def v2_runner_on_failed(self, result, *args, **kwargs):
        self._record(result, reachable=True, failed=True)

    def v2_runner_on_unreachable(self, result):
        self._record(result, reachable=False, failed=True)

    @property
    def results(self) -> dict[str, list[dict]]:
        return self._results


def _to_native_type(value: Any) -> Any:
    """Convert Ansible types (AnsibleUnicode, AnsibleUnsafeText, etc.) to native Python types.

    Args:
        value: Value to convert

    Returns:
        Native Python type (str, list, dict, etc.)
    """
    # Check if value has the Ansible unicode/text types
    if hasattr(value, '__class__') and value.__class__.__name__ in ('AnsibleUnicode', 'AnsibleUnsafeText'):
        return str(value)
    elif isinstance(value, dict):
        return {k: _to_native_type(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_to_native_type(item) for item in value]
    else:
        return value


class UnsupportedAnsibleModule(AnsibleError):
    pass


class NoAnsibleHostError(AnsibleError):
    pass


class MultipleAnsibleHostsError(AnsibleError):
    pass


class NoTasksError(AnsibleError):
    pass


class AnsibleModuleFailed(AnsibleError):
    pass


class AnsibleHostsBase:

    def __init__(
        self,
        inventory: str | list[str],
        pattern: str,
        hostvars: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None
    ) -> None:

        hostvars = hostvars or {}
        options = options or {}

        self.inventory = inventory
        self.pattern = pattern
        self._extra_hostvars = hostvars

        if pattern != 'localhost':
            inventory_files = inventory if isinstance(inventory, list) else [inventory]
            for inv_file in inventory_files:
                if not os.path.exists(inv_file):
                    raise AnsibleError(f"Inventory file does not exist: {inv_file}")

        self.loader = DataLoader()
        self.im = InventoryManager(loader=self.loader, sources=self.inventory)

        # Ansible inventory hosts: list of <class 'ansible.inventory.host.Host'>
        self.ans_inv_hosts = self.im.get_hosts(self.pattern)
        self.hostnames = [host.name for host in self.ans_inv_hosts]
        self.hosts_count = len(self.hostnames)
        self.ips = [host.get_vars().get("ansible_host", None) for host in self.ans_inv_hosts]
        self.v4ips = self.ips
        self.v6ips = [host.get_vars().get("ansible_hostv6", None) for host in self.ans_inv_hosts]

        self.vm = VariableManager(loader=self.loader, inventory=self.im)

        # Use C.XXXX, so that defaults are consistent with ansible.cfg, can be overridden by env vars
        self.options = {
            "forks": C.DEFAULT_FORKS,
            "connection": C.DEFAULT_TRANSPORT,
            "timeout": C.DEFAULT_TIMEOUT,
            "task_timeout": C.TASK_TIMEOUT,
            "become": C.DEFAULT_BECOME,
            "become_method": C.DEFAULT_BECOME_METHOD
        }
        if options:
            self.options.update(options)

        # Trigger ansible to load and render host variables in case host variables are defined as Jinja2 templates.
        # After this operation, self.vm._hostvars will be populated with content
        # self.vm._hostvars["example_hostname"] will return all variables visible by "example_hostname"
        # The best part is that if the variable is a template, it is automatically rendered with correct data type
        HostVars(inventory=self.im, variable_manager=self.vm, loader=self.loader)

        if hostvars:
            self.vm.extra_vars.update(hostvars)

        self._loaded_modules: list[dict] = []
        self._batch_mode: bool = False
        self._batch_results: dict = {}

    @staticmethod
    def _get_caller_info(stack_depth: int = 2) -> tuple[str, int]:
        """Get filename and line number of the caller.

        Args:
            stack_depth: How many levels up the stack to look (default 2)
                        Higher values go further up the call stack

        Returns:
            tuple: (filename, line_number) of the caller
        """
        frame = inspect.currentframe()
        try:
            # Go up the stack to find the actual caller
            for _ in range(stack_depth):
                frame = frame.f_back
                if frame is None:
                    return "unknown", 0

            frameinfo = inspect.getframeinfo(frame)
            return frameinfo.filename, frameinfo.lineno
        finally:
            del frame  # Avoid reference cycles

    @staticmethod
    def _validate_module_name(module_name: str) -> None:
        # Check if 'module_name' is a valid Ansible module
        _module = module_loader.find_plugin_with_context(module_name)

        if not _module.resolved:
            searched_paths = module_loader.print_paths()
            raise UnsupportedAnsibleModule(
                f"\n"
                f"    Ansible module '{module_name}' is not supported or could not be found.\n"
                f'    Searched paths: {searched_paths}\n'
                f'    Please ensure that ANSIBLE_LIBRARY is properly configured.\n'
                f'    Ref: https://docs.ansible.com/ansible/latest/reference_appendices/'
                f'config.html#envvar-ANSIBLE_LIBRARY'
            )

    @staticmethod
    def build_task(
        module_name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        task_directives: dict | None = None
    ) -> dict:

        args = args or []
        kwargs = kwargs or {}
        task_directives = task_directives or {}

        # Validate module name first
        AnsibleHostsBase._validate_module_name(module_name)

        kwargs = copy.deepcopy(kwargs)  # Copy to avoid argument passed by reference issue
        if args:
            kwargs["_raw_params"] = " ".join(args)

        # Support the "module_ignore_errors" kwarg added in the legacy class for backward compatibility
        _module_ignore_errors = False
        if 'module_ignore_errors' in kwargs:
            _module_ignore_errors = kwargs.pop('module_ignore_errors')

        task_data = {
            "action": {
                "module": module_name,
                "args": kwargs
            },
        }
        # Intentional `== True` (not truthy): only the literal bool True opts in;
        # other truthy values like 1 or "yes" must not silently enable ignore_errors.
        if _module_ignore_errors == True:  # noqa: E712
            # It could be overwritten by the 'ignore_errors' in task_directives if both are provided.
            # This is to encourage the using of formal 'ignore_errors' attribute.
            task_data['ignore_errors'] = True

        if task_directives:
            task_data.update(task_directives)

        return task_data

    def _check_failed_results(self, results):
        failed_results = []
        if isinstance(self, AnsibleHost) or isinstance(self, AnsibleLocalhost):
            # Single host
            if isinstance(results, dict):
                # Single task
                if results.get('failed', False):
                    if not results.get('_task_fields', {}).get('ignore_errors', False):
                        failed_results.append(results)
            elif isinstance(results, list):
                # Multiple tasks
                for res in results:
                    if res.get('failed', False):
                        if not res.get('_task_fields', {}).get('ignore_errors', False):
                            failed_results.append(res)
        elif isinstance(self, AnsibleHosts):
            # Multiple hosts
            if isinstance(results, dict):
                # Multiple hosts, multiple tasks
                for hostname in results:
                    host_results = results[hostname]
                    if isinstance(host_results, dict):
                        # Single task
                        if host_results.get('failed', False):
                            if not host_results.get('_task_fields', {}).get('ignore_errors', False):
                                failed_results.append(host_results)
                    elif isinstance(host_results, list):
                        # Multiple tasks
                        for res in host_results:
                            if res.get('failed', False):
                                if not res.get('_task_fields', {}).get('ignore_errors', False):
                                    failed_results.append(res)

        if failed_results:
            raise AnsibleModuleFailed(
                f"Ansible module failed. If failure is expected, use `task_directives={{'ignore_errors': True}}` "
                f"to avoid raising an exception. Details: "
                f"{json.dumps(failed_results, indent=4, default=str)}"
            )

    def _run(
        self,
        tasks: list[dict] | None = None,
        options: dict[str, Any] | None = None,
        gather_facts: bool = False
    ) -> dict | list[dict]:
        tasks = tasks or []
        options = options or {}

        # Validate tasks list
        if not tasks or len(tasks) == 0:
            raise NoTasksError("No tasks provided to execute")

        tqm = None
        try:
            _options = copy.deepcopy(self.options)
            _options.update(options)

            # According to the above logic, `verbosity` from `self._run` will overwrite the one from `self.__init__`.
            log_verbosity = _options.pop('verbosity', None)
            if log_verbosity is None:
                log_verbosity = int(os.environ.get('ANSIBLE_HOST_VERBOSITY', 2))

            # NOTE: ``ansible.context`` holds a process-global CLI context. This call
            # mutates it for the entire process. Two ``AnsibleHosts`` instances running
            # concurrently in different threads with different options will race on
            # this state. The library is currently designed for sequential use; for
            # concurrent execution, prefer Ansible's own forking via ``forks=`` over
            # threading multiple instances.
            context._init_global_context(ImmutableDict(**_options))

            # Get caller information for logging
            caller_file, caller_line = self._get_caller_info(stack_depth=3)
            caller_file_base = os.path.basename(caller_file)
            if logger.isEnabledFor(logging.DEBUG) and log_verbosity > 0:
                for task in tasks:
                    # To honor the ansible's no_log attribute
                    # Ref: https://docs.ansible.com/ansible/latest/reference_appendices/
                    #      logging.html#protecting-sensitive-data-with-no-log
                    no_log = task.get('no_log', False)

                    module_name = task['action']['module']
                    log_prefix = f'{caller_file_base}:{caller_line} >> {self.hostnames} =>'
                    if log_verbosity == 1:
                        if no_log:
                            log_details = '[no_log]'
                        else:
                            log_details = f'{module_name}'
                    elif log_verbosity >= 2:
                        if no_log:
                            log_details = '[no_log]'
                        else:
                            args = task['action'].get('args', {}).get('_raw_params', '').split(' ')
                            kwargs = {k: v for k, v in task['action'].get('args', {}).items() if k != '_raw_params'}
                            task_directives = {k: v for k, v in task.items() if k != 'action'}
                            log_details = (
                                f'{module_name}, args={json.dumps(args, default=str)}, '
                                f'kwargs={json.dumps(kwargs, default=str)}, '
                                f'task_directives={json.dumps(task_directives, default=str)}'
                            )
                    logger.debug(f'{log_prefix} {log_details}')

            # The ansible logging level is not determined by the `verbosity` value in options.
            # Set ansible logging level according to 'verbosity' configuration in ansible.cfg
            # or by ANSIBLE_VERBOSITY env var.
            # Ref: https://docs.ansible.com/projects/ansible/latest/reference_appendices/config.html#default-verbosity
            _original_display_verbosity = display.verbosity
            display.verbosity = C.DEFAULT_VERBOSITY

            play = Play().load(
                {
                    "hosts": self.pattern,
                    "gather_facts": gather_facts,
                    "become_method": _options.get('become_method', 'sudo'),
                    "connection": _options.get('connection', 'smart'),
                    "ignore_errors": _options.get('ignore_errors', False),
                    "tasks": tasks
                },
                variable_manager=self.vm,
                loader=self.loader
            )
            # TQM requires a registered stdout callback name to satisfy
            # load_callbacks(); we pass 'minimal' (one of Ansible's built-ins)
            # then evict it from _callback_plugins before run() so nothing
            # prints to stdout. Our own _JsonResultsCallback is attached as
            # the sole receiver of task events.
            if ansible.__version__ >= '2.19.0':
                tqm = TaskQueueManager(
                    inventory=self.im,
                    variable_manager=self.vm,
                    loader=self.loader,
                    passwords={},
                    stdout_callback_name='minimal',
                    run_tree=False,
                    forks=self.options.get("forks")
                )
            else:
                tqm = TaskQueueManager(
                    inventory=self.im,
                    variable_manager=self.vm,
                    loader=self.loader,
                    passwords={},
                    stdout_callback='minimal',
                    run_tree=False,
                    forks=self.options.get("forks")
                )
            tqm.load_callbacks()

            # Remove the built-in stdout callback (suppresses terminal output)
            # and attach our explicit result collector in its place.
            tqm._callback_plugins[:] = [
                cb for cb in tqm._callback_plugins
                if getattr(cb, 'CALLBACK_TYPE', None) != 'stdout'
            ]
            results_collector = _JsonResultsCallback()
            # _init_callback_methods populates _implemented_callback_methods,
            # which TQM.send_callback gates dispatch on. The plugin loader calls
            # this implicitly when loading callbacks via the loader; since we
            # construct our collector directly, we have to invoke it ourselves.
            if hasattr(results_collector, '_init_callback_methods'):
                results_collector._init_callback_methods()
            tqm._callback_plugins.append(results_collector)

            tqm.run(play)

            results = results_collector.results

            # results is a dict: {hostname: [task_result_dict, ...], ...}
            # It makes sense to return this format of results for multiple hosts and multiple tasks
            # However, for single host or single task scenarios, we can simplify the results structure
            # Simplify results based on number of tasks
            if len(tasks) == 1:  # This means single task, but could still be multiple hosts
                # Single task, results is a list of dict with single item. Convert to single dict per host
                for hostname in results:
                    if isinstance(results[hostname], list) and len(results[hostname]) == 1:
                        results[hostname] = results[hostname][0]

            # For single host scenarios, return just the single host's result without hostname key
            if isinstance(self, AnsibleHost) or isinstance(self, AnsibleLocalhost):
                results = results[self.hostname]

            _tqm_stats = {
                'processed': tqm._stats.processed,
                'failures': tqm._stats.failures,
                'ok': tqm._stats.ok,
                'unreachable': tqm._stats.dark,
                'changed': tqm._stats.changed,
                'skipped': tqm._stats.skipped,
                'rescued': tqm._stats.rescued,
                'ignored': tqm._stats.ignored,
            }

            if logger.isEnabledFor(logging.DEBUG) and log_verbosity > 0:
                if log_verbosity == 1:
                    logger.debug(f'{caller_file}:{caller_line} >> {self.hostnames} => done')
                elif log_verbosity == 2:
                    logger.debug(
                        f'{caller_file}:{caller_line} >> {self.hostnames} => '
                        f'{json.dumps(results, default=str)}'
                    )
                elif log_verbosity >= 3:
                    logger.debug(
                        f'{caller_file}:{caller_line} >> {self.hostnames} => '
                        f'{json.dumps(results, indent=4, default=str)}'
                    )
                    if log_verbosity >= 4:
                        logger.debug(
                            f'{caller_file}:{caller_line} >> TaskQueueManager Stats: '
                            f'{json.dumps(_tqm_stats, indent=4, default=str)}'
                        )

        finally:
            if tqm:
                tqm.cleanup()
            self.loader.cleanup_all_tmp_files()
            display.verbosity = _original_display_verbosity

        self._check_failed_results(results)

        return results

    def run_module(
        self,
        module_name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        task_directives: dict | None = None,
        options: dict | None = None,
        gather_facts: bool = False
    ) -> dict | list[dict] | None:

        args = args or []
        kwargs = kwargs or {}
        task_directives = task_directives or {}
        options = options or {}

        task = self.build_task(
            module_name=module_name,
            args=args,
            kwargs=kwargs,
            task_directives=task_directives
        )

        if self._batch_mode:
            # In batch mode the task is queued; execution is deferred to context-manager exit.
            self._loaded_modules.append(task)
            return None

        try:
            results = self._run(tasks=[task], options=options, gather_facts=gather_facts)
        except Exception as e:
            if isinstance(args, str):
                raise type(e)(
                    f"{str(e)}\n"
                    f"Note: 'args' parameter must be a list, not a string. "
                    f"You passed args='{args}' (string). Use args=['{args}'] instead."
                ) from e
            else:
                raise

        return results

    def load_module(
        self,
        module_name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        task_directives: dict | None = None
    ) -> None:
        task = self.build_task(
            module_name=module_name,
            args=args or [],
            kwargs=kwargs or {},
            task_directives=task_directives or {}
        )
        self._loaded_modules.append(task)

    def run_loaded_modules(
        self,
        options: dict[str, Any] | None = None,
        gather_facts: bool = False
    ) -> dict | list[dict]:
        options = options or {}
        try:
            if len(self._loaded_modules) == 0:
                return {}
            results = self._run(tasks=self._loaded_modules, options=options, gather_facts=gather_facts)
        finally:
            self._loaded_modules = []

        return results

    def __getattr__(self, name: str) -> callable:
        # Don't intercept dunder attribute lookups: Python's machinery (copy, pickle,
        # repr, debuggers, etc.) probes for things like __deepcopy__, __getstate__,
        # __wrapped__. Treating those as Ansible module names produces confusing
        # UnsupportedAnsibleModule errors. Let normal AttributeError be raised so
        # callers can fall back to default behavior.
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )

        def _run_ansible_module(
            *args,
            task_directives: dict | None = None,
            options: dict | None = None,
            gather_facts: bool = False,
            **kwargs
        ) -> dict | list[dict] | None:
            task = self.build_task(
                module_name=name,
                args=list(args),
                kwargs=kwargs,
                task_directives=task_directives or {}
            )
            if self._batch_mode:
                self._loaded_modules.append(task)
                # `options` and `gather_facts` are ignored in batch mode.
                # Module is not executed immediately, so no results to return.
                # Loaded modules will be executed when context manager exits.
                return None
            return self._run(tasks=[task], options=options or {}, gather_facts=gather_facts)

        return _run_ansible_module

    def __enter__(self) -> AnsibleHostsBase:
        self._batch_mode = True
        self._loaded_modules = []
        self._batch_results = {}
        logger.debug("===== Entering AnsibleHostsBase context manager for batch module execution. =====")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self._batch_results = self._run(tasks=self._loaded_modules)
        finally:
            self._batch_mode = False
            self._loaded_modules = []
            logger.debug(
                "===== Exiting AnsibleHostsBase context manager after batch module execution. "
                "Access results via 'results' property of the instance. ====="
            )

    @property
    def results(self) -> dict:
        '''Returns the results tasks executed in context.

        Clears the stored results after returning them to avoid stale data on subsequent calls.
        '''
        _batch_results = self._batch_results
        self._batch_results = {}
        return _batch_results

    def _get_host_vars_dict(self, hostname: str) -> dict[str, Any]:
        """Get all variables directly defined for a specific host.

        Returns only variables from host_vars/, not group_vars or other sources.

        Args:
            hostname: Name of the host to get variables for

        Returns:
            Dictionary of variables directly defined for this host

        Raises:
            KeyError: If hostname is not in the matched hosts
        """
        if hostname not in self.hostnames:
            raise KeyError(f"Host '{hostname}' not found in matched hosts: {self.hostnames}")

        # Get the inventory host object
        inv_host = None
        for host in self.ans_inv_hosts:
            if host.name == hostname:
                inv_host = host
                break

        # Get host-specific variables (not group vars)
        return inv_host.get_vars() if inv_host else {}

    def get_host_var(self, hostname: str, var_name: str, default: Any = None) -> Any:
        """Get a specific variable directly defined for a host.

        Returns only variables from host_vars/, not group_vars or other sources.

        Args:
            hostname: Name of the host to get variable for
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found

        Raises:
            KeyError: If hostname is not in the matched hosts
        """
        host_vars_dict = self._get_host_vars_dict(hostname)
        value = host_vars_dict.get(var_name, default)
        return _to_native_type(value)

    def _get_visible_vars_dict(self, hostname: str) -> dict[str, Any]:
        """Get all variables visible to a specific host.

        Includes host_vars, group_vars, inventory vars, extra_vars.
        Jinja2 templates are automatically rendered.

        Args:
            hostname: Name of the host to get variables for

        Returns:
            Dictionary of all variables visible to this host (computed/resolved)

        Raises:
            KeyError: If hostname is not in the matched hosts
        """
        if hostname not in self.hostnames:
            raise KeyError(f"Host '{hostname}' not found in matched hosts: {self.hostnames}")

        # Use VariableManager's _hostvars which contains all variables
        # including group vars, inventory vars, and extra vars
        # Templates are automatically rendered
        return dict(self.vm._hostvars[hostname])

    def get_visible_var(self, hostname: str, var_name: str, default: Any = None) -> Any:
        """Get a specific variable visible to a host.

        Includes host_vars, group_vars, inventory vars, extra_vars.
        Jinja2 templates are automatically rendered.

        Args:
            hostname: Name of the host to get variable for
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found

        Raises:
            KeyError: If hostname is not in the matched hosts
        """
        visible_vars_dict = self._get_visible_vars_dict(hostname)
        value = visible_vars_dict.get(var_name, default)
        return _to_native_type(value)

    @property
    def extra_vars(self) -> dict[str, Any]:
        """Get extra variables.

        Returns:
            Dictionary of extra variables
        """
        return self.vm.extra_vars


class AnsibleHosts(AnsibleHostsBase):
    """Subclass for working with multiple Ansible hosts.

    Supports container-like operations:
    - Indexing: hosts[0] or hosts['hostname']
    - Iteration: for host in hosts:
    - Length: len(hosts)
    """

    def __init__(
        self,
        inventory: str | list[str],
        pattern: str,
        hostvars: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None
    ) -> None:
        super().__init__(inventory, pattern, hostvars, options)

        # Validate that at least one host matches the 'pattern'
        if self.hosts_count == 0:
            raise NoAnsibleHostError(
                f"No host '{self.pattern}' in inventory '{self.inventory}'"
            )

    def _make_single_host(self, hostname: str) -> AnsibleHost:
        """Build an ``AnsibleHost`` for ``hostname`` reusing this group's parsed inventory.

        The naive path of constructing ``AnsibleHost(inventory, hostname, ...)`` re-parses
        the inventory and re-renders host variables on every call, which is O(N) per
        access on a group of N hosts. This helper bypasses the re-parse by sharing
        ``self.loader``, ``self.im`` and ``self.vm`` with the new instance.
        """
        host = AnsibleHost.__new__(AnsibleHost)
        # AnsibleHostsBase fields
        host.inventory = self.inventory
        host.pattern = hostname
        host._extra_hostvars = self._extra_hostvars
        host.loader = self.loader
        host.im = self.im
        host.vm = self.vm
        ans_hosts = [h for h in self.ans_inv_hosts if h.name == hostname]
        if not ans_hosts:
            raise NoAnsibleHostError(
                f"Host '{hostname}' is not part of pattern '{self.pattern}'"
            )
        host.ans_inv_hosts = ans_hosts
        host.hostnames = [hostname]
        host.hosts_count = 1
        host.ips = [ans_hosts[0].get_vars().get("ansible_host", None)]
        host.v4ips = host.ips
        host.v6ips = [ans_hosts[0].get_vars().get("ansible_hostv6", None)]
        host.options = dict(self.options)
        host._loaded_modules = []
        host._batch_mode = False
        host._batch_results = {}
        # AnsibleHost-specific singular convenience fields
        host.hostname = hostname
        host.ip = host.ips[0]
        host.v4ip = host.v4ips[0]
        host.v6ip = host.v6ips[0]
        return host

    def __getitem__(self, key: int | str) -> AnsibleHost:
        """Support both integer and string indexing.

        Args:
            key: Integer index (0-based) or hostname string

        Returns:
            AnsibleHost instance for the specified host

        Examples:
            hosts[0]          # First host by integer index
            hosts['vlab-01']  # Host by hostname
        """
        if isinstance(key, int):
            # Integer indexing
            if key < 0 or key >= len(self.hostnames):
                raise IndexError(f"Index {key} out of range for {len(self.hostnames)} hosts")
            hostname = self.hostnames[key]
        elif isinstance(key, str):
            # String indexing by hostname
            if key not in self.hostnames:
                raise KeyError(f"Host '{key}' not found in matched hosts: {self.hostnames}")
            hostname = key
        else:
            raise TypeError(f"Indices must be integers or strings, not {type(key).__name__}")

        return self._make_single_host(hostname)

    def __iter__(self):
        """Support iteration over hosts.

        Yields:
            AnsibleHost instances for each host

        Example:
            for host in hosts:
                print(host.hostname)
        """
        for hostname in self.hostnames:
            yield self._make_single_host(hostname)

    def __len__(self) -> int:
        """Return the number of hosts.

        Returns:
            Number of hosts matched by the 'pattern'

        Example:
            len(hosts)  # Returns count of matched hosts
        """
        return self.hosts_count

    def __str__(self) -> str:
        """Return a user-friendly string representation."""
        return f"AnsibleHosts(pattern='{self.pattern}', hostnames={self.hostnames})"

    def __repr__(self) -> str:
        """Return a detailed string representation for debugging."""
        inv_str = self.inventory if isinstance(self.inventory, str) else f"[{', '.join(self.inventory)}]"
        return f"AnsibleHosts(inventory={inv_str}, pattern='{self.pattern}', hostnames={self.hostnames})"


class AnsibleHost(AnsibleHostsBase):
    """Subclass for working with a single Ansible host."""

    def __init__(
        self,
        inventory: str | list[str],
        pattern: str,
        hostvars: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None
    ) -> None:
        super().__init__(inventory, pattern, hostvars, options)
        # Validate that exactly one host matches the 'pattern'
        if self.hosts_count == 0:
            raise NoAnsibleHostError(
                f"No host '{self.pattern}' in inventory '{self.inventory}'"
            )
        elif self.hosts_count > 1:
            raise MultipleAnsibleHostsError(
                f"Expected exactly one host, but '{self.pattern}' matched {self.hosts_count} hosts "
                f"in inventory '{self.inventory}': {self.hostnames}"
            )

        # Add singular attributes for single host access
        self.hostname = self.hostnames[0]
        self.ip = self.ips[0]
        self.v4ip = self.v4ips[0]
        self.v6ip = self.v6ips[0]

    def get_host_var(self, var_name: str, default: Any = None) -> Any:
        """Get a specific variable directly defined for this host.

        Returns only variables from host_vars/, not group_vars or other sources.

        Args:
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found
        """
        return super().get_host_var(self.hostname, var_name, default)

    def get_visible_var(self, var_name: str, default: Any = None) -> Any:
        """Get a specific variable visible to this host.

        Includes host_vars, group_vars, inventory vars, extra_vars.
        Jinja2 templates are automatically rendered.

        Args:
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found
        """
        return super().get_visible_var(self.hostname, var_name, default)

    def update_extra_vars(self, extra_vars: dict[str, Any]) -> None:
        """Update extra variables for this host.

        Args:
            extra_vars: Dictionary of variables to add/update in extra_vars
        """
        self.vm.extra_vars.update(extra_vars)

    @property
    def host_vars(self) -> dict[str, Any]:
        """Variables directly defined for this host (convenience property).

        Returns:
            Dictionary of variables directly defined for this host
        """
        return super()._get_host_vars_dict(self.hostname)

    @property
    def visible_vars(self) -> dict[str, Any]:
        """All variables visible to this host (convenience property).

        Returns:
            Dictionary of all variables visible to this host (computed/resolved)
        """
        return super()._get_visible_vars_dict(self.hostname)

    def __str__(self) -> str:
        """Return a user-friendly string representation."""
        ip_info = f", ip={self.ip}" if self.ip else ""
        return f"AnsibleHost(hostname='{self.hostname}'{ip_info})"

    def __repr__(self) -> str:
        """Return a detailed string representation for debugging."""
        inv_str = self.inventory if isinstance(self.inventory, str) else f"[{', '.join(self.inventory)}]"
        ip_info = f", ip={self.ip}" if self.ip else ""
        v6_info = f", v6ip={self.v6ip}" if self.v6ip else ""
        return f"AnsibleHost(inventory={inv_str}, hostname='{self.hostname}'{ip_info}{v6_info})"


class AnsibleLocalhost(AnsibleHostsBase):
    """Subclass for working with localhost."""

    def __init__(
        self,
        inventory: str | list[str] | None = None,
        hostvars: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None
    ) -> None:
        hostvars = hostvars or {}
        options = options or {}

        # Set default options for localhost
        localhost_options = {
            "connection": "local"
        }
        localhost_options.update(options)

        # If no inventory provided, use implicit localhost
        if not inventory:
            inventory = "/dev/null"  # Ansible accepts this for implicit localhost

        super().__init__(inventory=inventory, pattern="localhost", hostvars=hostvars, options=localhost_options)

        # Add singular attributes like AnsibleHost
        self.hostname = "localhost"

    def get_host_var(self, var_name: str, default: Any = None) -> Any:
        """Get a specific variable directly defined for localhost.

        Returns only variables from host_vars/, not group_vars or other sources.

        Args:
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found
        """
        return super().get_host_var(self.hostname, var_name, default)

    def get_visible_var(self, var_name: str, default: Any = None) -> Any:
        """Get a specific variable visible to localhost.

        Includes host_vars, group_vars, inventory vars, extra_vars.
        Jinja2 templates are automatically rendered.

        Args:
            var_name: Variable name to retrieve
            default: Default value to return if variable is not found

        Returns:
            Value of the specific variable, or default if not found
        """
        return super().get_visible_var(self.hostname, var_name, default)

    def update_extra_vars(self, extra_vars: dict[str, Any]) -> None:
        """Update extra variables for localhost.

        Args:
            extra_vars: Dictionary of variables to add/update in extra_vars
        """
        self.vm.extra_vars.update(extra_vars)

    @property
    def host_vars(self) -> dict[str, Any]:
        """Variables directly defined for localhost (convenience property).

        Returns:
            Dictionary of variables directly defined for localhost
        """
        return super()._get_host_vars_dict(self.hostname)

    @property
    def visible_vars(self) -> dict[str, Any]:
        """All variables visible to localhost (convenience property).

        Returns:
            Dictionary of all variables visible to localhost (computed/resolved)
        """
        return super()._get_visible_vars_dict(self.hostname)

    def __str__(self) -> str:
        """Return a user-friendly string representation."""
        return "AnsibleLocalhost(hostname='localhost')"

    def __repr__(self) -> str:
        """Return a detailed string representation for debugging."""
        inv_str = self.inventory if isinstance(self.inventory, str) else f"[{', '.join(self.inventory)}]"
        return f"AnsibleLocalhost(inventory={inv_str}, connection='local')"


__all__ = [
    "AnsibleHost",
    "AnsibleHosts",
    "AnsibleHostsBase",
    "AnsibleLocalhost",
    "AnsibleModuleFailed",
    "MultipleAnsibleHostsError",
    "NoAnsibleHostError",
    "NoTasksError",
    "UnsupportedAnsibleModule",
]
