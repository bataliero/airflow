# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import inspect
import itertools
import re
import textwrap
import warnings
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from functools import cached_property, update_wrapper
from typing import TYPE_CHECKING, Any, ClassVar, Generic, ParamSpec, Protocol, TypeVar, cast, overload

import attr
import typing_extensions

from airflow.sdk import timezone
from airflow.sdk.bases.operator import (
    BaseOperator,
    coerce_resources,
    coerce_timedelta,
    get_merged_defaults,
    parse_retries,
)
from airflow.sdk.definitions._internal.contextmanager import DagContext, TaskGroupContext
from airflow.sdk.definitions._internal.decorators import remove_task_decorator
from airflow.sdk.definitions._internal.expandinput import (
    EXPAND_INPUT_EMPTY,
    DictOfListsExpandInput,
    ListOfDictsExpandInput,
    is_mappable,
)
from airflow.sdk.definitions._internal.types import NOTSET
from airflow.sdk.definitions.asset import Asset
from airflow.sdk.definitions.mappedoperator import MappedOperator, ensure_xcomarg_return_value
from airflow.sdk.definitions.xcom_arg import XComArg
from airflow.utils.context import KNOWN_CONTEXT_KEYS
from airflow.utils.helpers import prevent_duplicates
from airflow.utils.trigger_rule import TriggerRule

if TYPE_CHECKING:
    from airflow.sdk.definitions._internal.expandinput import (
        ExpandInput,
        OperatorExpandArgument,
        OperatorExpandKwargsArgument,
    )
    from airflow.sdk.definitions.context import Context
    from airflow.sdk.definitions.dag import DAG
    from airflow.sdk.definitions.mappedoperator import ValidationSource
    from airflow.sdk.definitions.taskgroup import TaskGroup


class ExpandableFactory(Protocol):
    """
    Protocol providing inspection against wrapped function.

    This is used in ``validate_expand_kwargs`` and implemented by function
    decorators like ``@task`` and ``@task_group``.

    :meta private:
    """

    function: Callable

    @cached_property
    def function_signature(self) -> inspect.Signature:
        return inspect.signature(self.function)

    @cached_property
    def _mappable_function_argument_names(self) -> set[str]:
        """Arguments that can be mapped against."""
        return set(self.function_signature.parameters)

    def _validate_arg_names(self, func: ValidationSource, kwargs: dict[str, Any]) -> None:
        """Ensure that all arguments passed to operator-mapping functions are accounted for."""
        parameters = self.function_signature.parameters
        if any(v.kind == inspect.Parameter.VAR_KEYWORD for v in parameters.values()):
            return
        kwargs_left = kwargs.copy()
        for arg_name in self._mappable_function_argument_names:
            value = kwargs_left.pop(arg_name, NOTSET)
            if func == "expand" and value is not NOTSET and not is_mappable(value):
                tname = type(value).__name__
                raise ValueError(
                    f"expand() got an unexpected type {tname!r} for keyword argument {arg_name!r}"
                )
        if len(kwargs_left) == 1:
            raise TypeError(f"{func}() got an unexpected keyword argument {next(iter(kwargs_left))!r}")
        if kwargs_left:
            names = ", ".join(repr(n) for n in kwargs_left)
            raise TypeError(f"{func}() got unexpected keyword arguments {names}")


def get_unique_task_id(
    task_id: str,
    dag: DAG | None = None,
    task_group: TaskGroup | None = None,
) -> str:
    """
    Generate unique task id given a DAG (or if run in a DAG context).

    IDs are generated by appending a unique number to the end of
    the original task id.

    Example:
      task_id
      task_id__1
      task_id__2
      ...
      task_id__20
    """
    dag = dag or DagContext.get_current()
    if not dag:
        return task_id

    # We need to check if we are in the context of TaskGroup as the task_id may
    # already be altered
    task_group = task_group or TaskGroupContext.get_current(dag)
    tg_task_id = task_group.child_id(task_id) if task_group else task_id

    if tg_task_id not in dag.task_ids:
        return task_id

    def _find_id_suffixes(dag: DAG) -> Iterator[int]:
        prefix = re.split(r"__\d+$", tg_task_id)[0]
        for task_id in dag.task_ids:
            match = re.match(rf"^{prefix}__(\d+)$", task_id)
            if match:
                yield int(match.group(1))
        yield 0  # Default if there's no matching task ID.

    core = re.split(r"__\d+$", task_id)[0]
    return f"{core}__{max(_find_id_suffixes(dag)) + 1}"


