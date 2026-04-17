"""cli.py — ai-code-reviewer command-line interface.

Usage:
  cr review local [--base main] [--head HEAD] [--path .] [--stream] [--save]
  cr review github --repo owner/repo --pr 42 [--post-comment] [--stream] [--save]
  cr review gitlab --project namespace/repo --mr 42 [--stream] [--save]
  cr review diff path/to/changes.patch [--stream] [--save]
"""

import asyncio
import sys

import anyio
import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

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

console = Console()
err_console = Console(stderr=True)


# ── Main group ───────────────────────────────────────────────────────────────

@click.group()
def main():
    """ai-code-reviewer — LLM-powered code review from the terminal."""


@main.group()
def review():
    """Review a diff from various sources."""


# ── Common options ────────────────────────────────────────────────────────────

_COMMON_OPTIONS = [
    click.option("--stream/--no-stream", default=True, show_default=True,
                 help="Stream the review output as it is generated."),
    click.option("--save/--no-save", default=False, show_default=True,
                 help="Save the report to the output directory."),
    click.option("--output-dir", default=None,
                 help="Override the output directory (default from config)."),
]


def _add_common_options(func):
    for option in reversed(_COMMON_OPTIONS):
        func = option(func)
    return func


# ── review local ──────────────────────────────────────────────────────────────

