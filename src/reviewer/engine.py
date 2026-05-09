"""engine.py — ReviewEngine: orchestrates diff → LLM → ReviewResult.

Single-phase flow (no MCP configured):
  diff → review.xml prompt → LLM → ReviewResult

Two-phase flow (REVIEWER_MCP_URL is set):
  diff → analyze.xml prompt → LLM → symbol list
       → MCP tool calls (get_definition + get_references for each symbol)
       → review.xml prompt + diff + codebase context → LLM → ReviewResult
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import AsyncGenerator

from src.config.settings import Settings
from src.reviewer.diff import DiffResult
from src.reviewer.llm import LLMClient
from src.reviewer.report import ReviewResult

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load an XML prompt file and strip the outer <system_prompt> tags."""
    path = _PROMPTS_DIR / f"{name}.xml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("<system_prompt>"):
        text = text[len("<system_prompt>"):].strip()
    if text.endswith("</system_prompt>"):
        text = text[: -len("</system_prompt>")].strip()
    return text


def _build_user_message(diff: DiffResult, codebase_context: str = "") -> str:
    """Build the user message that describes the diff (and optional MCP context) to the LLM."""
    lines = []

    if diff.title:
        lines.append(f"**Title:** {diff.title}")
    if diff.author:
        lines.append(f"**Author:** {diff.author}")
    if diff.base_ref or diff.head_ref:
        lines.append(f"**Diff:** `{diff.base_ref}...{diff.head_ref}`")
    if diff.url:
        lines.append(f"**URL:** {diff.url}")
    if diff.description:
        lines.append(f"\n**Description:**\n{diff.description.strip()}")

    lines.append(f"\n**Stats:** {diff.stats}")
    if diff.changed_files:
        files_list = "\n".join(f"  - {f}" for f in diff.changed_files[:30])
        if len(diff.changed_files) > 30:
            files_list += f"\n  ... and {len(diff.changed_files) - 30} more"
        lines.append(f"\n**Changed files:**\n{files_list}")

    lines.append(f"\n---\n\n```diff\n{diff.raw_diff}\n```")

    if codebase_context:
        lines.append(
            f"\n---\n\n## Codebase Context (from full repository)\n\n"
            f"The following definitions and references were looked up from the full codebase "
            f"to help ground the review:\n\n{codebase_context}"
        )

    return "\n".join(lines)


class ReviewEngine:
    """Orchestrates the full code review pipeline."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm = LLMClient.from_settings(settings.llm)
        self._review_prompt = _load_prompt("review")
        self._analyze_prompt = _load_prompt("analyze")

    # ── Public: full review ───────────────────────────────────────────────────

    async def review(self, diff: DiffResult) -> ReviewResult:
        """Run a full review. Uses 2-phase MCP flow when mcp_url is configured."""
        diff = diff.truncate(self._settings.max_diff_lines)

        codebase_context = ""
        if self._settings.mcp_url:
            try:
                codebase_context = await self._fetch_codebase_context(diff)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mcp.context_fetch_failed: %s — continuing without context", exc)

        user_message = _build_user_message(diff, codebase_context)
        messages = [
            {"role": "system", "content": self._review_prompt},
            {"role": "user", "content": user_message},
        ]

        logger.info(
            "review.started source=%s title=%s files=%d mcp=%s",
            diff.source, diff.title[:60], len(diff.changed_files),
            "yes" if codebase_context else "no",
        )

        response = await self._llm.chat(messages)
        if response.text.startswith("Error:"):
            raise RuntimeError(f"LLM call failed: {response.text}")

        logger.info(
            "review.completed tokens=%d/%d",
            response.input_tokens, response.output_tokens,
        )

        return ReviewResult.from_llm_output(
            raw=response.text,
            source=diff.source,
            title=diff.title,
            url=diff.url,
            author=diff.author,
            base_ref=diff.base_ref,
            head_ref=diff.head_ref,
            changed_files=diff.changed_files,
            model=response.model or self._settings.llm.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    # ── Public: streaming review ──────────────────────────────────────────────

    async def review_stream(self, diff: DiffResult) -> AsyncGenerator[str, None]:
        """Stream the review as text chunks. MCP context is fetched before streaming starts."""
        diff = diff.truncate(self._settings.max_diff_lines)

        codebase_context = ""
        if self._settings.mcp_url:
            try:
                codebase_context = await self._fetch_codebase_context(diff)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mcp.context_fetch_failed: %s — continuing without context", exc)

        user_message = _build_user_message(diff, codebase_context)
        messages = [
            {"role": "system", "content": self._review_prompt},
            {"role": "user", "content": user_message},
        ]

        logger.info(
            "review.stream.started source=%s title=%s mcp=%s",
            diff.source, diff.title[:60],
            "yes" if codebase_context else "no",
        )

        async for chunk in self._llm.stream(messages):
            yield chunk

    # ── Phase 1: symbol extraction ────────────────────────────────────────────

    async def _fetch_codebase_context(self, diff: DiffResult) -> str:
        """
        Phase 1: ask the LLM which symbols to look up.
        Phase 2: query MCP for those symbols and return the assembled context string.
        """
        from src.mcp.client import MCPClient

        mcp = MCPClient(self._settings.mcp_url)

        if not await mcp.is_available():
            logger.warning("MCP server not reachable at %s — skipping context lookup", self._settings.mcp_url)
            return ""

        symbols = await self._identify_symbols(diff)
        if not symbols:
            logger.info("phase1.no_symbols_identified")
            return ""

        logger.info("phase1.symbols_identified count=%d symbols=%s", len(symbols), [s["name"] for s in symbols])

        context_parts: list[str] = []
        for sym in symbols:
            if not isinstance(sym, dict):
                logger.warning("phase1.symbol_skipped: unexpected type %s", type(sym))
                continue
            name = sym.get("name", "")
            kind = sym.get("kind", "")
            reason = sym.get("reason", "")
            if not name:
                continue

            logger.info("mcp.lookup symbol=%s kind=%s", name, kind)

            # Always get the definition
            definition = await mcp.call_tool("get_definition", {"symbol": name})

            # For methods and fields, also get references to understand usage patterns
            references = ""
            if kind in ("method", "field"):
                references = await mcp.call_tool("get_references", {"symbol": name})

            parts = [f"### {name}"]
            if reason:
                parts.append(f"*Why looked up: {reason}*\n")
            if definition and not definition.startswith("[MCP error"):
                parts.append(f"**Definition:**\n```java\n{definition}\n```")
            if references and not references.startswith("[MCP error") and not references.startswith("No references"):
                # Limit references output to keep context manageable
                ref_lines = references.splitlines()
                if len(ref_lines) > 20:
                    references = "\n".join(ref_lines[:20]) + f"\n  ... [{len(ref_lines) - 20} more]"
                parts.append(f"\n**References:**\n```\n{references}\n```")

            context_parts.append("\n".join(parts))

        if not context_parts:
            return ""

        return "\n\n---\n\n".join(context_parts)

    async def _identify_symbols(self, diff: DiffResult) -> list[dict]:
        """Use the LLM to extract a list of symbols worth looking up from the diff."""
        user_message = _build_user_message(diff)
        messages = [
            {"role": "system", "content": self._analyze_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            response = await self._llm.chat(messages)
            raw = response.text.strip()

            # Extract JSON object — handles preamble text and markdown fences
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if json_match:
                raw = json_match.group(0)

            data = json.loads(raw)
            symbols = data.get("symbols", [])
            if isinstance(symbols, list):
                return [s for s in symbols if isinstance(s, dict)]
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            logger.warning("phase1.parse_failed: %s", exc)

        return []