class DecoratedOperator(BaseOperator):
    """
    Wraps a Python callable and captures args/kwargs when called for execution.

    :param python_callable: A reference to an object that is callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function (templated)
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable (templated)
    :param multiple_outputs: If set to True, the decorated function's return value will be unrolled to
        multiple XCom values. Dict will unroll to XCom values with its keys as XCom keys. Defaults to False.
    :param kwargs_to_upstream: For certain operators, we might need to upstream certain arguments
        that would otherwise be absorbed by the DecoratedOperator (for example python_callable for the
        PythonOperator). This gives a user the option to upstream kwargs as needed.
    """

    template_fields: Sequence[str] = ("op_args", "op_kwargs")
    template_fields_renderers = {"op_args": "py", "op_kwargs": "py"}

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects (e.g protobuf).
    shallow_copy_attrs: Sequence[str] = ("python_callable",)

    def __init__(
        self,
        *,
        python_callable: Callable,
        task_id: str,
        op_args: Collection[Any] | None = None,
        op_kwargs: Mapping[str, Any] | None = None,
        kwargs_to_upstream: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        if not getattr(self, "_BaseOperator__from_mapped", False):
            # If we are being created from calling unmap(), then don't mangle the task id
            task_id = get_unique_task_id(task_id, kwargs.get("dag"), kwargs.get("task_group"))
        self.python_callable = python_callable
        kwargs_to_upstream = kwargs_to_upstream or {}
        op_args = op_args or []
        op_kwargs = op_kwargs or {}

        # Check the decorated function's signature. We go through the argument
        # list and "fill in" defaults to arguments that are known context keys,
        # since values for those will be provided when the task is run. Since
        # we're not actually running the function, None is good enough here.
        signature = inspect.signature(python_callable)

        # Don't allow context argument defaults other than None to avoid ambiguities.
        faulty_parameters = [
            param.name
            for param in signature.parameters.values()
            if param.name in KNOWN_CONTEXT_KEYS and param.default not in (None, inspect.Parameter.empty)
        ]
        if faulty_parameters:
            message = f"Context key parameter {faulty_parameters[0]} can't have a default other than None"
            raise ValueError(message)

        parameters = [
            param.replace(default=None) if param.name in KNOWN_CONTEXT_KEYS else param
            for param in signature.parameters.values()
        ]
        try:
            signature = signature.replace(parameters=parameters)
        except ValueError as err:
            message = textwrap.dedent(
                f"""
                The function signature broke while assigning defaults to context key parameters.

                The decorator is replacing the signature
                > {python_callable.__name__}({", ".join(str(param) for param in signature.parameters.values())})

                with
                > {python_callable.__name__}({", ".join(str(param) for param in parameters)})

                which isn't valid: {err}
                """
            )
            raise ValueError(message) from err

        # Check that arguments can be binded. There's a slight difference when
        # we do validation for task-mapping: Since there's no guarantee we can
        # receive enough arguments at parse time, we use bind_partial to simply
        # check all the arguments we know are valid. Whether these are enough
        # can only be known at execution time, when unmapping happens, and this
        # is called without the _airflow_mapped_validation_only flag.
        if kwargs.get("_airflow_mapped_validation_only"):
            signature.bind_partial(*op_args, **op_kwargs)
        else:
            signature.bind(*op_args, **op_kwargs)

        self.op_args = op_args
        self.op_kwargs = op_kwargs
        super().__init__(task_id=task_id, **kwargs_to_upstream, **kwargs)

    def execute(self, context: Context):
        # todo make this more generic (move to prepare_lineage) so it deals with non taskflow operators
        #  as well
        for arg in itertools.chain(self.op_args, self.op_kwargs.values()):
            if isinstance(arg, Asset):
                self.inlets.append(arg)
        return_value = super().execute(context)
        return self._handle_output(return_value=return_value)

    def _handle_output(self, return_value: Any):
        """
        Handle logic for whether a decorator needs to push a single return value or multiple return values.

        It sets outlets if any assets are found in the returned value(s)

        :param return_value:
        :param context:
        :param xcom_push:
        """
        if isinstance(return_value, Asset):
            self.outlets.append(return_value)
        if isinstance(return_value, list):
            for item in return_value:
                if isinstance(item, Asset):
                    self.outlets.append(item)
        return return_value

    def _hook_apply_defaults(self, *args, **kwargs):
        if "python_callable" not in kwargs:
            return args, kwargs

        python_callable = kwargs["python_callable"]
        default_args = kwargs.get("default_args") or {}
        op_kwargs = kwargs.get("op_kwargs") or {}
        f_sig = inspect.signature(python_callable)
        for arg in f_sig.parameters:
            if arg not in op_kwargs and arg in default_args:
                op_kwargs[arg] = default_args[arg]
        kwargs["op_kwargs"] = op_kwargs
        return args, kwargs

    def get_python_source(self):
        raw_source = inspect.getsource(self.python_callable)
        res = textwrap.dedent(raw_source)
        res = remove_task_decorator(res, self.custom_operator_name)
        return res


FParams = ParamSpec("FParams")

FReturn = TypeVar("FReturn")

OperatorSubclass = TypeVar("OperatorSubclass", bound="BaseOperator")


@attr.define(slots=False)
class _TaskDecorator(ExpandableFactory, Generic[FParams, FReturn, OperatorSubclass]):
    """
    Helper class for providing dynamic task mapping to decorated functions.

    ``task_decorator_factory`` returns an instance of this, instead of just a plain wrapped function.

    :meta private:
    """

    function: Callable[FParams, FReturn] = attr.ib(validator=attr.validators.is_callable())
    operator_class: type[OperatorSubclass]
    multiple_outputs: bool = attr.ib()
    kwargs: dict[str, Any] = attr.ib(factory=dict)

    decorator_name: str = attr.ib(repr=False, default="task")

    _airflow_is_task_decorator: ClassVar[bool] = True
    is_setup: bool = False
    is_teardown: bool = False
    on_failure_fail_dagrun: bool = False

    # This is set in __attrs_post_init__ by update_wrapper. Provided here for type hints.
    __wrapped__: Callable[FParams, FReturn] = attr.ib(init=False)

    @multiple_outputs.default
    def _infer_multiple_outputs(self):
        if "return" not in self.function.__annotations__:
            # No return type annotation, nothing to infer
            return False

        try:
            # We only care about the return annotation, not anything about the parameters
            def fake(): ...

            fake.__annotations__ = {"return": self.function.__annotations__["return"]}

            return_type = typing_extensions.get_type_hints(fake, self.function.__globals__).get("return", Any)
        except NameError as e:
            warnings.warn(
                f"Cannot infer multiple_outputs for TaskFlow function {self.function.__name__!r} with forward"
                f" type references that are not imported. (Error was {e})",
                stacklevel=4,
            )
            return False
        except TypeError:  # Can't evaluate return type.
            return False
        ttype = getattr(return_type, "__origin__", return_type)
        return isinstance(ttype, type) and issubclass(ttype, Mapping)

    def __attrs_post_init__(self):
        if "self" in self.function_signature.parameters:
            raise TypeError(f"@{self.decorator_name} does not support methods")
        self.kwargs.setdefault("task_id", self.function.__name__)
        update_wrapper(self, self.function)

    def __call__(self, *args: FParams.args, **kwargs: FParams.kwargs) -> XComArg:
        if self.is_teardown:
            if "trigger_rule" in self.kwargs:
                raise ValueError("Trigger rule not configurable for teardown tasks.")
            self.kwargs.update(trigger_rule=TriggerRule.ALL_DONE_SETUP_SUCCESS)
        on_failure_fail_dagrun = self.kwargs.pop("on_failure_fail_dagrun", self.on_failure_fail_dagrun)
        op = self.operator_class(
            python_callable=self.function,
            op_args=args,
            op_kwargs=kwargs,
            multiple_outputs=self.multiple_outputs,
            **self.kwargs,
        )
        op.is_setup = self.is_setup
        op.is_teardown = self.is_teardown
        op.on_failure_fail_dagrun = on_failure_fail_dagrun
        op_doc_attrs = [op.doc, op.doc_json, op.doc_md, op.doc_rst, op.doc_yaml]
        # Set the task's doc_md to the function's docstring if it exists and no other doc* args are set.
        if self.function.__doc__ and not any(op_doc_attrs):
            op.doc_md = self.function.__doc__
        return XComArg(op)

    def _validate_arg_names(self, func: ValidationSource, kwargs: dict[str, Any]):
        # Ensure that context variables are not shadowed.
        context_keys_being_mapped = KNOWN_CONTEXT_KEYS.intersection(kwargs)
        if len(context_keys_being_mapped) == 1:
            (name,) = context_keys_being_mapped
            raise ValueError(f"cannot call {func}() on task context variable {name!r}")
        if context_keys_being_mapped:
            names = ", ".join(repr(n) for n in context_keys_being_mapped)
            raise ValueError(f"cannot call {func}() on task context variables {names}")

        super()._validate_arg_names(func, kwargs)

    def expand(self, **map_kwargs: OperatorExpandArgument) -> XComArg:
        if self.kwargs.get("trigger_rule") == TriggerRule.ALWAYS and any(
            [isinstance(expanded, XComArg) for expanded in map_kwargs.values()]
        ):
            raise ValueError(
                "Task-generated mapping within a task using 'expand' is not allowed with trigger rule 'always'."
            )
        if not map_kwargs:
            raise TypeError("no arguments to expand against")
        self._validate_arg_names("expand", map_kwargs)
        prevent_duplicates(self.kwargs, map_kwargs, fail_reason="mapping already partial")
        # Since the input is already checked at parse time, we can set strict
        # to False to skip the checks on execution.
        if self.is_teardown:
            if "trigger_rule" in self.kwargs:
                raise ValueError("Trigger rule not configurable for teardown tasks.")
            self.kwargs.update(trigger_rule=TriggerRule.ALL_DONE_SETUP_SUCCESS)
        return self._expand(DictOfListsExpandInput(map_kwargs), strict=False)

    def expand_kwargs(self, kwargs: OperatorExpandKwargsArgument, *, strict: bool = True) -> XComArg:
        if (
            self.kwargs.get("trigger_rule") == TriggerRule.ALWAYS
            and not isinstance(kwargs, XComArg)
            and any(
                [
                    isinstance(v, XComArg)
                    for kwarg in kwargs
                    if not isinstance(kwarg, XComArg)
                    for v in kwarg.values()
                ]
            )
        ):
            raise ValueError(
                "Task-generated mapping within a task using 'expand_kwargs' is not allowed with trigger rule 'always'."
            )
        if isinstance(kwargs, Sequence):
            for item in kwargs:
                if not isinstance(item, (XComArg, Mapping)):
                    raise TypeError(f"expected XComArg or list[dict], not {type(kwargs).__name__}")
        elif not isinstance(kwargs, XComArg):
            raise TypeError(f"expected XComArg or list[dict], not {type(kwargs).__name__}")
        return self._expand(ListOfDictsExpandInput(kwargs), strict=strict)

    def _expand(self, expand_input: ExpandInput, *, strict: bool) -> XComArg:
        ensure_xcomarg_return_value(expand_input.value)

        task_kwargs = self.kwargs.copy()
        dag = task_kwargs.pop("dag", None) or DagContext.get_current()
        task_group = task_kwargs.pop("task_group", None) or TaskGroupContext.get_current(dag)

        default_args, partial_params = get_merged_defaults(
            dag=dag,
            task_group=task_group,
            task_params=task_kwargs.pop("params", None),
            task_default_args=task_kwargs.pop("default_args", None),
        )
        partial_kwargs: dict[str, Any] = {
            "is_setup": self.is_setup,
            "is_teardown": self.is_teardown,
            "on_failure_fail_dagrun": self.on_failure_fail_dagrun,
        }
        base_signature = inspect.signature(BaseOperator)
        ignore = {
            "default_args",  # This is target we are working on now.
            "kwargs",  # A common name for a keyword argument.
            "do_xcom_push",  # In the same boat as `multiple_outputs`
            "multiple_outputs",  # We will use `self.multiple_outputs` instead.
            "params",  # Already handled above `partial_params`.
            "task_concurrency",  # Deprecated(replaced by `max_active_tis_per_dag`).
        }
        partial_keys = set(base_signature.parameters) - ignore
        partial_kwargs.update({key: value for key, value in default_args.items() if key in partial_keys})
        partial_kwargs.update(task_kwargs)

        task_id = get_unique_task_id(partial_kwargs.pop("task_id"), dag, task_group)
        if task_group:
            task_id = task_group.child_id(task_id)

        # Logic here should be kept in sync with BaseOperatorMeta.partial().
        if partial_kwargs.get("wait_for_downstream"):
            partial_kwargs["depends_on_past"] = True
        start_date = timezone.convert_to_utc(partial_kwargs.pop("start_date", None))
        end_date = timezone.convert_to_utc(partial_kwargs.pop("end_date", None))
        if "pool_slots" in partial_kwargs:
            if partial_kwargs["pool_slots"] < 1:
                dag_str = ""
                if dag:
                    dag_str = f" in dag {dag.dag_id}"
                raise ValueError(f"pool slots for {task_id}{dag_str} cannot be less than 1")

        for fld, convert in (
            ("retries", parse_retries),
            ("retry_delay", coerce_timedelta),
            ("max_retry_delay", coerce_timedelta),
            ("resources", coerce_resources),
        ):
            if (v := partial_kwargs.get(fld, NOTSET)) is not NOTSET:
                partial_kwargs[fld] = convert(v)

        partial_kwargs.setdefault("executor_config", {})
        partial_kwargs.setdefault("op_args", [])
        partial_kwargs.setdefault("op_kwargs", {})

        # Mypy does not work well with a subclassed attrs class :(
        _MappedOperator = cast("Any", DecoratedMappedOperator)

        try:
            operator_name = self.operator_class.custom_operator_name  # type: ignore
        except AttributeError:
            operator_name = self.operator_class.__name__

        operator = _MappedOperator(
            operator_class=self.operator_class,
            expand_input=EXPAND_INPUT_EMPTY,  # Don't use this; mapped values go to op_kwargs_expand_input.
            partial_kwargs=partial_kwargs,
            task_id=task_id,
            params=partial_params,
            operator_extra_links=self.operator_class.operator_extra_links,
            template_ext=self.operator_class.template_ext,
            template_fields=self.operator_class.template_fields,
            template_fields_renderers=self.operator_class.template_fields_renderers,
            ui_color=self.operator_class.ui_color,
            ui_fgcolor=self.operator_class.ui_fgcolor,
            is_empty=False,
            is_sensor=self.operator_class._is_sensor,
            can_skip_downstream=self.operator_class._can_skip_downstream,
            task_module=self.operator_class.__module__,
            task_type=self.operator_class.__name__,
            operator_name=operator_name,
            dag=dag,
            task_group=task_group,
            start_date=start_date,
            end_date=end_date,
            multiple_outputs=self.multiple_outputs,
            python_callable=self.function,
            op_kwargs_expand_input=expand_input,
            disallow_kwargs_override=strict,
            # Different from classic operators, kwargs passed to a taskflow
            # task's expand() contribute to the op_kwargs operator argument, not
            # the operator arguments themselves, and should expand against it.
            expand_input_attr="op_kwargs_expand_input",
            start_trigger_args=self.operator_class.start_trigger_args,
            start_from_trigger=self.operator_class.start_from_trigger,
        )
        return XComArg(operator=operator)

    def partial(self, **kwargs: Any) -> _TaskDecorator[FParams, FReturn, OperatorSubclass]:
        self._validate_arg_names("partial", kwargs)
        old_kwargs = self.kwargs.get("op_kwargs", {})
        prevent_duplicates(old_kwargs, kwargs, fail_reason="duplicate partial")
        kwargs.update(old_kwargs)
        return attr.evolve(self, kwargs={**self.kwargs, "op_kwargs": kwargs})

    def override(self, **kwargs: Any) -> _TaskDecorator[FParams, FReturn, OperatorSubclass]:
        result = attr.evolve(self, kwargs={**self.kwargs, **kwargs})
        setattr(result, "is_setup", self.is_setup)
        setattr(result, "is_teardown", self.is_teardown)
        setattr(result, "on_failure_fail_dagrun", self.on_failure_fail_dagrun)
        return result


@attr.define(kw_only=True, repr=False)
class DecoratedMappedOperator(MappedOperator):
    """MappedOperator implementation for @task-decorated task function."""

    multiple_outputs: bool
    python_callable: Callable

    # We can't save these in expand_input because op_kwargs need to be present
    # in partial_kwargs, and MappedOperator prevents duplication.
    op_kwargs_expand_input: ExpandInput

    def __hash__(self):
        return id(self)

    def __attrs_post_init__(self):
        # The magic super() doesn't work here, so we use the explicit form.
        # Not using super(..., self) to work around pyupgrade bug.
        super(DecoratedMappedOperator, DecoratedMappedOperator).__attrs_post_init__(self)
        XComArg.apply_upstream_relationship(self, self.op_kwargs_expand_input.value)

    def _expand_mapped_kwargs(self, context: Mapping[str, Any]) -> tuple[Mapping[str, Any], set[int]]:
        # We only use op_kwargs_expand_input so this must always be empty.
        if self.expand_input is not EXPAND_INPUT_EMPTY:
            raise AssertionError(f"unexpected expand_input: {self.expand_input}")
        op_kwargs, resolved_oids = super()._expand_mapped_kwargs(context)
        return {"op_kwargs": op_kwargs}, resolved_oids

    def _get_unmap_kwargs(self, mapped_kwargs: Mapping[str, Any], *, strict: bool) -> dict[str, Any]:
        partial_op_kwargs = self.partial_kwargs["op_kwargs"]
        mapped_op_kwargs = mapped_kwargs["op_kwargs"]

        if strict:
            prevent_duplicates(partial_op_kwargs, mapped_op_kwargs, fail_reason="mapping already partial")

        kwargs = {
            "multiple_outputs": self.multiple_outputs,
            "python_callable": self.python_callable,
            "op_kwargs": {**partial_op_kwargs, **mapped_op_kwargs},
        }
        return super()._get_unmap_kwargs(kwargs, strict=False)


class Task(Protocol, Generic[FParams, FReturn]):
    """
    Declaration of a @task-decorated callable for type-checking.

    An instance of this type inherits the call signature of the decorated
    function wrapped in it (not *exactly* since it actually returns an XComArg,
    but there's no way to express that right now), and provides two additional
    methods for task-mapping.

    This type is implemented by ``_TaskDecorator`` at runtime.
    """

    __call__: Callable[FParams, XComArg]

    function: Callable[FParams, FReturn]

    @property
    def __wrapped__(self) -> Callable[FParams, FReturn]: ...

    def partial(self, **kwargs: Any) -> Task[FParams, FReturn]: ...

    def expand(self, **kwargs: OperatorExpandArgument) -> XComArg: ...

    def expand_kwargs(self, kwargs: OperatorExpandKwargsArgument, *, strict: bool = True) -> XComArg: ...

    def override(self, **kwargs: Any) -> Task[FParams, FReturn]: ...


class TaskDecorator(Protocol):
    """Type declaration for ``task_decorator_factory`` return type."""

    @overload
    def __call__(  # type: ignore[misc]
        self,
        python_callable: Callable[FParams, FReturn],
    ) -> Task[FParams, FReturn]:
        """For the "bare decorator" ``@task`` case."""

    @overload
    def __call__(
        self,
        *,
        multiple_outputs: bool | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[FParams, FReturn]], Task[FParams, FReturn]]:
        """For the decorator factory ``@task()`` case."""

    def override(self, **kwargs: Any) -> Task[FParams, FReturn]: ...