@review.command("local")
@click.option("--base", default="main", show_default=True, help="Base branch or commit.")
@click.option("--head", default="HEAD", show_default=True, help="Head branch or commit.")
@click.option("--path", "repo_path", default=".", show_default=True, help="Path to git repo.")
@click.option("--staged", is_flag=True, help="Review staged (cached) changes only.")
@_add_common_options
def review_local(base, head, repo_path, staged, stream, save, output_dir):
    """Review local git changes (branch diff or staged changes)."""
    try:
        diff = fetch_local(base=base, head=head, repo_path=repo_path, staged=staged)
    except (ValueError, RuntimeError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    _run_review(diff, stream=stream, save=save, output_dir=output_dir)


# ── review github ─────────────────────────────────────────────────────────────

@review.command("github")
@click.option("--repo", required=True, metavar="OWNER/REPO",
              help="GitHub repository (e.g. octocat/hello-world).")
@click.option("--pr", "pr_number", required=True, type=int, help="Pull request number.")
@click.option("--post-comment", is_flag=True,
              help="Post the review back as a PR comment (requires write:discussion scope).")
@_add_common_options
def review_github(repo, pr_number, post_comment, stream, save, output_dir):
    """Review a GitHub Pull Request."""
    async def _fetch():
        return await fetch_github_pr(
            repo=repo,
            pr_number=pr_number,
            token=settings.github.token,
            base_url=settings.github.base_url,
        )

    try:
        diff = anyio.from_thread.run_sync(lambda: asyncio.get_event_loop().run_until_complete(_fetch()))
    except Exception:
        diff = anyio.run(_fetch)

    _run_review(diff, stream=stream, save=save, output_dir=output_dir,
                post_comment_fn=_github_post_comment(repo, pr_number) if post_comment else None)


# ── review gitlab ─────────────────────────────────────────────────────────────

@review.command("gitlab")
@click.option("--project", required=True, metavar="NAMESPACE/REPO",
              help="GitLab project path (e.g. mygroup/myrepo) or numeric project ID.")
@click.option("--mr", "mr_iid", required=True, type=int, help="Merge request IID.")
@_add_common_options
def review_gitlab(project, mr_iid, stream, save, output_dir):
    """Review a GitLab Merge Request."""
    async def _fetch():
        return await fetch_gitlab_mr(
            project=project,
            mr_iid=mr_iid,
            token=settings.gitlab.token,
            base_url=settings.gitlab.base_url,
        )

    try:
        diff = asyncio.get_event_loop().run_until_complete(_fetch())
    except RuntimeError:
        diff = anyio.run(_fetch)

    _run_review(diff, stream=stream, save=save, output_dir=output_dir)


# ── review diff ───────────────────────────────────────────────────────────────

@review.command("diff")
@click.argument("diff_file", metavar="FILE")
@_add_common_options
def review_diff(diff_file, stream, save, output_dir):
    """Review a .diff or .patch file from disk."""
    try:
        diff = fetch_from_file(diff_file)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    _run_review(diff, stream=stream, save=save, output_dir=output_dir)


# ── Shared review runner ──────────────────────────────────────────────────────

def _run_review(
    diff: DiffResult,
    *,
    stream: bool,
    save: bool,
    output_dir: str | None,
    post_comment_fn=None,
) -> None:
    """Run the review synchronously (wraps async engine)."""
    _print_diff_header(diff)
    engine = ReviewEngine(settings)
    out_dir = output_dir or settings.output_dir

    if stream:
        result = asyncio.run(_stream_review(engine, diff))
    else:
        with console.status("[bold cyan]Reviewing…[/bold cyan]"):
            result = asyncio.run(engine.review(diff))
        _print_report(result)

    _print_summary(result)

    if save:
        path = result.save(out_dir)
        console.print(f"\n[dim]Report saved to:[/dim] {path}")

    if post_comment_fn:
        asyncio.run(post_comment_fn(result.raw_markdown))

    # Exit 1 if the review requests changes
    if result.verdict.upper() == "REQUEST CHANGES":
        sys.exit(1)


async def _stream_review(engine: ReviewEngine, diff: DiffResult) -> ReviewResult:
    """Stream and print the review, then return a ReviewResult."""
    collected = []
    async for chunk in engine.review_stream(diff):
        console.print(chunk, end="")
        collected.append(chunk)
    console.print()
    raw = "".join(collected)
    return ReviewResult.from_llm_output(
        raw=raw,
        source=diff.source,
        title=diff.title,
        url=diff.url,
        author=diff.author,
        base_ref=diff.base_ref,
        head_ref=diff.head_ref,
        changed_files=diff.changed_files,
        model=settings.llm.model,
    )


# ── Printing helpers ──────────────────────────────────────────────────────────

def _print_diff_header(diff: DiffResult) -> None:
    title = diff.title or diff.source
    lines = [f"[bold]{title}[/bold]"]
    if diff.url:
        lines.append(f"[link={diff.url}]{diff.url}[/link]")
    if diff.author:
        lines.append(f"Author: {diff.author}")
    lines.append(f"Stats: {diff.stats}")
    console.print(Panel("\n".join(lines), title="[cyan]Code Review[/cyan]", expand=False))


def _print_report(result: ReviewResult) -> None:
    """Print the full markdown report with Rich rendering."""
    console.print(Markdown(result.raw_markdown))


def _print_summary(result: ReviewResult) -> None:
    """Print the one-line verdict banner."""
    verdict = result.verdict.upper()
    style = {
        "APPROVE": "bold green",
        "REQUEST CHANGES": "bold red",
        "DISCUSS": "bold yellow",
    }.get(verdict, "bold white")

    text = Text()
    text.append(result.summary_line(), style=style)
    console.print(Panel(text, title="[bold]Verdict[/bold]", expand=False))


def _github_post_comment(repo: str, pr_number: int):
    """Return an async callable that posts a review comment to GitHub."""
    async def _post(markdown: str) -> None:
        import httpx
        token = settings.github.token
        if not token:
            err_console.print("[yellow]Warning: REVIEWER_GITHUB__TOKEN not set — skipping comment post[/yellow]")
            return
        url = f"{settings.github.base_url}/repos/{repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"body": markdown}, headers=headers, timeout=15)
            if resp.status_code == 201:
                data = resp.json()
                console.print(f"[green]Comment posted:[/green] {data.get('html_url', '')}")
            else:
                err_console.print(f"[red]Failed to post comment:[/red] HTTP {resp.status_code} — {resp.text[:200]}")

    return _post


if __name__ == "__main__":
    main()
