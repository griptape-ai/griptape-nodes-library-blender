"""Camera code that runs inside the open Blender.

camera_client.py sends this file's source to Blender through the Blender Lab MCP add-on and calls `list_cameras` or
`render_camera` there, so it must stay self-contained: only `bpy` and the standard library.
"""

import base64
import math
import os
import tempfile
import time

import bpy
from mathutils import Euler

# A scene with more than any of these gets a smaller render, and Workbench's OpenGL render, which is lighter.
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


def _find_camera(name: str) -> bpy.types.Object:
    """The camera called `name` in the open scene, or the scene's active camera when `name` is empty."""
    scene = bpy.context.scene
    if not name:
        if scene.camera is None:
            msg = "The scene has no active camera."
            raise ValueError(msg)
        return scene.camera
    obj = scene.objects.get(name)
    if obj is None or obj.type != "CAMERA":
        names = ", ".join(o.name for o in scene.objects if o.type == "CAMERA")
        msg = f"Camera '{name}' not found in the scene. Cameras: {names or 'none'}."
        raise ValueError(msg)
    return obj


def _describe(obj: bpy.types.Object) -> dict:
    scene = bpy.context.scene
    return {
        "camera": obj.name,
        "location": list(obj.location),
        "rotation_degrees": [math.degrees(v) for v in obj.matrix_basis.to_euler("XYZ")],
        "focal_length": obj.data.lens,
        "frame": scene.frame_current,
    }


def _set_rotation(obj: bpy.types.Object, rotation_degrees: list) -> str:
    """Turn `obj` to XYZ Euler `rotation_degrees` in the channel its rotation mode uses, and name that channel."""
    angles = Euler([math.radians(v) for v in rotation_degrees], "XYZ")
    if obj.rotation_mode == "QUATERNION":
        obj.rotation_quaternion = angles.to_quaternion()
        return "rotation_quaternion"
    if obj.rotation_mode == "AXIS_ANGLE":
        axis, angle = angles.to_quaternion().to_axis_angle()
        obj.rotation_axis_angle = (angle, *axis)
        return "rotation_axis_angle"
    obj.rotation_euler = angles.to_matrix().to_euler(obj.rotation_mode)
    return "rotation_euler"


def set_camera(
    camera: str,
    location: list | None,
    rotation_degrees: list | None,
    focal_length: float | None,
    keyframe: bool,
    frame: int | None,
) -> dict:
    """Move, turn or zoom a camera. Leave what is None alone. With `keyframe`, key what changed at `frame`."""
    obj = _find_camera(camera)
    at = bpy.context.scene.frame_current if frame is None else frame
    if location is not None:
        obj.location = location
        if keyframe:
            obj.keyframe_insert("location", frame=at)
    if rotation_degrees is not None:
        channel = _set_rotation(obj, rotation_degrees)
        if keyframe:
            obj.keyframe_insert(channel, frame=at)
    if focal_length is not None:
        obj.data.lens = focal_length
        if keyframe:
            obj.data.keyframe_insert("lens", frame=at)
    return {**_describe(obj), "keyframed_at": at if keyframe else None}


def set_frame(frame: int) -> dict:
    """Go to `frame` in the open scene."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    return {
        "frame": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "fps": scene.render.fps / scene.render.fps_base,
    }


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
            bpy.ops.render.opengl(write_still=True, view_context=False)
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
