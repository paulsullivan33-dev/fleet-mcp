# fleet-mcp

An MCP server that exposes your home-fleet scripts and commands as tools
an AI assistant can call. **Config-driven:** each entry in
`services.json` becomes a tool automatically — edit the JSON, call the
`reload_services` tool (or restart), and the new tool is live. No code
changes.

Runs on stdio, like the CompuServe sim scaffold: Cline, Claude Desktop,
or `mcpo` launches it and speaks MCP to it.

## Quick start (on the ProDesk)

```bash
cd ~/fleet-mcp
python3 -m venv .venv
.venv/bin/pip install "mcp<2" mcpo
```

Edit `services.json` (copy it from `services.example.json` first — the
real one is gitignored so your hostnames stay home) so the `command`
paths point at your real scripts, then smoke-test:

```bash
FLEET_SERVICES=$PWD/services.json .venv/bin/python fleet_mcp.py
```

(in another terminal, or via an MCP client — it speaks MCP over stdin/stdout)

Expose it to Open WebUI via mcpo:

```bash
.venv/bin/mcpo --port 8000 -- .venv/bin/python fleet_mcp.py
```

Then in Open WebUI: Admin Panel → Settings → Tools → add
`http://<prodesk>:8000`. Your tools appear in chat.

Keep it running with systemd — save as
`~/.config/systemd/user/fleet-mcpo.service`:

```ini
[Unit]
Description=fleet MCP via mcpo
After=network.target

[Service]
WorkingDirectory=%h/fleet-mcp
ExecStart=%h/fleet-mcp/.venv/bin/mcpo --port 8000 -- %h/fleet-mcp/.venv/bin/python %h/fleet-mcp/fleet_mcp.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now fleet-mcpo
```

## services.json format

```json
{
  "services": [
    {
      "name": "cpu_check",
      "description": "What this does, in plain words for the model.",
      "command": ["/home/paul/bin/cpu-check.sh", "{host}"],
      "parameters": {
        "host": {
          "type": "string",
          "description": "Fleet host: hostname, IP, or [user@]host.",
          "required": true
        },
        "count": {
          "type": "integer",
          "description": "How many lines.",
          "required": false,
          "default": 5
        }
      },
      "timeout_secs": 30,
      "workdir": "/home/paul/bin"
    }
  ]
}
```

- `name`: letters, digits, `_`; must start with a letter. Two names are
  reserved: `list_services` and `reload_services` (built in).
- `command`: a list of strings — **no shell**, each entry is one
  argument. `{param}` placeholders are replaced with the model's
  argument values. Every placeholder must match a declared parameter.
- `parameters`: `type` is one of `string`, `number`, `integer`,
  `boolean`. `required: true` means the model must supply it;
  otherwise `default` is used when it's omitted.
- `timeout_secs`: per-service timeout (default 60). `workdir`: optional
  directory the command runs in.

After editing, either restart or call the `reload_services` tool — new
and changed services take effect immediately. Config mistakes are
reported as warnings (the server keeps running with the services that
loaded cleanly).

## Companion scripts

`disk-check.sh` ships in this repo as an example companion script — copy
it next to your other check scripts and point a service entry at it (see
`services.example.json` for the `disk_check` entry).

## Built-in tools

- `list_services` — lists every configured service and its parameters.
- `reload_services` — re-reads `services.json` without restarting.
  (Restarting the systemd service itself is a config entry too — see the
  `restart_fleet_mcp` example in `services.example.json`.)

## Security notes

- Commands run as the user who launched the server, with that user's
  full permissions. Only expose scripts you trust, on a network you
  trust.
- Keep mcpo on your LAN. Do not expose its port to the internet —
  it executes real commands on real machines.
- Output is capped at 8000 characters per call so a chatty script
  can't flood the model's context.
