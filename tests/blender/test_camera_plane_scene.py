"""Checks for blender/camera_plane_scene.py. They need Blender, so they run inside it:

    blender -b --python tests/blender/test_camera_plane_scene.py

The script makes its own test images and movie (the movie needs ffmpeg), renders with Cycles at a small size, and exits
with an error if any check fails.
"""

import importlib.util
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cps", ROOT / "blender" / "camera_plane_scene.py")
assert spec is not None and spec.loader is not None
cps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cps)

WORK = Path(tempfile.mkdtemp(prefix="gtn_camera_plane_"))
RED, GREEN, BLUE, YELLOW, MAGENTA, BLACK = (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 0, 0)


def make_image(name: str, size: tuple[int, int], colors) -> Path:
    """An image of four quadrants, `colors` as top-left, top-right, bottom-left, bottom-right. None is transparent."""
    width, height = size
    image = bpy.data.images.new(name, width, height, alpha=True)
    pixels = []
    for y in range(height):
        for x in range(width):
            color = colors[(0 if y >= height // 2 else 2) + (0 if x < width // 2 else 1)]
            pixels.extend((*color, 1.0) if color is not None else (0, 0, 0, 0))
    image.pixels = pixels
    path = WORK / f"{name}.png"
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)
    return path


def setup_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 4
    scene.cycles.device = "CPU"
    scene.render.resolution_x, scene.render.resolution_y = 320, 180
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0, 0, 0, 1)
    scene.world = world
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.location, camera.rotation_euler = (3, -4, 2), (1.2, 0.1, 0.5)  # Not axis-aligned on purpose.
    return scene, camera


def render(scene, name: str):
    """Render and return a function that reads a pixel as (r, g, b, a), counted from the bottom left."""
    path = WORK / f"{name}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    width, _ = image.size
    pixels = list(image.pixels)
    return lambda x, y: tuple(round(v) for v in pixels[(y * width + x) * 4 : (y * width + x) * 4 + 4])


def check(condition, message: str) -> None:
    if not condition:
        print(f"FAILED: {message}")
        sys.exit(1)
    print(f"ok: {message}")


