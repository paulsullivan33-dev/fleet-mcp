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
import signal
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

CONFIG_PATH = os.environ.get(
    "FLEET_SERVICES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json"),
)
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 3600  # seconds; configured timeouts must stay within 1..MAX_TIMEOUT
MAX_OUTPUT = 8000  # chars; longer output is truncated with a notice
HARD_OUTPUT = MAX_OUTPUT * 8  # bytes; stored output never exceeds this, however noisy a tool gets
MAX_CONCURRENT = 4  # service processes allowed to run at once

PARAM_TYPES = {"string", "number", "integer", "boolean"}

server = Server("fleet")
_services = []  # list of validated service dicts, in file order


def _fail(msg):
    return [TextContent(type="text", text=f"error: {msg}")]


def _parse_timeout(raw, where, name, errors):
    """Validate one timeout_secs value. Returns a bounded int or None."""
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw.strip()):
        value = int(raw.strip())
    else:
        value = None
    if value is None:
        errors.append(
            f"{where} ({name}): 'timeout_secs' must be a positive integer, got {raw!r}"
        )
        return None
    if not 1 <= value <= MAX_TIMEOUT:
        errors.append(
            f"{where} ({name}): 'timeout_secs' {value} out of range 1..{MAX_TIMEOUT}"
        )
        return None
    return value


def load_services(path=CONFIG_PATH):
    """Read and validate services.json. Returns (services, errors, fatal).

    A malformed config must never crash startup or reload (F07): every
    shape problem becomes a validation error instead. ``fatal`` is True
    when the file as a whole could not be used (unreadable, invalid
    JSON, wrong top-level shape) -- as opposed to individual bad
    entries, which are skipped with an error each while the valid
    entries still load. On a fatal load, refresh_services() keeps the
    previously loaded service set.
    """
    services, errors = [], []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [], [f"config not found: {path}"], True
    except OSError as e:
        return [], [f"config could not be read: {e}"], True
    except json.JSONDecodeError as e:
        return [], [f"config is not valid JSON: {e}"], True

    if not isinstance(data, dict):
        return [], [f"config root must be a JSON object, got {type(data).__name__}"], True
    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        return [], ["'services' must be a list"], True

    seen = set()
    for i, svc in enumerate(raw_services):
        where = f"services[{i}]"
        if not isinstance(svc, dict):
            errors.append(f"{where}: service entry must be an object, got {type(svc).__name__}")
            continue
        name = svc.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
            errors.append(f"{where}: bad name {name!r} (letters, digits, _; must start with a letter)")
            continue
        if name in seen or name in ("list_services", "reload_services"):
            errors.append(f"{where}: duplicate or reserved name {name!r}")
            continue
        seen.add(name)
        description = svc.get("description", "")
        if not isinstance(description, str):
            errors.append(f"{where} ({name}): 'description' must be a string")
            continue
        cmd = svc.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
            errors.append(f"{where} ({name}): 'command' must be a non-empty list of strings")
            continue
        workdir = svc.get("workdir") or None
        if workdir is not None and not isinstance(workdir, str):
            errors.append(f"{where} ({name}): 'workdir' must be a string")
            continue
        params = svc.get("parameters", {})
        if not isinstance(params, dict):
            errors.append(f"{where} ({name}): 'parameters' must be an object")
            continue
        ok = True
        for pname, pdef in params.items():
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", pname):
                errors.append(f"{where} ({name}): bad parameter name {pname!r}")
                ok = False
                continue
            if not isinstance(pdef, dict):
                errors.append(f"{where} ({name}): parameter {pname!r} must be an object")
                ok = False
                continue
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
        timeout = _parse_timeout(
            svc.get("timeout_secs", DEFAULT_TIMEOUT), where, name, errors
        )
        if timeout is None:
            continue
        services.append(
            {
                "name": name,
                "description": description,
                "command": cmd,
                "parameters": params,
                "timeout_secs": timeout,
                "workdir": workdir,
            }
        )
    return services, errors, False


