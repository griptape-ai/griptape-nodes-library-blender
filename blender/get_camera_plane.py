from pathlib import Path

import blender_mcp_client
import camera_plane_client
from griptape.artifacts import ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from media_files import project_path, save_project_file

MOVIE_SOURCES = {"MOVIE", "SEQUENCE"}


class BlenderGetCameraPlane(SuccessFailureNode):
    """Get the image or movie on a Blender camera's image plane, from the scene open in Blender."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = (
            "Gets the image or movie from the image plane on the active camera of the scene open in Blender."
        )

        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterString(
                name="camera",
                display_name="Camera",
                tooltip="The camera to read. Leave empty for the scene's active camera.",
                default_value="",
                placeholder_text="Active camera",
            )
            ParameterString(
                name="scene",
                display_name="Scene",
                tooltip="The scene to work in. Leave empty for the scene showing in Blender's window.",
                default_value="",
                placeholder_text="Open scene",
            )
            ParameterInt(
                name="mcp_port",
                display_name="MCP Port",
                tooltip="Port of the Blender Lab MCP add-on's server.",
                default_value=blender_mcp_client.DEFAULT_PORT,
            )
        self.add_node_element(blender_group)

        self.add_parameter(
            Parameter(
                name="image",
                display_name="Image",
                type="ImageUrlArtifact",
                output_type="ImageUrlArtifact",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="The plane's image. Empty when the plane shows a movie.",
                ui_options={"is_full_width": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="video",
                display_name="Movie",
                type="VideoUrlArtifact",
                output_type="VideoUrlArtifact",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="The plane's movie. Empty when the plane shows an image.",
                ui_options={"is_full_width": True},
            )
        )
        self.add_parameter(
            ParameterString(
                name="plane_name",
                display_name="Plane",
                tooltip="The plane's name in Blender.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="camera_name",
                display_name="Camera Used",
                tooltip="The camera the plane is on.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterFloat(
                name="distance",
                display_name="Distance",
                tooltip="How far in front of the lens the plane sits, in scene units.",
                allow_input=False,
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="media_path",
                display_name="File",
                tooltip="The file the plane shows, where Blender has it.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self._create_status_parameters(
            result_details_tooltip="What was found.",
            result_details_placeholder="Details appear here.",
        )

    def process(self) -> AsyncResult[None]:
        yield lambda: self._process()

    def _process(self) -> None:
        self._clear_execution_status()
        try:
            details = self._get()
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        self._set_status_results(was_successful=True, result_details=details)

    def _get(self) -> str:
        value = self.get_parameter_value
        try:
            found = camera_plane_client.get_camera_plane(
                camera=(value("camera") or "").strip(),
                scene=(value("scene") or "").strip(),
                # Where Blender saves an image that has no file of its own, such as one packed in the .blend.
                export_path=str(project_path(self.name, "camera_plane_export.png")),
                port=int(value("mcp_port")),
            )
        except blender_mcp_client.BlenderMCPError as e:
            msg = f"Could not read the plane in Blender.\n{e}"
            raise blender_mcp_client.BlenderMCPError(msg) from e

        path = Path(found["path"])
        if not path.is_file():
            msg = f"Blender has the file at {path}, but it is not on this machine."
            raise FileNotFoundError(msg)
        is_movie = found["source"] in MOVIE_SOURCES
        saved = save_project_file(self.name, f"camera_plane{path.suffix or '.png'}", path.read_bytes())
        self.parameter_output_values["image"] = None if is_movie else ImageUrlArtifact(value=saved.location)
        self.parameter_output_values["video"] = VideoUrlArtifact(value=saved.location) if is_movie else None
        self.parameter_output_values["plane_name"] = found["plane"]
        self.parameter_output_values["camera_name"] = found["camera"]
        self.parameter_output_values["distance"] = found["distance"]
        self.parameter_output_values["media_path"] = str(path)

        kind = f"a movie of {found['frames']} frames" if is_movie else "an image"
        lines = [f"Found {kind} on '{found['plane']}', on the camera '{found['camera']}'."]
        if len(found["planes"]) > 1:
            lines.append(
                f"The camera has {len(found['planes'])} image planes ({', '.join(found['planes'])}). This is the first."
            )
        if not found["made_by_node"]:
            lines.append("This plane was not made by the Image to Camera Plane node.")
        if found["exported"]:
            lines.append("The image was not saved to a file in Blender, so it was exported.")
        return "\n".join(lines)
