"""Trace images into Grease Pencil objects. Runs inside a background Blender, never in the engine.

Run as `blender --background --factory-startup --python trace_grease_pencil.py -- <job.json> <result.json>`. The
job names the images and settings (see `grease_pencil_trace.trace_job`). The script saves a .blend holding only the
traced objects, renders one preview PNG per image, optionally brings the objects into a target .blend, and writes
the result JSON. It prints a line starting with `TRACE_OK` when it succeeds.
"""

import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).parent))

from grease_pencil_scene import copy_drawing, import_traced, remove_object  # noqa: E402

SUCCESS_MARKER = "TRACE_OK"
PREVIEW_MARGIN = 1.05
PREVIEW_MAX_SIZE = 1024


def _load_image(path: str, invert: bool, max_resolution: int):
    """Load an image flattened onto white, inverted and downscaled as asked."""
    image = bpy.data.images.load(path)
    width, height = image.size
    if width == 0 or height == 0:
        msg = f"Blender could not read the image {Path(path).name}."
        raise ValueError(msg)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(-1, 4)
    alpha = pixels[:, 3:4]
    pixels[:, :3] = pixels[:, :3] * alpha + (1.0 - alpha)
    pixels[:, 3] = 1.0
    if invert:
        pixels[:, :3] = 1.0 - pixels[:, :3]
    image.pixels.foreach_set(pixels.ravel())
    longest = max(width, height)
    if max_resolution and longest > max_resolution:
        factor = max_resolution / longest
        image.scale(max(1, round(width * factor)), max(1, round(height * factor)))
    return image


def _trace(image, job: dict):
    """Trace one image with Blender's operator. Returns the new object; its drawing spans 1 unit on the long side."""
    scene = bpy.context.scene
    reference = bpy.data.objects.new("TraceReference", None)
    reference.empty_display_type = "IMAGE"
    reference.empty_display_size = 1.0
    reference.data = image
    scene.collection.objects.link(reference)
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        obj.select_set(False)
    view_layer.objects.active = reference
    reference.select_set(True)
    before = set(bpy.data.objects)
    result = bpy.ops.grease_pencil.trace_image(
        target="NEW",
        threshold=job["threshold"],
        turnpolicy=job["turn_policy"],
        mode="SINGLE",
        use_current_frame=True,
    )
    traced = [obj for obj in bpy.data.objects if obj not in before and obj.type == "GREASEPENCIL"]
    bpy.data.objects.remove(reference, do_unlink=True)
    if "FINISHED" not in result or not traced:
        msg = f"Blender could not trace {image.name}."
        raise RuntimeError(msg)
    return traced[0]


def _apply_modifier(obj, kind: str, **settings) -> None:
    modifier = obj.modifiers.new(kind.lower(), kind)
    for key, value in settings.items():
        setattr(modifier, key, value)
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.modifier_apply(modifier=modifier.name)


def _clean_up(obj, job: dict) -> None:
    """Turn the traced Bezier outlines into poly strokes, simplify and smooth them, as the job asks."""
    if job["stroke_type"] != "POLY":
        return
    # Applying any modifier evaluates Bezier strokes into poly strokes; a zero-factor simplify changes nothing else.
    _apply_modifier(obj, "GREASE_PENCIL_SIMPLIFY", mode="ADAPTIVE", factor=job["simplify"])
    if job["smooth"] > 0:
        _apply_modifier(obj, "GREASE_PENCIL_SMOOTH", factor=job["smooth"], step=1, use_keep_shape=True)


def _set_radius(drawing, radius: float) -> None:
    count = sum(len(stroke.points) for stroke in drawing.strokes)
    if not count:
        return
    attribute = drawing.attributes.get("radius")
    if attribute is None:
        attribute = drawing.attributes.new("radius", "FLOAT", "POINT")
    attribute.data.foreach_set("value", np.full(count, radius, dtype=np.float32))


def _ink_material(job: dict):
    material = bpy.data.materials.new(job["material_name"])
    bpy.data.materials.create_gpencil_data(material)
    style = material.grease_pencil
    red, green, blue = job["color"]
    style.color = (red, green, blue, 1.0)
    style.fill_color = (red, green, blue, 1.0 if job["fill"] else 0.0)
    if bpy.app.version < (5, 3, 0):
        style.show_stroke = True
        style.show_fill = job["fill"]
    return material


def _placement(image_size: tuple[int, int], size: float) -> Matrix:
    """Map the trace's 1-unit space to `size` units wide, standing up on the XZ plane (Blender's front view)."""
    width, height = image_size
    scale = size * max(width, height) / width
    return Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(scale, 4)


def _names(base: str, count: int) -> list[str]:
    if count == 1:
        return [base]
    return [f"{base}_{index:03d}" for index in range(1, count + 1)]


