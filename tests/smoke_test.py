"""Drives server.py over stdio with a real MCP client against the mock."""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def run(read_only=False):
    env = {**os.environ, "MOONRAKER_URL": "http://127.0.0.1:7126", "READ_ONLY": "1" if read_only else "0"}
    params = StdioServerParameters(command=sys.executable, args=[f"{ROOT}/server.py"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = [t.name for t in (await s.list_tools()).tools]
        print(("READ_ONLY" if read_only else "FULL"), len(tools), "tools")
        if read_only:
            assert "start_print" not in tools and "get_printer_status" in tools
            return

        async def call(name, **a):
            res = await s.call_tool(name, a)
            txt = res.content[0].text if res.content and hasattr(res.content[0], "text") else f"<{res.content[0].type}>"
            print(f"{'ERR' if res.isError else 'ok '} {name}({a}) -> {txt[:110]}")
            return res

        st = await call("get_printer_status")
        assert not st.isError
        await call("list_files")
        await call("get_webcam_snapshot")
        await call("list_gcode_commands", filter="cfs")
        await call("set_temperature", heater="extruder", target_c=210)
        assert (await call("set_temperature", heater="extruder", target_c=400)).isError
        assert (await call("set_temperature", heater="nope", target_c=1)).isError
        assert (await call("send_gcode", script="M104 S999")).isError
        assert (await call("send_gcode", script="SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=300")).isError
        assert (await call("send_gcode", script="SAVE_CONFIG")).isError
        assert (await call("send_gcode", script="BAD")).isError  # Moonraker error surfaces
        await call("move_toolhead", x=10, speed_mm_s=30)
        assert (await call("move_toolhead", x=10, speed_mm_s=999)).isError
        await call("start_print", filename="benchy.gcode")
        assert (await call("start_print", filename="benchy.gcode")).isError  # already printing
        assert (await call("move_toolhead", x=1)).isError  # printing
        assert (await call("send_gcode", script="G28")).isError  # printing
        assert (await call("firmware_restart")).isError
        await call("cancel_print")


async def main():
    await run(False)
    await run(True)
    print("ALL PASSED")

asyncio.run(main())
