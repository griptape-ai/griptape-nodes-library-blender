"""Send the camera plane code to an open Blender through the Blender Lab MCP add-on."""

import json
from pathlib import Path
from typing import Any

import blender_mcp_client

SCENE_SCRIPT = Path(__file__).with_name("camera_plane_scene.py")

# These mirror the constants in camera_plane_scene.py. That file needs bpy, so the engine cannot import it.
DEFAULT_NAME = "Camera Plane"
FIT_FILL = "fill"
FIT_CONTAIN = "fit"
FIT_STRETCH = "stretch"


def _run(call: str, arguments: dict[str, Any], host: str, port: int) -> dict[str, Any]:
    code = (
        SCENE_SCRIPT.read_text(encoding="utf-8")
        + f"\n\nimport json as _json\nresult = {call}(**_json.loads({json.dumps(json.dumps(arguments))}))\n"
    )
    return blender_mcp_client.execute(code, host, port)


def add_camera_plane(  # noqa: PLR0913
    path: str,
    *,
    camera: str = "",
    scene: str = "",
    distance: float = 10.0,
    fit: str = FIT_FILL,
    name: str = DEFAULT_NAME,
    replace: bool = True,
    host: str = blender_mcp_client.DEFAULT_HOST,
    port: int = blender_mcp_client.DEFAULT_PORT,
) -> dict[str, Any]:
    """Add the image or movie at `path` to a camera in the open scene as a plane, and describe the plane."""
    arguments = {
        "path": path,
        "camera": camera,
        "scene": scene,
        "distance": distance,
        "fit": fit,
        "name": name,
        "replace": replace,
    }
    return _run("add_camera_plane", arguments, host, port)


def get_camera_plane(
    *,
    camera: str = "",
    scene: str = "",
    export_path: str = "",
    host: str = blender_mcp_client.DEFAULT_HOST,
    port: int = blender_mcp_client.DEFAULT_PORT,
) -> dict[str, Any]:
    """Describe the image plane on a camera in the open scene and say where its image or movie is."""
    arguments = {"camera": camera, "scene": scene, "export_path": export_path}
    return _run("get_camera_plane", arguments, host, port)
