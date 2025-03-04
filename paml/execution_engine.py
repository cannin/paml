from abc import ABC
from typing import List
import uuid
import datetime
import logging
import pandas as pd
import graphviz

import paml
from paml_convert.plate_coordinates import coordinate_rect_to_row_col_pairs, num2row
import uml
import sbol3

from paml_convert.behavior_specialization import BehaviorSpecialization, DefaultBehaviorSpecialization

l = logging.getLogger(__file__)
l.setLevel(logging.ERROR)

class ExecutionEngine(ABC):
    """
    Base class for implementing and recording a PAML executions.
    This class can handle common UML activities and the propagation of tokens, but does not execute primitives.
    It needs to be extended with specific implementations that have that capability.
    """

    def __init__(self,
                 specializations: List[BehaviorSpecialization] = [DefaultBehaviorSpecialization()],
                 use_ordinal_time = False):
        self.exec_counter = 0
        self.variable_counter = 0
        self.specializations = specializations

        # The EE uses a configurable start_time as the reference time.
        # Because the start_time is not always the actual time, then
        # we need to set times relative to the start time using the
        # relative wall clock time.
        # if use_oridinal_time, then use a new int for each time
        self.start_time = None  # The official start_time
        self.wall_clock_start_time = None # The actual now() time
        self.use_ordinal_time = use_ordinal_time # Use int instead of datetime
        self.ordinal_time = None

    def next_id(self):
        next = self.exec_counter
        self.exec_counter += 1
        return next

    def next_variable(self):
        variable = f"var_{self.variable_counter}"
        self.variable_counter += 1
        return variable

    def init_time(self, start_time):
        self.wall_clock_start_time = datetime.datetime.now()
        if self.use_ordinal_time:
            self.ordinal_time = datetime.datetime.strptime("1/1/00 00:00:00", "%d/%m/%y %H:%M:%S")
            self.start_time = self.ordinal_time
        else:
            start_time = start_time if start_time else datetime.datetime.now()
            self.start_time = start_time




    def get_current_time(self, as_string=False):
        if self.use_ordinal_time:
            now = self.ordinal_time
            self.ordinal_time += datetime.timedelta(seconds=1)
            start = self.start_time
        else:
            now = datetime.datetime.now()
            start = self.wall_clock_start_time

        # get the relative time from start
        rel_start =  now - start
        cur_time = self.start_time + rel_start
        return  cur_time if not as_string else str(cur_time)


    def execute(self,
                protocol: paml.Protocol,
                agent: sbol3.Agent,
                parameter_values: List[paml.ParameterValue] = {},
                id: str = uuid.uuid4(),
                start_time: datetime.datetime = None
                ) -> paml.ProtocolExecution:
        """
        Execute the given protocol against the provided parameters

        :param protocol: Protocol to execute
        :param agent: Agent that is executing this protocol
        :param parameter_values:  List of all input parameter values (if any)
        :param id: display_id or URI to be used as the name of this execution; defaults to a UUID display_id
        :param start_time: FIXME

        :return: ProtocolExecution containing a record of the execution
        """

        # Record in the document containing the protocol
        doc = protocol.document

        # First, set up the record for the protocol and parameter values
        ex = paml.ProtocolExecution(id, protocol=protocol)
        doc.add(ex)

        ex.association.append(sbol3.Association(agent=agent, plan=protocol))
        ex.parameter_values = parameter_values

        # Initialize specializations
        for specialization in self.specializations:
            specialization.initialize_protocol(ex)
            specialization.on_begin()

        self.init_time(start_time)
        ex.start_time = self.start_time # TODO: remove str wrapper after sbol_factory #22 fixed

        # Iteratively execute all unblocked activities until no more tokens can progress
        tokens = []  # no tokens to start
        ready = protocol.initiating_nodes()
        while ready:
            for node in ready:
                tokens = self.execute_activity_node(ex, node, tokens)
            ready = self.executable_activity_nodes(protocol, tokens, ex.parameter_values)

        ex.end_time = self.get_current_time()

        # TODO: think about infinite loops and how to abort

        # A Protocol has completed normally if all of its required output parameters have values
        set_parameters = (p.parameter.lookup() for p in ex.parameter_values)
        ex.completed_normally = all(p in set_parameters for p in protocol.get_required_outputs())

        # aggregate consumed material records from all behaviors executed within, mark end time, and return
        ex.aggregate_child_materials()


        # End specializations
        for specialization in self.specializations:
            specialization.on_end()

        return ex

    def executable_activity_nodes(self, protocol: paml.Protocol, tokens: List[paml.ActivityEdgeFlow],
                                  parameter_values: List[paml.ParameterValue])\
            -> List[uml.ActivityNode]:
        """
        Find all of the activity nodes that are ready to be run given the current set of tokens
        Note that this will NOT identify activities with no in-flows: those are only set up as initiating nodes

        :param protocol: paml.Protocol being executed
        :param tokens: set of ActivityEdgeFlow records that have not yet been consumed
        :param parameter_values: FIXME

        :return: List of ActivityNodes that are ready to be run
        """
        candidate_clusters = {}
        for t in tokens:
            target = t.get_target()
            candidate_clusters[target] = candidate_clusters.get(target,[])+[t]
        return [n for n,nt in candidate_clusters.items()
                if self.enabled_activity_node(protocol, n, nt, parameter_values)]

    def enabled_activity_node(self,  protocol: paml.Protocol, node: uml.ActivityNode,
                              tokens: List[paml.ActivityEdgeFlow], parameter_values: List[paml.ParameterValue]):
        """
        Check whether all incoming edges have values defined by a token in tokens and that all value pin values are defined.

        :param protocol: paml.Protocol being executed
        :param node: node to be executed
        :param tokens: current list of pending edge flows
        :param parameter_values: FIXME

        :return: bool if node is enabled
        """
        tokens_present = {node.document.find(t.edge) for t in tokens if t.edge}==protocol.incoming_edges(node)
        if hasattr(node, "inputs"):
            required_inputs = [node.input_pin(i.property_value.name)
                               for i in node.behavior.lookup().get_required_inputs()]
            required_value_pins = {p for p in required_inputs if isinstance(p, uml.ValuePin)}
            required_input_pins = {p for p in required_inputs if not isinstance(p, uml.ValuePin)}
            pins_with_tokens = {t.token_source.lookup().node.lookup() for t in tokens if not t.edge}
            parameter_names = {pv.parameter.lookup().property_value.name for pv in parameter_values}
            pins_with_params = {p for p in required_input_pins if p.name in parameter_names}
            satisfied_pins = set(list(pins_with_params) + list(pins_with_tokens))
            input_pins_satisfied = satisfied_pins == required_input_pins
            value_pins_assigned = all({i.value for i in required_value_pins})
            return tokens_present and input_pins_satisfied and value_pins_assigned
        else:
            return tokens_present




    def execute_activity_node(self, ex : paml.ProtocolExecution, node: uml.ActivityNode,
                              tokens: List[paml.ActivityEdgeFlow]) -> List[paml.ActivityEdgeFlow]:
        """
        Execute a node in an activity, consuming the incoming flows and recording execution and outgoing flows

        :param ex: Current execution record
        :param node: Node to be executed
        :param tokens: Current list of pending edge flows

        :return: Updated list of pending edge flows
        """
        # Extract the relevant set of incoming flow values
        # TODO change to pointer lookup after pySBOL #237
        # inputs are tokens that are either connected to node via an edge, or
        # are input pins connected to the node (implicitly) (i.e., the node owns the pin)
        # and the node identity is a prefix of the pin identity.
        inputs = [t for t in tokens if node == t.get_target()]
