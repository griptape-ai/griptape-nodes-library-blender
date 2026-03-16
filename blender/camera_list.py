import json
from typing import Any

from griptape.artifacts import ErrorArtifact, ListArtifact, TextArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.retained_mode.griptape_nodes import logger

# Import socket client utilities
from socket_client import get_scene_info, list_cameras


class BlenderCameraList(ControlNode):
    def __init__(self, **kwargs) -> None:
        # Initialize private state variable
        self._internal_state = None
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Lists all available cameras in the current Blender scene."
        self.metadata["author"] = "Griptape"
        self.metadata["dependencies"] = {}

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

    def _fetch_cameras(self) -> tuple[list[dict], str]:
        """Fetch comprehensive camera information from Blender socket server."""
        try:
            # Use execute_code to get detailed camera information with safety measures
            camera_info_code = """
import bpy
import json

try:
    cameras = []
    scene = bpy.context.scene

    # Get active camera safely first (without dependency graph stress)
    active_camera_name = None
    try:
        if scene.camera:
            active_camera_name = scene.camera.name
    except:
        pass  # Ignore active camera detection errors

    camera_objects = [obj for obj in bpy.data.objects if obj.type == 'CAMERA']

    # If too many cameras, use simplified data collection
    if len(camera_objects) > 20:
        print(f"DEBUG: Large scene detected ({len(camera_objects)} cameras), using simplified collection")
        for obj in camera_objects:
            try:
                camera_data = obj.data
                camera_info = {
                    "name": obj.name,
                    "location": list(obj.location),
                    "rotation": list(obj.rotation_euler),
                    "active": obj.name == active_camera_name,
                    "focal_length": camera_data.lens,
                    "sensor_width": camera_data.sensor_width,
                    "sensor_height": camera_data.sensor_height,
                    "sensor_fit": camera_data.sensor_fit,
                    "type": camera_data.type,
                    "data_format": "simplified"
                }
                cameras.append(camera_info)
            except Exception as e:
                print(f"DEBUG: Error collecting data for camera {obj.name}: {e}")
                # Add minimal data if there's an error
                cameras.append({
                    "name": obj.name,
                    "location": [0, 0, 0],
                    "rotation": [0, 0, 0],
                    "active": False,
                    "data_format": "minimal_error"
                })
    else:
        # Full data collection for smaller scenes
        for obj in camera_objects:
            try:
                camera_data = obj.data

                # Basic transform data (safe)
                camera_info = {
                    "name": obj.name,
                    "location": list(obj.location),
                    "rotation": list(obj.rotation_euler),
                    "scale": list(obj.scale),
                    "active": obj.name == active_camera_name,

                    # Camera-specific properties (safe)
                    "focal_length": camera_data.lens,
                    "sensor_width": camera_data.sensor_width,
                    "sensor_height": camera_data.sensor_height,
                    "sensor_fit": camera_data.sensor_fit,

                    # Clipping and depth (safe)
                    "clip_start": camera_data.clip_start,
                    "clip_end": camera_data.clip_end,

                    # Camera type and angle (safe)
                    "type": camera_data.type,

                    # Shift (safe)
                    "shift_x": camera_data.shift_x,
                    "shift_y": camera_data.shift_y,
                    "passepartout_alpha": camera_data.passepartout_alpha,

                    "data_format": "enhanced"
                }

                # Add optional properties with individual error handling
                try:
                    camera_info["angle"] = camera_data.angle if hasattr(camera_data, 'angle') else None
                    camera_info["angle_x"] = camera_data.angle_x if hasattr(camera_data, 'angle_x') else None
                    camera_info["angle_y"] = camera_data.angle_y if hasattr(camera_data, 'angle_y') else None
                except:
                    pass

                # Depth of field (with error handling)
                try:
                    if hasattr(camera_data, 'dof'):
                        camera_info["dof_use"] = camera_data.dof.use_dof
                        camera_info["dof_focus_distance"] = camera_data.dof.focus_distance
                        camera_info["dof_aperture_fstop"] = camera_data.dof.aperture_fstop
                    else:
                        camera_info["dof_use"] = False
                        camera_info["dof_focus_distance"] = None
                        camera_info["dof_aperture_fstop"] = None
                except:
                    camera_info["dof_use"] = False
                    camera_info["dof_focus_distance"] = None
                    camera_info["dof_aperture_fstop"] = None

                # Background images (with error handling)
                try:
                    camera_info["background_images_count"] = len(camera_data.background_images) if hasattr(camera_data, 'background_images') else 0
                except:
                    camera_info["background_images_count"] = 0

                # Matrix world (potentially dependency-sensitive, with error handling)
                try:
                    matrix_world = obj.matrix_world
                    camera_info["matrix_world"] = [list(row) for row in matrix_world]
                except Exception as e:
                    print(f"DEBUG: Skipping matrix_world for {obj.name} due to error: {e}")
                    camera_info["matrix_world"] = None

                cameras.append(camera_info)

            except Exception as e:
                print(f"DEBUG: Error collecting full data for camera {obj.name}: {e}")
                # Add minimal data if there's an error
                try:
                    cameras.append({
                        "name": obj.name,
                        "location": list(obj.location),
                        "rotation": list(obj.rotation_euler),
                        "active": obj.name == active_camera_name,
                        "data_format": "fallback_error"
                    })
                except:
                    cameras.append({
                        "name": obj.name,
                        "location": [0, 0, 0],
                        "rotation": [0, 0, 0],
                        "active": False,
                        "data_format": "minimal_error"
                    })

    result = {"success": True, "cameras": cameras}

except Exception as e:
    print(f"DEBUG: Major error in camera collection: {e}")
    result = {"success": False, "error": str(e)}
"""

            result = self._execute_camera_code(camera_info_code)

            if result.get("success"):
                # Parse the result from the executed code
                if "result" in result and result["result"].get("success"):
                    cameras = result["result"].get("cameras", [])
                    if cameras:
                        # Check data format to provide appropriate status
                        data_formats = {cam.get("data_format", "unknown") for cam in cameras}
                        if "simplified" in data_formats:
                            status = f"Successfully found {len(cameras)} camera(s) - used simplified collection for large scene"
                        elif "enhanced" in data_formats:
                            status = f"Successfully found {len(cameras)} camera(s) with detailed information"
                        else:
                            status = f"Successfully found {len(cameras)} camera(s) with basic information"
                        return cameras, status
                    else:
                        status = "No cameras found in the current Blender scene"
                        return [], status
                else:
                    # Fallback to simple list_cameras if enhanced version fails
                    return self._fetch_cameras_simple()
            else:
                # Fallback to simple list_cameras if code execution fails
                return self._fetch_cameras_simple()

        except Exception as e:
            f"Failed to fetch enhanced camera data: {str(e)}"
            # Fallback to simple method
            return self._fetch_cameras_simple()

    def _execute_camera_code(self, code: str) -> dict[str, Any]:
        """Execute Python code in Blender to get camera information with safety timeout."""
        from socket_client import BlenderSocketClient

        try:
            # Use a shorter timeout for camera operations to prevent hanging
            # on complex scenes that might cause dependency graph issues
            client = BlenderSocketClient(timeout=30)  # 30 second timeout instead of default 60
            return client.execute_code(code)
        except Exception as e:
            return {"success": False, "error": f"Camera code execution failed: {str(e)}"}

    def _fetch_cameras_simple(self) -> tuple[list[dict], str]:
        """Fallback method using the simple list_cameras command."""
        try:
            result = list_cameras()

            if result.get("success"):
                cameras = result.get("cameras", [])

                if cameras:
                    status = f"Successfully found {len(cameras)} camera(s) in the scene (basic info)"
                    return cameras, status
                else:
                    status = "No cameras found in the current Blender scene"
                    return [], status
            else:
                error_msg = f"Blender socket server error: {result.get('error', 'Unknown error')}"
                return [], error_msg

        except Exception as e:
            error_msg = f"Failed to fetch cameras: {str(e)}"
            return [], error_msg

    def _format_camera_info(self, cameras: list[dict]) -> list[dict]:
        """Format camera information for better readability."""
        formatted_cameras = []

        for camera in cameras:
            # Handle both enhanced and simple camera data formats
            if "focal_length" in camera:
                # Enhanced camera data with detailed properties
                formatted_camera = {
                    "name": camera.get("name", "Unknown"),
                    "location": {
                        "x": round(camera.get("location", [0, 0, 0])[0], 3),
                        "y": round(camera.get("location", [0, 0, 0])[1], 3),
                        "z": round(camera.get("location", [0, 0, 0])[2], 3),
                    },
                    "rotation": {
                        "x": round(camera.get("rotation", [0, 0, 0])[0], 3),
                        "y": round(camera.get("rotation", [0, 0, 0])[1], 3),
                        "z": round(camera.get("rotation", [0, 0, 0])[2], 3),
                    },
                    "scale": {
                        "x": round(camera.get("scale", [1, 1, 1])[0], 3),
                        "y": round(camera.get("scale", [1, 1, 1])[1], 3),
                        "z": round(camera.get("scale", [1, 1, 1])[2], 3),
                    },
                    "active": camera.get("active", False),
                    # Lens and sensor properties
                    "focal_length": round(camera.get("focal_length", 50.0), 2),
                    "sensor_width": round(camera.get("sensor_width", 36.0), 2),
                    "sensor_height": round(camera.get("sensor_height", 24.0), 2),
                    "sensor_fit": camera.get("sensor_fit", "AUTO"),
                    # Camera type and angles
                    "type": camera.get("type", "PERSP"),
                    "angle": round(camera.get("angle", 0.0), 4) if camera.get("angle") else None,
                    "angle_x": round(camera.get("angle_x", 0.0), 4) if camera.get("angle_x") else None,
                    "angle_y": round(camera.get("angle_y", 0.0), 4) if camera.get("angle_y") else None,
                    # Clipping distances
                    "clip_start": round(camera.get("clip_start", 0.1), 3),
                    "clip_end": round(camera.get("clip_end", 1000.0), 1),
                    # Depth of field
                    "depth_of_field": {
                        "enabled": camera.get("dof_use", False),
                        "focus_distance": round(camera.get("dof_focus_distance", 10.0), 2)
                        if camera.get("dof_focus_distance")
                        else None,
                        "f_stop": round(camera.get("dof_aperture_fstop", 2.8), 2)
                        if camera.get("dof_aperture_fstop")
                        else None,
                    },
                    # Shift and composition
                    "shift": {"x": round(camera.get("shift_x", 0.0), 3), "y": round(camera.get("shift_y", 0.0), 3)},
                    "passepartout_alpha": round(camera.get("passepartout_alpha", 0.5), 2),
                    # Additional info
                    "background_images_count": camera.get("background_images_count", 0),
                    "matrix_world": camera.get("matrix_world"),  # Keep full precision for matrix
                }
            else:
                # Simple camera data (fallback format)
                formatted_camera = {
                    "name": camera.get("name", "Unknown"),
                    "location": {
                        "x": round(camera.get("location", [0, 0, 0])[0], 3),
                        "y": round(camera.get("location", [0, 0, 0])[1], 3),
                        "z": round(camera.get("location", [0, 0, 0])[2], 3),
                    },
                    "rotation": {
                        "x": round(camera.get("rotation", [0, 0, 0])[0], 3),
                        "y": round(camera.get("rotation", [0, 0, 0])[1], 3),
                        "z": round(camera.get("rotation", [0, 0, 0])[2], 3),
                    },
                    "active": camera.get("active", False),
                    "data_format": "basic",
                }

            formatted_cameras.append(formatted_camera)

        return formatted_cameras

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate that Blender socket server is available before running."""
        is_connected, message = self._check_blender_connection()
        if not is_connected:
            return [ConnectionError(message)]
        return None

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
        try:
            # Update status
            self.parameter_output_values["status_output"] = "Fetching cameras from Blender..."

            # Fetch cameras from Blender
            cameras, status_msg = self._fetch_cameras()

            if cameras:
                # Format camera information
                formatted_cameras = self._format_camera_info(cameras)

                # Convert camera dictionaries to TextArtifacts for ListArtifact
                camera_artifacts = []
                for camera in formatted_cameras:
                    camera_json = json.dumps(camera, indent=2)
                    camera_artifact = TextArtifact(value=camera_json, name=f"camera_{camera['name']}")
                    camera_artifacts.append(camera_artifact)

                # Update outputs
                self.parameter_output_values["camera_count"] = len(formatted_cameras)
                self.parameter_output_values["status_output"] = status_msg

                # Create detailed camera list artifact
                self.parameter_output_values["cameras_output"] = ListArtifact(camera_artifacts, name="blender_cameras")

                # Propagate camera names to all BlenderCameraCapture nodes to refresh their dropdowns
                try:
                    from camera_capture import BlenderCameraCapture

                    camera_names = [cam["name"] for cam in formatted_cameras]
                    BlenderCameraCapture._update_all_camera_lists_with_names(camera_names)
                except Exception as e:
                    # Non-fatal if capture class isn't loaded yet
                    logger.debug(f"Camera list propagation skipped: {e}")

            else:
                # No cameras found or error occurred
                self.parameter_output_values["camera_count"] = 0
                self.parameter_output_values["status_output"] = status_msg

                if "error" in status_msg.lower() or "failed" in status_msg.lower():
                    self.parameter_output_values["cameras_output"] = ErrorArtifact(status_msg)
                else:
                    # No cameras but no error
                    self.parameter_output_values["cameras_output"] = ListArtifact([])

        except Exception as e:
            error_msg = f"Failed to list cameras: {str(e)}"
            logger.error(f"BlenderCameraList error: {error_msg}")
            self.parameter_output_values["status_output"] = f"Error: {error_msg}"
            self.parameter_output_values["camera_count"] = 0
            self.parameter_output_values["cameras_output"] = ErrorArtifact(error_msg)
