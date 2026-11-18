"""MCP server for a Klipper/Moonraker printer (Elegoo Centauri Carbon 2 + OpenCentauri COSMOS).

Runs off-printer and talks to Moonraker's HTTP API (default port 7125).
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


MOONRAKER_URL = os.getenv("MOONRAKER_URL", "http://centauri.local:7125").rstrip("/") + "/"
MOONRAKER_API_KEY = os.getenv("MOONRAKER_API_KEY")
WEBCAM_SNAPSHOT_URL = os.getenv("WEBCAM_SNAPSHOT_URL")

READ_ONLY = _env_bool("READ_ONLY")
ALLOW_UNSAFE_GCODE = _env_bool("ALLOW_UNSAFE_GCODE")

# Safety ceilings enforced here, on top of the max_temp values in printer.cfg
MAX_TEMPS = {
    "extruder": float(os.getenv("MAX_EXTRUDER_TEMP", "300")),
    "heater_bed": float(os.getenv("MAX_BED_TEMP", "110")),
}
MAX_OTHER_TEMP = float(os.getenv("MAX_OTHER_TEMP", "65"))  # e.g. chamber heater
MAX_MOVE_SPEED = float(os.getenv("MAX_MOVE_SPEED", "150"))  # mm/s

mcp = FastMCP(
    "centauri-carbon",
    instructions=(
        "Controls an Elegoo Centauri Carbon 2 (Klipper/Moonraker). Call get_printer_status "
        "first to see the state. Never start a print or heat anything unless the user asked. "
        "Use get_webcam_snapshot to visually confirm the bed is clear before starting."
    ),
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def write_tool(annotations: ToolAnnotations = WRITE):
    """Register a tool only when READ_ONLY is off."""

    def decorator(fn):
        return fn if READ_ONLY else mcp.tool(annotations=annotations)(fn)

    return decorator


# ---------------------------------------------------------------- HTTP helpers

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        headers = {"X-Api-Key": MOONRAKER_API_KEY} if MOONRAKER_API_KEY else {}
        _client = httpx.AsyncClient(base_url=MOONRAKER_URL, headers=headers, timeout=15)
    return _client


async def _call(method: str, path: str, *, timeout: float | None = None, **kwargs) -> Any:
    """Call Moonraker and return the unwrapped `result`, raising readable errors."""
    try:
        r = await _http().request(method, path, timeout=timeout or 15, **kwargs)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()["error"]["message"]
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"Moonraker {e.response.status_code}: {detail}") from None
    except httpx.TransportError as e:
        raise RuntimeError(f"Cannot reach Moonraker at {MOONRAKER_URL}: {e!r}") from None
    return r.json().get("result")


async def _query(objects: dict[str, list[str] | None]) -> dict[str, Any]:
    """Query Klipper objects. A value of None means 'all fields'."""
    res = await _call("POST", "printer/objects/query", json={"objects": objects})
    return res["status"]


async def _gcode(script: str, timeout: float = 120) -> str:
    # Moonraker blocks until Klipper finishes the script (homing can take a while)
    await _call("POST", "printer/gcode/script", json={"script": script}, timeout=timeout)
    return "ok"


async def _print_state() -> str:
    return (await _query({"print_stats": ["state"]}))["print_stats"]["state"]


# ---------------------------------------------------------------- G-code guard


def _heater_limit(heater: str) -> float:
    return MAX_TEMPS.get(heater, MAX_OTHER_TEMP)


_TEMP_CMDS = {  # command -> heater it targets
    "M104": "extruder", "M109": "extruder",
    "M140": "heater_bed", "M190": "heater_bed",
    "M141": "chamber", "M191": "chamber",
}
_MOTION = re.compile(r"^(G0|G1|G2|G3|G28|G29|G92|M84|M18|QUAD_GANTRY_LEVEL|Z_TILT_ADJUST)\b")
_ALWAYS_BLOCKED = {
    "M112": "use the emergency_stop tool instead",
    "SAVE_CONFIG": "rewrites printer.cfg and restarts Klipper; do this manually",
    "RUN_SHELL_COMMAND": "arbitrary shell execution is not allowed",
}


def _check_gcode(script: str, printing: bool) -> None:
    """Raise ValueError if any line breaks a safety rule."""
    if ALLOW_UNSAFE_GCODE:
        return
    for raw in script.splitlines():
        line = raw.split(";", 1)[0].strip().upper()
        if not line:
            continue
        cmd = line.split()[0]
        if cmd in _ALWAYS_BLOCKED:
            raise ValueError(f"{cmd} blocked: {_ALWAYS_BLOCKED[cmd]}")
        if printing and _MOTION.match(line):
            raise ValueError(f"'{line}' blocked: motion commands are not allowed while printing")

        target: float | None = None
        heater = ""
        if cmd in _TEMP_CMDS and (m := re.search(r"\bS(-?\d+(?:\.\d+)?)", line)):
            target, heater = float(m.group(1)), _TEMP_CMDS[cmd]
        elif cmd == "SET_HEATER_TEMPERATURE":
            t = re.search(r"\bTARGET=(-?\d+(?:\.\d+)?)", line)
            h = re.search(r'\bHEATER="?([^"\s]+)', line)
            if t and h:
                target, heater = float(t.group(1)), h.group(1).lower()
        if target is not None and target > _heater_limit(heater.lower()):
            raise ValueError(f"'{line}' blocked: {target}°C exceeds limit {_heater_limit(heater.lower())}°C")


# ---------------------------------------------------------------- Read tools


@mcp.tool(annotations=READ)
async def get_printer_status() -> dict:
    """Snapshot of the printer: Klipper state, print job, progress, temperatures, fans, speeds, position."""
    heaters = (await _query({"heaters": None}))["heaters"]
    names = heaters.get("available_heaters", []) + heaters.get("available_sensors", [])
    wanted: dict[str, list[str] | None] = {
        "webhooks": ["state", "state_message"],
        "print_stats": None,
        "virtual_sdcard": ["progress", "file_path", "is_active"],
        "display_status": ["message", "progress"],
        "toolhead": ["position", "homed_axes", "print_time", "extruder"],
        "gcode_move": ["speed_factor", "extrude_factor"],
        "fan": ["speed"],
        "idle_timeout": ["state"],
    }
    wanted.update({n: ["temperature", "target"] for n in names})
    s = await _query(wanted)

    temps = {
        n: {"temp": round(s[n]["temperature"], 1), **({"target": s[n]["target"]} if "target" in s[n] else {})}
        for n in names
        if n in s
    }
    ps = s["print_stats"]
    return {
        "klippy": s["webhooks"],
        "idle_timeout": s["idle_timeout"]["state"],
        "print": {
            "state": ps["state"],
            "filename": ps["filename"],
            "progress_pct": round(s["virtual_sdcard"]["progress"] * 100, 1),
            "print_duration_s": round(ps["print_duration"]),
            "total_duration_s": round(ps["total_duration"]),
            "filament_used_mm": round(ps["filament_used"]),
            "layer": ps.get("info"),
            "message": ps["message"] or s["display_status"]["message"],
        },
        "temperatures": temps,
        "fan_pct": round(s["fan"]["speed"] * 100),
        "speed_factor": s["gcode_move"]["speed_factor"],
        "flow_factor": s["gcode_move"]["extrude_factor"],
        "position_xyze": s["toolhead"]["position"],
        "homed_axes": s["toolhead"]["homed_axes"],
    }


@mcp.tool(annotations=READ)
async def list_printer_objects() -> list[str]:
    """List every Klipper object (heaters, sensors, fans, macros config...). Use with query_printer_objects."""
    return (await _call("GET", "printer/objects/list"))["objects"]


@mcp.tool(annotations=READ)
async def query_printer_objects(objects: dict[str, list[str] | None]) -> dict:
    """Query raw Klipper objects. Example: {"extruder": ["temperature", "pressure_advance"], "toolhead": null}.
    A null field list returns all fields."""
    return await _query(objects)


@mcp.tool(annotations=READ)
async def list_gcode_commands(filter: str = "") -> dict[str, str]:
    """List G-code commands/macros the firmware accepts (includes COSMOS-specific ones).
    `filter` is a case-insensitive substring match."""
    cmds = await _call("GET", "printer/gcode/help")
    f = filter.lower()
    return {k: v for k, v in sorted(cmds.items()) if f in k.lower()}


@mcp.tool(annotations=READ)
async def list_files(path: str = "", limit: int = 50) -> list[dict]:
    """List G-code files, newest first. `path` filters by directory prefix. Returns name, size and modified time."""
    files = await _call("GET", "server/files/list", params={"root": "gcodes"})
    files = [f for f in files if f["path"].startswith(path)]
    files.sort(key=lambda f: f["modified"], reverse=True)
    return [{"path": f["path"], "size_kb": round(f["size"] / 1024), "modified": f["modified"]} for f in files[:limit]]


@mcp.tool(annotations=READ)
async def get_file_info(filename: str) -> dict:
    """Slicer metadata for a G-code file: estimated time, filament, layer height, temps, object height."""
    m = await _call("GET", "server/files/metadata", params={"filename": filename})
    m.pop("thumbnails", None)
    m.pop("gcode_images", None)
    return m


@mcp.tool(annotations=READ)
async def get_print_history(limit: int = 10) -> list[dict]:
    """Recent print jobs with status (completed/cancelled/error), duration and filament used."""
    res = await _call("GET", "server/history/list", params={"limit": limit, "order": "desc"})
    keys = ("filename", "status", "start_time", "print_duration", "total_duration", "filament_used")
    return [{k: j.get(k) for k in keys} for j in res["jobs"]]


@mcp.tool(annotations=READ)
async def get_console_log(count: int = 30) -> list[str]:
    """Last N lines of the G-code console (commands and Klipper responses). Useful for debugging errors."""
    res = await _call("GET", "server/gcode_store", params={"count": count})
    return [e["message"] for e in res["gcode_store"]]


@mcp.tool(annotations=READ)
async def get_webcam_snapshot(camera: str = "") -> Image:
    """Capture a JPEG still from the printer camera. `camera` picks a webcam by name (default: first)."""
    url = WEBCAM_SNAPSHOT_URL
    if not url:
        cams = (await _call("GET", "server/webcams/list"))["webcams"]
        cams = [c for c in cams if c.get("enabled", True) and (not camera or c["name"] == camera)]
        if not cams:
            raise RuntimeError("No matching webcam configured in Moonraker; set WEBCAM_SNAPSHOT_URL")
        url = cams[0]["snapshot_url"]
    url = urljoin(MOONRAKER_URL, url)  # absolute URLs pass through unchanged
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        r.raise_for_status()
    return Image(data=r.content, format="jpeg")


# ---------------------------------------------------------------- Write tools


@write_tool()
async def set_temperature(heater: str, target_c: float) -> str:
    """Set a heater target in °C (0 turns it off). `heater` is a name from get_printer_status,
    e.g. "extruder" or "heater_bed". Returns immediately without waiting to reach temperature."""
    available = (await _query({"heaters": ["available_heaters"]}))["heaters"]["available_heaters"]
    if heater not in available:
        raise ValueError(f"Unknown heater '{heater}'. Available: {available}")
    limit = _heater_limit(heater.split()[-1])
    if not 0 <= target_c <= limit:
        raise ValueError(f"Target must be between 0 and {limit}°C for {heater}")
    # "heater_generic chamber" is addressed as just "chamber" by SET_HEATER_TEMPERATURE
    await _gcode(f"SET_HEATER_TEMPERATURE HEATER={heater.split()[-1]} TARGET={target_c}")
    return f"{heater} target set to {target_c}°C"


@write_tool()
async def set_fan_speed(percent: float) -> str:
    """Set the part-cooling fan speed, 0-100%."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be 0-100")
    await _gcode(f"M106 S{round(percent / 100 * 255)}")
    return f"Fan set to {percent}%"


