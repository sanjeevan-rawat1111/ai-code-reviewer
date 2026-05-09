# ai-code-reviewer — Interview Guide

> **How to explain every point in the project description to an interviewer**

---

## Project Description (for reference)

> *"Built ai-code-reviewer (Python/FastAPI/GenAI) — autonomous AI code review agent that fetches
> GitLab MR diffs, cross-references the full codebase via dynamic MCP tool discovery, and produces
> structured review reports with dedicated security and code-quality sections, per-finding risk
> breakdowns (likelihood, impact, blast radius), and an APPROVE / REQUEST CHANGES verdict —
> eliminating manual review overhead."*

---

## Point 1 — "Fetches GitLab MR diffs"

### What happened under the hood

I built a `DiffFetcher` abstraction in `diff.py` that supports **4 input sources** through a single
normalised `DiffResult` dataclass:

| Source | Command | How it works |
|--------|---------|--------------|
| GitLab MR | `cr review gitlab --project org/repo --mr 42` | GitLab REST API |
| GitHub PR | `cr review github --repo owner/repo --pr 42` | GitHub REST API |
| Local git | `cr review local --base main` | `git diff` subprocess |
| Patch file | `cr review diff changes.patch` | reads `.diff`/`.patch` from disk |

### GitLab specifically

For GitLab, I make two API calls using `httpx`:

1. `GET /api/v4/projects/{project}/merge_requests/{mr_iid}` — fetches MR metadata: title, author, source/target branch, description, web URL
2. `GET /api/v4/projects/{project}/merge_requests/{mr_iid}/diffs` — fetches per-file diff objects

GitLab returns diffs as JSON objects per file (not unified diff format), so I wrote a
`_gitlab_diffs_to_unified()` helper that reconstructs the standard `--- a/file` / `+++ b/file`
unified diff — so the rest of the pipeline doesn't need to know which source the diff came from.
Everything downstream just sees a `DiffResult`.

### Key files
- `src/reviewer/diff.py` — `fetch_gitlab_mr()` lines 209–264, `_gitlab_diffs_to_unified()` line 312

### Interview follow-up answers
- **"How do you handle auth?"** — GitLab token passed via `REVIEWER_GITLAB__TOKEN` env var, sent
  as `PRIVATE-TOKEN` header. Project path is URL-encoded via `urllib.parse.quote` to handle
  namespaces with slashes.
- **"What if the MR has 500 files?"** — `DiffResult.truncate(max_diff_lines)` is called at the
  start of `engine.py` — it clips the diff to `max_diff_lines` (default 2000) and appends a
  `[truncated]` note so the LLM knows it's seeing a subset.

---

## Point 2 — "Cross-references the full codebase via dynamic MCP tool discovery"

### What is MCP?

**MCP (Model Context Protocol)** is an open standard for connecting LLMs to external tools and
data sources via a JSON-RPC protocol over SSE (Server-Sent Events) transport. Think of it as a
plugin system for LLMs — the LLM client discovers what tools exist at runtime and calls them by
name.

### The two-phase flow

The word **"dynamic"** is critical here — I don't hardcode which tools to call. Here is the exact
flow in `engine.py`:

```
Phase 1 — Symbol Extraction
  diff → analyze.xml system prompt → LLM
  LLM returns: { "symbols": [ { "name": "DataService", "kind": "class", "reason": "..." } ] }

Phase 2 — Context Assembly + Grounded Review
  For each symbol → MCPClient.call_tool("get_definition", {"symbol": name})
  For methods/fields → MCPClient.call_tool("get_references", {"symbol": name})
  Assembled context → injected into review.xml prompt → LLM → final report
```

### How MCPClient discovers tools dynamically

`MCPClient` in `mcp/client.py` opens an SSE connection to the MCP server and calls
`session.list_tools()`. This returns whatever tools the server advertises at that moment — the
client never hardcodes tool names. The tool list is cached per session. This means if the MCP
server adds a new tool (e.g. `get_test_coverage`), the reviewer picks it up automatically without
any code change.

### The MCP server (codebase-mcp-server)

A companion service that:
1. Clones the target Java repo at startup (or mounts a local path via `REPO_PATH`)
2. Walks all `.java` files and builds an in-memory symbol index using regex + brace counting
3. Exposes 6 tools over SSE: `search_definitions`, `get_definition`, `get_references`,
   `read_file`, `search_code`, `repo_map`

