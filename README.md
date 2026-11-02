# Griptape Nodes Blender Integration

Nodes for working with the scene open in Blender, from Griptape workflows.

## Overview

Every node that works with an open Blender talks to the official
[Blender Lab MCP add-on](https://www.blender.org/lab/mcp-server/). The add-on runs a server inside Blender, on port 9876
by default. The nodes send it Python and read back the result. No AI agent is involved.

## Architecture

```
┌─────────────────┐    Socket/TCP     ┌──────────────────┐    bpy Python API ┌─────────┐
│ Griptape Nodes  │ ←───────────────→ │  Blender Lab MCP │ ←───────────────→ │ Blender │
│   (Clients)     │     JSON/9876     │  add-on          │                   │ (3D App) │
└─────────────────┘                   └──────────────────┘                   └─────────┘
```

## Files

- `blender/blender_mcp_client.py` - Client for the Blender Lab MCP add-on's socket
- `blender/camera_capture.py` - Camera capture node for Griptape workflows
- `blender/camera_list.py` - Node to list available cameras in Blender scene
- `blender/camera_scene.py` - Camera code that runs inside the open Blender
- `blender/camera_client.py` - Sends that code to the open Blender through the MCP add-on
- `blender/image_to_grease_pencil.py` - Node that traces images into Grease Pencil
- `blender/grease_pencil_trace.py` - Runs the trace in a background Blender and sends the result to an open one
- `blender/trace_grease_pencil.py` - Script the background Blender runs to trace and render previews
- `blender/grease_pencil_scene.py` - Places traced drawings in a scene, in the background or open Blender
- `blender/image_to_camera_plane.py` and `blender/get_camera_plane.py` - Nodes that put an image or movie on a camera as a plane, and read it back
- `blender/camera_plane_scene.py` - Camera plane code that runs inside the open Blender
- `blender/camera_plane_client.py` - Sends that code to the open Blender through the MCP add-on
- `blender/media_files.py` - Saves media in the project so Blender can point at a file that stays put

## Quick Setup

1. Open Blender and install the MCP add-on from [Blender Lab](https://www.blender.org/lab/mcp-server/).
2. Turn on Preferences > System > Allow Online Access.
3. Start the add-on's server. It listens on port 9876. If you use another port, set `MCP Port` on the node.
4. Use the nodes. They connect to the open Blender when they run.

## Add your library to your installed Engine! 

If you haven't already installed your Griptape Nodes engine, follow the installation steps [HERE](https://github.com/griptape-ai/griptape-nodes).
After you've completed those and you have your engine up and running: 

1. Copy the path to your `griptape_nodes_library.json` file within this `blender` directory. Right click on the file, and `Copy Path` (Not `Copy Relative Path`).
   ![Copy path of the griptape_nodes_library.json](./images/get_json_path.png)
2. Start up the engine! 
3. Navigate to settings.
   ![Open Settings](./images/open_settings.png)
4. Open your settings and go to the App Events tab. Add an item in **Libraries to Register**.
   ![Add Library to Register](./images/add_library.png)
5. Paste your copied `griptape_nodes_library.json` path from earlier into the new item.
   ![Paste in your absolute path](./images/paste_library.png)
6. Exit out of Settings. It will save automatically! 
7. Open up the **Libraries** dropdown on the left sidebar.
   ![See Libraries](./images/see_libraries.png)
8. Your newly registered library should appear! Drag and drop nodes to use them!
   ![Library Display](./images/final_image.png)

## Available Nodes

### Camera Capture Node

Captures single frames from Blender cameras with detailed camera metadata display.

**Flow Control:**
- `exec_in` - Flow input for control sequencing
- `exec_out` - Flow output for control sequencing

**Parameters:**
- `cameras_input` - Connect to Camera List Node for dynamic camera data (optional)
- `camera_name` - Name of camera in Blender scene (dropdown updates automatically)
- `resolution_x` - Image width in pixels (64-4096, default: 1920)
- `resolution_y` - Image height in pixels (64-4096, default: 1080) 
- `output_format` - PNG or JPEG (default: PNG)
- `quality` - JPEG quality 1-100 (default: 90)
- `mcp_port` - Port of the Blender Lab MCP add-on's server (default: 9876)

**Camera Metadata Labels (displayed under Camera dropdown):**
- `Status` - Shows if camera is active scene camera
- `Focal Length` - Lens focal length in mm
- `Sensor` - Sensor dimensions, fit mode, and camera type
- `Depth of Field` - DOF settings including focus distance and f-stop
- `Transform` - Camera location and rotation coordinates

**Outputs:**
- `image_output` - Captured image as ImageUrlArtifact
- `status_output` - Render information and timing

**Features:**
- ✅ **Dynamic camera dropdown** - Updates automatically when connected to Camera List Node
- ✅ **Rich metadata display** - Shows detailed camera properties in real-time
- ✅ **Enhanced camera data** - Accesses comprehensive Blender camera properties
- ✅ **Auto camera validation** - Switches to available camera if selection invalid

### Camera List Node

Lists all cameras in the current Blender scene with comprehensive metadata.

**Flow Control:**
- `exec_in` - Flow input for control sequencing  
- `exec_out` - Flow output for control sequencing

**Features:**
- ✅ **Always re-evaluates** - Fetches fresh camera data on every workflow run
- ✅ **Comprehensive camera data** - Collects detailed camera properties via Blender API

**Parameters:**
- `mcp_port` - Port of the Blender Lab MCP add-on's server (default: 9876)

**Outputs:**
- `cameras_output` - Detailed camera info including metadata (ListArtifact)
- `camera_count` - Total number of cameras found
- `status_output` - Operation status and connection info

**Enhanced Camera Data Collected:**
- **Basic Transform:** Location, rotation, scale, active status
- **Lens Properties:** Focal length, sensor dimensions, sensor fit mode
- **Camera Type:** Perspective, orthographic, panoramic
- **Field of View:** Angular measurements for framing calculations
- **Clipping Distances:** Near and far render boundaries
- **Depth of Field:** Focus distance, aperture f-stop settings
- **Composition:** Camera shift for perspective correction
- **Matrix Data:** Full 4x4 transformation matrix for precise positioning

### Image to Grease Pencil Node

Traces an image, or a list of images, into a Grease Pencil drawing. Tracing runs in a background Blender, so it works
with no Blender open; turn off `Send To Open Scene` in that case. It does not use a separate server.

**Modes:**
- **Animated** - one object, one keyframe per image (`Start Frame`, `Frame Step`)
- **Separate** - one object per image (`Drawing_001`, `Drawing_002`, ...)

**Settings:** object name. Threshold, invert and turn policy for the trace. Max resolution, stroke type, simplify and
smooth for cleanup. Thickness, color and fill for the look. Layer name, size, location (empty for the 3D cursor),
collection (empty for the active one), and replace for placement. Set `Target Object` to an existing Grease Pencil
object to add the trace to it as a new layer. In Separate mode each image gets its own layer.

**Outputs:** a preview of each image rendered on white, the `.blend` holding the drawing, and the object and layer
names. Each run overwrites the node's previews.

**Where the drawing goes:**
- `Target Blend` - adds it to that `.blend` file and saves the file
- `Send To Open Scene` - adds it to the scene open in Blender, through the official
  [Blender Lab MCP add-on](https://www.blender.org/lab/mcp-server/). Install the add-on, turn on
  Preferences > System > Allow Online Access, and start its server (port 9876). The node talks to the add-on's socket
  directly. No AI agent is involved.

The tracer outlines each line rather than following its center. With `Fill` on the lines are solid; with it off you see
the outlines, drawn at `Thickness`. Blender is
found from `Blender Path`, then `$BLENDER_PATH`, then `blender` on `PATH`, then `/Applications/Blender.app`. Tested
with Blender 5.3.

### Image to Camera Plane Node

Adds an image or a movie to a camera in the scene open in Blender as a plane that fills the camera's frame. The plane
is parented to the camera, so it follows every camera move, and it renders. (Blender's camera background images only
show in the viewport.) It talks to the open Blender through the Blender Lab MCP add-on, like Image to Grease Pencil, so
install the add-on, turn on Preferences > System > Allow Online Access, and start its server (port 9876).

**Parameters:**
- `Image or Movie` - an image, or a movie (a `VideoUrlArtifact`). A file already on disk is used where it is. Anything
  else is saved in the project first, so Blender has a file that stays put.
- `Distance` - how far in front of the lens the plane sits, in scene units. The plane is sized to fill the frame at that
  distance, so it looks the same at any distance. An orthographic camera's frame doesn't grow with distance.
- `Fit` - fill the frame (the overflow falls outside it), fit the whole image inside it, or stretch it to the frame.
- `Plane Name`, and `Replace Existing`, which swaps out the plane this node made earlier on the same camera with the
  same name (dots and all). Planes you made yourself are never replaced.
- `Camera` and `Scene` (under Blender) - empty means the scene's active camera in the scene showing in Blender's window.

The plane is laid out the way the [Camera Plane add-on](https://extensions.blender.org/add-ons/lfs_camera_plane/)
lays out the planes it imports, so the two work on the same camera and you don't need the add-on for this. The plane is
parented to the camera and one unit wide, its distance and scale are the custom property `camera_plane`, and drivers
turn those into a position and size from the camera's field of view and lens shift. Change the lens or the shift and
the plane keeps fitting the frame. Change `distance` or `scale` (in the add-on's panel, or on the property) and it moves
and resizes to match. Its transforms are locked and it is drawn with a wireframe outline. An orthographic camera's
frame doesn't grow with distance, so there the plane's size is baked in and has no drivers.

The material shows the image as it is (a camera ray sees the image, other rays see a plain surface, so the plane
neither lights the scene nor is lit), with the image's alpha, times an `Opacity Multiplier` slider, as transparency.
A movie plays from the scene's start frame, and the node reports its frame count.

### Get Camera Plane Node

Finds the image plane on a camera in the open scene and returns its image or movie, as an `ImageUrlArtifact` or a
`VideoUrlArtifact`, with the plane's name, the camera and the distance. It prefers a plane Image to Camera Plane made,
and it also finds any other mesh parented to the camera that shows an image, such as a paper plane you made by hand.
An image with no file of its own (packed in the .blend, or generated in Blender) is exported first. The file has to be
on the same machine as the engine.

### Testing the camera plane code

The code that runs inside Blender is checked in Blender itself, with a script that renders small test scenes and exits
with an error if a check fails:

```bash
blender -b --python tests/blender/test_camera_plane_scene.py
```

The movie check needs `ffmpeg`.

## Workflow Integration

### Connected Workflow (Recommended)

For the best experience, connect Camera List Node → Camera Capture Node:

```
┌─────────────────┐ cameras_output ┌──────────────────────┐
│ Camera List     │────────────────→│ Camera Capture      │
│                 │                 │                      │
│ • Always fresh  │                 │ • Dynamic dropdown  │
│ • Detailed data │                 │ • Metadata labels   │
│ • Flow control  │                 │ • Auto validation   │
└─────────────────┘                 └──────────────────────┘
```

**Benefits:**
- ✅ **Camera dropdown updates automatically** when scene changes
- ✅ **Rich metadata display** under camera selection
- ✅ **Always current data** - Camera List always re-evaluates  
- ✅ **Seamless flow control** - Both nodes support exec in/out

### Standalone Usage

Camera Capture Node works independently but with limited features:
- Static camera dropdown (populated at node creation)
- Basic status messages instead of detailed metadata
- Manual refresh required for scene changes

## Troubleshooting

### "Could not reach Blender at localhost:9876"

1. **Make sure Blender is running** with the MCP add-on installed.
2. **Turn on** Preferences > System > Allow Online Access, then start the add-on's server.
3. **Check the port.** If the add-on listens on another port, set `MCP Port` on the node.

### Render Issues

- Camera Capture renders with Workbench and puts the scene's settings back afterwards.
- Scenes with many objects, materials or meshes render at 512 pixels at most.
- Check Blender's console for render error messages.

## Requirements

- **Blender 4.2+** with the Blender Lab MCP add-on (tested with 5.3)
