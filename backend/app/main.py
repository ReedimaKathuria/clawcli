from __future__ import annotations
"""
FastAPI entrypoint. POST /api/chat streams NDJSON events:
plan -> exec_start -> log* -> exec_end -> result | error
"""
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import executor, llm, responder
from .intent import interpret
from .security import ValidationError, redact

app = FastAPI(title="ClawCLI", version="1.0")
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
_hits: dict[str, list[float]] = defaultdict(list)
_sem = asyncio.Semaphore(3)  # at most 3 concurrent CLI runs


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


HELP = ("I turn plain-English requests into **real CLI executions**. Try:\n"
        "- `Crawl https://example.com, summarise it and return JSON`\n"
        "- `Extract contact details from python.org, 5 pages`\n"
        "- `Scrape https://books.toscrape.com depth 2`\n\n"
        "I respect robots.txt and enforce page, depth and time limits.")


def _rate_ok(ip: str) -> bool:
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < 60][-20:]
    _hits[ip].append(now)
    return len(_hits[ip]) <= 10


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm.enabled(), "model": llm.MODEL if llm.enabled() else None,
            "tools": list(executor.TOOLS)}


@app.post("/api/cancel/{run_id}")
def cancel(run_id: str):
    return {"cancelled": executor.cancel(run_id)}


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    if not _rate_ok(request.client.host if request.client else "?"):
        return JSONResponse({"error": "Rate limit: 10 requests/minute."}, status_code=429)

    q: asyncio.Queue = asyncio.Queue()

    async def emit(ev):
        await q.put(ev)

    async def work():
        try:
            plan = await interpret(body.message)
            await emit({"type": "plan", "plan": plan.dict()})
            if plan.intent == "help" or plan.intent == "chitchat":
                await emit({"type": "message", "text": HELP})
                return
            async with _sem:
                res = await executor.run_tool("sitecrawler", plan, emit)
            await emit({"type": "result", **(await responder.build(plan, res))})
        except ValidationError as e:
            await emit({"type": "error", "stage": "validation", "error": str(e)})
        except Exception as e:  # honest failure, with secrets redacted
            await emit({"type": "error", "stage": "internal", "error": redact(f"{type(e).__name__}: {e}")})
        finally:
            await q.put(None)

    task = asyncio.create_task(work())

    async def stream():
        try:
            while (ev := await q.get()) is not None:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                task.cancel()  # client disconnected, so kill the run

    return StreamingResponse(stream(), media_type="application/x-ndjson")
