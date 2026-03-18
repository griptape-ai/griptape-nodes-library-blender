"""
Blender Socket Server - Run inside Blender
==========================================

Simple socket server that runs inside Blender to handle requests from Griptape nodes.
Based on socket architecture (more reliable than stdio).

Paste this script into Blender's Text Editor and run it.
"""

import socket
import threading
import json
import time
import base64
import io
import logging
import os

try:
    import bpy
    BLENDER_AVAILABLE = True
except ImportError:
    BLENDER_AVAILABLE = False
    print("ERROR: This script must be run inside Blender")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BlenderSocketServer:
    """Simple socket server for Blender communication"""
    
    def __init__(self, host='localhost', port=8765):
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.server_thread = None
        
    def start(self):
        """Start the socket server"""
        if self.running:
            logger.info("Server is already running")
            return
            
        if not BLENDER_AVAILABLE:
            logger.error("Blender not available. Run this script inside Blender.")
            return
            
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            
            self.running = True
            self.server_thread = threading.Thread(target=self._run_server, daemon=True)
            self.server_thread.start()
            
            logger.info(f"✓ Blender Socket Server started on {self.host}:{self.port}")
            logger.info("Ready to receive commands from Griptape nodes")
            
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
            self.running = False
    
    def stop(self):
        """Stop the socket server"""
        if not self.running:
            logger.info("Server is not running")
            return
            
        logger.info("Stopping Blender Socket Server...")
        self.running = False
        
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
                
        logger.info("✓ Blender Socket Server stopped")
    
    def _run_server(self):
        """Main server loop"""
        while self.running:
            try:
                if not self.server_socket:
                    break
                    
                client_socket, addr = self.server_socket.accept()
                logger.info(f"Connection from {addr}")
                
                # Handle client in separate thread
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket,),
                    daemon=True
                )
                client_thread.start()
                
            except Exception as e:
                if self.running:  # Only log if we're not shutting down
                    logger.error(f"Server error: {e}")
                break
    
    def _handle_client(self, client_socket):
        """Handle individual client connection using threading approach like reference project"""
        print("DEBUG SERVER: Client handler started")
        
        try:
            # Set timeout for receive operations
            client_socket.settimeout(5.0)
            
            # Receive all data
            buffer = b''
            while True:
                try:
                    chunk = client_socket.recv(1024)
                    if not chunk:
                        break
                    buffer += chunk
                    
                    # Try to parse as complete JSON
                    try:
                        request_str = buffer.decode('utf-8')
                        request = json.loads(request_str)
                        print(f"DEBUG SERVER: Complete request received: {request.get('command', 'NO_COMMAND')}")
                        break
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Need more data or incomplete
                        continue
                        
                except socket.timeout:
                    print("DEBUG SERVER: Timeout waiting for data")
                    break
                except Exception as e:
                    print(f"DEBUG SERVER: Error receiving data: {e}")
                    break
            
            if not buffer:
                print("DEBUG SERVER: No data received")
                return
            
            # Process the request
            try:
                response = self._process_request(request)
                print(f"DEBUG SERVER: Response ready: {response.get('success', 'Unknown')}")
            except Exception as e:
                print(f"DEBUG SERVER: Processing error: {e}")
                response = {"success": False, "error": f"Processing failed: {str(e)}"}
            
            # Send response back
            try:
                response_json = json.dumps(response)
                response_bytes = response_json.encode('utf-8')
                
                print(f"DEBUG SERVER: Sending {len(response_bytes)} bytes")
                client_socket.sendall(response_bytes)
                print("DEBUG SERVER: Response sent successfully")
                
                # Important: Signal end of transmission
                client_socket.shutdown(socket.SHUT_WR)
                
            except Exception as e:
                print(f"DEBUG SERVER: Failed to send response: {e}")
                
        except Exception as e:
            print(f"DEBUG SERVER: Client handler error: {e}")
        finally:
            try:
                client_socket.close()
                print("DEBUG SERVER: Client socket closed")
            except:
                pass
    
    def _process_request(self, request):
        """Process incoming request and return response"""
        command = request.get('command')
        params = request.get('params', {})
        
        logger.info(f"Processing command: {command}")
        print(f"DEBUG SERVER: Received command '{command}' with params: {list(params.keys())}")
        
        try:
            if command == 'health_check':
                result = self._health_check()
            elif command == 'get_scene_info':
                result = self._get_scene_info()
            elif command == 'list_cameras':
                result = self._list_cameras()
            elif command == 'execute_code':
                result = self._execute_code(params.get('code', ''))
            else:
                result = {"success": False, "error": f"Unknown command: {command}"}
            
            print(f"DEBUG SERVER: Command '{command}' completed, success: {result.get('success', 'Unknown')}")
            return result
            
        except Exception as e:
            error_result = {"success": False, "error": f"Command processing failed: {str(e)}"}
            print(f"DEBUG SERVER: Command '{command}' failed with error: {str(e)}")
            return error_result
    
    def _health_check(self):
        """Health check endpoint"""
        return {
            "success": True,
            "status": "healthy",
            "blender_version": bpy.app.version_string,
            "timestamp": time.time()
        }
    
    def _get_scene_info(self):
        """Get scene information"""
        try:
            scene = bpy.context.scene
            
            # Safely get build info
            try:
                build_date = bpy.app.build_date.decode('utf-8') if bpy.app.build_date else "Unknown"
            except (AttributeError, UnicodeDecodeError):
                build_date = str(bpy.app.build_date) if bpy.app.build_date else "Unknown"
                
            try:
                build_hash = bpy.app.build_hash.decode('utf-8') if bpy.app.build_hash else "Unknown"
            except (AttributeError, UnicodeDecodeError):
                build_hash = str(bpy.app.build_hash) if bpy.app.build_hash else "Unknown"
            
            return {
                "success": True,
                "scene": {
                    "name": scene.name,
                    "frame_current": scene.frame_current,
                    "frame_start": scene.frame_start,
                    "frame_end": scene.frame_end
                },
                "blender": {
                    "version": bpy.app.version_string,
                    "build_date": build_date,
                    "build_hash": build_hash
                },
                "render": {
                    "engine": scene.render.engine,
                    "resolution_x": scene.render.resolution_x,
                    "resolution_y": scene.render.resolution_y,
                    "resolution_percentage": scene.render.resolution_percentage
                }
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _list_cameras(self):
        """List all cameras in the scene"""
        try:
            cameras = []
            for obj in bpy.data.objects:
                if obj.type == 'CAMERA':
                    cameras.append({
                        "name": obj.name,
                        "location": list(obj.location),
                        "rotation": list(obj.rotation_euler),
                        "active": obj == bpy.context.scene.camera
                    })
            
            return {
                "success": True,
                "cameras": cameras,
                "count": len(cameras),
                "active_camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _execute_code(self, code: str):
        """Execute arbitrary Python code in Blender with safety measures"""
        try:
            # Add safety measures for large/complex scenes
            import gc
            
            # Create a safe namespace
            namespace = {
                'bpy': bpy,
                'result': None
            }
            
            try:
                # Execute the code with additional error catching
                exec(code, namespace)
                
                # Force garbage collection to help with memory issues in large scenes
                gc.collect()
                
                # Return the result
                result = namespace.get('result')
                if result is not None:
                    return {"success": True, "result": result}
                else:
                    return {"success": True, "message": "Code executed successfully"}
                    
            except MemoryError:
                gc.collect()  # Try to free memory
                return {"success": False, "error": "Out of memory - scene too complex for this operation"}
            except RecursionError:
                return {"success": False, "error": "Operation too complex - recursion limit exceeded"}
            except Exception as e:
                error_msg = str(e)
                # Check for dependency graph related errors
                if "dependency" in error_msg.lower() or "depsgraph" in error_msg.lower():
                    return {"success": False, "error": f"Scene dependency error: {error_msg}. Try with a simpler scene."}
                else:
                    return {"success": False, "error": f"Code execution failed: {error_msg}"}
                
        except Exception as e:
            # Ultimate fallback
            return {"success": False, "error": f"Code execution failed with system error: {str(e)}"}

    # Note: Complex operations like rendering should be done via execute_code
    # following the reference project pattern of keeping the server minimal
    # and stable. The external client handles complex rendering logic.


# Global server instance
blender_server = BlenderSocketServer()

# UI Integration (Blender operators and panel)
if BLENDER_AVAILABLE:
    
    class BLENDER_OT_start_socket_server(bpy.types.Operator):
        """Start the Blender Socket Server"""
        bl_idname = "blender.start_socket_server"
        bl_label = "Start Socket Server"
        bl_description = "Start the Blender Socket Server for Griptape nodes"
        
        def execute(self, context):
            blender_server.start()
            if blender_server.running:
                self.report({'INFO'}, f"Socket Server started on {blender_server.host}:{blender_server.port}")
            else:
                self.report({'ERROR'}, "Socket Server failed to start - check if port 8765 is already in use")
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

    class BLENDER_OT_stop_socket_server(bpy.types.Operator):
        """Stop the Blender Socket Server"""
        bl_idname = "blender.stop_socket_server"
        bl_label = "Stop Socket Server"
        bl_description = "Stop the Blender Socket Server"

        def execute(self, context):
            blender_server.stop()
            self.report({'INFO'}, "Socket Server stopped")
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

    class BLENDER_PT_socket_server_panel(bpy.types.Panel):
        """Socket Server control panel"""
        bl_label = "Blender Socket Server"
        bl_idname = "BLENDER_PT_socket_server"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = "Griptape"
        
        def draw(self, context):
            layout = self.layout
            
            if blender_server.running:
                layout.label(text="Status: Running ✓", icon='CHECKMARK')
                layout.label(text=f"Port: {blender_server.port}")
                layout.operator("blender.stop_socket_server", icon='CANCEL')
            else:
                layout.label(text="Status: Stopped", icon='CANCEL')
                layout.operator("blender.start_socket_server", icon='PLAY')
            
            layout.separator()
            layout.label(text="For Griptape Nodes")

    def register():
        """Register Blender classes"""
        bpy.utils.register_class(BLENDER_OT_start_socket_server)
        bpy.utils.register_class(BLENDER_OT_stop_socket_server)
        bpy.utils.register_class(BLENDER_PT_socket_server_panel)

    def unregister():
        """Unregister Blender classes"""
        blender_server.stop()
        bpy.utils.unregister_class(BLENDER_PT_socket_server_panel)
        bpy.utils.unregister_class(BLENDER_OT_stop_socket_server)
        bpy.utils.unregister_class(BLENDER_OT_start_socket_server)

# Convenience functions
def start_server():
    """Start the socket server"""
    blender_server.start()

def stop_server():
    """Stop the socket server"""
    blender_server.stop()

def server_status():
    """Check server status"""
    if blender_server.running:
        print(f"✓ Socket Server running on {blender_server.host}:{blender_server.port}")
    else:
        print("✗ Socket Server stopped")

# Auto-start when script is run
if __name__ == "__main__":
    if BLENDER_AVAILABLE:
        print("=" * 50)
        print("BLENDER SOCKET SERVER")
        print("=" * 50)
        print("Simple socket server for Griptape node communication")
        print()
        print("Functions:")
        print("- start_server()    # Start the server")
        print("- stop_server()     # Stop the server") 
        print("- server_status()   # Check status")
        print()
        
        # Register UI
        register()
        
        # Auto-start server
        start_server()
    else:
        print("ERROR: This script must be run inside Blender") 