#                    if (hasattr(t, "edge") and t.edge and ex.document.find(t.edge).target == node.identity) or \
#                       node.identity in ex.document.find(t.token_source).node ]

        # Create a record for the node execution
        record = None
        new_tokens = []

        # Dispatch execution based on node type, collecting any object flows produced
        # The dispatch methods are the ones that can (or must be) overridden for a particular execution environment
        if isinstance(node, uml.InitialNode):
            if len(inputs) != 0:
                raise ValueError(f'Initial node must have zero inputs, but {node.identity} had {len(inputs)}')
            record = paml.ActivityNodeExecution(node=node, incoming_flows=inputs)
            ex.executions.append(record)
            new_tokens = self.next_tokens(record, ex)
            # put a control token on all outgoing edges

        elif isinstance(node, uml.FlowFinalNode):
            record = paml.ActivityNodeExecution(node=node, incoming_flows=inputs)
            ex.executions.append(record)
            new_tokens = self.next_tokens(record, ex)

        elif isinstance(node, uml.ForkNode):
            if len(inputs) != 1:
                raise ValueError(f'Fork node must have precisely one input, but {node.identity} had {len(inputs)}')
            record = paml.ActivityNodeExecution(node=node, incoming_flows=inputs)
            ex.executions.append(record)
            new_tokens = self.next_tokens(record, ex)

        # elif isinstance(node, uml.JoinNode):
        #     pass
        # elif isinstance(node, uml.MergeNode):
        #     pass
        # elif isinstance(node, uml.DecisionNode):
        #     pass
        elif isinstance(node, uml.ActivityParameterNode):
            record = paml.ActivityNodeExecution(node=node, incoming_flows=inputs)
            ex.executions.append(record)
            if node.parameter.lookup().property_value.direction == uml.PARAMETER_IN:
                new_tokens = self.next_tokens(record, ex)
            else:
                [value] = [i.value.value for i in inputs if isinstance(i.edge.lookup(), uml.ObjectFlow)]
                value = uml.literal(value, reference=True)
                ex.parameter_values += [paml.ParameterValue(parameter=node.parameter.lookup(), value=value)]
        elif isinstance(node, uml.CallBehaviorAction):
            record = paml.CallBehaviorExecution(node=node, incoming_flows=inputs)

            # Get the parameter values from input tokens for input pins
            input_pin_values = {token.token_source.lookup().node.lookup().identity:
                                     uml.literal(token.value, reference=True)
                                for token in inputs if not token.edge}
            # Get Input value pins
            value_pin_values = {pin.identity: pin.value for pin in node.inputs if hasattr(pin, "value")}
            # Convert References
            value_pin_values = {k: (uml.LiteralReference(value=ex.document.find(v.value))
                                    if isinstance(v.value, sbol3.refobj_property.ReferencedURI) or
                                       isinstance(v, uml.LiteralReference)
                                    else  uml.LiteralReference(value=v))
                                for k, v in value_pin_values.items()}
            pin_values = { **input_pin_values, **value_pin_values} # merge the dicts

            parameter_values = [paml.ParameterValue(parameter=node.pin_parameter(pin.name),
                                                    value=pin_values[pin.identity])
                                for pin in node.inputs if pin.identity in pin_values]
            parameter_values.sort(key=lambda x: ex.document.find(x.parameter).index)
            call = paml.BehaviorExecution(f"execute_{self.next_id()}",
                                          parameter_values=parameter_values,
                                          completed_normally=True,
                                          start_time=self.get_current_time(), # TODO: remove str wrapper after sbol_factory #22 fixed
                                          end_time=self.get_current_time(), # TODO: remove str wrapper after sbol_factory #22 fixed
                                          consumed_material=[]) # FIXME handle materials
            record.call = call

            ex.document.add(call)
            ex.executions.append(record)
            new_tokens = self.next_tokens(record, ex)

            ## Add the output values to the call parameter-values
            for token in new_tokens:
                edge = token.edge.lookup()
                if isinstance(edge, uml.ObjectFlow):
                    source = edge.source.lookup()
                    parameter = node.pin_parameter(source.name)
                    parameter_value = uml.literal(token.value, reference=True)
                    pv = paml.ParameterValue(parameter=parameter, value=parameter_value)
                    call.parameter_values += [pv]

        elif isinstance(node, uml.Pin):
            record = paml.ActivityNodeExecution(node=node, incoming_flows=inputs)
            ex.executions.append(record)
            new_tokens = self.next_pin_tokens(record, ex)
        elif isinstance(node, uml.OrderedPropertyValue):
            # node is an output parameter
            out_param = node
            [value] = [i.value for i in inputs] # Assume a single input for params
            param_value = paml.ParameterValue(parameter=out_param,
                                              value=uml.literal(value.value, reference=True))
            ex.parameter_values.append(param_value)
        else:
            raise ValueError(f'Do not know how to execute node {node.identity} of type {node.type_uri}')

        if record:
            for specialization in self.specializations:
                try:
                    specialization.process(record)
                except Exception as e:
                    l.error("Could Not Process {record}: {e}")

        # Send outgoing control flows
        # Check that outgoing flows don't conflict with
        # return updated token list
        return [t for t in tokens if t not in inputs] + new_tokens

    def next_tokens(self, activity_node: paml.ActivityNodeExecution, ex: paml.ProtocolExecution):
        protocol = ex.protocol.lookup()
        out_edges = [e for e in protocol.edges
                     if activity_node.node == e.source or
                        activity_node.node == e.source.lookup().get_parent().identity]

        if isinstance(activity_node.node.lookup(), uml.ForkNode):
            [incoming_flow] = activity_node.incoming_flows
            incoming_value = incoming_flow.lookup().value
            edge_tokens = [paml.ActivityEdgeFlow(edge=edge, token_source=activity_node,
                                                 value=uml.literal(incoming_value, reference=True))
                           for edge in out_edges]
        elif isinstance(activity_node.node.lookup(), uml.ActivityParameterNode):
            [parameter_value] = [pv.value for pv in ex.parameter_values if pv.parameter == activity_node.node.lookup().parameter]
            edge_tokens = [paml.ActivityEdgeFlow(edge=edge, token_source=activity_node,
                                                 value=uml.literal(value=parameter_value, reference=True))
                           for edge in out_edges]
        else:
            edge_tokens = [paml.ActivityEdgeFlow(edge=edge, token_source=activity_node,
                                                 value=self.get_value(activity_node, edge=edge))
                           for edge in out_edges]

        # Save tokens in the protocol execution
        ex.flows += edge_tokens

        # Assume that unlinked output pins are possible output parameters for the protocol
        if isinstance(activity_node, paml.CallBehaviorExecution):
            output_pins = activity_node.node.lookup().outputs
            unlinked_output_pins = [p for p in output_pins if p not in {e.source.lookup() for e in out_edges}]
            possible_output_parameter_values = [paml.ParameterValue(parameter=activity_node.node.lookup().pin_parameter(p.name),
                                                                    value=self.get_value(activity_node))
                                                for p in unlinked_output_pins]
            ex.parameter_values.extend(possible_output_parameter_values)
        return edge_tokens

    def get_value(self, activity_node : paml.CallBehaviorExecution, edge: uml.ActivityEdge = None):
        value = ""
        if isinstance(edge, uml.ControlFlow):
            value = "uml.ControlFlow"
        elif isinstance(edge, uml.ObjectFlow):
            parameter = activity_node.node.lookup().pin_parameter(edge.source.lookup().name).property_value
            value = activity_node.compute_output(parameter)

        value = uml.literal(value)
        return value

    def next_pin_tokens(self, activity_node: paml.ActivityNodeExecution, ex: paml.ProtocolExecution):
        assert len(activity_node.incoming_flows) == 1 # One input per pin
        incoming_flow = activity_node.incoming_flows[0].lookup()
        pin_value = uml.literal(value=incoming_flow.value, reference=True)

        tokens = [ paml.ActivityEdgeFlow(edge=None, token_source=activity_node, value=pin_value) ]

        # Save tokens in the protocol execution
        ex.flows += tokens
        return tokens


    def execute_primitive(self, behavior: paml.Primitive, agent: sbol3.Agent, parameter_values: dict = {}, id: str = uuid.uuid4()) -> paml.BehaviorExecution:
        pass


