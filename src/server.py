"""server.py — FastAPI HTTP server for ai-code-reviewer.

Endpoints:
  GET  /health                   — liveness check
  POST /review/local             — review local git diff
  POST /review/github            — review a GitHub PR
  POST /review/gitlab            — review a GitLab MR
  POST /review/diff              — review a raw diff string
  GET  /review/{review_id}       — retrieve a saved review by ID (from output_dir)
  POST /review/github/stream     — SSE stream of a GitHub PR review
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.config.settings import settings
from src.reviewer.diff import (
    DiffResult,
    fetch_from_file,
    fetch_github_pr,
    fetch_gitlab_mr,
    fetch_local,
)
from src.reviewer.engine import ReviewEngine
from src.reviewer.report import ReviewResult

logger = logging.getLogger(__name__)


# ── Request / Response models ─────────────────────────────────────────────────

class LocalReviewRequest(BaseModel):
    base: str = Field(default="main", description="Base branch or commit")
    head: str = Field(default="HEAD", description="Head branch or commit")
    repo_path: str = Field(default=".", description="Path to git repo on this server")
    staged: bool = False


class GitHubReviewRequest(BaseModel):
    repo: str = Field(..., description="owner/repo")
    pr_number: int = Field(..., description="Pull request number")


class GitLabReviewRequest(BaseModel):
    project: str = Field(..., description="namespace/repo or numeric project ID")
    mr_iid: int = Field(..., description="Merge request IID")


class RawDiffReviewRequest(BaseModel):
    diff: str = Field(..., description="Unified diff text to review")
    title: str = Field(default="", description="Optional title for the review")


class ReviewResponse(BaseModel):
    review_id: str
    verdict: str
    risk: str
    findings_summary: str
    model: str
    input_tokens: int
    output_tokens: int
    generated_at: str
    report_markdown: str
    source: str
    title: str
    url: str
    changed_files: list[str]


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="ai-code-reviewer",
        description="LLM-powered code review API",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    engine = ReviewEngine(settings)

    # ── Health ────────────────────────────────────────────────────────────────

    @app.get("/health", tags=["ops"])
    async def health():
        return {
            "status": "ok",
            "model": settings.llm.model,
            "output_dir": settings.output_dir,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Review: local git ─────────────────────────────────────────────────────

    @app.post("/review/local", response_model=ReviewResponse, tags=["review"])
    async def review_local(req: LocalReviewRequest):
        try:
            diff = fetch_local(
                base=req.base,
                head=req.head,
                repo_path=req.repo_path,
                staged=req.staged,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return await _do_review(engine, diff)

    # ── Review: GitHub PR ──────────────────────────────────────────────────────

    @app.post("/review/github", response_model=ReviewResponse, tags=["review"])
    async def review_github(req: GitHubReviewRequest):
        try:
            diff = await fetch_github_pr(
                repo=req.repo,
                pr_number=req.pr_number,
                token=settings.github.token,
                base_url=settings.github.base_url,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return await _do_review(engine, diff)

    # ── Review: GitLab MR ──────────────────────────────────────────────────────

    @app.post("/review/gitlab", response_model=ReviewResponse, tags=["review"])
    async def review_gitlab(req: GitLabReviewRequest):
        try:
            diff = await fetch_gitlab_mr(
                project=req.project,
                mr_iid=req.mr_iid,
                token=settings.gitlab.token,
                base_url=settings.gitlab.base_url,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return await _do_review(engine, diff)

    # ── Review: raw diff ───────────────────────────────────────────────────────

    @app.post("/review/diff", response_model=ReviewResponse, tags=["review"])
    async def review_raw(req: RawDiffReviewRequest):
        if not req.diff.strip():
            raise HTTPException(status_code=400, detail="diff must not be empty")
        diff = DiffResult(source="raw", raw_diff=req.diff, title=req.title or "Raw diff")
        return await _do_review(engine, diff)

    # ── Streaming: GitHub PR ───────────────────────────────────────────────────

    @app.post("/review/github/stream", tags=["review"])
    async def review_github_stream(req: GitHubReviewRequest):
        """SSE stream — response body is the review markdown streamed in real time."""
        try:
            diff = await fetch_github_pr(
                repo=req.repo,
                pr_number=req.pr_number,
                token=settings.github.token,
                base_url=settings.github.base_url,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        async def _event_gen() -> AsyncGenerator[str, None]:
            async for chunk in engine.review_stream(diff):
                yield chunk

        return StreamingResponse(_event_gen(), media_type="text/plain")

    return app


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _do_review(engine: ReviewEngine, diff: DiffResult) -> dict[str, Any]:
    """Run the review and return the response dict."""
    try:
        result: ReviewResult = await engine.review(diff)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    review_id = str(uuid.uuid4())
    return ReviewResponse(
        review_id=review_id,
        verdict=result.verdict,
        risk=result.risk,
        findings_summary=result.findings_summary,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        generated_at=result.generated_at.isoformat(),
        report_markdown=result.raw_markdown,
        source=result.source,
        title=result.title,
        url=result.url,
        changed_files=result.changed_files,
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.server:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=True,
    )
