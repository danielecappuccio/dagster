from collections import defaultdict
from typing import (
    Callable,
    ContextManager,
    DefaultDict,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
    cast,
)

from typing_extensions import TypeAlias

import dagster._check as check
from dagster._core.definitions import GraphDefinition, JobDefinition, Node, NodeHandle
from dagster._core.definitions.dependency import GraphNode, OpNode
from dagster._core.definitions.events import (
    AssetMaterialization,
    ExpectationResult,
)
from dagster._core.definitions.utils import DEFAULT_OUTPUT
from dagster._core.errors import DagsterInvariantViolationError
from dagster._core.events import (
    DagsterEvent,
    DagsterEventType,
    StepExpectationResultData,
    StepMaterializationData,
)
from dagster._core.execution.context.compute import StepExecutionContext
from dagster._core.execution.plan.inputs import StepInputData
from dagster._core.execution.plan.objects import StepFailureData
from dagster._core.execution.plan.outputs import StepOutputData, StepOutputHandle
from dagster._core.execution.plan.step import ExecutionStep, StepKind
from dagster._core.execution.plan.utils import build_resources_for_manager

ReconstructContextFn: TypeAlias = Callable[[], ContextManager[StepExecutionContext]]


def _construct_events_by_step_key(
    event_list: Sequence[DagsterEvent],
) -> Mapping[str, Sequence[DagsterEvent]]:
    events_by_step_key: DefaultDict[str, List[DagsterEvent]] = defaultdict(list)
    for event in event_list:
        if event.step_key is not None:
            events_by_step_key[event.step_key].append(event)
    return dict(events_by_step_key)


class GraphExecutionResult:
    def __init__(
        self,
        container: GraphDefinition,
        event_list: Sequence[DagsterEvent],
        reconstruct_context: ReconstructContextFn,
        job_def: JobDefinition,
        handle: Optional[NodeHandle] = None,
        output_capture: Optional[Dict[StepOutputHandle, object]] = None,
    ):
        self.container = check.inst_param(container, "container", GraphDefinition)
        self.event_list = check.sequence_param(event_list, "step_event_list", of_type=DagsterEvent)
        self.reconstruct_context = check.callable_param(
            reconstruct_context,
            "reconstruct_context",
        )
        self.job_def = check.inst_param(job_def, "job_def", JobDefinition)
        self.handle = check.opt_inst_param(handle, "handle", NodeHandle)
        self.output_capture = check.opt_dict_param(
            output_capture, "output_capture", key_type=StepOutputHandle
        )
        self._events_by_step_key = _construct_events_by_step_key(event_list)

    @property
    def success(self) -> bool:
        """bool: Whether all steps in the execution were successful."""
        return all([not event.is_failure for event in self.event_list])

    @property
    def step_event_list(self) -> Sequence[DagsterEvent]:
        """List[DagsterEvent] The full list of events generated by steps in the execution.

        Excludes events generated by the pipeline lifecycle, e.g., ``PIPELINE_START``.
        """
        return [event for event in self.event_list if event.is_step_event]

    @property
    def events_by_step_key(self) -> Mapping[str, Sequence[DagsterEvent]]:
        return self._events_by_step_key

    def result_for_node(
        self, name: str
    ) -> Union["CompositeSolidExecutionResult", "OpExecutionResult"]:
        """Get the result of a top level solid.

        Args:
            name (str): The name of the top-level solid or aliased solid for which to retrieve the
                result.

        Returns:
            Union[CompositeSolidExecutionResult, SolidExecutionResult]: The result of the solid
            execution within the pipeline.
        """
        if not self.container.has_node_named(name):
            raise DagsterInvariantViolationError(
                "Tried to get result for solid '{name}' in '{container}'. No such top level "
                "solid.".format(name=name, container=self.container.name)
            )

        return self.result_for_handle(NodeHandle(name, None))

    def output_for_node(self, handle_str: str, output_name: str = DEFAULT_OUTPUT) -> object:
        """Get the output of a solid by its solid handle string and output name.

        Args:
            handle_str (str): The string handle for the solid.
            output_name (str): Optional. The name of the output, default to DEFAULT_OUTPUT.

        Returns:
            The output value for the handle and output_name.
        """
        check.str_param(handle_str, "handle_str")
        check.str_param(output_name, "output_name")
        return self.result_for_handle(NodeHandle.from_string(handle_str)).output_value(output_name)

    @property
    def node_result_list(
        self,
    ) -> Sequence[Union["CompositeSolidExecutionResult", "OpExecutionResult"]]:
        """List[Union[CompositeSolidExecutionResult, SolidExecutionResult]]: The results for each
        top level solid.
        """
        return [self.result_for_node(node.name) for node in self.container.nodes]

    def _result_for_handle(
        self, node: Node, handle: NodeHandle
    ) -> Union["CompositeSolidExecutionResult", "OpExecutionResult"]:
        if not node:
            raise DagsterInvariantViolationError(f"Can not find solid handle {handle.to_string()}.")

        events_by_kind: DefaultDict[StepKind, List[DagsterEvent]] = defaultdict(list)

        if isinstance(node, GraphNode):
            events: List[DagsterEvent] = []
            for event in self.event_list:
                if event.is_step_event:
                    event_node_handle = check.not_none(event.node_handle)
                    if event_node_handle.is_or_descends_from(handle.with_ancestor(self.handle)):
                        events_by_kind[event.step_kind].append(event)
                        events.append(event)

            return CompositeSolidExecutionResult(
                node,
                events,
                events_by_kind,
                self.reconstruct_context,
                self.job_def,
                handle=handle.with_ancestor(self.handle),
                output_capture=self.output_capture,
            )
        elif isinstance(node, OpNode):
            for event in self.event_list:
                if event.is_step_event:
                    event_node_handle = check.not_none(event.node_handle)
                    if event_node_handle.is_or_descends_from(handle.with_ancestor(self.handle)):
                        events_by_kind[event.step_kind].append(event)

            return OpExecutionResult(
                node,
                events_by_kind,
                self.reconstruct_context,
                self.job_def,
                output_capture=self.output_capture,
            )
        else:
            check.failed("Unexpected node type.")

    def result_for_handle(
        self, handle: Union[str, NodeHandle]
    ) -> Union["CompositeSolidExecutionResult", "OpExecutionResult"]:
        """Get the result of a solid by its solid handle.

        This allows indexing into top-level solids to retrieve the results of children of
        composite solids.

        Args:
            handle (Union[str,NodeHandle]): The handle for the solid.

        Returns:
            Union[CompositeSolidExecutionResult, SolidExecutionResult]: The result of the given
            solid.
        """
        if isinstance(handle, str):
            handle = NodeHandle.from_string(handle)
        else:
            check.inst_param(handle, "handle", NodeHandle)

        node = self.container.get_node(handle)

        return self._result_for_handle(node, handle)