@write_tool()
async def set_speed_and_flow(speed_percent: float | None = None, flow_percent: float | None = None) -> str:
    """Adjust the live print speed override (10-300%) and/or flow override (50-150%). Works mid-print."""
    lines = []
    if speed_percent is not None:
        if not 10 <= speed_percent <= 300:
            raise ValueError("speed_percent must be 10-300")
        lines.append(f"M220 S{speed_percent}")
    if flow_percent is not None:
        if not 50 <= flow_percent <= 150:
            raise ValueError("flow_percent must be 50-150")
        lines.append(f"M221 S{flow_percent}")
    if not lines:
        raise ValueError("Provide speed_percent and/or flow_percent")
    await _gcode("\n".join(lines))
    return "ok: " + ", ".join(lines)


@write_tool()
async def start_print(filename: str) -> str:
    """Start printing a file already on the printer (path as shown by list_files).
    Only call when the user explicitly asks; ideally confirm the bed is clear with a webcam snapshot."""
    state = await _print_state()
    if state in ("printing", "paused"):
        raise RuntimeError(f"Printer is already {state}")
    await _call("POST", "printer/print/start", params={"filename": filename})
    return f"Started {filename}"


@write_tool()
async def pause_print() -> str:
    """Pause the current print (toolhead parks via the firmware's PAUSE macro)."""
    await _call("POST", "printer/print/pause")
    return "Paused"


