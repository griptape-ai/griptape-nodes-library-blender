"""
Simple Socket Client for Blender Communication
==============================================

Connects to the Blender socket server to send commands.
Much simpler than MCP stdio - no async context issues.
"""

import json
import logging
import socket
import time
from typing import Any

logger = logging.getLogger(__name__)


class BlenderSocketClient:
    """Client for communicating with Blender socket server"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout: int = 60):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send_command(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a command to Blender and return the response with improved error handling"""
        request = {"command": command, "params": params or {}}

        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                # Create socket connection with shorter timeout for connection
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(10)  # 10 second connection timeout

                    try:
                        sock.connect((self.host, self.port))
                    except (TimeoutError, ConnectionRefusedError, OSError) as e:
                        if attempt == max_retries - 1:
                            return {
                                "success": False,
                                "error": f"Cannot connect to Blender server at {self.host}:{self.port}. Make sure Blender socket server is running. Error: {str(e)}",
                            }
                        print(f"Connection attempt {attempt + 1} failed, retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        continue

                    # Set longer timeout for command execution
                    sock.settimeout(self.timeout)

                    # Send request
                    request_json = json.dumps(request)
                    try:
                        sock.sendall(request_json.encode("utf-8"))
                    except (TimeoutError, BrokenPipeError, ConnectionResetError) as e:
                        return {"success": False, "error": f"Failed to send command: {str(e)}"}

                    # Receive response with chunked reading for large responses
                    try:
                        response_chunks = []
                        while True:
                            chunk = sock.recv(8192)
                            if not chunk:
                                break
                            response_chunks.append(chunk)

                        if not response_chunks:
                            return {"success": False, "error": "Empty response from server"}

                        response_data = b"".join(response_chunks).decode("utf-8")

                    except TimeoutError:
                        return {"success": False, "error": f"Command timed out after {self.timeout} seconds"}
                    except (ConnectionResetError, BrokenPipeError) as e:
                        return {"success": False, "error": f"Connection lost during command execution: {str(e)}"}

                    # Parse JSON response
                    try:
                        if not response_data.strip():
                            return {"success": False, "error": "Empty response from Blender server"}

                        response = json.loads(response_data)
                        return response

                    except json.JSONDecodeError:
                        return {
                            "success": False,
                            "error": f"Invalid JSON response from server. Raw response: {response_data[:100]}...",
                        }

            except Exception as e:
                if attempt == max_retries - 1:
                    return {"success": False, "error": f"Unexpected error: {str(e)}"}
                print(f"Attempt {attempt + 1} failed with error: {str(e)}, retrying...")
                time.sleep(retry_delay)

        return {"success": False, "error": "All connection attempts failed"}

    def health_check(self) -> dict[str, Any]:
        """Check if Blender server is responsive"""
        return self._send_command("health_check")

    def get_scene_info(self) -> dict[str, Any]:
        """Get current scene information"""
        return self._send_command("get_scene_info")

    def list_cameras(self) -> dict[str, Any]:
        """List all cameras in the scene"""
        return self._send_command("list_cameras")

    def render_camera(
        self,
        camera_name: str = "Camera",
        width: int = 1920,
        height: int = 1080,
        format_type: str = "PNG",
        quality: int = 90,
    ) -> dict[str, Any]:
        """Render from specified camera using ultra-safe approach for large scenes"""

        # Ultra-conservative approach - no timeouts, maximum safety
        render_code = f"""
import bpy
import os
import base64
import gc

try:
    print(f"DEBUG SAFE: Looking for camera named: '{camera_name}'")

    # Find camera
    camera = bpy.data.objects.get("{camera_name}")
    if not camera or camera.type != 'CAMERA':
        print(f"DEBUG SAFE: Camera '{camera_name}' not found")
        available_cameras = [obj.name for obj in bpy.data.objects if obj.type == 'CAMERA']
        print(f"DEBUG SAFE: Available cameras: {{available_cameras}}")
        result = {{"success": False, "error": f"Camera '{camera_name}' not found in scene. Available: {{', '.join(available_cameras)}}"}}
    else:
        print(f"DEBUG SAFE: Found camera '{camera_name}', preparing ultra-safe render")

        # Scene complexity detection
        scene = bpy.context.scene
        total_objects = len(bpy.data.objects)
        total_materials = len(bpy.data.materials)
        total_meshes = len(bpy.data.meshes)

        is_complex_scene = total_objects > 200 or total_materials > 50 or total_meshes > 100
        print(f"DEBUG SAFE: Scene stats - Objects: {{total_objects}}, Materials: {{total_materials}}, Meshes: {{total_meshes}}")
        print(f"DEBUG SAFE: Complex scene detected: {{is_complex_scene}} (using stricter thresholds)")

        # Store ALL original settings for complete restoration
        original_camera = scene.camera
        original_engine = scene.render.engine
        original_resolution_x = scene.render.resolution_x
        original_resolution_y = scene.render.resolution_y
        original_resolution_percentage = scene.render.resolution_percentage
        original_file_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_compression = scene.render.image_settings.compression
        original_use_motion_blur = scene.render.use_motion_blur
        original_filepath = scene.render.filepath

        # Store workbench specific settings if available
        original_render_aa = None
        original_viewport_aa = None
        if hasattr(scene.display, 'render_aa'):
            original_render_aa = scene.display.render_aa
        if hasattr(scene.display, 'viewport_aa'):
            original_viewport_aa = scene.display.viewport_aa

        try:
            print("DEBUG SAFE: Setting ultra-conservative render settings")

            # Force garbage collection before making any changes
            gc.collect()

            # Set camera
            scene.camera = camera
            print(f"DEBUG SAFE: Set active camera to '{{camera.name}}'")

            # Set resolution (but cap it for complex scenes)
            if is_complex_scene:
                # Reduce resolution for complex scenes to prevent crashes
                max_res = 512
                actual_width = min({width}, max_res)
                actual_height = min({height}, max_res)
                print(f"DEBUG SAFE: Reducing resolution for complex scene: {{actual_width}}x{{actual_height}}")
            else:
                actual_width = {width}
                actual_height = {height}

            scene.render.resolution_x = actual_width
            scene.render.resolution_y = actual_height
            scene.render.resolution_percentage = 100

            # Use ultra-safe render settings
            print("DEBUG SAFE: Setting Workbench engine with minimal settings")
            scene.render.engine = 'BLENDER_WORKBENCH'

            # Set workbench to absolute minimum settings
            if hasattr(scene.display, 'render_aa'):
                scene.display.render_aa = 'OFF'
            if hasattr(scene.display, 'viewport_aa'):
                scene.display.viewport_aa = 'OFF'

            # Disable ALL optional features
            scene.render.use_motion_blur = False
            if hasattr(scene.render, 'use_freestyle'):
                scene.render.use_freestyle = False
            if hasattr(scene.render, 'use_compositing'):
                scene.render.use_compositing = False
            if hasattr(scene.render, 'use_sequencer'):
                scene.render.use_sequencer = False

            # Set image format to safest possible
            scene.render.image_settings.file_format = 'PNG'
            scene.render.image_settings.color_mode = 'RGB'
            scene.render.image_settings.compression = 0  # No compression for speed

            # Use temporary file with timestamp to avoid any conflicts
            import time
            timestamp = int(time.time() * 1000000)  # Microsecond precision
            temp_file = f"/tmp/blender_safe_render_{{timestamp}}.png"
            scene.render.filepath = temp_file

            print(f"DEBUG SAFE: Starting ultra-safe render to {{temp_file}}")
            print("DEBUG SAFE: Using minimal Workbench settings, no anti-aliasing, no post-processing")

            # Force another garbage collection right before render
            gc.collect()

            # Advanced fallback: for complex scenes, create a temporary scene with ONLY the camera
            render_successful = False
            try:
                if is_complex_scene:
                    print("DEBUG SAFE: Creating temporary minimal scene for render")
                    temp_scene = bpy.data.scenes.new(name="Griptape_Temp")
                    # Duplicate camera (object and data) so we don't touch original
                    cam_copy = camera.copy()
                    cam_copy.data = camera.data.copy()
                    temp_scene.collection.objects.link(cam_copy)
                    temp_scene.camera = cam_copy

                    # Copy minimal render settings
                    temp_scene.render.engine = 'BLENDER_WORKBENCH'
                    temp_scene.render.resolution_x = actual_width
                    temp_scene.render.resolution_y = actual_height
                    temp_scene.render.resolution_percentage = 100

                    # Switch context to new scene (safe in background)
                    current_window = bpy.context.window
                    original_scene_ctx = current_window.scene
                    current_window.scene = temp_scene

                    # Perform OpenGL render (fast & light)
                    bpy.ops.render.opengl(write_still=True, view_context=False)

                    # Restore original context
                    current_window.scene = original_scene_ctx

                    # Cleanup: remove duplicated camera object and its data, then remove temp scene
                    try:
                        if cam_copy.name in bpy.data.objects:
                            bpy.data.objects.remove(cam_copy, do_unlink=True)
                        if cam_copy.data and cam_copy.data.users == 0:
                            if cam_copy.data.name in bpy.data.cameras:
                                bpy.data.cameras.remove(cam_copy.data, do_unlink=True)
                    except Exception as cleanup_obj_err:
                        print(f"DEBUG SAFE: Warning removing temp camera object: {{cleanup_obj_err}}")

                    try:
                        bpy.data.scenes.remove(temp_scene, do_unlink=True)
                    except Exception as cleanup_scene_err:
                        print(f"DEBUG SAFE: Warning removing temp scene: {{cleanup_scene_err}}")

                    # The OpenGL render writes to temp_scene.render.filepath; ensure it's saved
                    ogl_path = temp_scene.render.filepath if temp_scene.render.filepath else temp_file
                    # Move/symlink to our expected temp_file name
                    if os.path.exists(ogl_path):
                        os.rename(ogl_path, temp_file)

                else:
                    bpy.ops.render.render(write_still=True)

                print("DEBUG SAFE: Render completed successfully")
                render_successful = True

            except Exception as render_error:
                print(f"DEBUG SAFE: Render failed with error: {{str(render_error)}}")
                render_successful = False
                result = {{"success": False, "error": f"Render operation failed: {{str(render_error)}}"}}

            if render_successful:
                # Check if file was created and read it
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    try:
                        with open(temp_file, 'rb') as f:
                            image_data = base64.b64encode(f.read()).decode('utf-8')

                        # Clean up temp file immediately
                        os.remove(temp_file)

                        result = {{
                            "success": True,
                            "image": image_data,
                            "camera_used": camera.name,
                            "width": actual_width,
                            "height": actual_height,
                            "render_time": 0.0,
                            "scene_complexity": "complex" if is_complex_scene else "normal",
                            "render_mode": "ultra_safe"
                        }}
                        print(f"DEBUG SAFE: Successfully encoded {{len(image_data)}} bytes of image data")

                    except Exception as file_error:
                        result = {{"success": False, "error": f"Failed to read render result: {{str(file_error)}}"}}
                        # Clean up temp file on error
                        try:
                            if os.path.exists(temp_file):
                                os.remove(temp_file)
                        except:
                            pass
                else:
                    result = {{"success": False, "error": "Render completed but no output file was created or file is empty"}}
                    # Clean up temp file if it exists
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                    except:
                        pass

        except Exception as setup_error:
            print(f"DEBUG SAFE: Setup error: {{setup_error}}")
            result = {{"success": False, "error": f"Render setup failed: {{str(setup_error)}}"}}

        finally:
            # ALWAYS restore ALL original settings - this is critical
            try:
                print("DEBUG SAFE: Restoring all original settings")
                if original_camera:
                    scene.camera = original_camera
                scene.render.engine = original_engine
                scene.render.resolution_x = original_resolution_x
                scene.render.resolution_y = original_resolution_y
                scene.render.resolution_percentage = original_resolution_percentage
                scene.render.image_settings.file_format = original_file_format
                scene.render.image_settings.color_mode = original_color_mode
                scene.render.image_settings.compression = original_compression
                scene.render.use_motion_blur = original_use_motion_blur
                scene.render.filepath = original_filepath

                # Restore workbench settings if they were changed
                if original_render_aa is not None and hasattr(scene.display, 'render_aa'):
                    scene.display.render_aa = original_render_aa
                if original_viewport_aa is not None and hasattr(scene.display, 'viewport_aa'):
                    scene.display.viewport_aa = original_viewport_aa

                print("DEBUG SAFE: All settings restored successfully")
            except Exception as restore_error:
                print(f"DEBUG SAFE: Warning - failed to restore some settings: {{restore_error}}")

            # Final garbage collection
            gc.collect()
            print("DEBUG SAFE: Cleanup completed")

except Exception as e:
    import traceback
    error_details = traceback.format_exc()
    print(f"DEBUG SAFE: Major error in render operation: {{e}}")
    print(f"DEBUG SAFE: Full traceback: {{error_details}}")
    result = {{"success": False, "error": f"Render operation failed with system error: {{str(e)}}"}}
"""

        return self._send_command("execute_code", {"code": render_code})

    def execute_code(self, code: str) -> dict[str, Any]:
        """Execute arbitrary Python code in Blender"""
        return self._send_command("execute_code", {"code": code})


class BlenderSocketClientManager:
    """Singleton manager for Blender socket client"""

    _instance: BlenderSocketClient | None = None

    @classmethod
    def get_client(cls, host: str = "localhost", port: int = 8765) -> BlenderSocketClient:
        """Get or create the socket client instance"""
        if cls._instance is None:
            cls._instance = BlenderSocketClient(host, port)
        return cls._instance

    @classmethod
    def reset_client(cls):
        """Reset the client instance (useful for changing connection settings)"""
        cls._instance = None


# Convenience functions for easier integration
def health_check() -> dict[str, Any]:
    """Quick health check function"""
    client = BlenderSocketClient()
    return client.health_check()


def get_scene_info() -> dict[str, Any]:
    """Quick scene info function"""
    client = BlenderSocketClient()
    return client.get_scene_info()


def list_cameras() -> dict[str, Any]:
    """Quick camera list function"""
    client = BlenderSocketClient()
    return client.list_cameras()


def render_camera(
    camera_name: str = "Camera", width: int = 1920, height: int = 1080, format_type: str = "PNG", quality: int = 90
) -> dict[str, Any]:
    """Quick render function"""
    client = BlenderSocketClient(timeout=120)  # Longer timeout for rendering
    return client.render_camera(camera_name, width, height, format_type, quality)
