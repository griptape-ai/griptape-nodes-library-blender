from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import blender_mcp_client
import camera_plane_client
from camera_plane_client import DEFAULT_NAME, FIT_CONTAIN, FIT_FILL, FIT_STRETCH
from griptape.artifacts import ImageArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.files.file import File
from griptape_nodes.traits.options import Options
from media_files import hashed_filename, save_project_file

FIT_LABELS = {
    "Fill the frame (the overflow falls outside it)": FIT_FILL,
    "Fit inside the frame (the whole image shows)": FIT_CONTAIN,
    "Stretch to the frame": FIT_STRETCH,
}
MEDIA_TYPES = ["ImageArtifact", "ImageUrlArtifact", "VideoUrlArtifact", "VideoArtifact", "str"]


def is_remote(location: str) -> bool:
    """Whether `location` is a web address, which Blender cannot open as a file."""
    return location.startswith(("http://", "https://"))


class BlenderImageToCameraPlane(SuccessFailureNode):
    """Add an image or a movie to a Blender camera as a plane that fills its frame, in the scene open in Blender."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Adds an image or movie to the active camera of the scene open in Blender as a plane."

        self.add_parameter(
            Parameter(
                name="media",
                display_name="Image or Movie",
                tooltip="The image or movie to put on the camera.",
                input_types=MEDIA_TYPES,
                type="ImageUrlArtifact",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            ParameterFloat(
                name="distance",
                display_name="Distance",
                tooltip=(
                    "How far in front of the lens the plane sits, in scene units. The plane is sized to fill the "
                    "frame at that distance, so it looks the same at any distance unless something else is in front."
                ),
                default_value=10.0,
                min_val=0.001,
            )
        )
        self.add_parameter(
            ParameterString(
                name="fit",
                display_name="Fit",
                tooltip="How the image sits in the camera's frame when their shapes differ.",
                default_value=next(iter(FIT_LABELS)),
                traits={Options(choices=list(FIT_LABELS))},
                allow_output=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="plane_name",
                display_name="Plane Name",
                tooltip="Name of the plane in Blender.",
                default_value=DEFAULT_NAME,
            )
        )
        self.add_parameter(
            ParameterBool(
                name="replace",
                display_name="Replace Existing",
                tooltip=(
                    "Replace the plane this node made earlier on the same camera with the same name, rather than "
                    "adding another. Planes you made yourself are never replaced."
                ),
                default_value=True,
            )
        )

        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterString(
                name="camera",
                display_name="Camera",
                tooltip="The camera to put the plane on. Leave empty for the scene's active camera.",
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
            ParameterString(
                name="plane_name_output",
                display_name="Plane",
                tooltip="The plane's name in Blender.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="media_path",
                display_name="File",
                tooltip="The file the plane shows.",
                allow_input=False,
                placeholder_text="Appears after a run",
                allow_property=False,
            )
        )
        self._create_status_parameters(
            result_details_tooltip="What was added and where.",
            result_details_placeholder="Details appear here.",
        )

    def process(self) -> AsyncResult[None]:
        yield lambda: self._process()

    def _process(self) -> None:
        self._clear_execution_status()
        try:
            details = self._add()
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        self._set_status_results(was_successful=True, result_details=details)

    def _media_file(self, media: Any) -> str:
        """A path on this machine for `media`. A file already on disk is used where it is, and anything else is saved."""
        if isinstance(media, ImageArtifact):
            return save_project_file(
                self.name, hashed_filename("camera_plane", media.value, ".png"), media.value
            ).resolve()
        location = media if isinstance(media, str) else getattr(media, "value", None)
        if not isinstance(location, str) or not location:
            msg = "Connect an image or a movie."
            raise ValueError(msg)
        resolved = File(location).resolve()
        if not is_remote(resolved):
            if not Path(resolved).is_file():
                msg = f"The file {resolved} does not exist."
                raise FileNotFoundError(msg)
            return resolved
        suffix = Path(urlparse(resolved).path).suffix or ".png"
        content = File(resolved).read_bytes()
        return save_project_file(self.name, hashed_filename("camera_plane", content, suffix), content).resolve()

    def _add(self) -> str:
        value = self.get_parameter_value
        path = self._media_file(value("media"))
        name = (value("plane_name") or DEFAULT_NAME).strip()
        try:
            placed = camera_plane_client.add_camera_plane(
                path,
                camera=(value("camera") or "").strip(),
                scene=(value("scene") or "").strip(),
                distance=float(value("distance")),
                fit=FIT_LABELS.get(value("fit"), FIT_FILL),
                name=name,
                replace=bool(value("replace")),
                port=int(value("mcp_port")),
            )
        except blender_mcp_client.BlenderMCPError as e:
            msg = f"Could not add the plane in Blender.\n{e}"
            raise blender_mcp_client.BlenderMCPError(msg) from e
        self.parameter_output_values["camera_name"] = placed["camera"]
        self.parameter_output_values["plane_name_output"] = placed["plane"]
        self.parameter_output_values["media_path"] = placed["path"]
        kind = f"a movie of {placed['frames']} frames" if placed["frames"] else "an image"
        lines = [
            f"Added {kind} to the camera '{placed['camera']}' in the scene '{placed['scene']}' as '{placed['plane']}'."
        ]
        if placed["replaced"]:
            lines.append("Replaced the plane this node made earlier.")
        lines.append(
            f"It sits {placed['distance']:g} units from the lens, {placed['width']:.3g} by {placed['height']:.3g}."
        )
        return "\n".join(lines)
