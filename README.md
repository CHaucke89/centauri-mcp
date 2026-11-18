# centauri-mcp

MCP server for a Klipper/Moonraker printer (Elegoo Centauri Carbon 2 on OpenCentauri COSMOS).
Runs on your home server and talks to Moonraker over HTTP (port 7125).

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit MOONRAKER_URL and MCP_AUTH_TOKEN
set -a; . ./.env; set +a; .venv/bin/python server.py
```
Run as a service with `centauri-mcp.service` (systemd).

## Connect Claude
```bash
claude mcp add --transport http centauri http://HOMESERVER:8000/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
Or use `MCP_TRANSPORT=stdio` and register `python server.py` as a command (e.g. via SSH).

## Tests (no printer needed)
```bash
.venv/bin/python tests/mock_moonraker.py 7126 &
.venv/bin/python tests/smoke_test.py
```

## Safety
- Temperature ceilings (`MAX_*_TEMP`) apply to dedicated tools and raw G-code.
- Motion, homing and motor-off are refused while printing.
- `M112`, `SAVE_CONFIG`, `RUN_SHELL_COMMAND` are blocked in `send_gcode`.
- `READ_ONLY=1` removes all mutating tools. `ALLOW_UNSAFE_GCODE=1` disables the G-code guard.
- Serve over a trusted LAN/VPN only; never expose the port to the internet.
