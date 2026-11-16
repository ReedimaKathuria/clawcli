from __future__ import annotations
"""
CLI orchestration: runs ONLY allowlisted tools, never through a shell, with timeouts,
cancellation, a scrubbed environment and live streaming of stderr.
"""
import asyncio
import json
import os
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .security import redact

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = int(os.getenv("CLI_TIMEOUT_SEC", "90"))
MAX_STDOUT = 5_000_000

# The allowlist: tool name -> fixed executable plus the argument builder. The model cannot add entries.
TOOLS = {
    "sitecrawler": {
        "exe": [sys.executable, "-I", str(ROOT / "cli" / "sitecrawler.py")],
        "args": lambda p: ["--url", p.url, "--max-pages", str(p.max_pages),
                           "--max-depth", str(p.max_depth), "--timeout", "10"],
        "exit_codes": {0: "success", 2: "invalid_arguments", 3: "unreachable",
                       4: "blocked_by_robots", 10: "partial"},
    }
}

RUNS: dict[str, asyncio.subprocess.Process] = {}


@dataclass
class ExecResult:
    run_id: str
    command: str
    exit_code: int | None
    status: str
    duration_sec: float
    stdout_json: dict | None
    stderr_tail: list = field(default_factory=list)
    error: str | None = None


def _safe_env():
    # Least privilege: the child process gets no API keys or other secrets.
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONIOENCODING": "utf-8",
            "LANG": "C.UTF-8", **({k: os.environ[k] for k in ("HTTPS_PROXY", "HTTP_PROXY", "SSL_CERT_FILE")
                                   if k in os.environ})}


async def run_tool(tool: str, plan, on_log) -> ExecResult:
    spec = TOOLS[tool]  # KeyError means the tool is not allowlisted
    argv = spec["exe"] + spec["args"](plan)
    run_id = uuid.uuid4().hex[:8]
    shown = "sitecrawler " + " ".join(shlex.quote(a) for a in argv[3:])
    t0 = time.monotonic()
    await on_log({"type": "exec_start", "run_id": run_id, "command": shown, "timeout": TIMEOUT})

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_safe_env(), cwd=str(ROOT), start_new_session=True,
    )
    RUNS[run_id] = proc
    tail: list[str] = []

    async def pump_stderr():
        async for raw in proc.stderr:
            line = redact(raw.decode(errors="replace").rstrip())
            tail.append(line)
            del tail[:-40]
            await on_log({"type": "log", "run_id": run_id, "line": line,
                          "t": round(time.monotonic() - t0, 2)})

    async def read_stdout():
        return await proc.stdout.read(MAX_STDOUT)

    status, err, out = "unknown", None, b""
    try:
        out, _ = await asyncio.wait_for(asyncio.gather(read_stdout(), pump_stderr()), timeout=TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        status, err = "timeout", f"CLI exceeded {TIMEOUT}s and was terminated."
    except asyncio.CancelledError:
        proc.kill(); await proc.wait()
        status, err = "cancelled", "Run cancelled by user."
    finally:
        RUNS.pop(run_id, None)

    code = proc.returncode
    if status == "unknown":
        status = spec["exit_codes"].get(code, "cancelled" if code and code < 0 else "failed")

    parsed = None
    if out:
        try:
            parsed = json.loads(out.decode(errors="replace"))
        except json.JSONDecodeError:
            err = err or "CLI produced invalid JSON on stdout."
            status = "failed"

    res = ExecResult(run_id, shown, code, status, round(time.monotonic() - t0, 2), parsed, tail, err)
    await on_log({"type": "exec_end", "run_id": run_id, "exit_code": code, "status": status,
                  "duration_sec": res.duration_sec})
    return res


def cancel(run_id: str) -> bool:
    proc = RUNS.get(run_id)
    if proc and proc.returncode is None:
        proc.kill()
        return True
    return False