def _destinations(job: dict) -> list[tuple[str, str, int]]:
    """Where each image's drawing goes: (object name, layer name, frame number)."""
    count = len(job["images"])
    start = job["start_frame"]
    if job["mode"] == "animated":
        return [(job["object_name"], job["layer_name"], start + i * job["frame_step"]) for i in range(count)]
    if job["layers_per_image"]:
        return [(job["object_name"], layer, start) for layer in _names(job["layer_name"], count)]
    return [(name, job["layer_name"], start) for name in _names(job["object_name"], count)]


def _build(job: dict) -> list[dict]:
    """Trace every image into its destination. Returns, per image, where it went and the image's size."""
    scene = bpy.context.scene
    material = _ink_material(job)
    placed = []
    for path, (object_name, layer_name, frame) in zip(job["images"], _destinations(job), strict=True):
        scene.frame_set(frame)
        image = _load_image(path, job["invert"], job["max_resolution"])
        size = tuple(image.size)
        traced = _trace(image, job)
        _clean_up(traced, job)

        obj = bpy.data.objects.get(object_name)
        if obj is None:
            data = bpy.data.grease_pencils.new(object_name)
            data.materials.append(material)
            obj = bpy.data.objects.new(object_name, data)
            scene.collection.objects.link(obj)
        layer = obj.data.layers.get(layer_name)
        if layer is None:
            layer = obj.data.layers.new(layer_name)
            layer.use_lights = False
        drawing = layer.frames.new(frame).drawing
        source = traced.data.layers[0].frames[0].drawing
        material_map = [0] * max(1, len(traced.data.materials))
        copy_drawing(source, drawing, _placement(size, job["size"]), material_map)
        # The tracer hides its strokes and shows only fills; show them so Thickness and Fill off work.
        hidden = drawing.attributes.get("hide_stroke")
        if hidden is not None:
            drawing.attributes.remove(hidden)
        _set_radius(drawing, job["thickness"])

        remove_object(traced)
        bpy.data.images.remove(image)
        placed.append({"object": obj.name, "layer": layer.name, "frame": frame, "size": size})
    return placed


def _render_previews(placed: list[dict], job: dict) -> list[str]:
    """Render each traced image on white through an orthographic camera framing the image's area."""
    scene = bpy.context.scene
    world = bpy.data.worlds.new("Preview")
    if world.node_tree is None:
        world.use_nodes = True
    background = next(node for node in world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    background.inputs["Strength"].default_value = 1.0
    scene.world = world
    scene.render.engine = "BLENDER_EEVEE"
    scene.view_settings.view_transform = "Standard"
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.resolution_percentage = 100

    camera_data = bpy.data.cameras.new("Preview")
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("Preview", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.location = (0.0, -10.0, 0.0)
    camera.rotation_euler = (math.radians(90), 0.0, 0.0)

    objects = {entry["object"] for entry in placed}
    previews = []
    for index, entry in enumerate(placed, start=1):
        width, height = entry["size"]
        factor = min(1.0, PREVIEW_MAX_SIZE / max(width, height))
        scene.render.resolution_x = max(4, round(width * factor))
        scene.render.resolution_y = max(4, round(height * factor))
        drawing_width = job["size"]
        drawing_height = job["size"] * height / width
        camera_data.ortho_scale = max(drawing_width, drawing_height) * PREVIEW_MARGIN
        for name in objects:
            hidden = name != entry["object"]
            bpy.data.objects[name].hide_render = hidden
        for layer in bpy.data.objects[entry["object"]].data.layers:
            layer.hide = layer.name != entry["layer"]
        scene.frame_set(entry["frame"])
        scene.render.filepath = str(Path(job["preview_dir"]) / f"preview_{index:03d}.png")
        bpy.ops.render.render(write_still=True)
        previews.append(scene.render.filepath)

    for name in objects:
        bpy.data.objects[name].hide_render = False
        for layer in bpy.data.objects[name].data.layers:
            layer.hide = False
    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.cameras.remove(camera_data)
    scene.world = None
    bpy.data.worlds.remove(world)
    return previews


def _save_traced(objects: list[str], path: str) -> None:
    """Save a .blend holding only the traced objects, with their data and material."""
    bpy.data.libraries.write(path, {bpy.data.objects[name] for name in objects}, fake_user=False)


def main() -> None:
    job_path, result_path = sys.argv[sys.argv.index("--") + 1 :][:2]
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    bpy.ops.wm.read_factory_settings(use_empty=True)

    placed = _build(job)
    objects = list(dict.fromkeys(entry["object"] for entry in placed))
    layers = list(dict.fromkeys(entry["layer"] for entry in placed))
    previews = _render_previews(placed, job)
    _save_traced(objects, job["output_blend"])

    result = {"objects": objects, "layers": layers, "previews": previews, "target_blend": None}
    if job["target_blend"]:
        bpy.ops.wm.open_mainfile(filepath=job["target_blend"])
        result["target_blend"] = import_traced(job["output_blend"], objects, layers, **job["import"])
        bpy.ops.wm.save_mainfile()

    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    print(f"{SUCCESS_MARKER} {len(placed)} image(s) traced into {', '.join(objects)}")


try:
    main()
except Exception as error:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    print(f"TRACE_FAILED {error}")
    sys.exit(1)