### Key files
- `src/reviewer/engine.py` — `_fetch_codebase_context()` lines 163–221, `_identify_symbols()` lines 223–247
- `src/mcp/client.py` — `_fetch_tools()` lines 69–88, `_call()` lines 90–109
- `src/reviewer/prompts/analyze.xml` — Phase 1 symbol extraction prompt

### Interview follow-up answers
- **"Why two phases instead of sending the full codebase in one prompt?"** — Two reasons: (1)
  context window limits — a full Java repo is hundreds of thousands of tokens; (2) noise —
  sending everything makes the LLM lose focus. Phase 1 uses a cheap, fast LLM call to identify
  only the 3–5 symbols that actually matter for *this specific diff*, then Phase 2 fetches just
  those definitions.
- **"What if the MCP server is down?"** — `is_available()` does a GET `/healthz` health check
  before any tool calls. If it fails, the `try/except` in `engine.py` catches it, logs a warning,
  and falls back to a single-phase review without codebase context. The review still completes.
- **"What transport does MCP use?"** — SSE (Server-Sent Events) for the server→client stream,
  and HTTP POST for client→server messages. The MCP SDK handles all the protocol framing.

---

## Point 3 — "Structured review reports with dedicated security and code-quality sections"

### How the structure is enforced

The structure is defined in `review.xml` as a **system prompt with an explicit `<output_format>`
block** that the LLM must follow exactly. Every report has these mandatory sections:

1. **What Changed** — 2–3 sentences on *why* the change was made
2. **Risk Assessment** — table with Overall Risk, Security, Complexity, Blast Radius
3. **Security Analysis** — always present, even if "No security concerns identified"
4. **Code Quality Analysis** — always present, even if empty
5. **Already Handled** — things considered and dismissed (prevents false positives)
6. **Open Questions** — concerns that can't be confirmed from the diff alone
7. **Verdict** — APPROVE / REQUEST CHANGES / DISCUSS

### The two checklists baked into the prompt

**`<security_checklist>`** — for every changed file, check:
- SQL / command / XSS injection via string interpolation
- Auth bypass, weakened permissions, missing auth on new endpoints
- Hardcoded secrets, tokens, passwords
- Missing input validation, unchecked casts
- Weak crypto (MD5, SHA1, ECB mode)
- PII in logs, sensitive data in API responses
- Unsafe deserialization, SSRF, path traversal

**`<code_quality_checklist>`** — for every changed file, check:
- Dead code (unused imports, commented-out code)
- Duplication / copy-paste logic
- Deep nesting (>3 levels), methods over 50 lines
- Swallowed exceptions, catch-all handlers
- New logic without test coverage
- Magic values (hardcoded numbers/strings)

### Key design decision

Rule 9: *"False positives are worse than missed findings. If unsure, downgrade to Open Questions."*
This is intentional — a reviewer that cries wolf on every diff loses trust quickly. The agent
only reports confirmed findings with evidence from the diff.

### Key files
- `src/reviewer/prompts/review.xml` — checklists lines 37–59, output format lines 88–193

---

## Point 4 — "Per-finding risk breakdowns (likelihood, impact, blast radius)"

### How it works

Rule 5 in `review.xml` mandates this for every **CRITICAL or HIGH** finding:

```markdown
> **Risk:** Likelihood: HIGH | Impact: HIGH | Blast radius: SERVICE | Merge risk: HIGH
```

And the overall risk table at the top of every report:

| Dimension     | Rating                          | Rationale |
|---------------|---------------------------------|-----------|
| Overall Risk  | LOW / MEDIUM / HIGH / CRITICAL  | ...       |
| Security      | CLEAR / LOW / MEDIUM / HIGH     | ...       |
| Complexity    | LOW / MEDIUM / HIGH             | ...       |
| Blast Radius  | LOCAL / SERVICE / CROSS-SERVICE | ...       |

### What each dimension means

| Dimension | What it answers |
|-----------|-----------------|
| **Likelihood** | How probable is it that this bug gets triggered in production? |
| **Impact** | What's the worst-case consequence if it does? (data loss, outage, breach) |
| **Blast Radius** | LOCAL = one class; SERVICE = whole service; CROSS-SERVICE = multiple services |
| **Merge Risk** | How risky is merging this right now — timing, load, dependencies |