##################################
# Helper utility functions

def sum_measures(measure_list):
    """
    Add a list of measures and return a fresh measure
    Note: requires that all have the same unit and types

    :param measure_list: list of SBOL Measure objects

    :return: New Measure object with the sum of input measure amounts
    """
    prototype = measure_list[0]
    if not all(m.types == prototype.types and m.unit == prototype.unit for m in measure_list):
        raise ValueError(f'Can only merge measures with identical units and types: {([m.value, m.unit, m.types] for m in measure_list)}')
    total = sum(m.value for m in measure_list)
    return sbol3.Measure(value=total, unit=prototype.unit, types=prototype.types)


def protocol_execution_aggregate_child_materials(self):
    """
    Merge the consumed material from children, adding a fresh Material for each to this record.

    :param self: ProtocolExecution object
    :return:
    """
    child_materials = [e.call.consumed_material for e in self.executions
                       if isinstance(e, paml.CallBehaviorExecution) and
                          hasattr(e.call, "consumed_material")]
    specifications = {m.specification for m in child_materials}
    self.consumed_material = (paml.Material(s,sum_measures([m.amount for m in child_materials if m.specification==s]))
                              for s in specifications)
paml.ProtocolExecution.aggregate_child_materials = protocol_execution_aggregate_child_materials