class PipelineExecutionResult(GraphExecutionResult):
    """The result of executing a pipeline.

    Returned by :py:func:`execute_pipeline`. Users should not instantiate this class directly.
    """

    def __init__(
        self,
        job_def: JobDefinition,
        run_id: str,
        event_list: Sequence[DagsterEvent],
        reconstruct_context: ReconstructContextFn,
        output_capture: Optional[Dict[StepOutputHandle, object]] = None,
    ):
        self.run_id = check.str_param(run_id, "run_id")
        check.inst_param(job_def, "job_def", JobDefinition)

        super(PipelineExecutionResult, self).__init__(
            container=job_def.graph,
            event_list=event_list,
            reconstruct_context=reconstruct_context,
            job_def=job_def,
            output_capture=output_capture,
        )


class CompositeSolidExecutionResult(GraphExecutionResult):
    """Execution result for a composite solid in a pipeline.

    Users should not instantiate this class directly.
    """

    def __init__(
        self,
        node: GraphNode,
        event_list: Sequence[DagsterEvent],
        step_events_by_kind: Mapping[StepKind, Sequence[DagsterEvent]],
        reconstruct_context: ReconstructContextFn,
        job_def: JobDefinition,
        handle: Optional[NodeHandle] = None,
        output_capture: Optional[Dict[StepOutputHandle, object]] = None,
    ):
        check.inst_param(node, "node", GraphNode)
        self.node = node
        self.step_events_by_kind = check.dict_param(
            step_events_by_kind, "step_events_by_kind", key_type=StepKind, value_type=list
        )
        self.output_capture = check.opt_dict_param(
            output_capture, "output_capture", key_type=StepOutputHandle
        )
        super(CompositeSolidExecutionResult, self).__init__(
            container=node.definition,
            event_list=event_list,
            reconstruct_context=reconstruct_context,
            job_def=job_def,
            handle=handle,
            output_capture=output_capture,
        )

    def output_values_for_solid(self, name: str) -> Optional[Mapping[str, object]]:
        check.str_param(name, "name")
        return self.result_for_node(name).output_values

    def output_values_for_handle(self, handle_str: str) -> Optional[Mapping[str, object]]:
        check.str_param(handle_str, "handle_str")

        return self.result_for_handle(handle_str).output_values

    def output_value_for_solid(self, name: str, output_name: str = DEFAULT_OUTPUT) -> object:
        check.str_param(name, "name")
        check.str_param(output_name, "output_name")

        return self.result_for_node(name).output_value(output_name)

    def output_value_for_handle(self, handle_str: str, output_name: str = DEFAULT_OUTPUT) -> object:
        check.str_param(handle_str, "handle_str")
        check.str_param(output_name, "output_name")

        return self.result_for_handle(handle_str).output_value(output_name)

    @property
    def output_values(self) -> Mapping[str, object]:
        values: Dict[str, object] = {}

        for output_name in self.node.definition.output_dict:
            output_mapping = self.node.definition.get_output_mapping(output_name)

            inner_solid_values = self._result_for_handle(
                self.node.definition.node_named(output_mapping.maps_from.node_name),
                NodeHandle(output_mapping.maps_from.node_name, None),
            ).output_values

            if inner_solid_values is not None:  # may be None if inner solid was skipped
                if output_mapping.maps_from.output_name in inner_solid_values:
                    values[output_name] = inner_solid_values[output_mapping.maps_from.output_name]

        return values

    def output_value(self, output_name: str = DEFAULT_OUTPUT) -> object:
        check.str_param(output_name, "output_name")

        if not self.node.definition.has_output(output_name):
            raise DagsterInvariantViolationError(
                "Output '{output_name}' not defined in graph '{solid}': "
                "{outputs_clause}. If you were expecting this output to be present, you may "
                "be missing an output_mapping from an inner solid to its enclosing graph.".format(
                    output_name=output_name,
                    solid=self.node.name,
                    outputs_clause="found outputs {output_names}".format(
                        output_names=str(list(self.node.definition.output_dict.keys()))
                    )
                    if self.node.definition.output_dict
                    else "no output mappings were defined",
                )
            )

        output_mapping = self.node.definition.get_output_mapping(output_name)

        return self._result_for_handle(
            self.node.definition.node_named(output_mapping.maps_from.node_name),
            NodeHandle(output_mapping.maps_from.node_name, None),
        ).output_value(output_mapping.maps_from.output_name)


