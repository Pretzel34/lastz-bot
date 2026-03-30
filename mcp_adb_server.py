"""
mcp_adb_server.py
=================
MCP server that exposes ADB tools for Claude Code.

Tools:
  adb_screenshot   — capture current device screen (returns base64 PNG)
  adb_devices      — list connected ADB devices
  adb_shell        — run an adb shell command
  bot_logs         — read recent lines from the bot log file
"""

import base64
import io
import json
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_DIR   = Path(__file__).parent
LOG_DIR   = BOT_DIR / "logs"
ADB_PATH  = r"C:\Program Files (x86)\Nox\bin\nox_adb.exe"
ADB_PORT  = "62025"   # default NOX port; will be overridden by running config

def _adb_exe() -> str:
    """Return ADB executable path from config.json if available, else default."""
    try:
        cfg_path = BOT_DIR / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            emu = cfg.get("emulator", {})
            adb = emu.get("adb_path", "")
            if adb and Path(adb).exists():
                return adb
    except Exception:
        pass
    return ADB_PATH

def _adb_port() -> str:
    """Return ADB port from config.json if available."""
    try:
        cfg_path = BOT_DIR / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            ports = cfg.get("emulator", {}).get("ports", [])
            if ports:
                return str(ports[0])
    except Exception:
        pass
    return ADB_PORT

def _run_adb(*args, timeout=10) -> tuple[int, str, str]:
    """Run an ADB command, return (returncode, stdout, stderr)."""
    adb = _adb_exe()
    cmd = [adb] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"ADB command timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "", f"ADB not found at: {adb}"

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("adb-bot")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="adb_screenshot",
            description=(
                "Capture the current screen from the Android emulator. "
                "Returns a PNG image so you can see the current game state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {
                        "type": "string",
                        "description": "ADB port (e.g. '62025'). Defaults to config.json value."
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="adb_devices",
            description="List all ADB-connected devices/emulators.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="adb_shell",
            description=(
                "Run an adb shell command on the emulator. "
                "Example: 'dumpsys window' or 'ps | grep barrel'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run on device."
                    },
                    "port": {
                        "type": "string",
                        "description": "ADB port. Defaults to config.json value."
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="bot_logs",
            description="Read recent lines from the bot's log file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of recent log lines to return (default 100)."
                    },
                    "farm": {
                        "type": "string",
                        "description": "Farm name to filter logs (optional). Matches partial names."
                    }
                },
                "required": []
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "adb_screenshot":
        port = arguments.get("port") or _adb_port()
        target = f"127.0.0.1:{port}"

        # Connect
        _run_adb("connect", target)

        # screencap to stdout
        rc, _, stderr = _run_adb("-s", target, "wait-for-device", timeout=5)

        result = subprocess.run(
            [_adb_exe(), "-s", target, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            return [TextContent(type="text", text=f"Screenshot failed: {result.stderr.decode(errors='replace')}")]

        png_bytes = result.stdout
        # Verify PNG header
        if not png_bytes.startswith(b'\x89PNG'):
            return [TextContent(type="text", text=f"Bad PNG data ({len(png_bytes)} bytes). Device may not be ready.")]

        b64 = base64.b64encode(png_bytes).decode()
        return [ImageContent(type="image", data=b64, mimeType="image/png")]

    elif name == "adb_devices":
        rc, stdout, stderr = _run_adb("devices", "-l")
        text = stdout or stderr or "No output"
        return [TextContent(type="text", text=text)]

    elif name == "adb_shell":
        command = arguments.get("command", "")
        port = arguments.get("port") or _adb_port()
        target = f"127.0.0.1:{port}"
        _run_adb("connect", target)
        rc, stdout, stderr = _run_adb("-s", target, "shell", command, timeout=15)
        output = stdout or stderr or "(no output)"
        return [TextContent(type="text", text=output)]

    elif name == "bot_logs":
        n_lines = int(arguments.get("lines", 100))
        farm_filter = arguments.get("farm", "").lower()

        # Find most recent log file
        log_files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            return [TextContent(type="text", text="No log files found in logs/")]

        log_path = log_files[0]
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return [TextContent(type="text", text=f"Could not read log: {e}")]

        if farm_filter:
            lines = [l for l in lines if farm_filter in l.lower()]

        tail = lines[-n_lines:]
        text = f"[{log_path.name}]\n" + "\n".join(tail)
        return [TextContent(type="text", text=text)]

    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    asyncio.run(stdio_server(server))