def refresh_services():
    """(Re)load services.json into the live tool list. Returns error text or ''.

    A fatally malformed config keeps the previously loaded service set
    (F07) instead of discarding a working configuration.
    """
    global _services
    services, errors, fatal = load_services(CONFIG_PATH)
    if fatal:
        if _services:
            errors.append(
                f"kept the previous configuration ({len(_services)} service(s))"
            )
        return "; ".join(errors)
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


_ARG_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate_args(svc, args):
    """Check call arguments against the declared schema. Returns error or ''.

    The advertised JSON schema is not enforced by the transport, so a
    caller can send undeclared parameters or wrongly typed values;
    reject both here instead of silently substituting them (F08).
    """
    params = svc["parameters"]
    for key in args:
        if key not in params:
            return f"unknown parameter {key!r} for service {svc['name']!r}"
    for pname, pdef in params.items():
        if pname in args and args[pname] is not None:
            check = _ARG_CHECKS.get(pdef.get("type", "string"))
            if check is not None and not check(args[pname]):
                return (
                    f"parameter {pname!r} must be of type "
                    f"{pdef.get('type', 'string')}, got {args[pname]!r}"
                )
    return ""


_semaphore = None
_semaphore_loop = None


def _get_semaphore():
    """The concurrency limiter, (re)bound to the running event loop."""
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        _semaphore_loop = loop
    return _semaphore


async def _drain(stream, sink):
    """Read one pipe into sink, storing at most HARD_OUTPUT bytes total.

    Keeps draining past the cap (discarding) so a flooding child never
    blocks on a full pipe; memory stays bounded regardless (F08).
    Returns the total number of bytes seen on the stream.
    """
    total = 0
    stored = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return total
        total += len(chunk)
        if stored < HARD_OUTPUT:
            keep = chunk[: HARD_OUTPUT - stored]
            sink.append(keep)
            stored += len(keep)


def _kill_tree(proc):
    """Kill a service process and, on POSIX, its whole process group."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
            return
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


async def _run_process(argv, workdir, timeout):
    """Run argv as an async subprocess with streamed, bounded output.

    Returns (returncode, stdout_bytes, stderr_bytes, total_bytes_seen).
    On timeout or cancellation the whole process group is killed before
    the exception propagates (F08).
    """
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        **kwargs,
    )
    out_sink, err_sink = [], []

    async def collect():
        totals = await asyncio.gather(
            _drain(proc.stdout, out_sink), _drain(proc.stderr, err_sink)
        )
        await proc.wait()
        return totals

    try:
        totals = await asyncio.wait_for(collect(), timeout)
    except BaseException:
        _kill_tree(proc)
        try:
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode, b"".join(out_sink), b"".join(err_sink), sum(totals)


async def run_service(svc, args):
    """Run one service asynchronously; returns plain-text result for the model.

    Runs off the event loop's critical path: the handler awaits the
    child instead of blocking in subprocess.run, so a slow tool cannot
    freeze unrelated protocol work (F08).
    """
    err = validate_args(svc, args)
    if err:
        return f"error: {err}"
    argv, err = build_command(svc, args)
    if err:
        return f"error: {err}"
    async with _get_semaphore():
        try:
            rc, out_b, err_b, total = await _run_process(
                argv, svc["workdir"], svc["timeout_secs"]
            )
        except FileNotFoundError:
            return f"error: command not found: {argv[0]} (check 'command' and 'workdir' in services.json)"
        except (asyncio.TimeoutError, TimeoutError):
            return f"error: timed out after {svc['timeout_secs']}s"
        except OSError as e:
            return f"error: could not run command: {e}"
    out = out_b.decode("utf-8", errors="replace") + err_b.decode(
        "utf-8", errors="replace"
    )
    if rc != 0 and not out.strip():
        out = f"(exit code {rc}, no output)"
    elif rc != 0:
        out = f"(exit code {rc})\n{out}"
    if total > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n...[truncated, {total - MAX_OUTPUT} more chars]"
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
            return [TextContent(type="text", text=await run_service(svc, args))]
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