class OpExecutionResult:
    """Execution result for a leaf solid in a pipeline.

    Users should not instantiate this class.
    """

    def __init__(
        self,
        node: OpNode,
        step_events_by_kind: Mapping[StepKind, Sequence[DagsterEvent]],
        reconstruct_context: ReconstructContextFn,
        job_def: JobDefinition,
        output_capture: Optional[Dict[StepOutputHandle, object]] = None,
    ):
        check.inst_param(node, "node", OpNode)
        self.node = node
        self.step_events_by_kind = check.dict_param(
            step_events_by_kind, "step_events_by_kind", key_type=StepKind, value_type=list
        )
        self.reconstruct_context = check.callable_param(
            reconstruct_context,
            "reconstruct_context",
        )
        self.output_capture = check.opt_dict_param(output_capture, "output_capture")
        self.job_def = check.inst_param(job_def, "job_def", JobDefinition)

    @property
    def compute_input_event_dict(self) -> Mapping[str, DagsterEvent]:
        """Dict[str, DagsterEvent]: All events of type ``STEP_INPUT``, keyed by input name."""
        return {
            cast(StepInputData, se.event_specific_data).input_name: se
            for se in self.input_events_during_compute
        }

    @property
    def input_events_during_compute(self) -> Sequence[DagsterEvent]:
        """List[DagsterEvent]: All events of type ``STEP_INPUT``."""
        return self._compute_steps_of_type(DagsterEventType.STEP_INPUT)

    def get_output_event_for_compute(self, output_name: str = "result") -> DagsterEvent:
        """The ``STEP_OUTPUT`` event for the given output name.

        Throws if not present.

        Args:
            output_name (Optional[str]): The name of the output. (default: 'result')

        Returns:
            DagsterEvent: The corresponding event.
        """
        events = self.get_output_events_for_compute(output_name)
        check.invariant(
            len(events) == 1, "Multiple output events returned, use get_output_events_for_compute"
        )
        return events[0]

    @property
    def compute_output_events_dict(self) -> Mapping[str, Sequence[DagsterEvent]]:
        """Dict[str, List[DagsterEvent]]: All events of type ``STEP_OUTPUT``, keyed by output name.
        """
        results: DefaultDict[str, List[DagsterEvent]] = defaultdict(list)
        for se in self.output_events_during_compute:
            results[se.step_output_data.output_name].append(se)

        return dict(results)

    def get_output_events_for_compute(self, output_name: str = "result") -> Sequence[DagsterEvent]:
        """The ``STEP_OUTPUT`` event for the given output name.

        Throws if not present.

        Args:
            output_name (Optional[str]): The name of the output. (default: 'result')

        Returns:
            List[DagsterEvent]: The corresponding events.
        """
        return self.compute_output_events_dict[output_name]

    @property
    def output_events_during_compute(self) -> Sequence[DagsterEvent]:
        """List[DagsterEvent]: All events of type ``STEP_OUTPUT``."""
        return self._compute_steps_of_type(DagsterEventType.STEP_OUTPUT)

    @property
    def compute_step_events(self) -> Sequence[DagsterEvent]:
        """List[DagsterEvent]: All events generated by execution of the solid compute function."""
        return self.step_events_by_kind.get(StepKind.COMPUTE, [])

    @property
    def step_events(self) -> Sequence[DagsterEvent]:
        return self.compute_step_events

    @property
    def materializations_during_compute(
        self,
    ) -> Sequence[AssetMaterialization]:
        """List[AssetMaterialization]: All materializations yielded by the solid."""
        return [
            cast(StepMaterializationData, mat_event.event_specific_data).materialization
            for mat_event in self.materialization_events_during_compute
        ]

    @property
    def materialization_events_during_compute(self) -> Sequence[DagsterEvent]:
        """List[DagsterEvent]: All events of type ``ASSET_MATERIALIZATION``."""
        return self._compute_steps_of_type(DagsterEventType.ASSET_MATERIALIZATION)

    @property
    def expectation_events_during_compute(self):
        """List[DagsterEvent]: All events of type ``STEP_EXPECTATION_RESULT``."""
        return self._compute_steps_of_type(DagsterEventType.STEP_EXPECTATION_RESULT)

    def _compute_steps_of_type(self, dagster_event_type: DagsterEventType):
        return list(
            filter(lambda se: se.event_type == dagster_event_type, self.compute_step_events)
        )

    @property
    def expectation_results_during_compute(self) -> Sequence[ExpectationResult]:
        """List[ExpectationResult]: All expectation results yielded by the solid."""
        return [
            cast(StepExpectationResultData, expt_event.event_specific_data).expectation_result
            for expt_event in self.expectation_events_during_compute
        ]

    def get_step_success_event(self) -> DagsterEvent:
        """DagsterEvent: The ``STEP_SUCCESS`` event, throws if not present."""
        for step_event in self.compute_step_events:
            if step_event.event_type == DagsterEventType.STEP_SUCCESS:
                return step_event

        check.failed(f"Step success not found for solid {self.node.name}")

    @property
    def compute_step_failure_event(self) -> DagsterEvent:
        """DagsterEvent: The ``STEP_FAILURE`` event, throws if it did not fail."""
        if self.success:
            raise DagsterInvariantViolationError(
                "Cannot call compute_step_failure_event if successful"
            )

        step_failure_events = self._compute_steps_of_type(DagsterEventType.STEP_FAILURE)
        check.invariant(len(step_failure_events) == 1)
        return step_failure_events[0]

    @property
    def success(self) -> bool:
        """bool: Whether solid execution was successful."""
        any_success = False
        for step_event in self.compute_step_events:
            if step_event.event_type == DagsterEventType.STEP_FAILURE:
                return False
            if step_event.event_type == DagsterEventType.STEP_SUCCESS:
                any_success = True

        return any_success

    @property
    def skipped(self) -> bool:
        """bool: Whether solid execution was skipped."""
        return all(
            [
                step_event.event_type == DagsterEventType.STEP_SKIPPED
                for step_event in self.compute_step_events
            ]
        )

    @property
    def output_values(self) -> Optional[Mapping[str, object]]:
        """Union[None, Dict[str, Union[Any, Dict[str, Any]]]: The computed output values.

        Returns ``None`` if execution did not succeed.

        Returns a dictionary where keys are output names and the values are:
            * the output values in the normal case
            * a dictionary from mapping key to corresponding value in the mapped case

        Note that accessing this property will reconstruct the pipeline context (including, e.g.,
        resources) to retrieve materialized output values.
        """
        if not self.success or not self.compute_step_events:
            return None

        results: Dict[str, object] = {}
        with self.reconstruct_context() as context:
            for compute_step_event in self.compute_step_events:
                if compute_step_event.is_successful_output:
                    output = compute_step_event.step_output_data
                    step_key = check.not_none(compute_step_event.step_key)
                    step = cast(ExecutionStep, context.execution_plan.get_step_by_key(step_key))
                    value = self._get_value(
                        cast(StepExecutionContext, context.for_step(step)), output
                    )
                    check.invariant(
                        not (output.mapping_key and step.get_mapping_key()),
                        "Not set up to handle mapped outputs downstream of mapped steps",
                    )
                    mapping_key = output.mapping_key or step.get_mapping_key()
                    if mapping_key:
                        inner_dict = results.setdefault(output.output_name, {})
                        inner_dict[mapping_key] = value  # type: ignore
                    else:
                        results[output.output_name] = value

        return results

    def output_value(self, output_name: str = DEFAULT_OUTPUT) -> object:
        """Get a computed output value.

        Note that calling this method will reconstruct the pipeline context (including, e.g.,
        resources) to retrieve materialized output values.

        Args:
            output_name(str): The output name for which to retrieve the value. (default: 'result')

        Returns:
            Union[None, Any, Dict[str, Any]]: ``None`` if execution did not succeed, the output value
                in the normal case, and a dict of mapping keys to values in the mapped case.
        """
        check.str_param(output_name, "output_name")

        if not self.node.definition.has_output(output_name):
            raise DagsterInvariantViolationError(
                "Output '{output_name}' not defined in solid '{solid}': found outputs "
                "{output_names}".format(
                    output_name=output_name,
                    solid=self.node.name,
                    output_names=str(list(self.node.definition.output_dict.keys())),
                )
            )

        if not self.success:
            return None

        with self.reconstruct_context() as context:
            found = False
            result: object = None
            for compute_step_event in self.compute_step_events:
                if (
                    compute_step_event.is_successful_output
                    and compute_step_event.step_output_data.output_name == output_name
                ):
                    found = True
                    output = compute_step_event.step_output_data
                    step_key = check.not_none(compute_step_event.step_key)
                    step = cast(ExecutionStep, context.execution_plan.get_step_by_key(step_key))
                    value = self._get_value(
                        cast(StepExecutionContext, context.for_step(step)), output
                    )
                    check.invariant(
                        not (output.mapping_key and step.get_mapping_key()),
                        "Not set up to handle mapped outputs downstream of mapped steps",
                    )
                    mapping_key = output.mapping_key or step.get_mapping_key()
                    if mapping_key:
                        if result is None:
                            result = {mapping_key: value}
                        else:
                            result[mapping_key] = value  # type: ignore
                    else:
                        result = value

            if found:
                return result

            raise DagsterInvariantViolationError(
                f"Did not find result {output_name} in node {self.node.name} execution result"
            )

    def _get_value(self, context: StepExecutionContext, step_output_data: StepOutputData) -> object:
        step_output_handle = step_output_data.step_output_handle
        # output capture dictionary will only have values in the in process case, but will not have
        # values from steps launched via step launcher.
        if self.output_capture and step_output_handle in self.output_capture:
            return self.output_capture[step_output_handle]
        manager = context.get_io_manager(step_output_handle)
        manager_key = context.execution_plan.get_manager_key(step_output_handle, self.job_def)
        res = manager.load_input(
            context.for_input_manager(
                name=None,  # type: ignore
                config=None,
                metadata=None,
                dagster_type=self.node.output_def_named(step_output_data.output_name).dagster_type,
                source_handle=step_output_handle,
                resource_config=context.resolved_run_config.resources[manager_key].config,
                resources=build_resources_for_manager(manager_key, context),
            )
        )
        return res

    @property
    def failure_data(self) -> Optional[StepFailureData]:
        """Union[None, StepFailureData]: Any data corresponding to this step's failure, if it
        failed.
        """
        for step_event in self.compute_step_events:
            if step_event.event_type == DagsterEventType.STEP_FAILURE:
                return step_event.step_failure_data
        return None

    @property
    def retry_attempts(self) -> int:
        """Number of times this step retried."""
        count = 0
        for step_event in self.compute_step_events:
            if step_event.event_type == DagsterEventType.STEP_RESTARTED:
                count += 1
        return count
