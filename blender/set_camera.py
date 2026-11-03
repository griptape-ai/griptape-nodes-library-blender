import blender_mcp_client
import camera_choices
import camera_client
from griptape.artifacts import ListArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.traits.options import Options


class BlenderSetCamera(SuccessFailureNode):
    """Move, turn or zoom a camera in the scene open in Blender."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Sets the location, rotation or focal length of a camera in the scene open in Blender."

        self.add_parameter(
            Parameter(
                name="cameras_input",
                tooltip="Camera list from the Camera List node (optional). Its cameras become the choices for Camera.",
                type="ListArtifact",
                input_types=["ListArtifact"],
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            ParameterString(
                name="camera",
                display_name="Camera",
                tooltip="The camera to change. The scene's active camera by default.",
                default_value=camera_choices.ACTIVE_CAMERA,
                traits={Options(choices=[camera_choices.ACTIVE_CAMERA, *camera_choices.fetch_names()])},
            )
        )
        with ParameterGroup(name="Location") as location_group:
            ParameterBool(
                name="set_location",
                display_name="Set Location",
                tooltip="Move the camera. When off, the camera stays where it is.",
                default_value=False,
            )
            for axis in "xyz":
                ParameterFloat(
                    name=f"location_{axis}",
                    display_name=axis.upper(),
                    tooltip=f"Location on {axis.upper()}, in scene units.",
                    default_value=0.0,
                )
        self.add_node_element(location_group)
        with ParameterGroup(name="Rotation") as rotation_group:
            ParameterBool(
                name="set_rotation",
                display_name="Set Rotation",
                tooltip="Turn the camera. When off, the camera keeps its rotation.",
                default_value=False,
            )
            for axis in "xyz":
                ParameterFloat(
                    name=f"rotation_{axis}",
                    display_name=axis.upper(),
                    tooltip=f"Rotation around {axis.upper()}, in degrees.",
                    default_value=0.0,
                )
        self.add_node_element(rotation_group)
        with ParameterGroup(name="Lens") as lens_group:
            ParameterBool(
                name="set_focal_length",
                display_name="Set Focal Length",
                tooltip="Change the lens. When off, the camera keeps its focal length.",
                default_value=False,
            )
            ParameterFloat(
                name="focal_length",
                display_name="Focal Length",
                tooltip="Focal length in millimeters.",
                default_value=50.0,
            )
        self.add_node_element(lens_group)
        self.add_parameter(
            ParameterBool(
                name="keyframe",
                display_name="Keyframe",
                tooltip="Key what this node changes on Blender's current frame. Use Set Frame first to pick the frame.",
                default_value=False,
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
            ParameterString(
                name="camera_name",
                display_name="Camera Used",
                tooltip="The camera that was changed.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self._create_status_parameters(
            result_details_tooltip="What was changed.",
            result_details_placeholder="Details appear here.",
        )

    def _set_choices(self, names: list[str]) -> None:
        camera_choices.apply(self, "camera", [camera_choices.ACTIVE_CAMERA, *names], camera_choices.ACTIVE_CAMERA)

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.name == "cameras_input" and isinstance(value, ListArtifact):
            self._set_choices(camera_choices.names_from_list(value))
        elif parameter.name == "mcp_port":
            # The port decides which Blender the cameras come from, and a saved port loads after __init__
            self._set_choices(camera_choices.fetch_names(int(value)))  # type: ignore[arg-type]

    def process(self) -> AsyncResult[None]:
        yield lambda: self._process()

    def _process(self) -> None:
        self._clear_execution_status()
        try:
            details = self._set()
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        self._set_status_results(was_successful=True, result_details=details)

    def _set(self) -> str:
        value = self.get_parameter_value
        location = [float(value(f"location_{a}")) for a in "xyz"] if value("set_location") else None
        rotation = [float(value(f"rotation_{a}")) for a in "xyz"] if value("set_rotation") else None
        focal_length = float(value("focal_length")) if value("set_focal_length") else None
        if location is None and rotation is None and focal_length is None:
            msg = "Nothing to change. Turn on Set Location, Set Rotation or Set Focal Length."
            raise ValueError(msg)
        try:
            done = camera_client.set_camera(
                camera="" if value("camera") in (None, "", camera_choices.ACTIVE_CAMERA) else value("camera").strip(),
                location=location,
                rotation_degrees=rotation,
                focal_length=focal_length,
                keyframe=bool(value("keyframe")),
                port=int(value("mcp_port")),
            )
        except blender_mcp_client.BlenderMCPError as e:
            msg = f"Could not change the camera in Blender.\n{e}"
            raise blender_mcp_client.BlenderMCPError(msg) from e

        self.parameter_output_values["camera_name"] = done["camera"]
        changed = [
            name
            for name, new in (("location", location), ("rotation", rotation), ("focal length", focal_length))
            if new is not None
        ]
        lines = [f"Changed the {', '.join(changed)} of '{done['camera']}'."]
        if done["keyframed_at"] is not None:
            lines.append(f"Keyed on frame {done['keyframed_at']}.")
        return "\n".join(lines)
