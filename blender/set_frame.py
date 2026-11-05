import blender_mcp_client
import camera_client
from griptape_nodes.exe_types.core_types import ParameterGroup
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt


class BlenderSetFrame(SuccessFailureNode):
    """Go to a frame in the scene open in Blender."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Sets the current frame of the scene open in Blender."

        self.add_parameter(
            ParameterInt(
                name="frame",
                display_name="Frame",
                tooltip="The frame to go to.",
                default_value=1,
            )
        )
        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterInt(
                name="mcp_port",
                display_name="MCP Port",
                tooltip="Port of the Blender Lab MCP add-on's server.",
                default_value=blender_mcp_client.DEFAULT_PORT,
            )
        self.add_node_element(blender_group)

        self.add_parameter(
            ParameterInt(
                name="current_frame",
                display_name="Current Frame",
                tooltip="The frame Blender is on after the run.",
                allow_input=False,
                allow_property=False,
            )
        )
        self._create_status_parameters(
            result_details_tooltip="What was set.",
            result_details_placeholder="Details appear here.",
        )

    def process(self) -> AsyncResult[None]:
        yield lambda: self._process()

    def _process(self) -> None:
        self._clear_execution_status()
        # A run that fails must not leave the last run's values for a wired Failed output to pass on.
        self.parameter_output_values.update({"current_frame": None})
        try:
            details = self._set()
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        self._set_status_results(was_successful=True, result_details=details)

    def _set(self) -> str:
        value = self.get_parameter_value
        done = camera_client.set_frame(int(value("frame")), port=int(value("mcp_port")))

        self.parameter_output_values["current_frame"] = done["frame"]
        return f"Blender is on frame {done['frame']}. The scene runs from {done['frame_start']} to {done['frame_end']} at {done['fps']:g} fps."
