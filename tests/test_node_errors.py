"""Tests for how the camera nodes report a failure.

The socket client never raises: it returns `{"success": False, "error": ...}`. These tests answer at
the socket's lowest level, so the real client code runs, and check that each failure fails the node
with a NodeError.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import camera_capture
import camera_list
import pytest
import socket_client
from griptape.artifacts import ListArtifact
from griptape_nodes.exe_types.core_types import NodeError

LIBRARY_DIR = Path(__file__).resolve().parent.parent / "blender"

Responder = Callable[[str, dict[str, Any]], dict[str, Any]]
SCENE_INFO = {"success": True, "blender": {"version": "4.2"}}
ONE_CAMERA = {"success": True, "cameras": [{"name": "Camera", "location": [0, 0, 0], "rotation": [0, 0, 0]}]}


def _server_says(monkeypatch: pytest.MonkeyPatch, responder: Responder) -> None:
    """Answer every socket command with `responder(command, params)`, in place of a real server."""

    def send(_self: Any, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return responder(command, params or {})

    monkeypatch.setattr(socket_client.BlenderSocketClient, "_send_command", send)


def _running_server(render: dict[str, Any]) -> Responder:
    """A server that is up and has one camera, and answers a render with `render`."""

    def respond(command: str, _params: dict[str, Any]) -> dict[str, Any]:
        if command == "execute_code":
            return render
        if command == "list_cameras":
            return ONE_CAMERA
        return SCENE_INFO

    return respond


def _capture(monkeypatch: pytest.MonkeyPatch, render: dict[str, Any]) -> camera_capture.BlenderCameraCapture:
    _server_says(monkeypatch, _running_server(render))
    return camera_capture.BlenderCameraCapture(name="Capture")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("render", "message"),
    [
        pytest.param(
            {"success": False, "error": "Cannot connect to the server at 127.0.0.1:8765"},
            "Cannot connect to the server at 127.0.0.1:8765",
            id="socket-error",
        ),
        pytest.param({"success": False}, "Blender's socket server returned an error.", id="socket-error-no-reason"),
        pytest.param(
            {"success": True, "result": {"success": False, "error": "Camera 'Cam' was not found"}},
            "Camera 'Cam' was not found",
            id="render-failed",
        ),
        pytest.param(
            {"success": True, "result": {"success": True}},
            "Blender's socket server returned no image data.",
            id="no-image",
        ),
        pytest.param(
            {"success": True, "result": {"success": True, "image": base64.b64encode(b"tiny").decode()}},
            "The image data from Blender was empty or corrupted.",
            id="corrupted",
        ),
    ],
)
async def test_a_failed_capture_fails_the_node_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, render: dict[str, Any], message: str
) -> None:
    node = _capture(monkeypatch, render)

    with pytest.raises(NodeError) as raised:
        await node.aprocess()

    assert str(raised.value) == message
    assert node.parameter_output_values["status_output"] == f"Error: {message}"


@pytest.mark.asyncio
async def test_the_servers_response_is_attached_to_a_failed_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    render = {"success": True, "result": {"success": False, "error": "Camera 'Cam' was not found", "detail": 7}}
    node = _capture(monkeypatch, render)

    with pytest.raises(NodeError) as raised:
        await node.aprocess()

    assert raised.value.response == render["result"]


@pytest.mark.asyncio
async def test_undecodable_image_data_fails_the_node_and_keeps_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _capture(monkeypatch, {"success": True, "result": {"success": True, "image": "abc"}})

    with pytest.raises(NodeError, match="Could not decode the image data from Blender") as raised:
        await node.aprocess()

    assert raised.value.__cause__ is not None


@pytest.mark.asyncio
async def test_a_render_exception_fails_the_node_and_keeps_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _capture(monkeypatch, {"success": True, "result": {"success": True}})

    def broken(*_: Any, **__: Any) -> dict[str, Any]:
        raise KeyError("unexpected")

    monkeypatch.setattr(socket_client.BlenderSocketClient, "render_camera", broken)

    with pytest.raises(Exception, match="Render operation failed or timed out: 'unexpected'") as raised:
        await node.aprocess()

    assert isinstance(raised.value, NodeError)
    assert isinstance(raised.value.__cause__, KeyError)


@pytest.mark.asyncio
async def test_an_unexpected_error_after_the_render_is_raised_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    image = base64.b64encode(b"x" * 200).decode()
    node = _capture(monkeypatch, {"success": True, "result": {"success": True, "image": image}})

    def disk_full() -> Any:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(node._output_file, "build_file", disk_full)

    with pytest.raises(OSError, match="disk full") as raised:
        await node.aprocess()

    assert not isinstance(raised.value, NodeError)
    assert node.parameter_output_values["status_output"] == "Error: Failed to capture frame: disk full"


@pytest.mark.parametrize(
    ("node_class", "module"),
    [
        pytest.param(camera_capture.BlenderCameraCapture, camera_capture, id="capture"),
        pytest.param(camera_list.BlenderCameraList, camera_list, id="list"),
    ],
)
def test_a_missing_server_is_reported_as_a_node_error_before_the_node_runs(
    monkeypatch: pytest.MonkeyPatch, node_class: type, module: Any
) -> None:
    _server_says(monkeypatch, _running_server({}))
    node = node_class(name="Node")
    monkeypatch.setattr(module, "get_scene_info", lambda: {"success": False, "error": "Cannot connect to the server"})

    errors = node.validate_before_node_run()

    assert errors is not None
    assert len(errors) == 1
    assert isinstance(errors[0], NodeError)
    assert "Cannot connect to the server" in str(errors[0])


def test_a_listing_failure_fails_the_node_and_asks_the_server_once(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def respond(command: str, _params: dict[str, Any]) -> dict[str, Any]:
        asked.append(command)
        return {"success": False, "error": "The scene is locked"}

    _server_says(monkeypatch, respond)
    node = camera_list.BlenderCameraList(name="List")

    with pytest.raises(NodeError) as raised:
        node.process()

    assert str(raised.value) == "The scene is locked"
    assert raised.value.response == {"success": False, "error": "The scene is locked"}
    assert node.parameter_output_values["status_output"] == "Error: The scene is locked"
    assert node.parameter_output_values["camera_count"] == 0
    # One attempt with the detailed query, then one with the plain one. A failure is not tried again.
    assert asked == ["execute_code", "list_cameras"]


def test_a_scene_with_no_cameras_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(command: str, _params: dict[str, Any]) -> dict[str, Any]:
        if command == "execute_code":
            return {"success": True, "result": {"success": True, "cameras": []}}
        return {"success": True, "cameras": []}

    _server_says(monkeypatch, respond)
    node = camera_list.BlenderCameraList(name="List")

    node.process()

    assert node.parameter_output_values["camera_count"] == 0
    assert isinstance(node.parameter_output_values["cameras_output"], ListArtifact)
    assert "No cameras found" in node.parameter_output_values["status_output"]


@pytest.mark.parametrize("module_name", ["camera_capture", "camera_list"])
def test_a_node_file_loads_the_way_the_engine_loads_it(module_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine loads each node file on its own, with the library folder on sys.path.

    There is no parent package, and the library's package name is not importable there, so a node
    has to import its sibling modules by bare name. The dev environment installs the project, which
    would hide a package-name import, so make the package unimportable here.
    """
    monkeypatch.syspath_prepend(str(LIBRARY_DIR))
    monkeypatch.setitem(sys.modules, "blender", None)

    spec = importlib.util.spec_from_file_location(f"engine_loaded_{module_name}", LIBRARY_DIR / f"{module_name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "BlenderCamera" + ("Capture" if module_name == "camera_capture" else "List"))
