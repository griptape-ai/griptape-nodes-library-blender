"""Send the camera code to an open Blender through the Blender Lab MCP add-on."""

import json
from pathlib import Path
from typing import Any

import blender_mcp_client

SCENE_SCRIPT = Path(__file__).with_name("camera_scene.py")


def _run(call: str, arguments: dict[str, Any], host: str, port: int) -> dict[str, Any]:
    code = (
        SCENE_SCRIPT.read_text(encoding="utf-8")
        + f"\n\nimport json as _json\nresult = {call}(**_json.loads({json.dumps(json.dumps(arguments))}))\n"
    )
    return blender_mcp_client.execute(code, host, port)


def list_cameras(
    host: str = blender_mcp_client.DEFAULT_HOST,
    port: int = blender_mcp_client.DEFAULT_PORT,
) -> dict[str, Any]:
    """Describe the cameras in the file open in Blender, and say which Blender it is."""
    return _run("list_cameras", {}, host, port)


def render_camera(
    camera: str,
    width: int,
    height: int,
    host: str = blender_mcp_client.DEFAULT_HOST,
    port: int = blender_mcp_client.DEFAULT_PORT,
) -> dict[str, Any]:
    """Render a frame from `camera` in the open Blender. The PNG comes back base64 encoded under `image`."""
    return _run("render_camera", {"camera": camera, "width": width, "height": height}, host, port)
