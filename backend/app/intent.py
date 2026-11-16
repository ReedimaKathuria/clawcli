from __future__ import annotations
"""
Intent layer: turns a natural-language message into a validated, structured TaskPlan.

The LLM never produces a shell command. It can only fill a fixed schema (intent and parameters),
which is then clamped and validated in code before anything executes.
"""
import re
from dataclasses import asdict, dataclass, field

from . import llm
from .security import ValidationError, extract_url, validate_url

INTENTS = ("crawl_site", "help", "chitchat")

SYSTEM = """You are the intent parser for a CLI orchestration system.
Return ONLY a JSON object with this schema, with no prose:
{"intent": "crawl_site"|"help"|"chitchat",
 "url": string|null,
 "max_pages": int (1-25, default 8),
 "max_depth": int (0-3, default 1),
 "want_json": bool,
 "want_summary": bool,
 "focus": string  // what the user cares about, e.g. "contact details", "pricing", "" if general
}
Never output shell commands. If no URL is given for a crawl, set url to null."""


@dataclass
class TaskPlan:
    intent: str
    url: str | None = None
    max_pages: int = 8
    max_depth: int = 1
    want_json: bool = True
    want_summary: bool = True
    focus: str = ""
    parser: str = "rules"
    steps: list = field(default_factory=list)

    def dict(self):
        return asdict(self)


def _rules(msg: str) -> dict:
    low = msg.lower()
    url = extract_url(msg)
    n = re.search(r"(\d{1,2})\s*pages?", low)
    d = re.search(r"depth\s*(?:of\s*)?(\d)", low)
    if url or any(w in low for w in ("crawl", "scrape", "summari", "extract", "website", "site")):
        intent = "crawl_site"
    elif any(w in low for w in ("help", "what can you", "how do")):
        intent = "help"
    else:
        intent = "chitchat"
    focus = ""
    for kw in ("contact", "pricing", "price", "product", "team", "about", "blog", "service"):
        if kw in low:
            focus = kw
            break
    return {"intent": intent, "url": url, "max_pages": int(n.group(1)) if n else 8,
            "max_depth": int(d.group(1)) if d else 1, "want_json": True,
            "want_summary": True, "focus": focus}


async def interpret(message: str) -> TaskPlan:
    data, parser = None, "rules"
    if llm.enabled():
        data = llm.parse_json(await llm.complete(SYSTEM, message[:2000], 300))
        parser = "llm" if data else "rules"
    if not data:
        data = _rules(message)
    # Fall back to deterministic URL extraction if the model missed it
    if data.get("intent") == "crawl_site" and not data.get("url"):
        data["url"] = extract_url(message)

    plan = TaskPlan(
        intent=data.get("intent") if data.get("intent") in INTENTS else "chitchat",
        url=data.get("url"),
        max_pages=max(1, min(int(data.get("max_pages") or 8), 25)),
        max_depth=max(0, min(int(data.get("max_depth") or 1), 3)),
        want_json=bool(data.get("want_json", True)),
        want_summary=bool(data.get("want_summary", True)),
        focus=str(data.get("focus") or "")[:80],
        parser=parser,
    )
    if plan.intent == "crawl_site":
        if not plan.url:
            raise ValidationError("I need a website URL to crawl, e.g. https://example.com")
        plan.url = validate_url(plan.url)
        plan.steps = [
            f"Validate URL and resolve host ({plan.url})",
            "Check robots.txt permissions",
            f"Run sitecrawler CLI (≤{plan.max_pages} pages, depth ≤{plan.max_depth})",
            "Capture stdout / stderr / exit code",
            "Validate JSON output against schema",
            "Generate summary" + (f" focused on '{plan.focus}'" if plan.focus else ""),
        ]
    return plan
