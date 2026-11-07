"""Grease Pencil helpers that run inside Blender, never in the engine.

`trace_grease_pencil.py` imports this module in the background Blender that traces images. The Image to Grease Pencil
node also sends this file's source to an open Blender through the Blender Lab MCP add-on and calls
`import_traced` there, so it must stay self-contained: only `bpy`, `mathutils` and `numpy`, which Blender bundles.
"""

import bpy
import numpy as np
from mathutils import Matrix, Vector

_COMPONENTS = {
    "FLOAT": ("value", 1),
    "INT": ("value", 1),
    "INT8": ("value", 1),
    "BOOLEAN": ("value", 1),
    "FLOAT_VECTOR": ("vector", 3),
    "FLOAT2": ("vector", 2),
    "FLOAT_COLOR": ("color", 4),
    "BYTE_COLOR": ("color", 4),
    "QUATERNION": ("value", 4),
}
_DTYPES = {"FLOAT": np.float32, "INT": np.int32, "INT8": np.int32, "BOOLEAN": bool}
_POSITION_ATTRIBUTES = ("position", "handle_left", "handle_right")
# Replace Existing only removes what the node made. The scene's TRACED_OBJECTS points at the traced objects, so the
# record follows a rename and a duplicate the user makes isn't taken for a trace. Layers can't be pointed at or hold
# custom properties, so the target object data's TRACED_LAYERS records them by name.
TRACED_OBJECTS = "griptape_traced_objects"
TRACED_LAYERS = "griptape_traced_layers"


def _read_attribute(attribute) -> np.ndarray:
    prop, width = _COMPONENTS[attribute.data_type]
    values = np.empty(len(attribute.data) * width, dtype=_DTYPES.get(attribute.data_type, np.float32))
    attribute.data.foreach_get(prop, values)
    return values


def copy_drawing(source, target, matrix: Matrix, material_map: list[int]) -> tuple[slice, slice]:
    """Append the strokes of drawing `source` to drawing `target`, which may already hold strokes.

    Point positions and handles are transformed by `matrix` and radii are scaled by its average scale.
    `material_map[i]` is the target material slot for source slot `i`. Returns the curve range and the point range
    the new strokes occupy in `target`.
    """
    sizes = [len(stroke.points) for stroke in source.strokes]
    if not sizes:
        return slice(0, 0), slice(0, 0)
    curve_start = len(target.strokes)
    point_start = sum(len(stroke.points) for stroke in target.strokes)
    target.add_strokes(sizes)
    new_ranges = {
        "CURVE": slice(curve_start, curve_start + len(sizes)),
        "POINT": slice(point_start, point_start + sum(sizes)),
    }
    rotation_scale = np.array(matrix.to_3x3(), dtype=np.float32)
    translation = np.array(matrix.translation, dtype=np.float32)
    radius_scale = sum(matrix.to_scale()) / 3.0
    for attribute in source.attributes:
        if attribute.data_type not in _COMPONENTS or attribute.name.startswith("."):
            continue
        values = _read_attribute(attribute)
        if attribute.name in _POSITION_ATTRIBUTES:
            values = (values.reshape(-1, 3) @ rotation_scale.T + translation).ravel()
        elif attribute.name == "radius":
            values = values * radius_scale
        elif attribute.name == "material_index":
            values = np.asarray(material_map, dtype=np.int32)[values]
        destination = target.attributes.get(attribute.name)
        if destination is None:
            destination = target.attributes.new(attribute.name, attribute.data_type, attribute.domain)
        prop, width = _COMPONENTS[attribute.data_type]
        if curve_start or point_start:
            span = new_ranges[attribute.domain]
            merged = _read_attribute(destination)
            if attribute.name == "fill_id" and span.start:
                # Strokes sharing a fill_id are filled together as one shape with holes, so keep each copy's apart.
                values = values + merged[: span.start].max() + 1
            merged[span.start * width : span.stop * width] = values
            values = merged
        destination.data.foreach_set(prop, values)
    target.tag_positions_changed()
    return new_ranges["CURVE"], new_ranges["POINT"]


