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

from grease_pencil_scene import _read_attribute, copy_drawing, import_traced, remove_object, stamp_color  # noqa: E402

SUCCESS_MARKER = "TRACE_OK"
COLOR_LAYERS = "layers"
PREVIEW_MARGIN = 1.05
PREVIEW_MAX_SIZE = 1024
CLUSTER_SAMPLE = 50000
CLUSTER_ITERATIONS = 12
CLUSTER_CHUNK = 500_000
HALO = 2
LUMA = np.array([0.2126, 0.7152, 0.0722])


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


def _nearest(pixels: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """Index of the closest center for every pixel, in chunks to keep memory flat on large images."""
    labels = np.empty(len(pixels), dtype=np.int32)
    for start in range(0, len(pixels), CLUSTER_CHUNK):
        chunk = pixels[start : start + CLUSTER_CHUNK]
        labels[start : start + CLUSTER_CHUNK] = ((chunk[:, None, :] - centers[None]) ** 2).sum(axis=2).argmin(axis=1)
    return labels


def _cluster(pixels: np.ndarray, count: int) -> np.ndarray:
    """K-means on an (n, 3) array of colors, seeded k-means++ with a fixed seed so a rerun gives the same palette."""
    rng = np.random.default_rng(0)
    sample = pixels[rng.choice(len(pixels), min(len(pixels), CLUSTER_SAMPLE), replace=False)]
    centers = [sample[rng.integers(len(sample))]]
    while len(centers) < count:
        distance = ((sample[:, None, :] - np.array(centers)[None]) ** 2).sum(axis=2).min(axis=1)
        total = distance.sum()
        if total <= 0:
            break
        centers.append(sample[rng.choice(len(sample), p=distance / total)])
    found = np.array(centers)
    for _ in range(CLUSTER_ITERATIONS):
        labels = _nearest(sample, found)
        for index in range(len(found)):
            members = sample[labels == index]
            if len(members):
                found[index] = members.mean(axis=0)
    return found


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)  # noqa: PLR2004


def _erode(mask: np.ndarray, steps: int) -> np.ndarray:
    for _ in range(steps):
        eroded = mask.copy()
        eroded[1:] &= mask[:-1]
        eroded[:-1] &= mask[1:]
        eroded[:, 1:] &= mask[:, :-1]
        eroded[:, :-1] &= mask[:, 1:]
        mask = eroded
    return mask


def _dilate(mask: np.ndarray, steps: int) -> np.ndarray:
    for _ in range(steps):
        grown = mask.copy()
        grown[1:] |= mask[:-1]
        grown[:-1] |= mask[1:]
        grown[:, 1:] |= mask[:, :-1]
        grown[:, :-1] |= mask[:, 1:]
        mask = grown
    return mask


def _mask_image(region: np.ndarray, name: str, folder: str):
    """A black-on-white image of `region` for the tracer, which only accepts images that come from a file."""
    height, width = region.shape
    pixels = np.ones((height, width, 4), dtype=np.float32)
    pixels[region, :3] = 0.0
    generated = bpy.data.images.new(name, width, height, alpha=False)
    generated.pixels.foreach_set(pixels.ravel())
    path = Path(folder) / f"{name}.png"
    generated.filepath_raw = str(path)
    generated.file_format = "PNG"
    generated.save()
    bpy.data.images.remove(generated)
    mask = bpy.data.images.load(str(path))
    mask.pack()
    path.unlink()
    return mask


