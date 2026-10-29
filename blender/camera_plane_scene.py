"""Camera image planes that run inside Blender, never in the engine.

The Image to Camera Plane and Get Camera Plane nodes send this file's source to an open Blender through the Blender Lab
MCP add-on and call `add_camera_plane` or `get_camera_plane` there, so it must stay self-contained: only `bpy` and
`mathutils`, which Blender bundles.

A camera plane is a textured plane parented to a camera. It follows every camera move, and it renders. Blender's
camera background images do neither of those once you render, which is why a plane is used.

The planes are laid out the way the Camera Plane add-on (Les Fees Speciales, on extensions.blender.org) lays out the
ones it imports, so the two work together on the same camera. A plane is one unit wide, and its distance and scale are
the custom property `camera_plane` (`distance` and `scale`, the scale as a percentage). Drivers on its delta location
and delta scale turn those into a position and size from the camera's field of view and lens shift, so the plane keeps
fitting the frame when the lens changes, and changing its distance in the add-on's panel moves it and resizes it to
match. The plane's transforms are locked, and it is drawn with a wireframe outline. This file does not need the add-on.

Planes this module makes also carry `CAMERA_PLANE`, set to the name they were made with, as a custom property, so a
re-run replaces them and nothing the user made is ever removed.
"""

import math

import bpy
from mathutils import Vector

CAMERA_PLANE = "griptape_camera_plane"
ADDON_PROPERTY = "camera_plane"  # The Camera Plane add-on's own property: {"distance": ..., "scale": ...}.
OPACITY_NODE = "Opacity Multiplier"  # The add-on's name for the group that scales the image's alpha.
SHADELESS_NODE = "Camera Plane Shadeless"
FIT_FILL = "fill"
FIT_CONTAIN = "fit"
FIT_STRETCH = "stretch"
FITS = (FIT_FILL, FIT_CONTAIN, FIT_STRETCH)
DEFAULT_NAME = "Camera Plane"


def _scene(name: str):
    if not name:
        return bpy.context.scene
    scene = bpy.data.scenes.get(name)
    if scene is None:
        msg = f"There is no scene named '{name}' in the open file."
        raise ValueError(msg)
    return scene


def _camera(scene, name: str):
    """The camera object named `name`, or the scene's active camera when `name` is empty."""
    if name:
        camera = bpy.data.objects.get(name)
        if camera is None or camera.type != "CAMERA":
            msg = f"There is no camera named '{name}' in the open file."
            raise ValueError(msg)
        return camera
    if scene.camera is None:
        msg = f"The scene '{scene.name}' has no active camera."
        raise ValueError(msg)
    return scene.camera


def _frame_corners(camera, scene, distance: float) -> list[Vector]:
    """The four corners of the camera's frame at `distance` in front of the lens, in camera space.

    `view_frame` already accounts for the sensor, sensor fit, aspect ratio and lens shift, so the plane fits whatever
    the camera is set to. A perspective frame grows with distance. An orthographic frame does not.
    """
    corners = [Vector(corner) for corner in camera.data.view_frame(scene=scene)]
    if camera.data.type == "ORTHO":
        return [Vector((corner.x, corner.y, -distance)) for corner in corners]
    return [corner * (distance / -corner.z) for corner in corners]


def _plane_size(frame_size: tuple[float, float], media_size: tuple[int, int], fit: str) -> tuple[float, float]:
    """Width and height of the plane for a media of `media_size` pixels in a frame of `frame_size`."""
    frame_w, frame_h = frame_size
    media_w, media_h = media_size
    if fit == FIT_STRETCH or media_w <= 0 or media_h <= 0:
        return frame_w, frame_h
    media_aspect = media_w / media_h
    wider = media_aspect > frame_w / frame_h
    # Fit keeps the whole media inside the frame. Fill covers the frame and lets the overflow fall outside it.
    if wider == (fit == FIT_CONTAIN):
        return frame_w, frame_w / media_aspect
    return frame_h * media_aspect, frame_h


