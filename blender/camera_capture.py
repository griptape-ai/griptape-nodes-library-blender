import json
import base64
from typing import Optional, Any
from io import BytesIO

from griptape.artifacts import ImageArtifact, ImageUrlArtifact, ErrorArtifact, TextArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterGroup
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.retained_mode.griptape_nodes import logger
from griptape_nodes.traits.options import Options

# Import socket client utilities
from socket_client import health_check, get_scene_info, list_cameras, render_camera


class BlenderCameraCapture(ControlNode):
    # Class-level registry to track all instances
    _instances = []
    
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Captures a single frame from a Blender camera via socket server."
        self.metadata["author"] = "Griptape"
        self.metadata["dependencies"] = {}
        
        # Register this instance
        BlenderCameraCapture._instances.append(self)

        # Camera list input (optional - for connecting to BlenderCameraList node)
        self.add_parameter(
            Parameter(
                name="cameras_input",
                tooltip="Camera list from BlenderCameraList node (optional)",
                type="ListArtifact",
                input_types=["ListArtifact"],
                allowed_modes={ParameterMode.INPUT}
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
            ui_options={"display_name": "Camera"}
        )
        
        # Try adding the trait after parameter creation
        if hasattr(self.camera_param, 'add_trait'):
            self.camera_param.add_trait(options_trait)
        elif hasattr(self.camera_param, 'traits'):
            if self.camera_param.traits is None:
                self.camera_param.traits = set()
            self.camera_param.traits.add(options_trait)
        else:
            if not hasattr(self.camera_param, '_traits'):
                self.camera_param._traits = set()
            self.camera_param._traits.add(options_trait)
        
        self.add_parameter(self.camera_param)
        
        # Camera metadata label parameters (read-only, displayed under camera selection)
        self.add_parameter(
            Parameter(
                name="camera_status_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera status (active/available)",
                ui_options={"display_name": "Status"}
            )
        )
        self.add_parameter(
            Parameter(
                name="focal_length_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera focal length",
                ui_options={"display_name": "Focal Length"}
            )
        )
        self.add_parameter(
            Parameter(
                name="sensor_info_label",
                type="str", 
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera sensor dimensions and type",
                ui_options={"display_name": "Sensor"}
            )
        )
        self.add_parameter(
            Parameter(
                name="dof_info_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Depth of field settings",
                ui_options={"display_name": "Depth of Field"}
            )
        )
        self.add_parameter(
            Parameter(
                name="transform_info_label",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Camera location and rotation",
                ui_options={"display_name": "Transform"}
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
                ui_options={"display_name": "Format"}
            )
            Parameter(
                name="resolution_x",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=1920,
                tooltip="Output image width in pixels (64-4096).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 64, "max": 4096, "display_name": "Width"}
            )
            Parameter(
                name="resolution_y",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=1080,
                tooltip="Output image height in pixels (64-4096).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 64, "max": 4096, "display_name": "Height"}
            )
            Parameter(
                name="quality",
                input_types=["int"],
                output_type="int",
                type="int",
                default_value=90,
                tooltip="Image quality (1-100, applies to JPEG format only).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"min": 1, "max": 100, "display_name": "Quality"}
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
                ui_options={"pulse_on_run": True, "is_full_width": True}
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
                ui_options={"multiline": True}
            )
        )

        # Update camera metadata display
        self._update_camera_metadata_display()

    def _get_available_cameras(self) -> list[str]:
        """Fetch available cameras from the Blender socket server."""
        try:
            result = list_cameras()
            if result.get("success") and result.get("cameras"):
                return [camera["name"] for camera in result["cameras"]]
            else:
                logger.warning(f"Could not fetch cameras: {result.get('error', 'Unknown error')}")
                return ["Camera"]
        except Exception as e:
            logger.warning(f"Could not fetch cameras from Blender socket server: {e}")
            return ["Camera"]

    @classmethod
    def _update_all_camera_lists(cls):
        """Update camera lists for all BlenderCameraCapture instances."""
        try:
            # Get fresh camera list
            cameras = []
            result = list_cameras()
            if result.get("success") and result.get("cameras"):
                cameras = [camera["name"] for camera in result["cameras"]]
            else:
                cameras = ["Camera"]  # Fallback
            
            # Update all instances
            for instance in cls._instances:
                if instance:  # Check instance is still valid
                    camera_param = instance.get_parameter_by_name("camera_name")
                    if camera_param:
                        instance._update_camera_choices(camera_param, cameras)
        except Exception as e:
            logger.warning(f"Failed to update camera lists: {e}")

    @classmethod
    def _update_camera_lists_from_blender(cls):
        """Update camera lists for all instances by fetching from Blender."""
        try:
            # Get fresh camera list from Blender
            cameras = []
            result = list_cameras()
            if result.get("success") and result.get("cameras"):
                cameras = [camera["name"] for camera in result["cameras"]]
            else:
                cameras = ["Camera"]  # Fallback
            
            # Update all instances with the fetched camera names
            cls._update_all_camera_lists_with_names(cameras)
        except Exception as e:
            logger.warning(f"Failed to update camera lists from Blender: {e}")

    def _check_blender_connection(self) -> tuple[bool, str]:
        """Check if Blender socket server is available."""
        try:
            result = get_scene_info()
            if result.get("success"):
                blender_info = result.get("blender", {})
                version = blender_info.get("version", "Unknown")
                return True, f"Connected to Blender {version}"
            else:
                return False, f"Blender socket server error: {result.get('error', 'Unknown error')}"
        except Exception as e:
            return False, f"Cannot connect to Blender socket server: {str(e)}"

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate that Blender socket server is available before running."""
        # Note: Camera list updates are now handled by the BlenderCameraList node
        # which always re-evaluates and provides fresh data via cameras_input
        
        is_connected, message = self._check_blender_connection()
        if not is_connected:
            return [ConnectionError(message)]
        
        # Validate camera selection if cameras_input is connected
        cameras_input = self.get_parameter_value("cameras_input")
        if cameras_input and hasattr(cameras_input, 'value'):
            current_camera = self.get_parameter_value("camera_name")
            available_cameras = []
            
            for item in cameras_input.value:
                if hasattr(item, 'value'):
                    try:
                        camera_data = json.loads(item.value)
                        if 'name' in camera_data:
                            available_cameras.append(camera_data['name'])
                    except (json.JSONDecodeError, AttributeError):
                        continue
            
            if available_cameras and current_camera not in available_cameras:
                # Update choices first, then auto-correct to first available camera
                camera_param = self.get_parameter_by_name("camera_name")
                if camera_param:
                    self._update_camera_choices(camera_param, available_cameras)
                    # Set the value directly to bypass validation
                    camera_param.default_value = available_cameras[0]
                    if hasattr(camera_param, 'value'):
                        camera_param.value = available_cameras[0]
                    self.parameter_values["camera_name"] = available_cameras[0]
        
        return None

    def process(self):
        """Capture a frame from the specified Blender camera."""
        try:
            # Get parameters
            camera_name = self.get_parameter_value("camera_name") or "Camera"
            output_format = self.get_parameter_value("output_format") or "PNG"
            resolution_x = self.get_parameter_value("resolution_x") or 1920
            resolution_y = self.get_parameter_value("resolution_y") or 1080
            quality = self.get_parameter_value("quality") or 90

            # If cameras_input is connected, validate camera_name exists in the list
            cameras_input = self.get_parameter_value("cameras_input")
            
            if cameras_input and hasattr(cameras_input, 'value'):
                available_cameras = []
                for item in cameras_input.value:
                    if hasattr(item, 'value'):
                        try:
                            # Parse JSON from TextArtifact
                            camera_data = json.loads(item.value)
                            if 'name' in camera_data:
                                available_cameras.append(camera_data['name'])
                        except (json.JSONDecodeError, AttributeError):
                            # Skip invalid items
                            continue
                
                if camera_name not in available_cameras and available_cameras:
                    camera_name = available_cameras[0]  # Use first available camera

            # Update camera metadata display 
            self._update_camera_metadata_display()

            # Update status
            self.parameter_output_values["status_output"] = f"Capturing frame from camera '{camera_name}'..."

            # Call socket server to render camera
            try:
                # Use shorter timeout for large scenes to prevent dependency graph crashes
                from socket_client import BlenderSocketClient
                client = BlenderSocketClient(timeout=90)  # 90 second timeout for rendering
                result = client.render_camera(
                    camera_name=camera_name,
                    width=resolution_x,
                    height=resolution_y,
                    format_type=output_format.upper(),
                    quality=quality
                )
            except Exception as e:
                error_msg = f"Render operation failed or timed out: {str(e)}"
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            if not result.get("success"):
                error_msg = result.get("error", "Unknown error from Blender socket server")
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Get the actual render result from execute_code response
            render_result = result.get("result", {})
            if not render_result.get("success"):
                error_msg = render_result.get("error", "Render failed in Blender")
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Get image data from render result
            image_b64 = render_result.get("image")
            if not image_b64:
                error_msg = "No image data received from Blender socket server"
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Decode base64 image data
            try:
                image_data = base64.b64decode(image_b64)
            except Exception as decode_error:
                error_msg = f"Failed to decode image data: {str(decode_error)}"
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Validate image data
            if not image_data or len(image_data) < 100:
                error_msg = "Received empty or corrupted image data"
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Save image
            try:
                dest = self._output_file.build_file()
                saved = dest.write_bytes(image_data)
            except Exception as save_error:
                error_msg = f"Failed to save image: {str(save_error)}"
                self.parameter_output_values["status_output"] = f"Error: {error_msg}"
                self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)
                return

            # Create ImageUrlArtifact and set output
            image_artifact = ImageUrlArtifact(value=saved.location, name=f"blender_capture_{camera_name}")
            self.parameter_output_values["image_output"] = image_artifact

            # Update status with success info
            render_time = render_result.get("render_time", 0)
            engine = "BLENDER_WORKBENCH"  # We know this from the render code
            actual_width = render_result.get("width", resolution_x)
            actual_height = render_result.get("height", resolution_y)
            
            status_msg = f"Successfully captured {actual_width}x{actual_height} {output_format} image from camera '{camera_name}'\n"
            status_msg += f"Render time: {render_time:.2f}s, Engine: {engine}"
            self.parameter_output_values["status_output"] = status_msg

        except Exception as e:
            error_msg = f"Failed to capture frame: {str(e)}"
            logger.error(f"BlenderCameraCapture error: {error_msg}")
            self.parameter_output_values["status_output"] = f"Error: {error_msg}"
            self.parameter_output_values["image_output"] = ErrorArtifact(error_msg)

    def after_value_set(self, parameter, value, modified_parameters_set):
        """Update camera choices when cameras_input receives new data."""
        if parameter.name == "cameras_input" and value:
            try:
                # Extract camera names from the ListArtifact
                if hasattr(value, 'value') and isinstance(value.value, list):
                    # Parse camera data from TextArtifacts containing JSON
                    camera_names = []
                    for i, item in enumerate(value.value):
                        if hasattr(item, 'value'):
                            try:
                                # Each item should be a TextArtifact with JSON camera data
                                camera_data = json.loads(item.value)
                                if 'name' in camera_data:
                                    camera_names.append(camera_data['name'])
                            except (json.JSONDecodeError, AttributeError) as e:
                                # Skip invalid items
                                continue
                    
                    if camera_names:
                        # Update current instance camera parameter choices
                        camera_param = self.get_parameter_by_name("camera_name")
                        if camera_param:
                            # First update the choices
                            success = self._update_camera_choices(camera_param, camera_names)
                            if success:
                                # Now check if current selection is still valid and update if needed
                                current_camera = self.get_parameter_value("camera_name")
                                if current_camera not in camera_names:
                                    # Current selection is invalid, switch to first available camera
                                    # Use direct parameter value setting to bypass validation temporarily
                                    camera_param.default_value = camera_names[0]
                                    if hasattr(camera_param, 'value'):
                                        camera_param.value = camera_names[0]
                                    self.parameter_values["camera_name"] = camera_names[0]
                                    modified_parameters_set.add("camera_name")
                                
                                # Include the parameter in modified_parameters_set to trigger UI update
                                modified_parameters_set.add("camera_name")
                            
                            # Note: Not updating all other instances here to prevent feedback loops
                            # Other instances will get updated when their own cameras_input changes
                        
                        # Update camera metadata display after camera list is updated
                        try:
                            self._update_camera_metadata_display(modified_parameters_set)
                        except Exception as metadata_error:
                            # Don't let metadata errors break the camera list update
                            pass
                            
            except Exception as e:
                # Don't let processing errors break the node
                pass
                
        elif parameter.name == "camera_name":
            # Update metadata display when camera selection changes
            try:
                self._update_camera_metadata_display(modified_parameters_set)
            except Exception as metadata_error:
                # Don't let metadata errors break camera selection
                pass

    def _update_camera_choices(self, camera_param, camera_names):
        """Helper method to update camera choices for a parameter."""
        try:
            # Try to find existing Options trait
            options_trait = None
            trait_found = False
            
            # Check multiple possible trait storage locations
            trait_collections = []
            if hasattr(camera_param, 'traits') and camera_param.traits:
                trait_collections.append(camera_param.traits)
            if hasattr(camera_param, '_traits') and camera_param._traits:
                trait_collections.append(camera_param._traits)
            
            # Look for Options trait in all collections
            for traits_collection in trait_collections:
                for trait in traits_collection:
                    if hasattr(trait, 'choices'):
                        trait.choices = camera_names
                        trait_found = True
                        break
                if trait_found:
                    break
            
            # If no traits found, try to create and add one
            if not trait_found:
                try:
                    from griptape_nodes.traits.options import Options
                    new_options_trait = Options(choices=camera_names)
                    
                    # Try different ways to add the trait
                    if hasattr(camera_param, 'add_trait'):
                        camera_param.add_trait(new_options_trait)
                        trait_found = True
                    elif hasattr(camera_param, 'traits'):
                        if camera_param.traits is None:
                            camera_param.traits = set()
                        camera_param.traits.add(new_options_trait)
                        trait_found = True
                    else:
                        if not hasattr(camera_param, '_traits'):
                            camera_param._traits = set()
                        camera_param._traits.add(new_options_trait)
                        trait_found = True
                        
                except Exception as e:
                    # If we can't create/add traits, that's ok - we'll work without validation
                    pass
            
            return trait_found
        except Exception as e:
            # Return False if update failed, but don't crash
            return False

    @classmethod
    def _update_all_camera_lists_with_names(cls, camera_names, skip_instance=None):
        """Update camera lists for all instances with provided camera names."""
        for i, instance in enumerate(cls._instances):
            if instance and instance != skip_instance:  # Skip the instance that was already updated
                camera_param = instance.get_parameter_by_name("camera_name")
                if camera_param:
                    instance._update_camera_choices(camera_param, camera_names)

    def after_incoming_connection(self, source_node, source_parameter, target_parameter, modified_parameters_set=None):
        """Refresh camera list when connections are made."""
        if target_parameter.name == "camera_name":
            # Camera updates now handled by BlenderCameraList node via cameras_input
            pass
        elif target_parameter.name == "cameras_input":
            # Camera list data will flow through this connection automatically
            pass

    def _update_camera_metadata_display(self, modified_parameters_set=None):
        """Update the camera metadata label parameters based on current selection and available data."""
        camera_name = self.get_parameter_value("camera_name") or "Camera"
        cameras_input = self.get_parameter_value("cameras_input")
        
        # Find the selected camera's data
        camera_data = None
        if cameras_input and hasattr(cameras_input, 'value'):
            for item in cameras_input.value:
                if hasattr(item, 'value'):
                    try:
                        parsed_data = json.loads(item.value)
                        if parsed_data.get('name') == camera_name:
                            camera_data = parsed_data
                            break
                    except (json.JSONDecodeError, AttributeError):
                        continue
        
        if camera_data and 'focal_length' in camera_data:
            # Enhanced camera data available - show detailed labels
            
            # Status label
            status_text = "✓ Active Scene Camera" if camera_data.get('active') else "Available Camera"
            self.set_parameter_value("camera_status_label", status_text)
            
            # Focal length label
            focal_length = camera_data.get('focal_length', 50.0)
            self.set_parameter_value("focal_length_label", f"{focal_length} mm")
            
            # Sensor info label
            sensor_w = camera_data.get('sensor_width', 36.0)
            sensor_h = camera_data.get('sensor_height', 24.0) 
            sensor_fit = camera_data.get('sensor_fit', 'AUTO')
            cam_type = camera_data.get('type', 'PERSP')
            sensor_text = f"{sensor_w}×{sensor_h}mm, {sensor_fit}, {cam_type}"
            self.set_parameter_value("sensor_info_label", sensor_text)
            
            # Depth of field label - fix structure mismatch
            dof = camera_data.get('depth_of_field', {})
            if dof.get('enabled'):
                focus_dist = dof.get('focus_distance', 10.0)
                f_stop = dof.get('f_stop', 2.8)
                dof_text = f"Enabled: {focus_dist}BU @ f/{f_stop}"
            else:
                dof_text = "Disabled"
            self.set_parameter_value("dof_info_label", dof_text)
            
            # Transform label
            location = camera_data.get('location', {})
            rotation = camera_data.get('rotation', {})
            loc_text = f"({location.get('x', 0.0):.2f}, {location.get('y', 0.0):.2f}, {location.get('z', 0.0):.2f})"
            rot_text = f"({rotation.get('x', 0.0):.2f}, {rotation.get('y', 0.0):.2f}, {rotation.get('z', 0.0):.2f})"
            transform_text = f"Loc: {loc_text} Rot: {rot_text}"
            self.set_parameter_value("transform_info_label", transform_text)
            
        elif camera_data:
            # Basic camera data available - show what we can
            status_text = "✓ Active Scene Camera" if camera_data.get('active') else "Available Camera"
            self.set_parameter_value("camera_status_label", status_text)
            
            # Show basic location/rotation if available
            location = camera_data.get('location', {})
            rotation = camera_data.get('rotation', {})
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
            
        # Mark all label parameters as modified for UI updates
        if modified_parameters_set is not None:
            modified_parameters_set.update([
                "camera_status_label", "focal_length_label", "sensor_info_label",
                "dof_info_label", "transform_info_label"
            ]) 