"""The camera dropdown the nodes that pick a camera share: where its choices come from, and how a node takes them."""

import json
from typing import Any

import blender_mcp_client
import camera_client
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.traits.options import Options

# The choice that stands for the scene's active camera, for nodes where that is a sensible thing to pick.
ACTIVE_CAMERA = "Active camera"


def names_from_list(cameras: Any) -> list[str]:
    """The camera names in the output of Camera List, which is a list of text artifacts holding JSON."""
    names = []
    for item in getattr(cameras, "value", None) or []:
        try:
            name = json.loads(item.value).get("name")
        except (AttributeError, TypeError, json.JSONDecodeError):
            continue
        if name:
            names.append(name)
    return names


def fetch_names(port: int = blender_mcp_client.DEFAULT_PORT) -> list[str]:
    """The cameras in the scene open in Blender, or none when Blender cannot be reached."""
    try:
        return [camera["name"] for camera in camera_client.list_cameras(port=port)["cameras"]]
    except Exception:  # noqa: BLE001
        return []


def apply(node: BaseNode, parameter_name: str, choices: list[str], default: str) -> None:
    """Offer `choices` on the node's parameter, and move its value to `default` when the value is not one of them."""
    parameter = node.get_parameter_by_name(parameter_name)
    traits = parameter.find_elements_by_type(Options) if parameter else []
    if not parameter or not traits:
        return
    traits[0].choices = choices
    if node.get_parameter_value(parameter_name) not in choices:
        parameter.default_value = default
        node.set_parameter_value(parameter_name, default)
