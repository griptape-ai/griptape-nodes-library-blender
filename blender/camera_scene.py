"""Camera code that runs inside the open Blender.

camera_client.py sends this file's source to Blender through the Blender Lab MCP add-on and calls `list_cameras` or
`render_camera` there, so it must stay self-contained: only `bpy` and the standard library.
"""

import base64
import os
import tempfile
import time

import bpy

# A scene with more than any of these gets a smaller render, and the lighter OpenGL path.
COMPLEX_OBJECTS = 200
COMPLEX_MATERIALS = 50
COMPLEX_MESHES = 100
COMPLEX_MAX_RESOLUTION = 512


def list_cameras() -> dict:
    """Describe every camera object in the file open in Blender."""
    scene = bpy.context.scene
    active = scene.camera.name if scene.camera else None
    cameras = []
    for obj in bpy.data.objects:
        if obj.type != "CAMERA":
            continue
        data = obj.data
        cameras.append(
            {
                "name": obj.name,
                "location": list(obj.location),
                "rotation": list(obj.rotation_euler),
                "scale": list(obj.scale),
                "active": obj.name == active,
                "focal_length": data.lens,
                "sensor_width": data.sensor_width,
                "sensor_height": data.sensor_height,
                "sensor_fit": data.sensor_fit,
                "clip_start": data.clip_start,
                "clip_end": data.clip_end,
                "type": data.type,
                "angle": data.angle,
                "angle_x": data.angle_x,
                "angle_y": data.angle_y,
                "shift_x": data.shift_x,
                "shift_y": data.shift_y,
                "passepartout_alpha": data.passepartout_alpha,
                "dof_use": data.dof.use_dof,
                "dof_focus_distance": data.dof.focus_distance,
                "dof_aperture_fstop": data.dof.aperture_fstop,
                "background_images_count": len(data.background_images),
                "matrix_world": [list(row) for row in obj.matrix_world],
            }
        )
    return {"blender_version": bpy.app.version_string, "cameras": cameras}


def render_camera(camera: str, width: int, height: int) -> dict:
    """Render one frame from `camera` with Workbench and return the PNG, base64 encoded.

    The scene's settings are put back afterwards.
    """
    obj = bpy.data.objects.get(camera)
    if obj is None or obj.type != "CAMERA":
        names = ", ".join(o.name for o in bpy.data.objects if o.type == "CAMERA")
        msg = f"Camera '{camera}' not found in the scene. Cameras: {names or 'none'}."
        raise ValueError(msg)

    scene = bpy.context.scene
    render = scene.render
    complex_scene = (
        len(bpy.data.objects) > COMPLEX_OBJECTS
        or len(bpy.data.materials) > COMPLEX_MATERIALS
        or len(bpy.data.meshes) > COMPLEX_MESHES
    )
    if complex_scene:
        width = min(width, COMPLEX_MAX_RESOLUTION)
        height = min(height, COMPLEX_MAX_RESOLUTION)

    saved = {
        "camera": scene.camera,
        "engine": render.engine,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "use_motion_blur": render.use_motion_blur,
        "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "color_mode": render.image_settings.color_mode,
        "compression": render.image_settings.compression,
    }
    handle, temp_file = tempfile.mkstemp(prefix="gtn_camera_capture_", suffix=".png")
    os.close(handle)
    started = time.time()
    try:
        scene.camera = obj
        render.engine = "BLENDER_WORKBENCH"
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.use_motion_blur = False
        render.image_settings.file_format = "PNG"
        render.image_settings.color_mode = "RGB"
        render.image_settings.compression = 0
        render.filepath = temp_file
        if complex_scene:
            _render_opengl(obj, width, height, temp_file)
        else:
            bpy.ops.render.render(write_still=True)
        with open(temp_file, "rb") as f:
            png = f.read()
        if not png:
            msg = "Blender finished the render but wrote no image."
            raise RuntimeError(msg)
    finally:
        scene.camera = saved["camera"]
        render.engine = saved["engine"]
        render.resolution_x = saved["resolution_x"]
        render.resolution_y = saved["resolution_y"]
        render.resolution_percentage = saved["resolution_percentage"]
        render.use_motion_blur = saved["use_motion_blur"]
        render.filepath = saved["filepath"]
        render.image_settings.file_format = saved["file_format"]
        render.image_settings.color_mode = saved["color_mode"]
        render.image_settings.compression = saved["compression"]
        if os.path.exists(temp_file):
            os.remove(temp_file)
    return {
        "image": base64.b64encode(png).decode("ascii"),
        "camera": obj.name,
        "width": width,
        "height": height,
        "render_time": time.time() - started,
        "scene_complexity": "complex" if complex_scene else "normal",
    }


def _render_opengl(camera: bpy.types.Object, width: int, height: int, filepath: str) -> None:
    """Render through a scene that holds only a copy of `camera`, so a heavy scene is not evaluated."""
    window = bpy.context.window
    original_scene = window.scene
    temp_scene = bpy.data.scenes.new(name="Griptape_Temp")
    camera_copy = camera.copy()
    camera_copy.data = camera.data.copy()
    try:
        temp_scene.collection.objects.link(camera_copy)
        temp_scene.camera = camera_copy
        temp_scene.render.engine = "BLENDER_WORKBENCH"
        temp_scene.render.resolution_x = width
        temp_scene.render.resolution_y = height
        temp_scene.render.resolution_percentage = 100
        temp_scene.render.image_settings.file_format = "PNG"
        temp_scene.render.filepath = filepath
        window.scene = temp_scene
        bpy.ops.render.opengl(write_still=True, view_context=False)
    finally:
        window.scene = original_scene
        camera_data = camera_copy.data
        bpy.data.objects.remove(camera_copy, do_unlink=True)
        if camera_data.users == 0:
            bpy.data.cameras.remove(camera_data)
        bpy.data.scenes.remove(temp_scene, do_unlink=True)
