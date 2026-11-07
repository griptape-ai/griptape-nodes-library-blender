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

from grease_pencil_scene import (  # noqa: E402
    _read_attribute,
    clear_fill,
    copy_drawing,
    import_traced,
    remove_object,
    stamp_color,
)

SUCCESS_MARKER = "TRACE_OK"
COLOR_LAYERS = "layers"
PREVIEW_MARGIN = 1.05
PREVIEW_MAX_SIZE = 1024
CLUSTER_SAMPLE = 50000
CLUSTER_ITERATIONS = 12
CLUSTER_CHUNK = 500_000
HALO = 2
COLOR_MATCH = 0.02  # linear RGB distance within which a fill of the same number reuses another image's material
BORDER_SHARE = 0.05  # a color needs this share of the image border to count as a candidate for the paper
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


def _solid_image(lines: dict):
    """The solid areas of a centerline trace as an image the tracer can read, or None when there are none."""
    return bpy.data.images.load(lines["solid"]) if lines["solid"] else None


def _ink_mask(flat: np.ndarray, width: int, height: int, job: dict, lines: dict | None) -> np.ndarray:
    """Pixels that are line art. With a centerline trace this is the mask the engine used, so the colors are split
    around the same lines that were traced; otherwise every pixel whose brightest channel is below the threshold."""
    if lines is not None and lines["ink"]:
        picture = bpy.data.images.load(lines["ink"])
        if tuple(picture.size) == (width, height):
            pixels = np.empty(width * height * 4, dtype=np.float32)
            picture.pixels.foreach_get(pixels)
            bpy.data.images.remove(picture)
            return (pixels.reshape(-1, 4)[:, 0] < 0.5).reshape(height, width)  # noqa: PLR2004
        bpy.data.images.remove(picture)
    return (flat.max(axis=1) < job["threshold"]).reshape(height, width)


