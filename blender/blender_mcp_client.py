"""Run Python in an open Blender through the Blender Lab MCP add-on.

The add-on (https://www.blender.org/lab/mcp-server/) runs a TCP server inside Blender, on localhost:9876 by default.
It reads one null-terminated JSON request, `{"type": "execute", "code": ..., "strict_json": true}`, runs the code
on Blender's main thread, and replies with a null-terminated JSON response: `{"status": "ok", "result": ...}`, where
`result` is the dict the code assigned to its `result` variable, or `{"status": "error", "message": ...}`.

This talks to the add-on's socket directly. The MCP server process and an LLM are not involved.
"""

import json
import socket
from typing import Any

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
CONNECT_TIMEOUT_SECONDS = 5
RESPONSE_TIMEOUT_SECONDS = 120


class BlenderMCPError(RuntimeError):
    """The open Blender could not be reached, or the code it ran failed."""


def execute(code: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Run `code` in the open Blender and return the dict it assigned to `result`."""
    request = json.dumps({"type": "execute", "code": code, "strict_json": True}).encode("utf-8") + b"\0"
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    except (OSError, socket.gaierror) as e:
        msg = (
            f"Could not reach Blender at {host}:{port}. In Blender, install the MCP add-on from Blender Lab, turn on "
            "Preferences > System > Allow Online Access, and start the MCP server."
        )
        raise BlenderMCPError(msg) from e
    try:
        with sock:
            sock.settimeout(RESPONSE_TIMEOUT_SECONDS)
            sock.sendall(request)
            buffer = bytearray()
            while b"\0" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
    except TimeoutError as e:
        msg = f"Blender at {host}:{port} did not answer within {RESPONSE_TIMEOUT_SECONDS} seconds."
        raise BlenderMCPError(msg) from e
    except OSError as e:
        msg = f"Lost the connection to Blender at {host}:{port}: {e}"
        raise BlenderMCPError(msg) from e

    if b"\0" not in buffer:
        msg = f"Blender at {host}:{port} closed the connection without a complete answer."
        raise BlenderMCPError(msg)
    response = json.loads(bytes(buffer[: buffer.index(b"\0")]))
    if response.get("status") != "ok":
        lines = str(response.get("message", response)).strip().splitlines()
        msg = f"Blender reported an error: {lines[-1] if lines else response}"
        raise BlenderMCPError(msg)
    return response.get("result") or {}
