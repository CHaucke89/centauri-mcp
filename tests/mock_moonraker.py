"""Tiny fake Moonraker for offline testing."""
import sys
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

STATE = {"state": "standby", "gcode": []}
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


def ok(r):
    return JSONResponse({"result": r})


async def query(req):
    body = await req.json()
    full = {
        "heaters": {"available_heaters": ["extruder", "heater_bed"], "available_sensors": ["temperature_sensor chamber"]},
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": {"state": STATE["state"], "filename": "benchy.gcode", "print_duration": 120.4,
                        "total_duration": 130, "filament_used": 812.3, "message": "", "info": {"current_layer": 5, "total_layer": 100}},
        "virtual_sdcard": {"progress": 0.1234, "file_path": "benchy.gcode", "is_active": False},
        "display_status": {"message": None, "progress": 0},
        "toolhead": {"position": [10, 20, 5, 0], "homed_axes": "xyz", "print_time": 1, "extruder": "extruder"},
        "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
        "fan": {"speed": 0.5}, "idle_timeout": {"state": "Idle"},
        "extruder": {"temperature": 201.23, "target": 200},
        "heater_bed": {"temperature": 59.9, "target": 60},
        "temperature_sensor chamber": {"temperature": 31.0},
    }
    return ok({"status": {k: full[k] for k in body["objects"] if k in full}})


async def script(req):
    s = (await req.json())["script"]
    STATE["gcode"].append(s)
    if "BAD" in s:
        return JSONResponse({"error": {"code": 400, "message": "Unknown command BAD"}}, status_code=400)
    return ok("ok")


async def start(req):
    STATE["state"] = "printing"
    return ok("ok")


async def files(req):
    return ok([{"path": "benchy.gcode", "modified": 2, "size": 2048000}, {"path": "old.gcode", "modified": 1, "size": 1024}])


routes = [
    Route("/printer/objects/query", query, methods=["POST"]),
    Route("/printer/objects/list", lambda r: ok({"objects": ["extruder", "heater_bed"]})),
    Route("/printer/gcode/script", script, methods=["POST"]),
    Route("/printer/gcode/help", lambda r: ok({"G28": "Home", "CFS_LOAD": "Load filament"})),
    Route("/printer/print/start", start, methods=["POST"]),
    Route("/printer/print/cancel", lambda r: ok("ok"), methods=["POST"]),
    Route("/server/files/list", files),
    Route("/server/history/list", lambda r: ok({"jobs": [{"filename": "a.gcode", "status": "completed", "print_duration": 5}]})),
    Route("/server/gcode_store", lambda r: ok({"gcode_store": [{"message": "G28"}]})),
    Route("/server/webcams/list", lambda r: ok({"webcams": [{"name": "cam", "enabled": True, "snapshot_url": "/webcam/snap.jpg"}]})),
    Route("/webcam/snap.jpg", lambda r: Response(JPEG, media_type="image/jpeg")),
    Route("/_debug", lambda r: JSONResponse(STATE)),
]
app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=int(sys.argv[1]), log_level="warning")
