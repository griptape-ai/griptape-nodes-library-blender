import base64
import json

import blender_mcp_client
import camera_choices
import camera_client
from griptape.artifacts import ImageUrlArtifact
from griptape_nodes.exe_types.core_types import NodeError, Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.traits.options import Options

# A real PNG is far larger. Anything smaller means Blender sent something that is not an image.
MIN_IMAGE_BYTES = 100


class BlenderCameraCapture(ControlNode):
    # Class-level registry to track all instances
    _instances = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Captures a single frame from a camera in the scene open in Blender."
        self.metadata["author"] = "Griptape"
        self.metadata["dependencies"] = {}

        # Register this instance
        BlenderCameraCapture._instances.append(self)

        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterInt(
                name="mcp_port",
                display_name="MCP Port",
                tooltip="Port of the Blender Lab MCP add-on's server.",
                default_value=blender_mcp_client.DEFAULT_PORT,
            )
        self.add_node_element(blender_group)

        # Camera list input (optional - for connecting to BlenderCameraList node)
        self.add_parameter(
            Parameter(
                name="cameras_input",
                tooltip="Camera list from BlenderCameraList node (optional)",
                type="ListArtifact",
                input_types=["ListArtifact"],
                allowed_modes={ParameterMode.INPUT},
            )
        )

        # Camera name parameter (add directly to ensure traits work properly)
        available_cameras = self._get_available_cameras()

        options_trait = Options(choices=available_cameras)

        self.camera_param = Parameter(
            name="camera_name",
            input_types=["str"],
            output_type="str",
            type="str",
            default_value="Camera",
            tooltip="Name of the camera in the Blender scene to capture from.",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            traits={options_trait},
            ui_options={"display_name": "Camera"},
        )

        self.add_parameter(self.camera_param)

        # Camera metadata label parameters (read-only, displayed under camera selection)
        self.add_parameter(
            Parameter(
                name="camera_status_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera status (active/available)",
                ui_options={"display_name": "Status"},
            )
        )
        self.add_parameter(
            Parameter(
                name="focal_length_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera focal length",
                ui_options={"display_name": "Focal Length"},
            )
        )
        self.add_parameter(
            Parameter(
                name="sensor_info_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera sensor dimensions and type",
                ui_options={"display_name": "Sensor"},
            )
        )
        self.add_parameter(
            Parameter(
                name="dof_info_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Depth of field settings",
                ui_options={"display_name": "Depth of Field"},
            )
        )
        self.add_parameter(
            Parameter(
                name="transform_info_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera location and rotation",
                ui_options={"display_name": "Transform"},
            )
        )

        # Output Settings Group
        with ParameterGroup(name="Output Settings") as output_group:
            Parameter(
                name="output_format",
                input_types=["str"],
                output_type="str",
                type="str",
                default_value="PNG",
                tooltip="Output image format for the captured frame.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                traits={Options(choices=["PNG", "JPEG"])},
                ui_options={"display_name": "Format"},
            )
            Parameter(
                name="resolution_x",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=1920,
                tooltip="Output image width in pixels (64-4096).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 64, "max": 4096, "display_name": "Width"},
            )
            Parameter(
                name="resolution_y",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=1080,
                tooltip="Output image height in pixels (64-4096).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 64, "max": 4096, "display_name": "Height"},
            )
            Parameter(
                name="quality",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=90,
                tooltip="Image quality (1-100, applies to JPEG format only).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 1, "max": 100, "display_name": "Quality"},
            )
        self.add_node_element(output_group)

        # Output Parameters
        self._output_file = ProjectFileParameter(
            node=self,
            name="output_file",
            default_filename="blender_capture.png",
        )
        self._output_file.add_parameter()

        self.add_parameter(
            Parameter(
                name="image_output",
                output_type="ImageUrlArtifact",
                type="ImageUrlArtifact",
                default_value=None,
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Captured image from the Blender camera.",
                ui_options={"pulse_on_run": True, "is_full_width": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="status_output",
                output_type="str",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Status message from the capture operation.",
                ui_options={"multiline": True},
            )
        )

        # Update camera metadata display
        self._update_camera_metadata_display()

    def _get_available_cameras(self) -> list[str]:
        """The cameras in the scene open in Blender, on the node's MCP port."""
        try:
            port = int(self.get_parameter_value("mcp_port"))
        except (TypeError, ValueError):
            port = blender_mcp_client.DEFAULT_PORT
        return camera_choices.fetch_names(port) or ["Camera"]

    def validate_before_node_run(self) -> list[Exception] | None:
        """Keep the selected camera among the ones the connected Camera List found."""
        # Note: Camera list updates are handled by the BlenderCameraList node
        # which always re-evaluates and provides fresh data via cameras_input

        names = camera_choices.names_from_list(self.get_parameter_value("cameras_input"))
        if names:
            camera_choices.apply(self, "camera_name", names, names[0])

        return None

    @staticmethod
    def _image_bytes(render_result: dict) -> bytes:
        """The PNG in Blender's reply, or a NodeError saying what is wrong with it."""
        image_b64 = render_result.get("image")
        if not image_b64:
            msg = "Blender returned no image data."
            raise NodeError(msg, response={key: value for key, value in render_result.items() if key != "image"})
        try:
            image_data = base64.b64decode(image_b64)
        except ValueError as e:
            msg = "Could not decode the image data from Blender."
            raise NodeError(msg) from e
        if len(image_data) < MIN_IMAGE_BYTES:
            msg = "The image data from Blender was empty or corrupted. Run the node again."
            raise NodeError(msg)
        return image_data

    async def aprocess(self) -> None:
        """Capture a frame from the specified Blender camera."""
        camera_name = self.get_parameter_value("camera_name") or "Camera"
        output_format = self.get_parameter_value("output_format") or "PNG"
        resolution_x = self.get_parameter_value("resolution_x") or 1920
        resolution_y = self.get_parameter_value("resolution_y") or 1080

        # If a Camera List is connected, the camera must be one it found
        names = camera_choices.names_from_list(self.get_parameter_value("cameras_input"))
        if names and camera_name not in names:
            camera_name = names[0]

        self._update_camera_metadata_display()
        self.parameter_output_values["status_output"] = f"Capturing frame from camera '{camera_name}'..."

        try:
            render_result = camera_client.render_camera(
                camera_name,
                resolution_x,
                resolution_y,
                port=int(self.get_parameter_value("mcp_port")),
            )
            saved = self._output_file.build_file().write_bytes(self._image_bytes(render_result))
        except Exception as e:
            self.parameter_output_values["status_output"] = f"Error: {e}"
            raise

        self.parameter_output_values["image_output"] = ImageUrlArtifact(value=saved.location)
        render_time = render_result.get("render_time", 0)
        actual_width = render_result.get("width", resolution_x)
        actual_height = render_result.get("height", resolution_y)
        self.parameter_output_values["status_output"] = (
            f"Successfully captured {actual_width}x{actual_height} {output_format} image from camera '{camera_name}'\n"
            f"Render time: {render_time:.2f}s, Engine: BLENDER_WORKBENCH"
        )

    def after_value_set(self, parameter, value):
        """Update camera choices when cameras_input receives new data."""
        if parameter.name == "cameras_input" and value:
            names = camera_choices.names_from_list(value)
            if names:
                # Other instances are not updated here, to prevent feedback loops
                camera_choices.apply(self, "camera_name", names, names[0])
                self._update_camera_metadata_display()

        elif parameter.name == "mcp_port":
            # The port decides which Blender the cameras come from, and a saved port loads after __init__
            names = self._get_available_cameras()
            camera_choices.apply(self, "camera_name", names, names[0])

        elif parameter.name == "camera_name":
            # Update metadata display when camera selection changes
            try:
                self._update_camera_metadata_display()
            except Exception:
                # Don't let metadata errors break camera selection
                pass

    @classmethod
    def _update_all_camera_lists_with_names(cls, camera_names, skip_instance=None):
        """Update camera lists for all instances with provided camera names."""
        for instance in cls._instances:
            if instance and instance != skip_instance:  # Skip the instance that was already updated
                camera_choices.apply(instance, "camera_name", camera_names, camera_names[0])

    def after_incoming_connection(self, source_node, source_parameter, target_parameter):
        """Refresh camera list when connections are made."""
        if target_parameter.name == "camera_name":
            # Camera updates now handled by BlenderCameraList node via cameras_input
            pass
        elif target_parameter.name == "cameras_input":
            # Camera list data will flow through this connection automatically
            pass

    def _update_camera_metadata_display(self):
        """Update the camera metadata label parameters based on current selection and available data."""
        camera_name = self.get_parameter_value("camera_name") or "Camera"
        cameras_input = self.get_parameter_value("cameras_input")

        # Find the selected camera's data
        camera_data = None
        if cameras_input and hasattr(cameras_input, "value"):
            for item in cameras_input.value:
                if hasattr(item, "value"):
                    try:
                        parsed_data = json.loads(item.value)
                        if parsed_data.get("name") == camera_name:
                            camera_data = parsed_data
                            break
                    except (json.JSONDecodeError, AttributeError):
                        continue

        if camera_data and "focal_length" in camera_data:
            # Enhanced camera data available - show detailed labels

            # Status label
            status_text = "✓ Active Scene Camera" if camera_data.get("active") else "Available Camera"
            self.set_parameter_value("camera_status_label", status_text)

            # Focal length label
            focal_length = camera_data.get("focal_length", 50.0)
            self.set_parameter_value("focal_length_label", f"{focal_length} mm")

            # Sensor info label
            sensor_w = camera_data.get("sensor_width", 36.0)
            sensor_h = camera_data.get("sensor_height", 24.0)
            sensor_fit = camera_data.get("sensor_fit", "AUTO")
            cam_type = camera_data.get("type", "PERSP")
            sensor_text = f"{sensor_w}×{sensor_h}mm, {sensor_fit}, {cam_type}"
            self.set_parameter_value("sensor_info_label", sensor_text)

            # Depth of field label - fix structure mismatch
            dof = camera_data.get("depth_of_field", {})
            if dof.get("enabled"):
                focus_dist = dof.get("focus_distance", 10.0)
                f_stop = dof.get("f_stop", 2.8)
                dof_text = f"Enabled: {focus_dist}BU @ f/{f_stop}"
            else:
                dof_text = "Disabled"
            self.set_parameter_value("dof_info_label", dof_text)

            # Transform label
            location = camera_data.get("location", {})
            rotation = camera_data.get("rotation", {})
            loc_text = f"({location.get('x', 0.0):.2f}, {location.get('y', 0.0):.2f}, {location.get('z', 0.0):.2f})"
            rot_text = f"({rotation.get('x', 0.0):.2f}, {rotation.get('y', 0.0):.2f}, {rotation.get('z', 0.0):.2f})"
            transform_text = f"Loc: {loc_text} Rot: {rot_text}"
            self.set_parameter_value("transform_info_label", transform_text)

        elif camera_data:
            # Basic camera data available - show what we can
            status_text = "✓ Active Scene Camera" if camera_data.get("active") else "Available Camera"
            self.set_parameter_value("camera_status_label", status_text)

            # Show basic location/rotation if available
            location = camera_data.get("location", {})
            rotation = camera_data.get("rotation", {})
            if location and rotation:
                loc_text = f"({location.get('x', 0.0):.2f}, {location.get('y', 0.0):.2f}, {location.get('z', 0.0):.2f})"
                rot_text = f"({rotation.get('x', 0.0):.2f}, {rotation.get('y', 0.0):.2f}, {rotation.get('z', 0.0):.2f})"
                transform_text = f"Loc: {loc_text} Rot: {rot_text}"
                self.set_parameter_value("transform_info_label", transform_text)
            else:
                self.set_parameter_value("transform_info_label", "Basic data only")

            # Set others to indicate limited data
            self.set_parameter_value("focal_length_label", "Basic mode")
            self.set_parameter_value("sensor_info_label", "Basic mode")
            self.set_parameter_value("dof_info_label", "Basic mode")

        else:
            # No enhanced data - show placeholder values
            self.set_parameter_value("camera_status_label", "Connect BlenderCameraList for details")
            self.set_parameter_value("focal_length_label", "-")
            self.set_parameter_value("sensor_info_label", "-")
            self.set_parameter_value("dof_info_label", "-")
            self.set_parameter_value("transform_info_label", "-")
