- **Breaking:** `Blender Camera Capture` and `Blender Camera List` talk to the open Blender through the
  Blender Lab MCP add-on instead of the Griptape socket server script on port 8765. Install the add-on,
  turn on Preferences > System > Allow Online Access, and start its server. Both nodes have a new
  `MCP Port` parameter, 9876 by default.
