import re
import tempfile
from pathlib import Path
from typing import Any

import blender_mcp_client
from grease_pencil_trace import (
    MODE_ANIMATED,
    MODE_SEPARATE,
    TURN_POLICIES,
    Placement,
    TraceSettings,
    find_blender,
    run_trace,
    send_to_open_scene,
    trace_job,
)
from griptape.artifacts import ImageArtifact, ImageUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.files.file import File
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.traits.color_picker import ColorPicker
from griptape_nodes.traits.options import Options

MODE_LABELS = {"Animated (one object, a keyframe per image)": MODE_ANIMATED, "Separate (one per image)": MODE_SEPARATE}
STROKE_LABELS = {"Poly (simplify and smooth apply)": "POLY", "Bezier (as traced)": "BEZIER"}


def parse_location(text: str) -> list[float] | None:
    """`x, y, z` as three floats, or None for an empty string, which means the 3D cursor."""
    parts = [part for part in text.replace(",", " ").split() if part]
    if not parts:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError as e:
        msg = f"Location '{text}' must be three numbers such as 0, 0, 1, or empty for the 3D cursor."
        raise ValueError(msg) from e
    if len(values) != 3:  # noqa: PLR2004
        msg = f"Location '{text}' must be three numbers such as 0, 0, 1, or empty for the 3D cursor."
        raise ValueError(msg)
    return values


