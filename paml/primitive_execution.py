import json
import paml
from paml_convert.plate_coordinates import coordinate_rect_to_row_col_pairs, get_aliquot_list, num2row
import uml
import xarray as xr
import logging
import sbol3


l = logging.getLogger(__file__)
l.setLevel(logging.ERROR)


def call_behavior_execution_compute_output(self, parameter):
    """
    Get parameter value from call behavior execution

    :param self:
    :param parameter: output parameter to define value
    :return: value
    """
    primitive = self.node.lookup().behavior.lookup()
    call = self.call.lookup()
    inputs = [x for x in call.parameter_values if x.parameter.lookup().property_value.direction == uml.PARAMETER_IN]
    value = primitive.compute_output(inputs, parameter)
    return value
paml.CallBehaviorExecution.compute_output = call_behavior_execution_compute_output

def call_behavior_action_compute_output(self, inputs, parameter):
    """
    Get parameter value from call behavior action

    :param self:
    :param inputs: token values for object pins
    :param parameter: output parameter to define value
    :return: value
    """
    primitive = self.behavior.lookup()
    inputs = self.input_parameter_values(inputs=inputs)
    value = primitive.compute_output(inputs, parameter)
    return value
uml.CallBehaviorAction.compute_output = call_behavior_action_compute_output

def call_behavior_action_input_parameter_values(self, inputs=None):
    """
    Get parameter values for all inputs

    :param self:
    :param parameter: output parameter to define value
    :return: value
    """

    # Get the parameter values from input tokens for input pins
    input_pin_values = {}
    if inputs:
        input_pin_values = {token.token_source.lookup().node.lookup().identity:
                                    uml.literal(token.value, reference=True)
                            for token in inputs if not token.edge}


    # Get Input value pins
    value_pin_values = {pin.identity: pin.value for pin in self.inputs if hasattr(pin, "value")}
    # Convert References
    value_pin_values = {k: (uml.LiteralReference(value=self.document.find(v.value))
                            if hasattr(v, "value") and
                               (isinstance(v.value, sbol3.refobj_property.ReferencedURI) or
                                isinstance(v, uml.LiteralReference))
                            else  uml.LiteralReference(value=v))
                        for k, v in value_pin_values.items()}
    pin_values = { **input_pin_values, **value_pin_values} # merge the dicts

    parameter_values = [paml.ParameterValue(parameter=self.pin_parameter(pin.name).property_value,
                                            value=pin_values[pin.identity])
                        for pin in self.inputs if pin.identity in pin_values]
    return parameter_values
uml.CallBehaviorAction.input_parameter_values = call_behavior_action_input_parameter_values



def primitive_compute_output(self, inputs, parameter):
    """
    Compute the value for parameter given the inputs. This default function will be overridden for specific primitives.

    :param self:
    :param inputs: list of paml.ParameterValue
    :param parameter: Parameter needing value
    :return: value
    """

    l.debug(f"Computing the output of primitive: {self.identity}, parameter: {parameter.name}")

    def resolve_value(v):
        if not isinstance(v, uml.LiteralReference):
            return v.value
        else:
            resolved = v.value.lookup()
            if isinstance(resolved, uml.LiteralSpecification):
                return resolved.value
            else:
                return resolved

    if self.identity == 'https://bioprotocols.org/paml/primitives/sample_arrays/EmptyContainer' and \
        parameter.name == "samples" and \
        parameter.type == 'http://bioprotocols.org/paml#SampleArray':
        # Make a SampleArray
        for input in inputs:
            i_parameter = input.parameter.lookup().property_value
            value = input.value
            if i_parameter.name == "specification":
                spec = resolve_value(value)
        contents = self.initialize_contents()
        name = f"{parameter.name}"
        sample_array = paml.SampleArray(name=name,
                                   container_type=spec,
                                   contents=contents)
        return sample_array
    elif self.identity == 'https://bioprotocols.org/paml/primitives/sample_arrays/PlateCoordinates' and \
        parameter.name == "samples" and \
        parameter.type == 'http://bioprotocols.org/paml#SampleCollection':
        for input in inputs:
            i_parameter = input.parameter.lookup().property_value
            value = input.value
            if i_parameter.name == "source":
                source = resolve_value(value)
            elif i_parameter.name == "coordinates":
                coordinates = resolve_value(value)
                # convert coordinates into a boolean sample mask array
                # 1. read source contents into array
                # 2. create parallel array for entries noted in coordinates
                mask_array = source.mask(coordinates)
        mask = paml.SampleMask(source=source,
                               mask=mask_array)
        return mask
    elif self.identity == 'https://bioprotocols.org/paml/primitives/spectrophotometry/MeasureAbsorbance' and \
        parameter.name == "measurements" and \
        parameter.type == 'http://bioprotocols.org/paml#SampleData':
        for input in inputs:
            i_parameter = input.parameter.lookup().property_value
            value = input.value
            if i_parameter.name == "samples":
                samples = resolve_value(value)

        sample_data = paml.SampleData(from_samples=samples)
        return sample_data
    else:
        return f"{parameter.name}"
paml.Primitive.compute_output = primitive_compute_output

def empty_container_initialize_contents(self):
    if self.identity == 'https://bioprotocols.org/paml/primitives/sample_arrays/EmptyContainer':
        # FIXME need to find a definition of the container topology from the type
        # FIXME this assumes a 96 well plate

        l.warn("Warning: Assuming that the SampleArray is a 96 well microplate!")
        aliquots = get_aliquot_list(geometry="A1:H12")
        #contents = json.dumps(xr.DataArray(dims=("aliquot", "contents"), 
        #                                   coords={"aliquot": aliquots}).to_dict())
        contents = json.dumps(xr.DataArray(aliquots, dims=("aliquot")).to_dict())
    else:
        raise Exception(f"Cannot initialize contents of: {self.identity}")
    return contents
paml.Primitive.initialize_contents = empty_container_initialize_contents
