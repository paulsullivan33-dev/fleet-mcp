"""fleet_mcp.py -- config-driven MCP server for home-fleet services (stdio).

Each "service" in services.json becomes an MCP tool automatically: edit
the JSON to expose a new script or command, then call the reload_services
tool (or restart) -- no code changes needed.

Requires: pip install "mcp<2"   (v1 API, same as the compuserve sim scaffold)

Config path: ./services.json, or set FLEET_SERVICES=/path/to/services.json.
"""

import asyncio
import json
import os
import re
import subprocess
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

CONFIG_PATH = os.environ.get(
    "FLEET_SERVICES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json"),
)
DEFAULT_TIMEOUT = 60
MAX_OUTPUT = 8000  # chars; longer output is truncated with a notice

PARAM_TYPES = {"string", "number", "integer", "boolean"}

server = Server("fleet")
_services = []  # list of validated service dicts, in file order


def _fail(msg):
    return [TextContent(type="text", text=f"error: {msg}")]


def load_services(path=CONFIG_PATH):
    """Read and validate services.json. Returns (services, errors)."""
    services, errors = [], []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [], [f"config not found: {path}"]
    except json.JSONDecodeError as e:
        return [], [f"config is not valid JSON: {e}"]

    seen = set()
    for i, svc in enumerate(data.get("services", [])):
        where = f"services[{i}]"
        name = svc.get("name", "")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
            errors.append(f"{where}: bad name {name!r} (letters, digits, _; must start with a letter)")
            continue
        if name in seen or name in ("list_services", "reload_services"):
            errors.append(f"{where}: duplicate or reserved name {name!r}")
            continue
        seen.add(name)
        cmd = svc.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
            errors.append(f"{where} ({name}): 'command' must be a non-empty list of strings")
            continue
        params = svc.get("parameters", {})
        ok = True
        for pname, pdef in params.items():
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", pname):
                errors.append(f"{where} ({name}): bad parameter name {pname!r}")
                ok = False
            if pdef.get("type", "string") not in PARAM_TYPES:
                errors.append(f"{where} ({name}): parameter {pname!r} has bad type {pdef.get('type')!r}")
                ok = False
        if not ok:
            continue
        # every {placeholder} in the command must be a declared parameter
        placeholders = set(re.findall(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", " ".join(cmd)))
        unknown = placeholders - set(params)
        if unknown:
            errors.append(f"{where} ({name}): command uses undeclared parameters {sorted(unknown)}")
            continue
        services.append(
            {
                "name": name,
                "description": svc.get("description", ""),
                "command": cmd,
                "parameters": params,
                "timeout_secs": int(svc.get("timeout_secs", DEFAULT_TIMEOUT)),
                "workdir": svc.get("workdir") or None,
            }
        )
    return services, errors


def refresh_services():
    """(Re)load services.json into the live tool list. Returns error text or ''."""
    global _services
    services, errors = load_services()
    _services = services
    return "; ".join(errors)


def _input_schema(params):
    props, required = {}, []
    for pname, pdef in params.items():
        props[pname] = {
            "type": pdef.get("type", "string"),
            "description": pdef.get("description", ""),
        }
        if "default" in pdef:
            props[pname]["default"] = pdef["default"]
        if pdef.get("required", False):
            required.append(pname)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def build_command(svc, args):
    """Substitute {params} into the command arg list. Returns (argv, error)."""
    argv = []
    for part in svc["command"]:
        def sub(m):
            pname = m.group(1)
            if pname in args and args[pname] is not None:
                return str(args[pname])
            default = svc["parameters"][pname].get("default")
            return str(default) if default is not None else m.group(0)

        expanded = re.sub(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", sub, part)
        leftovers = re.findall(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", expanded)
        if leftovers:
            return None, f"missing required parameter(s): {', '.join(leftovers)}"
        argv.append(expanded)
    return argv, ""


def run_service(svc, args):
    """Run one service; returns plain-text result for the model."""
    argv, err = build_command(svc, args)
    if err:
        return f"error: {err}"
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=svc["timeout_secs"],
            cwd=svc["workdir"],
        )
    except FileNotFoundError:
        return f"error: command not found: {argv[0]} (check 'command' and 'workdir' in services.json)"
    except subprocess.TimeoutExpired:
        return f"error: timed out after {svc['timeout_secs']}s"
    except OSError as e:
        return f"error: could not run command: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not out.strip():
        out = f"(exit code {proc.returncode}, no output)"
    elif proc.returncode != 0:
        out = f"(exit code {proc.returncode})\n{out}"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n...[truncated, {len(out) - MAX_OUTPUT} more chars]"
    return out.strip() or "(no output)"


def describe_services():
    lines = []
    for s in _services:
        params = ", ".join(
            f"{n}{'' if p.get('required') else ' (optional)'}"
            for n, p in s["parameters"].items()
        )
        lines.append(f"- {s['name']}({params}): {s['description']}")
    return "services:\n" + "\n".join(lines) if lines else "no services configured"


@server.list_tools()
async def _list_tools():
    tools = [
        Tool(
            name=s["name"],
            description=s["description"] or s["name"],
            inputSchema=_input_schema(s["parameters"]),
        )
        for s in _services
    ]
    tools.append(
        Tool(
            name="list_services",
            description="List all configured services and their parameters.",
            inputSchema={"type": "object", "properties": {}},
        )
    )
    tools.append(
        Tool(
            name="reload_services",
            description="Re-read services.json so newly added or edited services take effect without restarting.",
            inputSchema={"type": "object", "properties": {}},
        )
    )
    return tools


@server.call_tool()
async def _call_tool(name, arguments):
    args = arguments or {}
    if name == "list_services":
        return [TextContent(type="text", text=describe_services())]
    if name == "reload_services":
        err = refresh_services()
        msg = f"reloaded {len(_services)} service(s)"
        return [TextContent(type="text", text=msg if not err else f"{msg}; warnings: {err}")]
    for svc in _services:
        if svc["name"] == name:
            return [TextContent(type="text", text=run_service(svc, args))]
    return _fail(f"unknown tool {name!r}; call list_services to see what's available")


async def _main():
    err = refresh_services()
    if err:
        print(f"fleet_mcp: config warnings: {err}", file=sys.stderr)
    print(f"fleet_mcp: serving {len(_services)} service(s) from {CONFIG_PATH}", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
