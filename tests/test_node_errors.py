"""Tests for how the Blender nodes report a failure.

The nodes talk to Blender through the Blender Lab MCP add-on. These tests start a small fake of that add-on on a local
port, so the real client code runs, and check that each failure reaches the editor as a NodeError.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import socketserver
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import blender_mcp_client
import camera_capture
import camera_choices
import camera_list
import image_to_camera_plane
import pytest
import set_frame
from griptape.artifacts import ListArtifact
from griptape_nodes.exe_types.core_types import NodeError
from griptape_nodes.exe_types.node_types import NodeResolutionState

LIBRARY_DIR = Path(__file__).resolve().parent.parent / "blender"
PNG = base64.b64encode(b"x" * 200).decode()
ONE_CAMERA = {
    "blender_version": "5.3",
    "cameras": [
        {
            "name": "Camera",
            "location": [0, 0, 0],
            "rotation": [0, 0, 0],
            "scale": [1, 1, 1],
            "active": True,
            "focal_length": 50.0,
            "sensor_width": 36.0,
            "sensor_height": 24.0,
            "sensor_fit": "AUTO",
            "type": "PERSP",
            "angle": 0.69,
            "angle_x": 0.69,
            "angle_y": 0.4,
            "clip_start": 0.1,
            "clip_end": 100.0,
            "shift_x": 0.0,
            "shift_y": 0.0,
            "passepartout_alpha": 0.5,
            "dof_use": False,
            "dof_focus_distance": 10.0,
            "dof_aperture_fstop": 2.8,
            "background_images_count": 0,
            "matrix_world": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        }
    ],
}

Reply = dict[str, Any]
BlenderFactory = Callable[[Reply], int]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = b""
        while b"\0" not in data:
            chunk = self.request.recv(65536)
            if not chunk:
                return
            data += chunk
        self.server.requests.append(json.loads(data.split(b"\0")[0]))  # type: ignore[attr-defined]
        self.request.sendall(json.dumps(self.server.reply).encode() + b"\0")  # type: ignore[attr-defined]


class _FakeBlender(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reply: Reply) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply = reply
        self.requests: list[dict[str, Any]] = []


@pytest.fixture
def blender() -> Iterator[BlenderFactory]:
    """Start a fake MCP add-on that answers every request with `reply`, and return its port."""
    servers: list[_FakeBlender] = []

    def start(reply: Reply) -> int:
        server = _FakeBlender(reply)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_live_blender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nodes fetch camera choices when they are made. Keep that away from a Blender that may be open."""
    monkeypatch.setattr(camera_choices, "fetch_names", lambda *_a, **_k: [])


def _ok(result: Reply) -> Reply:
    return {"status": "ok", "result": result}


def _error(text: str) -> Reply:
    return {"status": "error", "message": f"Traceback (most recent call last):\n  ...\n{text}"}


def _unused_port() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        return server.server_address[1]


def _capture(port: int) -> camera_capture.BlenderCameraCapture:
    node = camera_capture.BlenderCameraCapture(name="Capture")
    node.set_parameter_value("mcp_port", port)
    return node


# ---- the client ---------------------------------------------------------------------------------------------------


def test_an_unreachable_blender_is_a_node_error_with_the_port_and_a_link() -> None:
    port = _unused_port()

    with pytest.raises(NodeError, match="Could not reach Blender") as raised:
        blender_mcp_client.execute("result = {}", port=port)

    assert raised.value.fields == {"host": "localhost", "port": port}
    assert [link.url for link in raised.value.links] == [blender_mcp_client.ADDON_URL]
    assert isinstance(raised.value, blender_mcp_client.BlenderMCPError)


def test_an_error_from_blender_gives_its_last_line_and_attaches_the_reply(blender: BlenderFactory) -> None:
    reply = _error("ValueError: Camera 'Cam' not found in the scene.")

    with pytest.raises(NodeError) as raised:
        blender_mcp_client.execute("result = {}", port=blender(reply))

    assert str(raised.value) == "Blender reported an error: ValueError: Camera 'Cam' not found in the scene."
    assert raised.value.response == reply


# ---- Camera Capture -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_capture_with_no_blender_fails_the_node() -> None:
    node = _capture(_unused_port())

    with pytest.raises(NodeError, match="Could not reach Blender"):
        await node.aprocess()

    assert node.parameter_output_values["status_output"].startswith("Error: Could not reach Blender")


