"""Trace images into Grease Pencil with Blender running in the background, and send the result to an open Blender.

No engine imports, so this can be tested without a running engine. The script that runs inside the background
Blender is `trace_grease_pencil.py`; the code that places the result in a scene is `grease_pencil_scene.py`.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import blender_mcp_client

BLENDER_SCRIPT = Path(__file__).parent / "trace_grease_pencil.py"
SCENE_SCRIPT = Path(__file__).parent / "grease_pencil_scene.py"
MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
SUCCESS_MARKER = "TRACE_OK"
RUN_TIMEOUT_SECONDS = 600

MODE_ANIMATED = "animated"
MODE_SEPARATE = "separate"
TURN_POLICIES = ["MINORITY", "MAJORITY", "FOREGROUND", "BACKGROUND", "LEFT", "RIGHT", "RANDOM"]
STROKE_TYPES = ["POLY", "BEZIER"]


@dataclass
class TraceSettings:
    """Everything the background Blender needs besides the image files."""

    mode: str = MODE_ANIMATED
    threshold: float = 0.5
    turn_policy: str = "MINORITY"
    invert: bool = False
    max_resolution: int = 2048
    stroke_type: str = "POLY"
    simplify: float = 0.0
    smooth: float = 0.0
    thickness: float = 0.002
    color: str = "#000000"
    fill: bool = True
    object_name: str = "Drawing"
    layer_name: str = "Lines"
    size: float = 2.0
    start_frame: int = 1
    frame_step: int = 1


@dataclass
class Placement:
    """Where the traced drawing goes in a scene. `location` None means the 3D cursor."""

    collection: str = ""
    location: list[float] | None = None
    target_object: str = ""
    replace: bool = True

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "location": self.location,
            "target_object": self.target_object,
            "replace": self.replace,
        }


def hex_to_linear(color: str) -> list[float]:
    """Convert an sRGB hex color such as #1a1a1a to the linear RGB Blender stores in materials."""
    text = color.strip().lstrip("#")
    if len(text) == 3:  # noqa: PLR2004
        text = "".join(c * 2 for c in text)
    try:
        channels = [int(text[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError as e:
        msg = f"'{color}' is not a hex color such as #000000."
        raise ValueError(msg) from e
    return [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]  # noqa: PLR2004


def _blender_program(text: str) -> Path | None:
    path = Path(text).expanduser()
    if path.suffix == ".app":
        path = path / "Contents" / "MacOS" / "Blender"
    return path if path.is_file() else None


def find_blender(setting: str = "") -> str:
    """The Blender executable. A path that was set is used or refused, never replaced by another Blender.

    With no path set, the search is $BLENDER_PATH, then `blender` on PATH, then the macOS default.
    """
    chosen = (setting or "").strip()
    if chosen:
        program = _blender_program(chosen)
        if program is None:
            msg = f"Blender was not found at {chosen}. Choose the Blender program, or clear 'Blender' to find it automatically."
            raise FileNotFoundError(msg)
        return str(program)
    for candidate in (os.environ.get("BLENDER_PATH", ""), shutil.which("blender") or "", MAC_BLENDER):
        program = _blender_program(candidate.strip()) if candidate.strip() else None
        if program is not None:
            return str(program)
    msg = (
        "Blender was not found. Set 'Blender' to the Blender program (on a Mac, /Applications/Blender.app), "
        "or set the BLENDER_PATH environment variable."
    )
    raise FileNotFoundError(msg)


def trace_job(
    images: list[str],
    settings: TraceSettings,
    output_blend: str,
    preview_dir: str,
    target_blend: str = "",
    placement: Placement | None = None,
) -> dict[str, Any]:
    """The JSON job `trace_grease_pencil.py` reads."""
    if not images:
        msg = "There are no images to trace."
        raise ValueError(msg)
    if settings.mode not in (MODE_ANIMATED, MODE_SEPARATE):
        msg = f"Mode must be {MODE_ANIMATED} or {MODE_SEPARATE}, not {settings.mode}."
        raise ValueError(msg)
    placement = placement or Placement()
    on_layers = bool(placement.target_object)
    return {
        "images": images,
        "mode": settings.mode,
        "layers_per_image": on_layers and settings.mode == MODE_SEPARATE,
        "threshold": settings.threshold,
        "turn_policy": settings.turn_policy,
        "invert": settings.invert,
        "max_resolution": settings.max_resolution,
        "stroke_type": settings.stroke_type,
        "simplify": settings.simplify,
        "smooth": settings.smooth,
        "thickness": settings.thickness,
        "color": hex_to_linear(settings.color),
        "fill": settings.fill,
        "object_name": settings.object_name,
        "layer_name": settings.layer_name,
        "material_name": f"{settings.layer_name if on_layers else settings.object_name}_Ink",
        "size": settings.size,
        "start_frame": settings.start_frame,
        "frame_step": max(1, settings.frame_step),
        "output_blend": output_blend,
        "preview_dir": preview_dir,
        "target_blend": target_blend,
        "import": placement.as_kwargs(),
    }


def run_trace(blender: str, job: dict[str, Any]) -> dict[str, Any]:
    """Run the trace job in a background Blender. Returns the script's result."""
    with tempfile.TemporaryDirectory() as folder:
        job_file = Path(folder) / "job.json"
        result_file = Path(folder) / "result.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        command = [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(BLENDER_SCRIPT),
            "--",
            str(job_file),
            str(result_file),
        ]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as e:
            msg = f"Blender did not finish tracing within {RUN_TIMEOUT_SECONDS} seconds."
            raise RuntimeError(msg) from e
        succeeded = any(line.startswith(SUCCESS_MARKER) for line in (done.stdout or "").splitlines())
        if done.returncode != 0 or not succeeded or not result_file.is_file():
            tail = "\n".join(((done.stderr or "") + "\n" + (done.stdout or "")).strip().splitlines()[-12:])
            msg = f"Blender could not trace the images (exit code {done.returncode}).\n{tail}"
            raise RuntimeError(msg)
        return json.loads(result_file.read_text(encoding="utf-8"))


def send_to_open_scene(
    blend_path: str,
    object_names: list[str],
    layer_names: list[str],
    placement: Placement,
    host: str = blender_mcp_client.DEFAULT_HOST,
    port: int = blender_mcp_client.DEFAULT_PORT,
) -> dict[str, Any]:
    """Bring the traced objects from `blend_path` into the scene open in Blender, through the MCP add-on."""
    arguments = {"blend_path": blend_path, "object_names": object_names, "layer_names": layer_names}
    arguments.update(placement.as_kwargs())
    code = (
        SCENE_SCRIPT.read_text(encoding="utf-8")
        + f"\n\nimport json as _json\nresult = import_traced(**_json.loads({json.dumps(json.dumps(arguments))}))\n"
    )
    return blender_mcp_client.execute(code, host, port)