def clear_fill(drawing, curves: slice) -> None:
    """Stop the strokes in `curves` from filling. Open strokes fill by joining their ends, which a line must not do."""
    attribute = drawing.attributes.get("fill_opacity")
    if attribute is None:
        attribute = drawing.attributes.new("fill_opacity", "FLOAT", "CURVE")
        attribute.data.foreach_set("value", np.ones(len(attribute.data), dtype=np.float32))
    values = _read_attribute(attribute)
    values[curves] = 0.0
    attribute.data.foreach_set("value", values)


def stamp_color(drawing, curves: slice, points: slice, color: tuple[float, float, float]) -> None:
    """Set the color attributes of some strokes, so they show `color` (linear RGB) in Vertex Paint's color data.

    `fill_color` is per stroke and `vertex_color` is per point. The material must be white for them to show true.
    """
    rgba = (*color, 1.0)
    for name, domain, span in (("fill_color", "CURVE", curves), ("vertex_color", "POINT", points)):
        attribute = drawing.attributes.get(name)
        if attribute is None:
            attribute = drawing.attributes.new(name, "FLOAT_COLOR", domain)
        values = _read_attribute(attribute).reshape(-1, 4)
        values[span] = rgba
        attribute.data.foreach_set("color", values.ravel())


def _base_name(name: str) -> str:
    stem, _, suffix = name.rpartition(".")
    return stem if stem and suffix.isdigit() else name


def _copy_style(source, target) -> None:
    target.grease_pencil.color = source.grease_pencil.color
    target.grease_pencil.fill_color = source.grease_pencil.fill_color
    if bpy.app.version < (5, 3, 0):
        target.grease_pencil.show_stroke = source.grease_pencil.show_stroke
        target.grease_pencil.show_fill = source.grease_pencil.show_fill


def _material_slots(source_materials, target_data) -> list[int]:
    """Target slot for each source material. A target material with the same name is reused and takes its colors."""
    slots = []
    for material in source_materials:
        index = next(
            (
                i
                for i, existing in enumerate(target_data.materials)
                if existing and existing.name == _base_name(material.name)
            ),
            -1,
        )
        if index >= 0:
            if target_data.materials[index] != material:
                _copy_style(material, target_data.materials[index])
        else:
            target_data.materials.append(material)
            index = len(target_data.materials) - 1
        slots.append(index)
    return slots


def _earlier_trace(name: str, requested: str) -> bool:
    """Whether `name` is `requested` or a copy Blender suffixed because `requested` was taken."""
    return name == requested or _base_name(name) == requested


def _is_fill_layer(name: str, fill_prefixes: list[str]) -> bool:
    """Whether `name` is a numbered fill layer such as `Fill 4`, with a prefix from `fill_prefixes` such as `Fill `."""
    base = _base_name(name)
    return any(base.startswith(prefix) and base[len(prefix) :].isdigit() for prefix in fill_prefixes)


def _recorded_objects(scene) -> list:
    record = scene.get(TRACED_OBJECTS)
    return [obj for obj in record.values() if isinstance(obj, bpy.types.Object)] if hasattr(record, "values") else []


def _recorded_layers(data) -> set[str]:
    return {name for name in data.get(TRACED_LAYERS, []) if name in data.layers}


def copy_layer(source_object, source_layer, target_object, layer_name: str, matrix: Matrix):
    """Copy `source_layer` with all its frames into `target_object` as a new layer named `layer_name`.

    `matrix` maps the source layer's points into the target object's space.
    """
    material_map = _material_slots(source_object.data.materials, target_object.data)
    layer = target_object.data.layers.new(layer_name)
    layer.use_lights = source_layer.use_lights
    for frame in source_layer.frames:
        copy_drawing(frame.drawing, layer.frames.new(frame.frame_number).drawing, matrix, material_map)
    return layer


