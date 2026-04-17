"""report.py — ReviewResult dataclass and output formatting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ReviewResult:
    """Structured result of a code review."""

    # Core content — always present
    raw_markdown: str

    # Parsed metadata (extracted from the markdown if possible)
    verdict: str = ""          # APPROVE | REQUEST CHANGES | DISCUSS
    risk: str = ""             # LOW | MEDIUM | HIGH | CRITICAL
    findings_summary: str = "" # "N critical, N high, N medium, N low"

    # Source context
    source: str = ""           # local | github | gitlab | file
    title: str = ""
    url: str = ""
    author: str = ""
    base_ref: str = ""
    head_ref: str = ""
    changed_files: list[str] = field(default_factory=list)

    # Generation metadata
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ── Parsing ───────────────────────────────────────────────────────────

    @classmethod
    def from_llm_output(cls, raw: str, **kwargs) -> "ReviewResult":
        """Build a ReviewResult from raw LLM markdown output."""
        verdict, risk, findings = _parse_verdict_line(raw)
        return cls(
            raw_markdown=raw,
            verdict=verdict,
            risk=risk,
            findings_summary=findings,
            **kwargs,
        )

    # ── Output ────────────────────────────────────────────────────────────

    def save(self, output_dir: str) -> Path:
        """Write the review report to a markdown file and return the path."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        ts = self.generated_at.strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r"[^\w\-]", "_", self.title or self.source)[:40]
        filename = f"review_{safe_title}_{ts}.md"
        out_path = Path(output_dir) / filename
        out_path.write_text(self._full_report(), encoding="utf-8")
        return out_path

    def save_json(self, output_dir: str) -> Path:
        """Write a JSON summary (without the full markdown body)."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        ts = self.generated_at.strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r"[^\w\-]", "_", self.title or self.source)[:40]
        out_path = Path(output_dir) / f"review_{safe_title}_{ts}.json"
        out_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return out_path

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "risk": self.risk,
            "findings_summary": self.findings_summary,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "changed_files": self.changed_files,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "generated_at": self.generated_at.isoformat(),
        }

    def summary_line(self) -> str:
        """One-line summary for terminal output."""
        verdict_icon = {
            "APPROVE": "✔",
            "REQUEST CHANGES": "✖",
            "DISCUSS": "◆",
        }.get(self.verdict.upper(), "?")
        risk_label = f"[{self.risk}]" if self.risk else ""
        parts = [verdict_icon, self.verdict or "?", risk_label, self.findings_summary]
        return "  ".join(p for p in parts if p)

    def _full_report(self) -> str:
        """Full markdown file content including front-matter."""
        meta_lines = [
            "---",
            f"source: {self.source}",
            f"title: {self.title}",
        ]
        if self.url:
            meta_lines.append(f"url: {self.url}")
        if self.author:
            meta_lines.append(f"author: {self.author}")
        if self.base_ref or self.head_ref:
            meta_lines.append(f"diff: {self.base_ref}...{self.head_ref}")
        meta_lines += [
            f"model: {self.model}",
            f"generated_at: {self.generated_at.isoformat()}",
            "---",
            "",
        ]
        return "\n".join(meta_lines) + self.raw_markdown


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_verdict_line(markdown: str) -> tuple[str, str, str]:
    """
    Extract verdict, risk, and findings summary from the first blockquote line, e.g.:
      > **Verdict: APPROVE** | **Risk: LOW** | **Findings: 0 critical, 1 medium**
    """
    m = re.search(r"Verdict:\s*([A-Z\s]+?)\*\*", markdown, re.IGNORECASE)
    verdict = m.group(1).strip() if m else ""

    m = re.search(r"Risk:\s*([A-Z]+)\b", markdown, re.IGNORECASE)
    risk = m.group(1).strip().upper() if m else ""

    m = re.search(r"Findings:\s*([^\n*|]+)", markdown, re.IGNORECASE)
    findings = m.group(1).strip().rstrip("*") if m else ""

    return verdict, risk, findings
