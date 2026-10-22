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
# Replace Existing only removes what the node made, recorded by name: objects in the scene's TRACED_OBJECTS, layers
# in the target object data's TRACED_LAYERS. Names rather than a tag on the object, so a duplicate the user makes of
# a traced object isn't taken for one (and layers can't hold custom properties).
TRACED_OBJECTS = "griptape_traced_objects"
TRACED_LAYERS = "griptape_traced_layers"


def _read_attribute(attribute) -> np.ndarray:
    prop, width = _COMPONENTS[attribute.data_type]
    values = np.empty(len(attribute.data) * width, dtype=_DTYPES.get(attribute.data_type, np.float32))
    attribute.data.foreach_get(prop, values)
    return values


def copy_drawing(source, target, matrix: Matrix, material_map: list[int]) -> None:
    """Append the strokes of drawing `source` to the empty drawing `target`.

    Point positions and handles are transformed by `matrix` and radii are scaled by its average scale.
    `material_map[i]` is the target material slot for source slot `i`.
    """
    sizes = [len(stroke.points) for stroke in source.strokes]
    if not sizes:
        return
    target.add_strokes(sizes)
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
        destination.data.foreach_set(_COMPONENTS[attribute.data_type][0], values)
    target.tag_positions_changed()


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


def _recorded(owner, key: str, existing) -> set[str]:
    """Names recorded on `owner` under `key` that still exist in `existing`."""
    return {name for name in owner.get(key, []) if name in existing}


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
        traced_objects = _recorded(scene, TRACED_OBJECTS, bpy.data.objects)
        if replace:
            # A loaded object whose name was taken comes in with a numeric suffix. It takes the name back, or the
            # name of the earlier trace it replaces, so its name stays the same from run to run.
            for name, obj in named:
                earlier = sorted(n for n in traced_objects if _earlier_trace(n, name) and bpy.data.objects[n] != obj)
                for existing in earlier:
                    traced_objects.discard(existing)
                    remove_object(bpy.data.objects[existing])
                for wanted in [name, *earlier]:
                    if bpy.data.objects.get(wanted) is None:
                        obj.name = wanted
                        break
        destination = _collection(scene, collection)
        for obj in loaded:
            destination.objects.link(obj)
            obj.location = origin + obj.location
        scene[TRACED_OBJECTS] = sorted(traced_objects | {obj.name for obj in loaded})
        return {"objects": [obj.name for obj in loaded], "layers": list(layer_names), "target_object": ""}

    traced_layers = _recorded(target.data, TRACED_LAYERS, target.data.layers)
    if replace:
        for existing in list(target.data.layers):
            if existing.name in traced_layers and any(_earlier_trace(existing.name, name) for name in layer_names):
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