def remove_object(obj) -> None:
    """Remove `obj`, then its Grease Pencil data and materials if nothing else uses them."""
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is None or data.users:
        return
    materials = [material for material in data.materials if material is not None]
    bpy.data.grease_pencils.remove(data)
    for material in materials:
        if material.users == 0:
            bpy.data.materials.remove(material)


def _collection(scene, name: str):
    if not name:
        return bpy.context.view_layer.active_layer_collection.collection
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if collection not in scene.collection.children_recursive and collection != scene.collection:
        scene.collection.children.link(collection)
    return collection


def import_traced(  # noqa: PLR0913
    blend_path: str,
    object_names: list[str],
    layer_names: list[str],
    collection: str = "",
    location: list[float] | None = None,
    target_object: str = "",
    replace: bool = True,
    fill_prefixes: list[str] | None = None,
) -> dict:
    """Bring traced Grease Pencil objects from `blend_path` into the current scene.

    With no `target_object`, each object is added to `collection` (the active collection when empty). With a
    `target_object`, each traced layer is copied onto that Grease Pencil object instead, keeping the layer's name.
    The drawing lands at `location`, or at the 3D cursor when `location` is None. `replace` removes an earlier
    object or layer with the same name, so a re-run swaps it out rather than adding a copy. It only removes what an
    earlier trace made, and only once the new trace has loaded.
    """
    scene = bpy.context.scene
    origin = Vector(location) if location is not None else scene.cursor.location.copy()

    target = None
    if target_object:
        target = bpy.data.objects.get(target_object)
        if target is None or target.type != "GREASEPENCIL":
            msg = f"There is no Grease Pencil object named '{target_object}' in the open file."
            raise ValueError(msg)

    with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
        names = [name for name in object_names if name in data_from.objects]
        data_to.objects = list(names)
    named = [(name, obj) for name, obj in zip(names, data_to.objects, strict=True) if obj is not None]
    loaded = [obj for _, obj in named]
    if not loaded:
        msg = f"No traced objects were found in {blend_path}."
        raise ValueError(msg)

    if target is None:
        traced_objects = _recorded_objects(scene)
        if replace:
            # A loaded object whose name was taken comes in with a numeric suffix. It takes the name back, or the
            # name of the earlier trace it replaces, so its name stays the same from run to run.
            for name, obj in named:
                earlier = sorted(
                    (o for o in traced_objects if o != obj and _earlier_trace(o.name, name)), key=lambda o: o.name
                )
                earlier_names = [o.name for o in earlier]
                for existing in earlier:
                    traced_objects.remove(existing)
                    remove_object(existing)
                for wanted in [name, *earlier_names]:
                    if bpy.data.objects.get(wanted) is None:
                        obj.name = wanted
                        break
        destination = _collection(scene, collection)
        for obj in loaded:
            destination.objects.link(obj)
            obj.location = origin + obj.location
        scene[TRACED_OBJECTS] = {str(i): obj for i, obj in enumerate([*traced_objects, *loaded])}
        return {"objects": [obj.name for obj in loaded], "layers": list(layer_names), "target_object": ""}

    fill_prefixes = fill_prefixes or []
    traced_layers = _recorded_layers(target.data)
    if replace:
        for existing in list(target.data.layers):
            replaced = any(_earlier_trace(existing.name, name) for name in layer_names)
            # An earlier trace may have made more fill layers than this one, so its other fills go too.
            if existing.name in traced_layers and (replaced or _is_fill_layer(existing.name, fill_prefixes)):
                traced_layers.discard(existing.name)
                target.data.layers.remove(existing)
    layers = []
    for obj in loaded:
        matrix = target.matrix_world.inverted() @ Matrix.Translation(origin) @ obj.matrix_basis
        for source_layer in list(obj.data.layers):
            layers.append(copy_layer(obj, source_layer, target, source_layer.name, matrix).name)
        remove_object(obj)
    target.data[TRACED_LAYERS] = sorted(traced_layers | set(layers))
    return {"objects": [target.name], "layers": layers, "target_object": target.name}