def _color_parts(image, job: dict) -> list[tuple[int, tuple[float, float, float], object]]:
    """Split an image into the line art and its flat colors. Returns (number, linear RGB, mask image) per part.

    Number 0 is the line art: every pixel whose brightest channel is below the threshold, in the ink color. The rest of the pixels are
    clustered into `colors` colors, numbered from 1, darkest first, so a number means the same kind of color on
    every image. Soft edges would otherwise make a thin ring of in-between colors around every line, thousands of
    tiny shapes, so each color region is opened to drop slivers and then grown back to tuck under the lines.
    Parts come biggest color first and the line art last, so large fills sit behind and the lines end up on top.
    """
    width, height = image.size
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    flat = pixels.reshape(-1, 4)[:, :3]
    ink = (flat.max(axis=1) < job["threshold"]).reshape(height, width)
    rest = flat[~ink.ravel()]
    parts = []
    if len(rest):
        centers = _cluster(rest, job["colors"])
        labels = _nearest(flat, centers).reshape(height, width)
        areas = np.bincount(labels[~ink], minlength=len(centers))
        # The paper often splits into a few near-identical clusters, so every color close to the one touching the
        # edge is skipped, or the others trace as hundreds of blobs.
        background = set()
        if job["skip_background"]:
            border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
            edge = int(np.bincount(border, minlength=len(centers)).argmax())
            distance = np.linalg.norm(centers - centers[edge], axis=1)
            background = {int(i) for i in np.flatnonzero(distance <= job["background_tolerance"])} | {edge}
        number = {int(cluster): place + 1 for place, cluster in enumerate(np.argsort(centers @ LUMA))}
        cleanup = job["color_cleanup"]
        # The soft edge beside a line is neither line nor color, so count it as line when sorting the colors.
        beside_ink = _dilate(ink, HALO)
        for cluster in np.argsort(-areas):
            if int(cluster) in background or areas[cluster] == 0:
                continue
            region = (labels == cluster) & ~beside_ink
            if cleanup:
                region = _dilate(_erode(region, cleanup), cleanup + 1)
            if not region.any():
                continue
            color = _srgb_to_linear(centers[cluster])
            mask = _mask_image(region, f"Mask_{number[int(cluster)]}", job["preview_dir"])
            parts.append((number[int(cluster)], (float(color[0]), float(color[1]), float(color[2])), mask))
    if ink.any():
        parts.append((0, tuple(job["color"]), _mask_image(ink, "Mask_ink", job["preview_dir"])))
    if not parts:
        msg = "The image has only one color, so there is nothing to trace after skipping the background."
        raise ValueError(msg)
    return parts


def _trace(image, job: dict):
    """Trace one image with Blender's operator. Returns the new object; its drawing spans 1 unit on the long side."""
    scene = bpy.context.scene
    reference = bpy.data.objects.new("TraceReference", None)
    reference.empty_display_type = "IMAGE"
    reference.empty_display_size = 1.0
    reference.data = image
    scene.collection.objects.link(reference)
    view_layer = bpy.context.view_layer
    # Objects removed by an earlier trace in the same frame linger as None until the view layer updates.
    view_layer.update()
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


def _drop_small_shapes(drawing, min_area: float) -> None:
    """Remove strokes enclosing less than `min_area` (the image's long side is 1 unit), such as specks and slivers."""
    if min_area <= 0:
        return
    small = []
    positions = _read_attribute(drawing.attributes["position"]).reshape(-1, 3)
    start = 0
    for index, stroke in enumerate(drawing.strokes):
        count = len(stroke.points)
        x, y = positions[start : start + count, 0], positions[start : start + count, 1]
        start += count
        area = abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0 if count >= 3 else 0.0  # noqa: PLR2004
        if area < min_area:
            small.append(index)
    if small:
        drawing.remove_strokes(indices=small)


def _set_radius(drawing, radius: float) -> None:
    count = sum(len(stroke.points) for stroke in drawing.strokes)
    if not count:
        return
    attribute = drawing.attributes.get("radius")
    if attribute is None:
        attribute = drawing.attributes.new("radius", "FLOAT", "POINT")
    attribute.data.foreach_set("value", np.full(count, radius, dtype=np.float32))


def _ink_material(name: str, color: tuple[float, float, float], fill: bool):
    material = bpy.data.materials.new(name)
    bpy.data.materials.create_gpencil_data(material)
    style = material.grease_pencil
    red, green, blue = color
    style.color = (red, green, blue, 1.0)
    style.fill_color = (red, green, blue, 1.0 if fill else 0.0)
    if bpy.app.version < (5, 3, 0):
        style.show_stroke = True
        style.show_fill = fill
    return material