class BlenderImageToGreasePencil(SuccessFailureNode):
    """Trace images into a Grease Pencil drawing with a background Blender, and optionally send it to the open scene."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Blender"
        self.description = "Traces images into a Grease Pencil drawing and can send it to the scene open in Blender."

        self.add_parameter(
            ParameterList(
                name="images",
                tooltip="The image to trace, or several: one keyframe each when animated, one drawing each when separate.",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageUrlArtifact",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            ParameterString(
                name="mode",
                display_name="Mode",
                tooltip="Animated puts every image in one object, a keyframe each. Separate makes one drawing per image.",
                default_value=next(iter(MODE_LABELS)),
                traits={Options(choices=list(MODE_LABELS))},
                allow_output=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="object_name",
                display_name="Object Name",
                tooltip="Name of the Grease Pencil object.",
                default_value="Drawing",
            )
        )

        with ParameterGroup(name="Trace") as trace_group:
            ParameterFloat(
                name="threshold",
                display_name="Threshold",
                tooltip="Brightness below which a pixel counts as ink (0-1).",
                default_value=0.5,
                min_val=0.0,
                max_val=1.0,
                slider=True,
                step=0.01,
            )
            ParameterBool(
                name="invert",
                display_name="Invert",
                tooltip="Trace the light parts instead, for light lines on dark.",
                default_value=False,
            )
            ParameterString(
                name="turn_policy",
                display_name="Turn Policy",
                tooltip="How the tracer resolves ambiguous pixel corners. The default suits most drawings.",
                default_value="MINORITY",
                traits={Options(choices=TURN_POLICIES)},
                allow_output=False,
            )
        self.add_node_element(trace_group)

        with ParameterGroup(name="Cleanup", collapsed=True) as cleanup_group:
            ParameterInt(
                name="max_resolution",
                display_name="Max Resolution",
                tooltip="Downscale images whose long side is larger than this before tracing. 0 keeps full size.",
                default_value=2048,
                min_val=0,
            )
            ParameterString(
                name="stroke_type",
                display_name="Stroke Type",
                tooltip="Poly strokes can be simplified and smoothed; Bezier keeps the traced curves.",
                default_value=next(iter(STROKE_LABELS)),
                traits={Options(choices=list(STROKE_LABELS))},
                allow_output=False,
            )
            ParameterFloat(
                name="simplify",
                display_name="Simplify",
                tooltip="Remove points that barely change the shape. 0 keeps every point.",
                default_value=0.0,
                min_val=0.0,
                max_val=0.05,
                slider=True,
                step=0.001,
            )
            ParameterFloat(
                name="smooth",
                display_name="Smooth",
                tooltip="Smooth the strokes (0-1).",
                default_value=0.0,
                min_val=0.0,
                max_val=1.0,
                slider=True,
                step=0.05,
            )
        self.add_node_element(cleanup_group)

        with ParameterGroup(name="Look") as look_group:
            ParameterFloat(
                name="thickness",
                display_name="Thickness",
                tooltip="Stroke radius in Blender units.",
                default_value=0.002,
                min_val=0.0,
                step=0.001,
            )
            ParameterString(
                name="color",
                display_name="Color",
                tooltip="Ink color.",
                default_value="#000000",
                traits={ColorPicker(format="hex")},
            )
            ParameterBool(
                name="fill",
                display_name="Fill",
                tooltip="Fill the traced shapes. The tracer outlines each line, so turn this off to see only the outlines.",
                default_value=True,
            )
        self.add_node_element(look_group)

        with ParameterGroup(name="Placement", collapsed=True) as placement_group:
            ParameterString(
                name="layer_name",
                display_name="Layer Name",
                tooltip="Name of the layer the strokes go on.",
                default_value="Lines",
            )
            ParameterFloat(
                name="size",
                display_name="Size",
                tooltip="Width of the drawing in Blender units. It stands upright, facing the front view.",
                default_value=2.0,
                min_val=0.001,
            )
            ParameterString(
                name="location",
                display_name="Location",
                tooltip="Where the drawing's center goes, as x, y, z. Leave empty to use the 3D cursor.",
                default_value="",
                placeholder_text="3D cursor",
            )
            ParameterString(
                name="collection",
                display_name="Collection",
                tooltip="Collection for new objects. Leave empty for the active collection.",
                default_value="",
                placeholder_text="Active collection",
            )
            ParameterString(
                name="target_object",
                display_name="Target Object",
                tooltip="An existing Grease Pencil object to add the strokes to as new layers, instead of a new object.",
                default_value="",
                placeholder_text="New object",
            )
            ParameterBool(
                name="replace",
                display_name="Replace Existing",
                tooltip="Replace the object or layer this node made on an earlier run, rather than adding a copy. Objects and layers you made yourself are never replaced.",
                default_value=True,
            )
            ParameterInt(
                name="start_frame", display_name="Start Frame", tooltip="Frame of the first image.", default_value=1
            )
            ParameterInt(
                name="frame_step",
                display_name="Frame Step",
                tooltip="Frames between images when animated.",
                default_value=1,
                min_val=1,
            )
        self.add_node_element(placement_group)

        with ParameterGroup(name="Blender", collapsed=True) as blender_group:
            ParameterBool(
                name="send_to_open_scene",
                display_name="Send to Open Scene",
                tooltip="Add the drawing to the scene open in Blender. Needs the Blender Lab MCP add-on running.",
                default_value=True,
            )
            ParameterString(
                name="target_blend",
                display_name="Target .blend",
                tooltip="A .blend file to add the drawing to and save. Leave empty to skip.",
                default_value="",
                placeholder_text="None",
            )
            ParameterString(
                name="blender_path",
                display_name="Blender Path",
                tooltip="The Blender program used for tracing. Leave empty to find it automatically.",
                default_value="",
                placeholder_text="Find automatically",
            )
            ParameterInt(
                name="mcp_port",
                display_name="MCP Port",
                tooltip="Port of the Blender Lab MCP add-on's server.",
                default_value=blender_mcp_client.DEFAULT_PORT,
            )
        self.add_node_element(blender_group)

        self._blend_file = ProjectFileParameter(
            node=self,
            name="output_file",
            default_filename="grease_pencil.blend",
            ui_options={"display_name": "Output .blend"},
        )
        self._blend_file.add_parameter()

        self.add_parameter(
            Parameter(
                name="preview",
                display_name="Preview",
                type="ImageUrlArtifact",
                output_type="ImageUrlArtifact",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="The first traced image, rendered on white.",
                ui_options={"pulse_on_run": True, "is_full_width": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="previews",
                display_name="All Previews",
                type="list",
                output_type="list[ImageUrlArtifact]",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="A preview for every image.",
            )
        )
        self.add_parameter(
            ParameterString(
                name="blend_path",
                display_name="Blend Path",
                tooltip="The .blend holding only the traced drawing.",
                allow_input=False,
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="object_name_output",
                tooltip="The Grease Pencil object(s) the drawing ended up in, comma separated.",
                display_name="Object",
                allow_input=False,
                allow_property=False,
            )
        )
        self.add_parameter(
            ParameterString(
                name="layer_name_output",
                tooltip="The layer(s) the drawing ended up on, comma separated.",
                display_name="Layer",
                allow_input=False,
                allow_property=False,
            )
        )
        self._create_status_parameters(
            result_details_tooltip="What was traced and where it went.",
            result_details_placeholder="Trace details appear here.",
        )

    def process(self) -> AsyncResult[None]:
        yield lambda: self._process()

    def _process(self) -> None:
        self._clear_execution_status()
        try:
            details = self._trace()
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        self._set_status_results(was_successful=True, result_details=details)

    def _settings(self) -> TraceSettings:
        value = self.get_parameter_value
        return TraceSettings(
            mode=MODE_LABELS.get(value("mode"), MODE_ANIMATED),
            threshold=float(value("threshold")),
            turn_policy=value("turn_policy"),
            invert=bool(value("invert")),
            max_resolution=int(value("max_resolution")),
            stroke_type=STROKE_LABELS.get(value("stroke_type"), "POLY"),
            simplify=float(value("simplify")),
            smooth=float(value("smooth")),
            thickness=float(value("thickness")),
            color=value("color") or "#000000",
            fill=bool(value("fill")),
            object_name=(value("object_name") or "Drawing").strip(),
            layer_name=(value("layer_name") or "Lines").strip(),
            size=float(value("size")),
            start_frame=int(value("start_frame")),
            frame_step=int(value("frame_step")),
        )

    def _placement(self) -> Placement:
        value = self.get_parameter_value
        return Placement(
            collection=(value("collection") or "").strip(),
            location=parse_location(value("location") or ""),
            target_object=(value("target_object") or "").strip(),
            replace=bool(value("replace")),
        )

    def _write_images(self, folder: Path) -> list[str]:
        paths = []
        for index, image in enumerate(self.get_parameter_list_value("images"), start=1):
            if isinstance(image, ImageUrlArtifact):
                content = File(image.value).read_bytes()
            elif isinstance(image, ImageArtifact):
                content = image.value
            elif isinstance(image, str) and image:
                content = File(image).read_bytes()
            else:
                continue
            path = folder / f"image_{index:03d}.png"
            path.write_bytes(content)
            paths.append(str(path))
        return paths

    def _trace(self) -> str:
        settings = self._settings()
        placement = self._placement()
        target_blend = (self.get_parameter_value("target_blend") or "").strip()
        if target_blend and not Path(target_blend).expanduser().is_file():
            msg = f"The target .blend {target_blend} does not exist."
            raise FileNotFoundError(msg)
        blender = find_blender(self.get_parameter_value("blender_path") or "")

        with tempfile.TemporaryDirectory(prefix="gtn_grease_pencil_") as folder:
            work = Path(folder)
            images = self._write_images(work)
            traced_blend = work / "traced.blend"
            job = trace_job(
                images,
                settings,
                str(traced_blend),
                str(work),
                target_blend=str(Path(target_blend).expanduser()) if target_blend else "",
                placement=placement,
            )
            result = run_trace(blender, job)
            blend = self._blend_file.build_file().write_bytes(traced_blend.read_bytes())
            previews = [self._save_preview(Path(preview)) for preview in result["previews"]]

        blend_path = blend.resolve()
        placed: dict[str, Any] = {"objects": result["objects"], "layers": result["layers"]}
        lines = [f"Traced {len(images)} image(s) into {', '.join(result['objects'])}."]
        if result.get("target_blend"):
            placed = result["target_blend"]
            lines.append(f"Added to {target_blend} and saved it.")
        self.parameter_output_values["preview"] = previews[0] if previews else None
        self.parameter_output_values["previews"] = previews
        self.parameter_output_values["blend_path"] = blend_path
        self._set_placed(placed)

        if self.get_parameter_value("send_to_open_scene"):
            try:
                placed = send_to_open_scene(
                    blend_path,
                    result["objects"],
                    result["layers"],
                    placement,
                    port=int(self.get_parameter_value("mcp_port")),
                )
            except blender_mcp_client.BlenderMCPError as e:
                # The previews and .blend outputs stay set, so the trace is still usable.
                msg = f"Traced, but could not send it to the open Blender. Turn off 'Send to Open Scene' to skip.\n{e}"
                raise blender_mcp_client.BlenderMCPError(msg) from e
            self._set_placed(placed)
            lines.append("Added to the scene open in Blender.")

        lines.append(f"Object: {', '.join(placed['objects'])}. Layers: {', '.join(placed['layers'])}.")
        return "\n".join(lines)

    def _set_placed(self, placed: dict[str, Any]) -> None:
        self.parameter_output_values["object_name_output"] = ", ".join(placed["objects"])
        self.parameter_output_values["layer_name_output"] = ", ".join(placed["layers"])

    def _save_preview(self, path: Path) -> ImageUrlArtifact:
        # Each run overwrites this node's previews, so re-tracing doesn't pile up files. The node name is in the
        # filename because the project's template may leave it out, and two nodes would then share previews.
        # `_index` is bound because only CREATE_NEW fills it, and resolve() fails while it's required and empty.
        node = re.sub(r"[^A-Za-z0-9_-]+", "_", self.name).strip("_")
        situated = ProjectFileDestination.from_situation(
            f"{node}_grease_pencil_{path.name}", ProjectFileParameter.DEFAULT_SITUATION, _index=1
        )
        destination = ProjectFileDestination(situated.resolve(), existing_file_policy=ExistingFilePolicy.OVERWRITE)
        saved = destination.write_bytes(path.read_bytes())
        return ImageUrlArtifact(value=saved.location)