@pytest.mark.asyncio
async def test_a_render_error_from_blender_fails_the_node_with_the_reason(blender: BlenderFactory) -> None:
    node = _capture(blender(_error("ValueError: Camera 'Cam' not found in the scene.")))

    with pytest.raises(NodeError) as raised:
        await node.aprocess()

    assert str(raised.value) == "Blender reported an error: ValueError: Camera 'Cam' not found in the scene."
    assert raised.value.response is not None
    assert node.parameter_output_values["status_output"] == f"Error: {raised.value}"
    assert "image_output" not in node.parameter_output_values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "message"),
    [
        pytest.param(
            {"width": 64},
            "Blender returned no image data. Run the node again. If it keeps happening, check Blender's console for errors.",
            id="no-image",
        ),
        pytest.param(
            {"image": base64.b64encode(b"tiny").decode()},
            "The image data from Blender was empty or corrupted. Run the node again.",
            id="corrupted",
        ),
    ],
)
async def test_unusable_image_data_fails_the_node(blender: BlenderFactory, result: Reply, message: str) -> None:
    node = _capture(blender(_ok(result)))

    with pytest.raises(NodeError) as raised:
        await node.aprocess()

    assert str(raised.value) == message
    assert node.parameter_output_values["status_output"] == f"Error: {message}"


@pytest.mark.asyncio
async def test_undecodable_image_data_fails_the_node_and_keeps_the_cause(blender: BlenderFactory) -> None:
    node = _capture(blender(_ok({"image": "abc"})))

    with pytest.raises(NodeError, match="Could not decode the image data from Blender. Run the node again.") as raised:
        await node.aprocess()

    assert raised.value.__cause__ is not None