def _color_slot(data, job: dict, number: int, color: tuple[float, float, float]) -> int:
    """Material slot for color `number` on `data`, adding a material of that color the first time."""
    name = f"{job['material_name'].removesuffix('_Ink')}_Color_{number:02d}" if number else job["material_name"]
    for slot, material in enumerate(data.materials):
        if material and material.name == name:
            return slot
    data.materials.append(_ink_material(name, color, fill=True))
    return len(data.materials) - 1


def _placement(image_size: tuple[int, int], size: float) -> Matrix:
    """Map the trace's 1-unit space to `size` units wide, standing up on the XZ plane (Blender's front view)."""
    width, height = image_size
    scale = size * max(width, height) / width
    return Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(scale, 4)


def _fill_layer_name(layer_name: str, number: int, job: dict) -> str:
    """`layer_name` with its Lines base swapped for the fill name, such as `Fill 2` or `Fill_001 2`."""
    return f"{layer_name.replace(job['layer_name'], job['fill_name'], 1)} {number}"


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
    by_layer = job["colors"] and job["color_target"] == COLOR_LAYERS
    if not job["colors"]:
        material = _ink_material(job["material_name"], tuple(job["color"]), job["fill"])
    elif not by_layer:
        # Color attributes tint the material's color, so it has to be white to show them true.
        material = _ink_material(job["material_name"], (1.0, 1.0, 1.0), fill=True)
    else:
        material = None
    placed = []
    for path, (object_name, layer_name, frame) in zip(job["images"], _destinations(job), strict=True):
        scene.frame_set(frame)
        image = _load_image(path, job["invert"], job["max_resolution"])
        size = tuple(image.size)
        parts = _color_parts(image, job) if job["colors"] else [(None, tuple(job["color"]), image)]

        obj = bpy.data.objects.get(object_name)
        if obj is None:
            data = bpy.data.grease_pencils.new(object_name)
            if material is not None:
                data.materials.append(material)
            obj = bpy.data.objects.new(object_name, data)
            scene.collection.objects.link(obj)
        drawings = {}
        for number, color, mask in parts:
            traced = _trace(mask, job)
            _clean_up(traced, job)
            if number is not None:
                long_side = max(size)
                _drop_small_shapes(traced.data.layers[0].frames[0].drawing, job["min_shape"] / long_side**2)
                if not traced.data.layers[0].frames[0].drawing.strokes:
                    # Everything in this color was a speck, so it gets no layer.
                    remove_object(traced)
                    bpy.data.images.remove(mask)
                    continue
            label = _fill_layer_name(layer_name, number, job) if by_layer and number else layer_name
            if label not in drawings:
                layer = obj.data.layers.get(label)
                if layer is None:
                    layer = obj.data.layers.new(label)
                    layer.use_lights = False
                drawings[label] = layer.frames.new(frame).drawing
            drawing = drawings[label]
            slot = _color_slot(obj.data, job, number, color) if by_layer and number is not None else 0
            source = traced.data.layers[0].frames[0].drawing
            material_map = [slot] * max(1, len(traced.data.materials))
            curves, points = copy_drawing(source, drawing, _placement(size, job["size"]), material_map)
            # The tracer hides its strokes and shows only fills; show them so Thickness and Fill off work.
            hidden = drawing.attributes.get("hide_stroke")
            if hidden is not None:
                drawing.attributes.remove(hidden)
            _set_radius(drawing, job["thickness"])
            if number is not None and not by_layer:
                stamp_color(drawing, curves, points, color)
            remove_object(traced)
            if mask is not image:
                bpy.data.images.remove(mask)
        bpy.data.images.remove(image)
        placed.append({"object": obj.name, "layers": list(drawings), "frame": frame, "size": size})
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
            layer.hide = layer.name not in entry["layers"]
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
    layers = list(dict.fromkeys(layer for entry in placed for layer in entry["layers"]))
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
