#!/usr/bin/env python3
"""
sitecrawler — a small, self-contained website crawler CLI.

It needs only the standard library, so it runs in any sandbox without extra packages.
Contract with the backend:
  * stdout  -> exactly ONE JSON document (the crawl result)
  * stderr  -> human-readable progress lines, prefixed "[crawl]"
  * exit    -> 0 success | 2 bad args | 3 unreachable | 4 blocked by robots.txt | 10 partial

Usage:
  sitecrawler.py --url https://example.com --max-pages 10 --max-depth 2 --timeout 10
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

UA = "ClawCLI-Crawler/1.0 (+educational prototype; respects robots.txt)"
MAX_BYTES = 2_000_000  # never download more than ~2 MB per page
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".mp3",
            ".css", ".js", ".ico", ".webp", ".woff", ".woff2", ".xml")


def log(msg):
    print(f"[crawl] {msg}", file=sys.stderr, flush=True)


class PageParser(HTMLParser):
    """Collects the title, meta tags, headings, paragraphs, links and images from a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.headings = "", {}, []
        self.paragraphs, self.links, self.images = [], [], 0
        self._stack, self._buf, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "keywords", "og:title", "og:description", "og:site_name"):
                self.meta[key] = (a.get("content") or "").strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            self.images += 1
        if tag in ("title", "h1", "h2", "h3", "p", "li"):
            self._stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if not text:
                return
            if tag == "title":
                self.title = text
            elif tag in ("h1", "h2", "h3"):
                self.headings.append({"level": tag, "text": text[:200]})
            elif len(text) > 40:
                self.paragraphs.append(text[:600])

    def handle_data(self, data):
        if not self._skip and self._stack:
            self._buf.append(data)


def norm(url):
    url, _ = urldefrag(url)
    return url.rstrip("/") or url


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype:
            return r.status, ctype, None, r.geturl()
        raw = r.read(MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.status, ctype, raw.decode(charset, errors="replace"), r.geturl()


def main():
    p = argparse.ArgumentParser(description="Bounded same-domain website crawler")
    p.add_argument("--url", required=True)
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--delay", type=float, default=0.3, help="politeness delay between requests")
    p.add_argument("--ignore-robots", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    start = urlparse(args.url)
    if start.scheme not in ("http", "https") or not start.netloc:
        log("invalid URL")
        print(json.dumps({"status": "error", "error": "invalid_url"}))
        return 2
    args.max_pages = max(1, min(args.max_pages, 50))
    args.max_depth = max(0, min(args.max_depth, 3))

    t0 = time.time()
    root = f"{start.scheme}://{start.netloc}"
    robots = urllib.robotparser.RobotFileParser(root + "/robots.txt")
    try:
        robots.read()
        log(f"robots.txt loaded from {root}/robots.txt")
    except Exception:
        robots = None
        log("robots.txt unavailable; continuing with conservative limits")

    def allowed(u):
        return args.ignore_robots or robots is None or robots.can_fetch(UA, u)

    if not allowed(args.url):
        log("start URL disallowed by robots.txt")
        print(json.dumps({"status": "blocked", "error": "robots_disallowed", "start_url": args.url}))
        return 4

    queue, seen = deque([(norm(args.url), 0)]), {norm(args.url)}
    pages, errors, emails, phones = [], [], set(), set()
    host = start.netloc.lower()

    while queue and len(pages) < args.max_pages:
        url, depth = queue.popleft()
        log(f"fetch depth={depth} {url}")
        try:
            status, ctype, html, final = fetch(url, args.timeout)
        except urllib.error.HTTPError as e:
            errors.append({"url": url, "error": f"HTTP {e.code}"}); log(f"HTTP {e.code} {url}"); continue
        except Exception as e:  # DNS, TLS, timeout, connection refused, ...
            errors.append({"url": url, "error": type(e).__name__ + ": " + str(e)[:150]})
            log(f"error {type(e).__name__} {url}")
            continue
        if html is None:
            log(f"skip non-HTML ({ctype})"); continue

        pp = PageParser()
        try:
            pp.feed(html)
        except Exception as e:
            errors.append({"url": url, "error": f"parse: {e}"}); continue
        text = " ".join(pp.paragraphs)
        emails.update(EMAIL_RE.findall(html)[:20])
        phones.update(m.strip() for m in PHONE_RE.findall(text)[:10])

        internal, external = [], []
        for href in pp.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = norm(urljoin(final, href))
            pu = urlparse(absu)
            if pu.scheme not in ("http", "https"):
                continue
            (internal if pu.netloc.lower() == host else external).append(absu)

        pages.append({
            "url": final, "status_code": status, "depth": depth,
            "title": pp.title, "meta_description": pp.meta.get("description") or pp.meta.get("og:description", ""),
            "headings": pp.headings[:15], "key_paragraphs": pp.paragraphs[:5],
            "word_count": len(text.split()), "image_count": pp.images,
            "internal_links": len(set(internal)), "external_links": len(set(external)),
        })
        log(f"ok {status} '{pp.title[:60]}' ({len(set(internal))} internal links)")

        if depth < args.max_depth:
            for u in dict.fromkeys(internal):
                if u not in seen and not u.lower().endswith(SKIP_EXT) and allowed(u):
                    seen.add(u); queue.append((u, depth + 1))
        time.sleep(args.delay)

    home = pages[0] if pages else {}
    result = {
        "status": "success" if pages and not errors else ("partial" if pages else "failed"),
        "start_url": args.url,
        "site": {
            "domain": host,
            "title": home.get("title", ""),
            "description": home.get("meta_description", ""),
        },
        "stats": {
            "pages_crawled": len(pages), "pages_failed": len(errors),
            "urls_discovered": len(seen), "duration_sec": round(time.time() - t0, 2),
            "limits": {"max_pages": args.max_pages, "max_depth": args.max_depth},
        },
        "contacts": {"emails": sorted(emails)[:20], "phones": sorted(phones)[:10]},
        "pages": pages,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False))
    log(f"done: {len(pages)} pages, {len(errors)} errors in {result['stats']['duration_sec']}s")
    if not pages:
        return 3
    return 10 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