@pytest.mark.asyncio
async def test_a_file_error_is_raised_as_it_is(blender: BlenderFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _capture(blender(_ok({"image": PNG})))

    def disk_full() -> Any:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(node._output_file, "build_file", disk_full)

    with pytest.raises(OSError, match="disk full") as raised:
        await node.aprocess()

    assert not isinstance(raised.value, NodeError)
    assert node.parameter_output_values["status_output"] == "Error: disk full"


@pytest.mark.asyncio
async def test_a_good_capture_sets_the_image(blender: BlenderFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _capture(blender(_ok({"image": PNG, "width": 64, "height": 48, "render_time": 0.5})))
    written: list[bytes] = []

    class Destination:
        def write_bytes(self, data: bytes) -> Any:
            written.append(data)
            return type("Saved", (), {"location": "/tmp/capture.png"})()

    monkeypatch.setattr(node._output_file, "build_file", Destination)

    await node.aprocess()

    assert written == [b"x" * 200]
    assert node.parameter_output_values["image_output"].value == "/tmp/capture.png"
    assert "Successfully captured 64x48" in node.parameter_output_values["status_output"]


# ---- Camera List --------------------------------------------------------------------------------------------------


def _list(port: int) -> camera_list.BlenderCameraList:
    node = camera_list.BlenderCameraList(name="List")
    node.set_parameter_value("mcp_port", port)
    return node


def test_a_listing_with_no_blender_fails_the_node() -> None:
    node = _list(_unused_port())

    with pytest.raises(NodeError, match="Could not reach Blender"):
        node.process()

    assert node.parameter_output_values["camera_count"] == 0
    assert node.parameter_output_values["status_output"].startswith("Error: Could not reach Blender")


def test_a_listing_error_from_blender_fails_the_node(blender: BlenderFactory) -> None:
    node = _list(blender(_error("RuntimeError: The scene is locked")))

    with pytest.raises(NodeError, match="The scene is locked"):
        node.process()

    assert node.parameter_output_values["camera_count"] == 0


def test_a_scene_with_no_cameras_is_not_a_failure(blender: BlenderFactory) -> None:
    node = _list(blender(_ok({"blender_version": "5.3", "cameras": []})))

    node.process()

    assert node.parameter_output_values["camera_count"] == 0
    assert isinstance(node.parameter_output_values["cameras_output"], ListArtifact)
    assert "No cameras found" in node.parameter_output_values["status_output"]


def test_a_listing_returns_the_cameras(blender: BlenderFactory) -> None:
    node = _list(blender(_ok(ONE_CAMERA)))

    node.process()

    assert node.parameter_output_values["camera_count"] == 1
    assert json.loads(node.parameter_output_values["cameras_output"].value[0].value)["name"] == "Camera"


# ---- Nodes with a Failed output -----------------------------------------------------------------------------------


def _set_frame(port: int, *, failed_is_wired: bool) -> set_frame.BlenderSetFrame:
    node = set_frame.BlenderSetFrame(name="Frame")
    node.set_parameter_value("mcp_port", port)
    node._has_outgoing_connections = lambda _parameter: failed_is_wired  # type: ignore[method-assign]
    return node


def test_a_failure_raises_when_failed_is_unwired(blender: BlenderFactory) -> None:
    node = _set_frame(blender(_error("ValueError: Frame out of range")), failed_is_wired=False)

    with pytest.raises(NodeError, match="Frame out of range"):
        node._process()

    assert node.parameter_output_values["was_successful"] is False


def test_a_failure_follows_failed_when_it_is_wired(blender: BlenderFactory) -> None:
    node = _set_frame(blender(_error("ValueError: Frame out of range")), failed_is_wired=True)

    node._process()

    assert node.parameter_output_values["was_successful"] is False
    assert "Frame out of range" in node.parameter_output_values["result_details"]


def test_no_blender_is_a_failure_the_failed_output_can_catch() -> None:
    node = _set_frame(_unused_port(), failed_is_wired=True)

    node._process()

    assert node.parameter_output_values["was_successful"] is False
    assert "Could not reach Blender" in node.parameter_output_values["result_details"]


def _node_with_failed(node: Any, port: int, *, failed_is_wired: bool) -> Any:
    node.set_parameter_value("mcp_port", port)
    node._has_outgoing_connections = lambda _parameter: failed_is_wired
    return node


def test_no_media_says_which_parameter_to_fill_in() -> None:
    node = image_to_camera_plane.BlenderImageToCameraPlane(name="Plane")
    node._has_outgoing_connections = lambda _parameter: False  # type: ignore[method-assign]

    with pytest.raises(NodeError, match="Connect an image or a movie to 'Image or Movie'"):
        node._process()


def test_a_missing_file_says_which_parameter_to_check() -> None:
    node = image_to_camera_plane.BlenderImageToCameraPlane(name="Plane")
    node._has_outgoing_connections = lambda _parameter: False  # type: ignore[method-assign]
    node.set_parameter_value("media", "/no/such/file.png")

    with pytest.raises(NodeError, match=r"does not exist\. Check the file connected to 'Image or Movie'"):
        node._process()


# ---- Camera List always reads Blender again -----------------------------------------------------------------------


def test_camera_list_is_made_unresolved_before_each_run(blender: BlenderFactory) -> None:
    node = _list(blender(_ok(ONE_CAMERA)))
    node.state = NodeResolutionState.RESOLVED

    assert node.validate_before_node_run() is None
    assert node.state == NodeResolutionState.UNRESOLVED

    node.state = NodeResolutionState.RESOLVED
    assert node.validate_before_workflow_run() is None
    assert node.state == NodeResolutionState.UNRESOLVED


# ---- Camera Capture's labels --------------------------------------------------------------------------------------


def _cameras_artifact(*cameras: dict[str, Any]) -> ListArtifact:
    from griptape.artifacts import TextArtifact

    return ListArtifact([TextArtifact(json.dumps(camera)) for camera in cameras])


def test_the_labels_show_the_selected_cameras_details() -> None:
    node = camera_capture.BlenderCameraCapture(name="Capture")
    camera = {
        "name": "Shot_A",
        "active": True,
        "focal_length": 35.0,
        "sensor_width": 36.0,
        "sensor_height": 24.0,
        "sensor_fit": "AUTO",
        "type": "PERSP",
        "depth_of_field": {"enabled": True, "focus_distance": 4.0, "f_stop": 2.0},
        "location": {"x": 1.0, "y": 2.0, "z": 3.0},
        "rotation": {"x": 0.0, "y": 0.0, "z": 0.5},
    }
    node.set_parameter_value("cameras_input", _cameras_artifact(camera))
    node.set_parameter_value("camera_name", "Shot_A")

    node._update_camera_metadata_display()

    assert node.get_parameter_value("camera_status_label") == "✓ Active Scene Camera"
    assert node.get_parameter_value("focal_length_label") == "35.0 mm"
    assert node.get_parameter_value("sensor_info_label") == "36.0×24.0mm, AUTO, PERSP"
    assert node.get_parameter_value("dof_info_label") == "Enabled: 4.0BU @ f/2.0"
    assert node.get_parameter_value("transform_info_label") == "Loc: (1.00, 2.00, 3.00) Rot: (0.00, 0.00, 0.50)"


def test_the_labels_say_to_connect_a_camera_list_when_there_is_none() -> None:
    node = camera_capture.BlenderCameraCapture(name="Capture")

    node._update_camera_metadata_display()

    assert node.get_parameter_value("camera_status_label") == "Connect BlenderCameraList for details"
    assert node.get_parameter_value("focal_length_label") == "-"


def test_a_dropped_capture_node_leaves_the_registry() -> None:
    import gc

    node = camera_capture.BlenderCameraCapture(name="Temporary")
    assert node in camera_capture.BlenderCameraCapture._instances

    del node
    gc.collect()

    assert all(instance.name != "Temporary" for instance in camera_capture.BlenderCameraCapture._instances)


# ---- Loading ------------------------------------------------------------------------------------------------------


def _node_files() -> list[tuple[str, str]]:
    manifest = json.loads((LIBRARY_DIR / "griptape_nodes_library.json").read_text())
    return [(node["file_path"], node["class_name"]) for node in manifest["nodes"]]


@pytest.mark.parametrize(("file_path", "class_name"), _node_files())
def test_a_node_file_loads_the_way_the_engine_loads_it(
    file_path: str, class_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine loads each node file on its own, with the library folder on sys.path.

    There is no parent package, and the library's package name is not importable there, so a node
    has to import its sibling modules by bare name. The dev environment installs the project, which
    would hide a package-name import, so make the package unimportable here.
    """
    monkeypatch.syspath_prepend(str(LIBRARY_DIR))
    monkeypatch.setitem(sys.modules, "blender", None)

    spec = importlib.util.spec_from_file_location(f"engine_loaded_{class_name}", LIBRARY_DIR / file_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, class_name)