This gives reviewers an instant triage signal: a MEDIUM severity finding with HIGH blast radius
is more urgent than a HIGH severity finding with LOCAL blast radius.

### Key files
- `src/reviewer/prompts/review.xml` — Rule 5 line 79, output format lines 107–131

---

## Point 5 — "APPROVE / REQUEST CHANGES verdict"

### The three verdicts

| Verdict | When | Meaning |
|---------|------|---------|
| **APPROVE** | No unresolved CRITICAL or HIGH findings | Safe to merge |
| **REQUEST CHANGES** | One or more CRITICAL or HIGH findings | Lists each blocking finding by title and severity |
| **DISCUSS** | Architectural or design questions | Needs team input before merging |

### How it's surfaced in the CLI

After the LLM outputs the report, `report.py`'s `from_llm_output()` parses the markdown to extract
the verdict, risk level, and finding counts. The CLI then renders a coloured summary panel at the
bottom of the terminal:

```
╭─────────────────────── Verdict ────────────────────────╮
│ ✔  APPROVE  [LOW]  0 critical, 0 high, 2 medium, 1 low │
╰────────────────────────────────────────────────────────╯
```
or:
```
╭──────────────────────────────────────────────────────────────╮
│ ✖  REQUEST CHANGES  [HIGH]  1 critical, 2 high, 0 medium     │
╰──────────────────────────────────────────────────────────────╯
```

### Key files
- `src/reviewer/prompts/review.xml` — Verdict section lines 182–193
- `src/reviewer/report.py` — `from_llm_output()` parsing
- `cli.py` — terminal panel rendering

---

## Point 6 — "Eliminating manual review overhead"

### One command, full review

```bash
cr review gitlab --project org/java-data-api --mr 42
```

That single command:
1. Fetches MR diff from GitLab API
2. Runs Phase 1 — LLM identifies key symbols
3. Calls MCP server to fetch definitions + references
4. Runs Phase 2 — grounded review with codebase context
5. Outputs structured markdown report with verdict
6. Optionally saves to `reports/` with `--save`

**Under 10 seconds for a typical MR.**

### CI/CD integration via FastAPI server

`server.py` exposes a FastAPI HTTP server. In GitLab CI you can configure a webhook — when a new
MR is opened, GitLab POSTs to `/review`, the server runs the pipeline, and can post the findings
back as a GitLab comment via the API. Engineers get automated feedback before a human reviewer
ever opens the diff.

### Streaming output

`--stream` (default) uses `review_stream()` in `engine.py` which calls `LLMClient.stream()` —
the review appears in the terminal as it's generated, just like ChatGPT typing. This makes it feel
responsive even for large diffs.

### Key files
- `cli.py` — CLI commands for all 4 sources
- `src/server.py` — FastAPI `POST /review`, `GET /health`
- `src/reviewer/engine.py` — `review()` and `review_stream()`

---

## Quick-fire Interview Q&A

| Question | Answer |
|----------|--------|
| **"What tech stack?"** | Python 3.9+, FastAPI, httpx (async HTTP), Pydantic Settings (config), Rich (terminal UI), MCP SDK (SSE client) |
| **"Which LLMs are supported?"** | Any OpenAI-compatible endpoint — GPT-4o, Gemini Flash, Groq (Llama), Ollama locally. Configured via `REVIEWER_LLM__MODEL` + `REVIEWER_LLM__BASE_URL` env vars. |
| **"How do you handle LLM failures?"** | `LLMClient` has primary + fallback model. On 429 or 5xx it retries with exponential backoff, then switches to `fallback_model` if configured. |
| **"Why XML for prompts?"** | XML tags (`<workflow>`, `<rules>`, `<output_format>`) give clear structure that the LLM can parse internally. It's easier to edit sections independently than a plain string. |
| **"How is config managed?"** | Pydantic `BaseSettings` with env prefix `REVIEWER_`. Supports `.env` file or env vars. Nested with `__` delimiter, e.g. `REVIEWER_LLM__API_KEY`. |
| **"Is it production-ready?"** | Core pipeline is solid. Known gaps: CLI async pattern for GitHub/GitLab fetch could use `asyncio.run()` consistently; MCP `search_code` has no regex complexity bound (ReDoS risk for untrusted input). |
| **"How would you scale it?"** | The FastAPI server is stateless — deploy multiple instances behind a load balancer. The MCP server is read-only after startup — can run as a sidecar or shared service. |