def _color_parts(
    image, job: dict, lines: dict | None
) -> tuple[list[tuple[int, tuple[float, float, float], object]], tuple[float, float, float] | None]:
    """Split an image into the line art and its flat colors. Returns the parts and the linear RGB of the paper that
    was skipped, or None when no paper was skipped. A part is (number, linear RGB, mask image).

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
    ink = _ink_mask(flat, width, height, job, lines)
    rest = flat[~ink.ravel()]
    parts = []
    paper = None
    if len(rest):
        centers = _cluster(rest, job["colors"])
        labels = _nearest(flat, centers).reshape(height, width)
        areas = np.bincount(labels[~ink], minlength=len(centers))
        # The paper often splits into a few near-identical clusters, so every color close to the paper color is
        # skipped, or the others trace as hundreds of blobs. The paper is the lightest color with a real share of
        # the border. The color touching most of it can be a painted area that runs off the edge, such as a hillside.
        background = set()
        if job["skip_background"]:
            border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
            on_border = np.bincount(border, minlength=len(centers))
            touching = np.flatnonzero(on_border >= BORDER_SHARE * len(border))
            if not len(touching):
                touching = np.array([on_border.argmax()])
            edge = int(touching[np.argmax((centers @ LUMA)[touching])])
            distance = np.linalg.norm(centers - centers[edge], axis=1)
            background = {int(i) for i in np.flatnonzero(distance <= job["background_tolerance"])} | {edge}
            red, green, blue = (float(v) for v in _srgb_to_linear(centers[edge]))
            paper = (red, green, blue)
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
            if lines is not None:
                # A centerline stroke is thinner than the ink where lines meet, so a fill that stops at the ink would
                # leave a white gap there. Let it grow into the ink next to it, under the stroke. It stops at the ink, or it
                # would spill past the outline as a thin colored fringe on the paper side.
                grow = HALO + int(np.ceil(lines["line_radius"])) + 1
                region = region | (_dilate(region, grow) & ink)
            color = _srgb_to_linear(centers[cluster])
            mask = _mask_image(region, f"Mask_{number[int(cluster)]}", job["preview_dir"])
            parts.append((number[int(cluster)], (float(color[0]), float(color[1]), float(color[2])), mask))
    if lines is not None:
        # The lines are strokes; only the solid areas are outlined.
        if lines["strokes"] or lines["solid"]:
            parts.append((0, tuple(job["color"]), _solid_image(lines)))
    elif ink.any():
        parts.append((0, tuple(job["color"]), _mask_image(ink, "Mask_ink", job["preview_dir"])))
    if not parts:
        msg = "The image has only one color, so there is nothing to trace after skipping the background."
        raise ValueError(msg)
    return parts, paper


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


def _set_radius(drawing, radius: float, points: slice) -> None:
    """Give the points in `points` a radius, leaving the rest of the drawing as it is."""
    total = sum(len(stroke.points) for stroke in drawing.strokes)
    if not total:
        return
    attribute = drawing.attributes.get("radius")
    if attribute is None:
        attribute = drawing.attributes.new("radius", "FLOAT", "POINT")
    values = _read_attribute(attribute)
    values[points] = radius
    attribute.data.foreach_set("value", values)


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
    """Material slot for color `number` on `data`. Each image is clustered on its own, so the same number can be a
    different color on another image. A material is reused only when its color is close, or the new color gets its
    own material, which Blender suffixes (`.001`)."""
    name = f"{job['material_name'].removesuffix('_Ink')}_Color_{number:02d}" if number else job["material_name"]
    for slot, material in enumerate(data.materials):
        if material and (material.name == name or material.name.startswith(f"{name}.")):
            fill = material.grease_pencil.fill_color
            if max(abs(fill[axis] - color[axis]) for axis in range(3)) <= COLOR_MATCH:
                return slot
    # Color fills always fill. The Fill setting is for the line art, which is number 0.
    data.materials.append(_ink_material(name, color, fill=job["fill"] if number == 0 else True))
    return len(data.materials) - 1


def _placement(image_size: tuple[int, int], size: float) -> Matrix:
    """Map the trace's 1-unit space to `size` units wide, standing up on the XZ plane (Blender's front view)."""
    width, height = image_size
    scale = size * max(width, height) / width
    return Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(scale, 4)


def _add_centerlines(drawing, lines: dict, matrix: Matrix, slot: int) -> tuple[slice, slice]:
    """Append the centerline strokes to `drawing`. Returns the curve and point ranges they occupy."""
    width, height = lines["size"]
    longest = max(width, height)
    strokes = lines["strokes"]
    data = bpy.data.grease_pencils.new("Centerlines")
    source = data.layers.new("Centerlines").frames.new(1).drawing
    source.add_strokes([len(stroke["pts"]) for stroke in strokes])
    positions = np.array(
        [
            ((col - width / 2) / longest, (height / 2 - row) / longest, 0.0)
            for stroke in strokes
            for row, col in stroke["pts"]
        ],
        dtype=np.float32,
    )
    radii = np.array([radius / longest for stroke in strokes for radius in stroke["r"]], dtype=np.float32)
    source.attributes["position"].data.foreach_set("vector", positions.ravel())
    source.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", radii)
    # copy_drawing maps material slots by this attribute, so without it the strokes land in slot 0.
    source.attributes.new("material_index", "INT", "CURVE").data.foreach_set(
        "value", np.zeros(len(strokes), dtype=np.int32)
    )
    source.tag_positions_changed()
    curves, points = copy_drawing(source, drawing, matrix, [slot])
    clear_fill(drawing, curves)
    ranges = (curves, points)
    bpy.data.grease_pencils.remove(data)
    return ranges


def _fill_layer_name(layer_name: str, number: int, job: dict) -> str:
    """`layer_name` with its Lines base swapped for the fill name, such as `Fill 2` or `Fill_001 2`."""
    return f"{layer_name.replace(job['layer_name'], job['fill_name'], 1)} {number}"


def _fill_prefixes(job: dict) -> list[str]:
    """The start of every fill layer name this job can make, such as `Fill ` for `Fill 2`."""
    names = {layer for _, layer, _ in _destinations(job)}
    return sorted({f"{name.replace(job['layer_name'], job['fill_name'], 1)} " for name in names})


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
        materials = [_ink_material(job["material_name"], tuple(job["color"]), job["fill"])]
    elif not by_layer:
        # Color attributes tint the material's color, so it has to be white to show them true. The color fills are
        # slot 0 and always fill. The line art is slot 1 so the Fill setting reaches it alone, as it does in Layers mode.
        fills = f"{job['material_name'].removesuffix('_Ink')}_Color"
        materials = [
            _ink_material(fills, (1.0, 1.0, 1.0), fill=True),
            _ink_material(job["material_name"], (1.0, 1.0, 1.0), fill=job["fill"]),
        ]
    else:
        materials = []
    centerlines = job["centerline"] or [""] * len(job["images"])
    placed = []
    for path, (object_name, layer_name, frame), lines_path in zip(
        job["images"], _destinations(job), centerlines, strict=True
    ):
        scene.frame_set(frame)
        image = _load_image(path, job["invert"], job["max_resolution"])
        size = tuple(image.size)
        lines = json.loads(Path(lines_path).read_text(encoding="utf-8")) if lines_path else None
        if lines is not None and tuple(image.size) != tuple(lines["size"]):
            # The engine traced a rescaled copy, so match it or the ink mask for the colors would not line up.
            image.scale(*lines["size"])
        size = tuple(image.size)
        paper = None
        if job["colors"]:
            parts, paper = _color_parts(image, job, lines)
        elif lines is not None:
            parts = [(None, tuple(job["color"]), _solid_image(lines))]
        else:
            parts = [(None, tuple(job["color"]), image)]

        obj = bpy.data.objects.get(object_name)
        if obj is None:
            data = bpy.data.grease_pencils.new(object_name)
            for material in materials:
                data.materials.append(material)
            obj = bpy.data.objects.new(object_name, data)
            scene.collection.objects.link(obj)
        drawings = {}
        for number, color, mask in parts:
            is_ink = number in (None, 0)
            strokes = lines["strokes"] if lines is not None and is_ink else []
            traced = None
            if mask is not None:
                traced = _trace(mask, job)
                _clean_up(traced, job)
                if number is not None:
                    long_side = max(size)
                    _drop_small_shapes(traced.data.layers[0].frames[0].drawing, job["min_shape"] / long_side**2)
                if not traced.data.layers[0].frames[0].drawing.strokes:
                    remove_object(traced)
                    traced = None
            if traced is None and not strokes:
                # Everything in this color was a speck, so it gets no layer.
                if mask is not None and mask is not image:
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
            if by_layer and number is not None:
                slot = _color_slot(obj.data, job, number, color)
            else:
                slot = 1 if materials[1:] and number == 0 else 0
            placement = _placement(size, job["size"])
            ranges = []
            if traced is not None:
                source = traced.data.layers[0].frames[0].drawing
                material_map = [slot] * max(1, len(traced.data.materials))
                curves, points = copy_drawing(source, drawing, placement, material_map)
                # The tracer hides its strokes and shows only fills; show them so Thickness and Fill off work.
                hidden = drawing.attributes.get("hide_stroke")
                if hidden is not None:
                    drawing.attributes.remove(hidden)
                _set_radius(drawing, job["thickness"], points)
                ranges.append((curves, points))
                remove_object(traced)
            if strokes and lines is not None:
                ranges.append(_add_centerlines(drawing, lines, placement, slot))
            if number is not None and not by_layer:
                for curves, points in ranges:
                    stamp_color(drawing, curves, points, color)
            if mask is not None and mask is not image:
                bpy.data.images.remove(mask)
        bpy.data.images.remove(image)
        if by_layer:
            # A fill first seen on a later image is created above `Lines`, and it would cover part of the strokes.
            lines_layer = obj.data.layers.get(layer_name)
            if lines_layer is not None:
                obj.data.layers.move_top(lines_layer)
        if job["mode"] == "animated":
            # A layer holds its last keyframe until the next one, so a layer this image did not draw on needs an empty
            # keyframe here, or the previous image's drawing would stay on screen.
            for layer in obj.data.layers:
                if (
                    layer.name not in drawings
                    and len(layer.frames)
                    and all(f.frame_number != frame for f in layer.frames)
                ):
                    layer.frames.new(frame)
        placed.append({"object": obj.name, "layers": list(drawings), "frame": frame, "size": size, "paper": paper})
    return placed


def _reload_scene(job: dict) -> None:
    """Save the scene and open it again. Bezier fills made through the API render from their control points, as
    polygons, until the file is reloaded, which would make the previews wrong."""
    path = Path(job["preview_dir"]) / "reload.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
    bpy.ops.wm.open_mainfile(filepath=str(path))
    path.unlink()


def _render_previews(placed: list[dict], job: dict) -> list[str]:
    """Render each traced image through an orthographic camera framing the image's area. The background is the paper
    color that was skipped, or white when none was."""
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
        paper = entry["paper"] or (1.0, 1.0, 1.0)
        background.inputs["Color"].default_value = (*paper, 1.0)
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
    _reload_scene(job)
    previews = _render_previews(placed, job)
    _save_traced(objects, job["output_blend"])

    result = {
        "objects": objects,
        "layers": layers,
        "fill_prefixes": _fill_prefixes(job),
        "previews": previews,
        "target_blend": None,
    }
    if job["target_blend"]:
        bpy.ops.wm.open_mainfile(filepath=job["target_blend"])
        result["target_blend"] = import_traced(
            job["output_blend"], objects, layers, fill_prefixes=_fill_prefixes(job), **job["import"]
        )
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