def test_stretch_orientation_and_replace():
    scene, camera = setup_scene()
    quad = make_image("quad", (400, 200), (RED, GREEN, BLUE, YELLOW))
    result = cps.add_camera_plane(str(quad), distance=5, fit="stretch")
    at = render(scene, "stretch")
    w, h = scene.render.resolution_x, scene.render.resolution_y
    corners = [at(3, h - 4)[:3], at(w - 4, h - 4)[:3], at(3, 3)[:3], at(w - 4, 3)[:3]]
    check(corners == [RED, GREEN, BLUE, YELLOW], "stretch shows the image right way up in the four corners")
    check(result["replaced"] == 0 and [o.name for o in camera.children] == ["Camera Plane"], "first run adds one plane")
    result = cps.add_camera_plane(str(quad), distance=8, fit="fit")
    check(result["replaced"] == 1 and len(camera.children) == 1, "second run replaces the plane instead of adding one")
    at = render(scene, "fit")
    check(at(w // 2, h - 3)[:3] == BLACK, "fit leaves bars above and below a 2:1 image in a 16:9 frame")
    check(at(w // 4, h // 2 + 20)[:3] == RED, "fit still shows the image")
    cps.add_camera_plane(str(quad), distance=3, fit="fill")
    at = render(scene, "fill")
    check(at(w // 4, h - 3)[:3] == RED, "fill covers the frame with no bars")


def test_names_with_dots():
    scene, camera = setup_scene()
    quad = make_image("quad4", (400, 200), (RED, GREEN, BLUE, YELLOW))
    cps.add_camera_plane(str(quad), name="plate.v2")
    result = cps.add_camera_plane(str(quad), name="plate.v2")
    check(
        result["replaced"] == 1 and [o.name for o in camera.children] == ["plate.v2"], "a name with a dot is replaced"
    )
    cps.add_camera_plane(str(quad), name="Camera Plane.old")
    cps.add_camera_plane(str(quad))
    names = sorted(o.name for o in camera.children)
    check(names == ["Camera Plane", "Camera Plane.old", "plate.v2"], "a default-name run leaves other names alone")
    cps.add_camera_plane(str(quad), name="  ")
    check(
        len([o for o in camera.children if o.name.startswith("Camera Plane")]) == 2, "a blank name is the default name"
    )


def test_addon_layout():
    """The plane is laid out like the Camera Plane add-on's, so it follows the lens and the add-on can drive it."""
    scene, camera = setup_scene()
    camera.data.lens, camera.data.shift_x = 35, 0.1
    quad = make_image("layout", (400, 200), (RED, GREEN, BLUE, YELLOW))
    cps.add_camera_plane(str(quad), distance=10, fit="fit")
    bpy.context.view_layer.update()
    plane = camera.children[0]
    check(
        plane.parent == camera and tuple(plane.location) == (0, 0, 0),
        "the plane is parented to the camera at its origin",
    )
    check(dict(plane["camera_plane"]) == {"distance": 10.0, "scale": 100.0}, "the plane carries its distance and scale")
    check(
        all(plane.lock_location) and all(plane.lock_rotation) and all(plane.lock_scale),
        "the plane's transforms are locked",
    )
    check(plane.show_wire, "the plane is drawn with a wireframe outline")
    expected = sorted(
        ["-distance"] + ["tan(FOV/2) * distance*2 * shift"] * 2 + ["tan(FOV/2) * distance*2 * scale/100.0"] * 2
    )
    check(
        sorted(d.driver.expression for d in plane.animation_data.drivers) == expected,
        "drivers set its position and size",
    )
    unit = 2 * math.tan(camera.data.angle / 2) * 10
    check(
        abs(plane.delta_scale[0] - unit) < 1e-4 and abs(plane.delta_location[2] + 10) < 1e-6,
        "the drivers size it to the frame",
    )
    check(abs(plane.delta_location[0] - unit * 0.1) < 1e-4, "the drivers follow the lens shift")

    camera.data.lens = 85
    bpy.context.view_layer.update()
    check(
        abs(plane.delta_scale[0] - 2 * math.tan(camera.data.angle / 2) * 10) < 1e-4,
        "changing the lens resizes the plane",
    )
    plane["camera_plane"]["distance"] = 20.0
    plane.update_tag(refresh={"OBJECT"})  # A change inside a custom property does not tell Blender's dependency graph.
    for driver in plane.animation_data.drivers:
        driver.update()
    bpy.context.view_layer.update()
    expected_scale = 2 * math.tan(camera.data.angle / 2) * 20
    check(
        abs(plane.delta_location[2] + 20) < 1e-6 and abs(plane.delta_scale[0] - expected_scale) < 1e-4,
        "changing the distance moves and resizes it",
    )
    check("Opacity Multiplier" in plane.active_material.node_tree.nodes, "the material has the opacity slider")
    if hasattr(plane, "camera_plane"):
        check(plane.camera_plane.distance == 20.0, "the Camera Plane add-on reads the plane's distance")
    else:
        print("skipped: the Camera Plane add-on is not installed")

    # Stretch still fills the frame after the lens and shift change.
    for old in list(camera.children):
        cps._remove_plane(old)
    cps.add_camera_plane(str(quad), distance=6, fit="stretch")
    camera.data.lens, camera.data.shift_y = 24, -0.05
    at = render(scene, "refit")
    w, h = scene.render.resolution_x, scene.render.resolution_y
    corners = [at(3, h - 4)[:3], at(w - 4, h - 4)[:3], at(3, 3)[:3], at(w - 4, 3)[:3]]
    check(corners == [RED, GREEN, BLUE, YELLOW], "a stretched plane still fills the frame after the lens changes")


def test_alpha_and_shift():
    scene, camera = setup_scene()
    half = make_image("half", (200, 200), (MAGENTA, None, MAGENTA, None))
    cps.add_camera_plane(str(half), distance=5, fit="fit")
    at = render(scene, "alpha")
    w, h = scene.render.resolution_x, scene.render.resolution_y
    check(at(w // 2 - 20, h // 2)[:3] == MAGENTA and at(w // 2 + 20, h // 2)[:3] == BLACK, "alpha is transparent")
    for plane in list(camera.children):
        cps._remove_plane(plane)
    camera.data.shift_x = 0.2
    scene.render.resolution_x, scene.render.resolution_y = 180, 320
    quad = make_image("quad2", (400, 200), (RED, GREEN, BLUE, YELLOW))
    cps.add_camera_plane(str(quad), distance=4, fit="stretch")
    at = render(scene, "shift")
    w, h = 180, 320
    corners = [at(2, h - 3)[:3], at(w - 3, h - 3)[:3], at(2, 2)[:3], at(w - 3, 2)[:3]]
    check(corners == [RED, GREEN, BLUE, YELLOW], "lens shift and a portrait frame still fill the frame")


def test_movie():
    scene, camera = setup_scene()
    if shutil.which("ffmpeg") is None:
        print("skipped: no ffmpeg for the movie check")
        return
    movie = WORK / "movie.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=3",
            "-pix_fmt",
            "yuv420p",
            str(movie),
        ],
        check=True,
    )
    result = cps.add_camera_plane(str(movie), distance=5, fit="fit")
    check(result["source"] == "MOVIE" and result["frames"] == 72, "a movie is a movie source with its frame count")
    scene.frame_start, scene.frame_end = 1, 72
    pictures = []
    for frame in (1, 48):
        scene.frame_set(frame)
        render(scene, f"movie_{frame}")
        pictures.append((WORK / f"movie_{frame}.png").read_bytes())
    check(pictures[0] != pictures[1], "the movie plays as the frame changes")


def test_ortho_get_and_errors():
    scene, camera = setup_scene()
    camera.data.type, camera.data.ortho_scale = "ORTHO", 4
    quad = make_image("quad3", (400, 200), (RED, GREEN, BLUE, YELLOW))
    result = cps.add_camera_plane(str(quad), distance=3, fit="stretch")
    check(abs(result["width"] - 4) < 1e-4, "an orthographic frame does not grow with distance")
    camera.data.type = "PERSP"
    for plane in list(camera.children):
        cps._remove_plane(plane)

    foreign_mesh = bpy.data.meshes.new("Foreign")
    foreign_mesh.from_pydata([(-1, -1, -5), (1, -1, -5), (1, 1, -5), (-1, 1, -5)], [], [(0, 1, 2, 3)])
    material = bpy.data.materials.new("ForeignMaterial")
    material.use_nodes = True
    material.node_tree.nodes.new("ShaderNodeTexImage").image = bpy.data.images.new("Generated", 64, 64)
    foreign_mesh.materials.append(material)
    foreign = bpy.data.objects.new("Foreign", foreign_mesh)
    scene.collection.objects.link(foreign)
    foreign.parent = camera
    try:
        cps.get_camera_plane()
        check(False, "an image with no file and no export path is an error")
    except ValueError:
        check(True, "an image with no file and no export path is an error")
    found = cps.get_camera_plane(export_path=str(WORK / "exported.png"))
    check(found["exported"] and (WORK / "exported.png").is_file(), "an image with no file is exported")
    check(
        abs(found["distance"] - 5) < 1e-3 and not found["made_by_node"],
        "a plane made by hand is found, with its distance",
    )
    cps.add_camera_plane(str(quad), distance=4, fit="fit", name="Mine")
    check(cps.get_camera_plane()["plane"] == "Mine", "a plane the node made is preferred")

    # A hand-made mesh with more than four vertices, none of them at its origin: a 3x3 grid of points at 5 units.
    grid = [(x, y, -5) for y in (-1, 0, 1) for x in (-1, 0, 1)]
    faces = [
        (row * 3 + col, row * 3 + col + 1, (row + 1) * 3 + col + 1, (row + 1) * 3 + col)
        for row in range(2)
        for col in range(2)
    ]
    grid_mesh = bpy.data.meshes.new("Grid")
    grid_mesh.from_pydata(grid, [], faces)
    grid_mesh.materials.append(material)
    grid_plane = bpy.data.objects.new("Grid", grid_mesh)
    scene.collection.objects.link(grid_plane)
    grid_plane.parent = camera
    for other in list(camera.children):
        if other != grid_plane:
            cps._remove_plane(other)
    check(
        abs(cps.get_camera_plane(export_path=str(WORK / "grid.png"))["distance"] - 5) < 1e-3,
        "the distance of a mesh with more than four vertices is right",
    )

    for kwargs, label in (
        ({"distance": 0}, "distance"),
        ({"fit": "bogus"}, "fit"),
        ({"camera": "nope"}, "camera"),
        ({"scene": "nope"}, "scene"),
    ):
        try:
            cps.add_camera_plane(str(quad), **kwargs)
            check(False, f"a bad {label} is an error")
        except ValueError:
            check(True, f"a bad {label} is an error")
    try:
        cps.add_camera_plane(str(WORK / "missing.png"))
        check(False, "a missing file is an error")
    except ValueError:
        check(True, "a missing file is an error")


for test in (
    test_stretch_orientation_and_replace,
    test_names_with_dots,
    test_addon_layout,
    test_alpha_and_shift,
    test_movie,
    test_ortho_get_and_errors,
):
    print(f"-- {test.__name__}")
    test()
print("ALL PASSED")