@write_tool()
async def resume_print() -> str:
    """Resume a paused print."""
    await _call("POST", "printer/print/resume")
    return "Resumed"


@write_tool(DESTRUCTIVE)
async def cancel_print() -> str:
    """Cancel the current print. This cannot be undone."""
    await _call("POST", "printer/print/cancel")
    return "Cancelled"


@write_tool(DESTRUCTIVE)
async def emergency_stop() -> str:
    """Immediately halt Klipper (M112). Heaters and motors shut off; a firmware_restart is needed afterwards."""
    await _call("POST", "printer/emergency_stop")
    return "Emergency stop triggered. Run firmware_restart to recover."


@write_tool()
async def firmware_restart() -> str:
    """Restart Klipper firmware and host. Use after an error/shutdown. Refused while printing."""
    if await _print_state() == "printing":
        raise RuntimeError("Refusing to restart during a print; cancel_print first")
    await _call("POST", "printer/firmware_restart", timeout=60)
    return "Firmware restart requested"


@write_tool()
async def home_axes(axes: str = "") -> str:
    """Home the printer. `axes` is any of X, Y, Z (e.g. "XY"); empty homes everything."""
    axes = axes.upper().replace(" ", "")
    if not set(axes) <= set("XYZ"):
        raise ValueError("axes must only contain X, Y, Z")
    _check_gcode("G28", await _print_state() == "printing")
    await _gcode("G28 " + " ".join(axes) if axes else "G28", timeout=180)
    return "Homed " + (axes or "all axes")