def _image_node(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    return next((node for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image), None)


def _plane_media(obj):
    """The Image Texture node a mesh object shows, from its first material that has one, or None."""
    for slot in obj.material_slots:
        node = _image_node(slot.material)
        if node is not None:
            return node
    return None


def _remove_plane(obj, keep=None) -> None:
    """Remove `obj`, then its mesh, materials and images if nothing else uses them. `keep` is an image to leave alone."""
    mesh = obj.data
    materials = [slot.material for slot in obj.material_slots if slot.material]
    images = [node.image for material in materials for node in _image_nodes(material)]
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    for material in materials:
        if material.users == 0:
            bpy.data.materials.remove(material)
    for image in images:
        if image != keep and image.users == 0:
            bpy.data.images.remove(image)


def _image_nodes(material) -> list:
    if not material.use_nodes or material.node_tree is None:
        return []
    return [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image]


def _opacity_group():
    """The node group the add-on uses for a plane's opacity: Base times Multiplier."""
    group = bpy.data.node_groups.get(OPACITY_NODE)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(OPACITY_NODE, "ShaderNodeTree")
    group.interface.new_socket(name="Base", in_out="INPUT", socket_type="NodeSocketFloat")
    multiplier = group.interface.new_socket(name="Multiplier", in_out="INPUT", socket_type="NodeSocketFloat")
    multiplier.default_value, multiplier.min_value, multiplier.max_value = 1.0, 0.0, 1.0
    group.interface.new_socket(name="Value", in_out="OUTPUT", socket_type="NodeSocketFloat")
    inputs = group.nodes.new("NodeGroupInput")
    outputs = group.nodes.new("NodeGroupOutput")
    outputs.location.x = 200
    inputs.location.x = -200
    multiply = group.nodes.new("ShaderNodeMath")
    multiply.operation = "MULTIPLY"
    group.links.new(inputs.outputs["Base"], multiply.inputs[0])
    group.links.new(inputs.outputs["Multiplier"], multiply.inputs[1])
    group.links.new(multiply.outputs[0], outputs.inputs["Value"])
    return group


def _shadeless_group():
    """A node group that shows its Color as it is to the camera, and acts as a plain grey surface to every other ray.

    The plane then looks exactly like the image, without lighting the scene or being lit. Blender's own Import Images as
    Planes builds the same behaviour for its Shadeless option: emission for a camera ray, diffuse for the rays that
    bounce off it, and emission again for shadow rays and for the first glossy bounce so it still casts and shows.
    """
    group = bpy.data.node_groups.get(SHADELESS_NODE)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(SHADELESS_NODE, "ShaderNodeTree")
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
    inputs = group.nodes.new("NodeGroupInput")
    outputs = group.nodes.new("NodeGroupOutput")
    path = group.nodes.new("ShaderNodeLightPath")
    diffuse = group.nodes.new("ShaderNodeBsdfDiffuse")
    emission = group.nodes.new("ShaderNodeEmission")
    mix = group.nodes.new("ShaderNodeMixShader")

    def math(operation: str, first, second):
        node = group.nodes.new("ShaderNodeMath")
        node.operation = operation
        for index, value in enumerate((first, second)):
            if isinstance(value, (int, float)):
                node.inputs[index].default_value = value
            else:
                group.links.new(value, node.inputs[index])
        return node.outputs[0]

    # 1 for a camera ray, which has no bounces. 0 or less for a ray that has bounced off something.
    camera_ray = math("SUBTRACT", 1.0, math("SUBTRACT", path.outputs["Ray Depth"], path.outputs["Transmission Depth"]))
    first_glossy = math("MULTIPLY", math("SUBTRACT", 2.0, path.outputs["Ray Depth"]), path.outputs["Is Glossy Ray"])
    factor = math("MAXIMUM", math("MAXIMUM", first_glossy, path.outputs["Is Shadow Ray"]), camera_ray)
    group.links.new(inputs.outputs["Color"], diffuse.inputs["Color"])
    group.links.new(inputs.outputs["Color"], emission.inputs["Color"])
    group.links.new(factor, mix.inputs["Fac"])
    group.links.new(diffuse.outputs["BSDF"], mix.inputs[1])
    group.links.new(emission.outputs["Emission"], mix.inputs[2])
    group.links.new(mix.outputs["Shader"], outputs.inputs["Shader"])
    return group


def _make_material(name: str, image, scene):
    """A material that shows `image` as it is, with the image's alpha (times an opacity slider) as transparency."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Linear"
    texture.extension = "CLIP"
    if image.source in {"MOVIE", "SEQUENCE"}:
        texture.image_user.frame_duration = image.frame_duration
        texture.image_user.frame_start = scene.frame_start
        texture.image_user.use_auto_refresh = True
    opacity = tree.nodes.new("ShaderNodeGroup")
    opacity.node_tree = _opacity_group()
    opacity.name = opacity.label = OPACITY_NODE
    opacity.inputs["Multiplier"].default_value = 1.0
    shadeless = tree.nodes.new("ShaderNodeGroup")
    shadeless.node_tree = _shadeless_group()
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(texture.outputs["Color"], shadeless.inputs["Color"])
    tree.links.new(texture.outputs["Alpha"], opacity.inputs["Base"])
    tree.links.new(opacity.outputs["Value"], mix.inputs["Fac"])
    tree.links.new(transparent.outputs["BSDF"], mix.inputs[1])
    tree.links.new(shadeless.outputs["Shader"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
    material.use_backface_culling = False
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    if hasattr(material, "show_transparent_back"):
        material.show_transparent_back = True
    return material


def _add_driver(plane, data_path: str, index: int, expression: str, variables: list[tuple[str, object, str]]) -> None:
    driver = plane.driver_add(data_path, index).driver
    driver.type = "SCRIPTED"
    for name, target, path in variables:
        variable = driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = target
        variable.targets[0].data_path = path
    driver.expression = expression


def _add_drivers(plane, cam) -> None:
    """Tie the plane's position and size to its distance and scale and to the camera's field of view and lens shift.

    A plane one unit wide, `distance` in front of the lens and scaled by `tan(FOV / 2) * distance * 2`, is exactly as
    wide as the camera's frame at that distance. `scale` is a percentage on top of that.
    """
    distance = ("distance", plane, f'["{ADDON_PROPERTY}"]["distance"]')
    scale = ("scale", plane, f'["{ADDON_PROPERTY}"]["scale"]')
    fov = ("FOV", cam, "data.angle")
    for axis, shift in enumerate(("data.shift_x", "data.shift_y")):
        _add_driver(
            plane, "delta_location", axis, "tan(FOV/2) * distance*2 * shift", [distance, fov, ("shift", cam, shift)]
        )
    _add_driver(plane, "delta_location", 2, "-distance", [distance])
    for axis in range(2):
        _add_driver(plane, "delta_scale", axis, "tan(FOV/2) * distance*2 * scale/100.0", [distance, fov, scale])


def _lock(plane) -> None:
    plane.lock_location = (True, True, True)
    plane.lock_rotation = (True, True, True)
    plane.lock_rotation_w = True
    plane.lock_scale = (True, True, True)


def _media_size(image) -> tuple[int, int]:
    width, height = image.size
    return int(width), int(height)


def _collection_for(camera, scene):
    return camera.users_collection[0] if camera.users_collection else scene.collection


def add_camera_plane(  # noqa: PLR0913
    path: str,
    camera: str = "",
    scene: str = "",
    distance: float = 10.0,
    fit: str = FIT_FILL,
    name: str = DEFAULT_NAME,
    replace: bool = True,
) -> dict:
    """Add the image or movie at `path` to a camera as a plane that fills its frame at `distance`.

    `camera` defaults to the scene's active camera and `scene` to the open window's scene. `fit` is `fill` (cover the
    frame), `fit` (keep the whole image inside it) or `stretch` (match the frame exactly). `replace` removes the plane
    this function made earlier on the same camera and with the same name, once the new one has loaded, so a re-run
    swaps it out rather than adding a copy.
    """
    if fit not in FITS:
        msg = f"Fit must be one of {', '.join(FITS)}, not '{fit}'."
        raise ValueError(msg)
    if distance <= 0:
        msg = "The distance must be more than 0."
        raise ValueError(msg)
    name = name.strip() or DEFAULT_NAME
    target_scene = _scene(scene)
    cam = _camera(target_scene, camera)
    try:
        image = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as e:
        msg = f"Blender could not load {path}: {e}"
        raise ValueError(msg) from e
    image.reload()  # A file rewritten at the same path (the node overwrites its own copies) must be read again.

    ortho = cam.data.type == "ORTHO"
    corners = _frame_corners(cam, target_scene, distance)
    xs = [corner.x for corner in corners]
    ys = [corner.y for corner in corners]
    frame_size = (max(xs) - min(xs), max(ys) - min(ys))
    media_size = _media_size(image)

    # The marker holds the name the plane was made with, so a rename or a dot in the name cannot hide it from a re-run.
    earlier = [obj for obj in cam.children if obj.get(CAMERA_PLANE) == name]
    if replace:
        for obj in earlier:
            _remove_plane(obj, keep=image)

    if ortho:
        # An orthographic frame does not grow with distance, so the add-on's drivers do not apply: the size is baked.
        center = sum(corners, Vector()) / len(corners)
        width, height = _plane_size(frame_size, media_size, fit)
        half_w, half_h = width / 2, height / 2
        vertices = [
            (center.x - half_w, center.y - half_h, center.z),
            (center.x + half_w, center.y - half_h, center.z),
            (center.x + half_w, center.y + half_h, center.z),
            (center.x - half_w, center.y + half_h, center.z),
        ]
        percent = 100.0
    else:
        # A one-unit-wide plane with the image's shape. `scale` sizes it against the frame, as the drivers expect.
        unit = 2 * math.tan(cam.data.angle / 2) * distance
        frame_w, frame_h = frame_size[0] / unit, frame_size[1] / unit
        if fit == FIT_STRETCH or media_size[0] <= 0 or media_size[1] <= 0:
            aspect, factor = frame_w / frame_h, frame_w
        else:
            aspect = media_size[0] / media_size[1]
            to_fit, to_fill = min(frame_w, frame_h * aspect), max(frame_w, frame_h * aspect)
            factor = to_fit if fit == FIT_CONTAIN else to_fill
        half_h = 0.5 / aspect
        vertices = [(-0.5, -half_h, 0.0), (0.5, -half_h, 0.0), (-0.5, half_h, 0.0), (0.5, half_h, 0.0)]
        percent = round(factor * 100.0, 4)
        width, height = factor * unit, factor * unit / aspect

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], [(0, 1, 3, 2) if not ortho else (0, 1, 2, 3)])
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(mesh.loops, [(0, 0), (1, 0), (1, 1), (0, 1)], strict=True):
        uv_layer.data[loop.index].uv = uv
    mesh.materials.append(_make_material(name, image, target_scene))

    plane = bpy.data.objects.new(name, mesh)
    plane[CAMERA_PLANE] = name
    _collection_for(cam, target_scene).objects.link(plane)
    plane.parent = cam
    plane.show_wire = True
    if not ortho:
        plane[ADDON_PROPERTY] = {"distance": distance, "scale": percent}
        _add_drivers(plane, cam)
        _lock(plane)
    return {
        "scene": target_scene.name,
        "camera": cam.name,
        "plane": plane.name,
        "distance": distance,
        "width": width,
        "height": height,
        "source": image.source,
        "frames": int(image.frame_duration) if image.source in {"MOVIE", "SEQUENCE"} else 0,
        "path": bpy.path.abspath(image.filepath),
        "replaced": len(earlier) if replace else 0,
    }


def _camera_planes(cam) -> list:
    """Mesh children of `cam` that show an image. The ones this module made come first."""
    planes = [obj for obj in cam.children if obj.type == "MESH" and _plane_media(obj) is not None]
    return sorted(planes, key=lambda obj: not obj.get(CAMERA_PLANE))


def _plane_geometry(plane, cam) -> tuple[float, float, float]:
    """The distance from the lens and the world size of `plane`, as (distance, width, height).

    A plane laid out like the add-on's carries its distance and scale, so the size is worked out from them and from
    the camera's field of view. Any other mesh is measured from its bounding box.
    """
    corners = [Vector(corner) for corner in plane.bound_box]
    local_w = max(c.x for c in corners) - min(c.x for c in corners)
    local_h = max(c.y for c in corners) - min(c.y for c in corners)
    settings = plane.get(ADDON_PROPERTY)
    if settings is not None and "distance" in settings and cam.data.type != "ORTHO":
        distance = float(settings["distance"])
        width = 2 * math.tan(cam.data.angle / 2) * distance * float(settings.get("scale", 100.0)) / 100.0
        return distance, width, width * local_h / local_w if local_w else 0.0
    # The middle of the bounding box, so a mesh with any number of vertices is measured the same way.
    middle = sum(corners, Vector()) / len(corners)
    center = cam.matrix_world.inverted() @ plane.matrix_world @ middle
    scale = plane.matrix_world.to_scale()
    return -center.z, local_w * scale.x, local_h * scale.y


def get_camera_plane(camera: str = "", scene: str = "", export_path: str = "") -> dict:
    """Describe the image plane on a camera, and say where its image or movie is.

    `camera` defaults to the scene's active camera. A plane this module made is preferred, and any other mesh parented
    to the camera that shows an image counts too. If the image has no file on disk (it is packed in the .blend or
    generated in Blender), it is saved to `export_path` and that path is returned as `path`.
    """
    target_scene = _scene(scene)
    cam = _camera(target_scene, camera)
    planes = _camera_planes(cam)
    if not planes:
        msg = f"The camera '{cam.name}' has no image plane."
        raise ValueError(msg)
    plane = planes[0]
    bpy.context.view_layer.update()  # A plane parented a moment ago has no world matrix yet.
    media = _plane_media(plane)
    if media is None:
        msg = f"The plane '{plane.name}' no longer shows an image."
        raise ValueError(msg)
    image = media.image
    path = bpy.path.abspath(image.filepath) if image.filepath else ""
    exported = False
    if image.packed_file or image.source == "GENERATED" or not path:
        if not export_path:
            msg = f"The image on '{plane.name}' is not saved to a file, and no export path was given."
            raise ValueError(msg)
        image.save(filepath=export_path)
        path, exported = export_path, True
    distance, width, height = _plane_geometry(plane, cam)
    return {
        "scene": target_scene.name,
        "camera": cam.name,
        "plane": plane.name,
        "planes": [obj.name for obj in planes],
        "made_by_node": bool(plane.get(CAMERA_PLANE)),
        "distance": distance,
        "width": width,
        "height": height,
        "source": image.source,
        "frames": int(image.frame_duration) if image.source in {"MOVIE", "SEQUENCE"} else 0,
        "path": path,
        "exported": exported,
    }