def task_decorator_factory(
    python_callable: Callable | None = None,
    *,
    multiple_outputs: bool | None = None,
    decorated_operator_class: type[BaseOperator],
    **kwargs,
) -> TaskDecorator:
    """
    Generate a wrapper that wraps a function into an Airflow operator.

    Can be reused in a single DAG.

    :param python_callable: Function to decorate.
    :param multiple_outputs: If set to True, the decorated function's return
        value will be unrolled to multiple XCom values. Dict will unroll to XCom
        values with its keys as XCom keys. If set to False (default), only at
        most one XCom value is pushed.
    :param decorated_operator_class: The operator that executes the logic needed
        to run the python function in the correct environment.

    Other kwargs are directly forwarded to the underlying operator class when
    it's instantiated.
    """
    if multiple_outputs is None:
        multiple_outputs = cast("bool", attr.NOTHING)
    if python_callable:
        decorator = _TaskDecorator(
            function=python_callable,
            multiple_outputs=multiple_outputs,
            operator_class=decorated_operator_class,
            kwargs=kwargs,
        )
        return cast("TaskDecorator", decorator)
    if python_callable is not None:
        raise TypeError("No args allowed while using @task, use kwargs instead")

    def decorator_factory(python_callable):
        return _TaskDecorator(
            function=python_callable,
            multiple_outputs=multiple_outputs,
            operator_class=decorated_operator_class,
            kwargs=kwargs,
        )

    return cast("TaskDecorator", decorator_factory)