@write_tool()
async def move_toolhead(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    relative: bool = True,
    speed_mm_s: float = 50,
) -> str:
    """Move the toolhead in mm. Relative by default. Axes must be homed first. Refused while printing."""
    if await _print_state() == "printing":
        raise RuntimeError("Cannot move while printing")
    if not 0 < speed_mm_s <= MAX_MOVE_SPEED:
        raise ValueError(f"speed_mm_s must be in (0, {MAX_MOVE_SPEED}]")
    coords = " ".join(f"{a}{v}" for a, v in (("X", x), ("Y", y), ("Z", z)) if v is not None)
    if not coords:
        raise ValueError("Provide at least one of x, y, z")
    move = f"G1 {coords} F{round(speed_mm_s * 60)}"
    await _gcode(f"G91\n{move}\nG90" if relative else f"G90\n{move}")
    return f"Moved: {coords} ({'relative' if relative else 'absolute'})"


@write_tool()
async def disable_motors() -> str:
    """Disable stepper motors (M84). Loses homing. Refused while printing."""
    if await _print_state() == "printing":
        raise RuntimeError("Cannot disable motors while printing")
    await _gcode("M84")
    return "Motors disabled"


@write_tool()
async def send_gcode(script: str) -> str:
    """Run raw G-code/macros (newline-separated). Temperature limits are enforced, motion is blocked
    while printing, and M112/SAVE_CONFIG/RUN_SHELL_COMMAND are refused. Prefer the dedicated tools."""
    _check_gcode(script, await _print_state() == "printing")
    await _gcode(script)
    return "ok"


# ---------------------------------------------------------------- Entrypoint


class BearerAuth:
    """ASGI middleware: require `Authorization: Bearer <token>` on every HTTP request."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if auth != self.expected:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn

    app = mcp.streamable_http_app()  # served at /mcp
    if token := os.getenv("MCP_AUTH_TOKEN"):
        app = BearerAuth(app, token)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set; anyone on the network can control the printer")
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)


if __name__ == "__main__":
    main()
