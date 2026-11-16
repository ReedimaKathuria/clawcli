# Form answers (copy and paste)

**What problem does your solution solve?**
Most AI chat apps only generate text. They can't prove that they did anything. ClawCLI closes that gap. It turns a plain-English request into a controlled, real CLI execution on the backend (a website crawl), captures what actually happened (stdout, stderr, exit code, logs), validates it, and returns a summary and structured JSON. Every answer is backed by evidence from a live run, and failures are reported honestly instead of being made up.

**Which programming languages and technologies did you use?**
Python 3.12, FastAPI, Uvicorn, Pydantic, httpx, asyncio subprocesses, and a custom standard-library CLI crawler (urllib, html.parser, robotparser). The frontend is HTML, CSS and JavaScript with streaming NDJSON. It is packaged with Docker.

**Which AI models, APIs, or AI development tools did you use?**
The Anthropic Claude API (Claude Sonnet) parses intent into a fixed JSON schema and summarises the crawl data. Claude was also used as an AI coding assistant during development. The app also runs without an LLM, using a rule-based parser and extractive summaries.

**How does your application understand natural language user requests?**
The message goes to an intent layer. An LLM is prompted to return ONLY a strict JSON schema (intent, url, max_pages, max_depth, want_json, focus), never commands. If the LLM is unavailable or returns invalid output, a deterministic rule-based parser takes over (regexes for URLs, page counts, depth and focus keywords). The result is clamped and validated in code into a TaskPlan with explicit execution steps, and the plan is shown to the user before execution.

**How does your backend trigger and manage CLI execution?**
The executor looks up the tool in a hard-coded allowlist and builds an argv list from validated fields only. It starts the tool with asyncio.create_subprocess_exec (no shell), in a new process session, with a scrubbed environment. stderr is streamed line by line to the chat in real time, and stdout is captured as a single JSON document. A global timeout kills the process, users can cancel a run, and closing the browser cancels it too. Exit codes map to statuses (0 success, 10 partial, 3 unreachable, 4 robots-blocked, 2 invalid). The output is schema-validated before it is summarised and returned.

**What security measures have you implemented?**
There is no shell execution and only allowlisted tools run. LLM output is constrained to a schema and clamped. URLs are strictly validated: http(s) only, no credentials, standard ports, and shell metacharacters are rejected. Hostnames are DNS-resolved to block private, loopback, link-local and metadata IPs (SSRF). robots.txt is respected. Crawl page, depth and byte limits apply, along with per-request and global timeouts, a concurrency cap and per-IP rate limiting. The child process gets an environment with no secrets and runs as non-root in Docker. Web content is fenced as untrusted data to resist prompt injection. Secrets are redacted from logs and output. Status is reported honestly, and nothing is marked successful without evidence.