def protocol_execution_to_dot(self):
    """
    Create a dot graph that illustrates edge values appearing the execution of the protocol.

    :param self:
    :return: graphviz.Digraph
    """
    dot = graphviz.Digraph(comment=self.protocol,
                           strict=True,
                           graph_attr={"rankdir": "TB",
                                       "concentrate": "true"},
                           node_attr={"ordering": "out"})
    def _make_object_edge(dot, incoming_flow, target, dest_parameter=None):
        flow_source = incoming_flow.lookup().token_source.lookup()
        source = incoming_flow.lookup().edge.lookup().source.lookup()
        value = incoming_flow.lookup().value
        value = value.value.lookup() if isinstance(value, uml.LiteralReference) else value.value

        if isinstance(source, uml.Pin):
            src_parameter = source.get_parent().pin_parameter(source.name).property_value
            src_var = src_parameter.name
        else:
            src_var = ""

        dest_var = dest_parameter.name if dest_parameter else ""

        source_id = source.dot_label(parent_identity=self.protocol)
        if isinstance(source, uml.CallBehaviorAction):
            source_id = f'{source_id}:node'
        target_id = target.dot_label(parent_identity=self.protocol)
        if isinstance(target, uml.CallBehaviorAction):
            target_id = f'{target_id}:node'

        edge_label = f"{src_var}-[{value}]->{dest_var}"
        attrs = {"color": "orange"}
        dot.edge(source_id, target_id, edge_label, _attributes=attrs)


    dot = graphviz.Digraph(name=f"cluster_{self.identity}",
                           graph_attr={
                               "label": self.identity
                           })

    # Protocol graph
    protocol_graph = self.document.find(self.protocol).to_dot()
    dot.subgraph(protocol_graph)


    # Execution graph
    for execution in self.executions:
        exec_target = execution.node.lookup()
        execution_label = ""

        # Make a self loop that includes the and start and end time.
        if isinstance(execution, paml.CallBehaviorExecution):
            execution_label += f"[{execution.call.lookup().start_time},\n  {execution.call.lookup().end_time}]"
            target_id = exec_target.dot_label(parent_identity=self.protocol)
            target_id = f'{target_id}:node'
            dot.edge(target_id, target_id,  _attributes={"tailport": "w",
                                                         "headport": "w",
                                                         "taillabel": execution_label,
                                                         "color": "invis"})

        for incoming_flow in execution.incoming_flows:
            # Executable Nodes have incoming flow from their input pins and ControlFlows
            flow_source = incoming_flow.lookup().token_source.lookup()
            exec_source = flow_source.node.lookup()
            edge_ref = incoming_flow.lookup().edge
            if edge_ref and isinstance(edge_ref.lookup(), uml.ObjectFlow):
                if isinstance(exec_target, uml.ActivityParameterNode):
                    # ActivityParameterNodes are ObjectNodes that have a parameter
                    _make_object_edge(dot, incoming_flow, exec_target, dest_parameter=exec_target.parameter.lookup())
                elif isinstance(exec_target, uml.ControlNode):
                    # This in an object flow into the node itself, which happens for ControlNodes
                    _make_object_edge(dot, incoming_flow, exec_target)
            elif isinstance(exec_source, uml.Pin):
                # This incoming_flow is from an input pin, and need the flow into the pin
                into_pin_flow = flow_source.incoming_flows[0]
                #source = src_to_pin_edge.source.lookup()
                #target = src_to_pin_edge.target.lookup()
                dest_parameter = exec_target.pin_parameter(exec_source.name).property_value
                _make_object_edge(dot, into_pin_flow, into_pin_flow.lookup().edge.lookup().target.lookup(), dest_parameter=dest_parameter)

    return dot
paml.ProtocolExecution.to_dot = protocol_execution_to_dot
