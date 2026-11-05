import json

import blender_mcp_client
import camera_client
from griptape.artifacts import ListArtifact, TextArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.retained_mode.griptape_nodes import logger


class BlenderCameraList(ControlNode):
    def __init__(self, **kwargs) -> None:
        # Initialize private state variable
        self._internal_state = None
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Lists all available cameras in the scene open in Blender."
        self.metadata["author"] = "Griptape"
        self.metadata["dependencies"] = {}

        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterInt(
                name="mcp_port",
                display_name="MCP Port",
                tooltip="Port of the Blender Lab MCP add-on's server.",
                default_value=blender_mcp_client.DEFAULT_PORT,
            )
        self.add_node_element(blender_group)

        # Output Parameters
        self.add_parameter(
            Parameter(
                name="cameras_output",
                output_type="ListArtifact",
                type="ListArtifact",
                default_value=None,
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="List of camera information including names, positions, and rotations.",
                ui_options={"pulse_on_run": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="camera_count",
                output_type="int",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Total number of cameras found in the scene.",
            )
        )
        self.add_parameter(
            Parameter(
                name="status_output",
                output_type="str",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Status message from the camera discovery operation.",
                ui_options={"multiline": True},
            )
        )

    @property
    def always_run(self) -> bool:
        """Force this node to always run to get fresh camera data."""
        return True

    def _fetch_cameras(self) -> tuple[list[dict], str]:
        """Fetch the cameras from the scene open in Blender, and a status message to show."""
        found = camera_client.list_cameras(port=int(self.get_parameter_value("mcp_port")))
        cameras = found["cameras"]
        if not cameras:
            return [], "No cameras found in the current Blender scene"
        return cameras, f"Found {len(cameras)} camera(s) in Blender {found['blender_version']}"

    def _format_camera_info(self, cameras: list[dict]) -> list[dict]:
        """Format camera information for better readability."""
        return [
            {
                "name": camera["name"],
                "location": dict(zip("xyz", (round(v, 3) for v in camera["location"]), strict=True)),
                "rotation": dict(zip("xyz", (round(v, 3) for v in camera["rotation"]), strict=True)),
                "scale": dict(zip("xyz", (round(v, 3) for v in camera["scale"]), strict=True)),
                "active": camera["active"],
                # Lens and sensor properties
                "focal_length": round(camera["focal_length"], 2),
                "sensor_width": round(camera["sensor_width"], 2),
                "sensor_height": round(camera["sensor_height"], 2),
                "sensor_fit": camera["sensor_fit"],
                # Camera type and angles
                "type": camera["type"],
                "angle": round(camera["angle"], 4) if camera["angle"] else None,
                "angle_x": round(camera["angle_x"], 4) if camera["angle_x"] else None,
                "angle_y": round(camera["angle_y"], 4) if camera["angle_y"] else None,
                # Clipping distances
                "clip_start": round(camera["clip_start"], 3),
                "clip_end": round(camera["clip_end"], 1),
                # Depth of field
                "depth_of_field": {
                    "enabled": camera["dof_use"],
                    "focus_distance": round(camera["dof_focus_distance"], 2) if camera["dof_focus_distance"] else None,
                    "f_stop": round(camera["dof_aperture_fstop"], 2) if camera["dof_aperture_fstop"] else None,
                },
                # Shift and composition
                "shift": {"x": round(camera["shift_x"], 3), "y": round(camera["shift_y"], 3)},
                "passepartout_alpha": round(camera["passepartout_alpha"], 2),
                # Additional info
                "background_images_count": camera["background_images_count"],
                "matrix_world": camera["matrix_world"],  # Keep full precision for matrix
            }
            for camera in cameras
        ]

    def validate_before_workflow_run(self) -> list[Exception] | None:
        """Reset node state to ensure it always re-evaluates in workflows."""
        # Clear any cached output values to force re-evaluation
        self.parameter_output_values.clear()
        return None

    def initialize_spotlight(self) -> None:
        """Override to ensure this node always re-evaluates to get fresh camera data."""
        # By overriding this method to do nothing, we prevent the default
        # dependency resolution that might cache this node's results
        pass

    def process(self):
        """Fetch and list all cameras from the Blender scene."""
        self.parameter_output_values["status_output"] = "Fetching cameras from Blender..."
        try:
            cameras, status_msg = self._fetch_cameras()
        except Exception as e:
            self.parameter_output_values["status_output"] = f"Error: {e}"
            self.parameter_output_values["camera_count"] = 0
            raise

        self.parameter_output_values["status_output"] = status_msg
        if not cameras:
            # A scene with no cameras is not a failure
            self.parameter_output_values["camera_count"] = 0
            self.parameter_output_values["cameras_output"] = ListArtifact([])
            return

        formatted_cameras = self._format_camera_info(cameras)
        camera_artifacts = [
            TextArtifact(value=json.dumps(camera, indent=2), name=f"camera_{camera['name']}")
            for camera in formatted_cameras
        ]
        self.parameter_output_values["camera_count"] = len(formatted_cameras)
        self.parameter_output_values["cameras_output"] = ListArtifact(camera_artifacts, name="blender_cameras")

        # Propagate camera names to all BlenderCameraCapture nodes to refresh their dropdowns
        try:
            from camera_capture import BlenderCameraCapture

            BlenderCameraCapture._update_all_camera_lists_with_names([cam["name"] for cam in formatted_cameras])
        except Exception as e:
            # Non-fatal if capture class isn't loaded yet
            logger.debug(f"Camera list propagation skipped: {e}")
