"""diff.py — fetches code diffs from multiple sources.

Supported sources:
  - Local git repository  (compare branches, commits, or staged changes)
  - GitHub Pull Request   (via GitHub REST API)
  - GitLab Merge Request  (via GitLab REST API)
  - Diff file             (read a .patch / .diff file from disk)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class DiffResult:
    """Normalised diff output from any source."""

    source: str                     # "local" | "github" | "gitlab" | "file"
    raw_diff: str                   # full unified diff text
    title: str = ""                 # PR/MR title or branch description
    description: str = ""          # PR/MR body or empty for local
    author: str = ""
    base_ref: str = ""              # base branch / commit
    head_ref: str = ""              # head branch / commit
    changed_files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    url: str = ""                   # link back to the PR/MR if available

    @property
    def stats(self) -> str:
        parts = []
        if self.changed_files:
            parts.append(f"{len(self.changed_files)} file(s) changed")
        if self.additions or self.deletions:
            parts.append(f"+{self.additions} / -{self.deletions}")
        return ", ".join(parts) or "no stats"

    def truncate(self, max_lines: int) -> "DiffResult":
        """Return a copy with the diff truncated to max_lines."""
        lines = self.raw_diff.splitlines()
        if len(lines) <= max_lines:
            return self
        truncated = "\n".join(lines[:max_lines])
        truncated += f"\n\n... [truncated — showing {max_lines} of {len(lines)} lines]"
        return DiffResult(
            source=self.source,
            raw_diff=truncated,
            title=self.title,
            description=self.description,
            author=self.author,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            changed_files=self.changed_files,
            additions=self.additions,
            deletions=self.deletions,
            url=self.url,
        )


# ── Local git ────────────────────────────────────────────────────────────────

def fetch_local(
    base: str = "main",
    head: str = "HEAD",
    repo_path: str = ".",
    staged: bool = False,
) -> DiffResult:
    """
    Run git diff in the given repo and return the result.

    Args:
        base:      base branch/commit (default "main")
        head:      head branch/commit (default "HEAD")
        repo_path: path to the git repo (default CWD)
        staged:    if True, diff staged changes only (ignores base/head)
    """
    cwd = str(Path(repo_path).resolve())

    # ── Validate it's a git repo ──────────────────────────────────────────
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Not a git repository: {cwd}")

    # ── Get the diff ──────────────────────────────────────────────────────
    if staged:
        diff_cmd = ["git", "diff", "--cached"]
        title = "Staged changes"
    else:
        diff_cmd = ["git", "diff", f"{base}...{head}"]
        title = f"{base}...{head}"

    diff_out = subprocess.run(
        diff_cmd, cwd=cwd, capture_output=True, text=True,
    )
    if diff_out.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff_out.stderr.strip()}")

    raw_diff = diff_out.stdout
    if not raw_diff.strip():
        raise ValueError(f"No diff found between {base} and {head}")

    # ── Collect stats ─────────────────────────────────────────────────────
    stat_out = subprocess.run(
        ["git", "diff", "--stat", f"{base}...{head}"] if not staged
        else ["git", "diff", "--stat", "--cached"],
        cwd=cwd, capture_output=True, text=True,
    )
    changed_files = _parse_changed_files(raw_diff)
    additions, deletions = _parse_additions_deletions(stat_out.stdout)

    # ── Author / branch info ──────────────────────────────────────────────
    author_out = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=cwd, capture_output=True, text=True,
    )
    author = author_out.stdout.strip()

    return DiffResult(
        source="local",
        raw_diff=raw_diff,
        title=title,
        base_ref=base,
        head_ref=head if not staged else "staged",
        changed_files=changed_files,
        additions=additions,
        deletions=deletions,
        author=author,
    )


# ── GitHub PR ─────────────────────────────────────────────────────────────────

async def fetch_github_pr(
    repo: str,
    pr_number: int,
    token: str,
    base_url: str = "https://api.github.com",
) -> DiffResult:
    """
    Fetch a GitHub Pull Request diff via the GitHub REST API.

    Args:
        repo:      "owner/repo" string
        pr_number: pull request number
        token:     GitHub personal access token (ghp_...)
        base_url:  API base (override for GitHub Enterprise)
    """
    if not token:
        raise ValueError("REVIEWER_GITHUB__TOKEN is required for GitHub PR review")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as client:
        # ── Fetch PR metadata ──────────────────────────────────────────────
        pr_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}")
        _raise_for_status(pr_resp, f"GitHub PR {repo}#{pr_number}")
        pr = pr_resp.json()

        # ── Fetch diff ────────────────────────────────────────────────────
        diff_resp = await client.get(
            f"/repos/{repo}/pulls/{pr_number}",
            headers={**headers, "Accept": "application/vnd.github.v3.diff"},
        )
        _raise_for_status(diff_resp, f"GitHub PR diff {repo}#{pr_number}")
        raw_diff = diff_resp.text

        # ── Fetch file list ───────────────────────────────────────────────
        files_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}/files")
        _raise_for_status(files_resp, f"GitHub PR files {repo}#{pr_number}")
        files = files_resp.json()

    changed_files = [f["filename"] for f in files]
    additions = sum(f.get("additions", 0) for f in files)
    deletions = sum(f.get("deletions", 0) for f in files)

    return DiffResult(
        source="github",
        raw_diff=raw_diff,
        title=pr.get("title", ""),
        description=pr.get("body", "") or "",
        author=pr.get("user", {}).get("login", ""),
        base_ref=pr.get("base", {}).get("ref", ""),
        head_ref=pr.get("head", {}).get("ref", ""),
        changed_files=changed_files,
        additions=additions,
        deletions=deletions,
        url=pr.get("html_url", ""),
    )


# ── GitLab MR ────────────────────────────────────────────────────────────────

async def fetch_gitlab_mr(
    project: str,
    mr_iid: int,
    token: str,
    base_url: str = "https://gitlab.com",
) -> DiffResult:
    """
    Fetch a GitLab Merge Request diff via the GitLab REST API.

    Args:
        project:  "namespace/repo" or numeric project ID
        mr_iid:   MR internal ID (the number you see in the UI)
        token:    GitLab personal access token (glpat-...)
        base_url: GitLab instance base URL
    """
    if not token:
        raise ValueError("REVIEWER_GITLAB__TOKEN is required for GitLab MR review")

    from urllib.parse import quote
    encoded_project = quote(str(project), safe="")
    api_base = base_url.rstrip("/") + "/api/v4"
    headers = {"PRIVATE-TOKEN": token}

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        # ── MR metadata ───────────────────────────────────────────────────
        mr_resp = await client.get(
            f"{api_base}/projects/{encoded_project}/merge_requests/{mr_iid}"
        )
        _raise_for_status(mr_resp, f"GitLab MR {project}!{mr_iid}")
        mr = mr_resp.json()

        # ── Diff ──────────────────────────────────────────────────────────
        diff_resp = await client.get(
            f"{api_base}/projects/{encoded_project}/merge_requests/{mr_iid}/diffs"
        )
        _raise_for_status(diff_resp, f"GitLab MR diff {project}!{mr_iid}")
        diffs = diff_resp.json()

    raw_diff = _gitlab_diffs_to_unified(diffs)
    changed_files = [d.get("new_path") or d.get("old_path", "") for d in diffs]
    additions = sum(d.get("diff", "").count("\n+") for d in diffs)
    deletions = sum(d.get("diff", "").count("\n-") for d in diffs)

    return DiffResult(
        source="gitlab",
        raw_diff=raw_diff,
        title=mr.get("title", ""),
        description=mr.get("description", "") or "",
        author=mr.get("author", {}).get("username", ""),
        base_ref=mr.get("target_branch", ""),
        head_ref=mr.get("source_branch", ""),
        changed_files=changed_files,
        additions=additions,
        deletions=deletions,
        url=mr.get("web_url", ""),
    )


# ── Diff file ─────────────────────────────────────────────────────────────────

def fetch_from_file(path: str) -> DiffResult:
    """Read a .diff / .patch file from disk."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Diff file not found: {path}")
    raw_diff = p.read_text(encoding="utf-8", errors="replace")
    if not raw_diff.strip():
        raise ValueError(f"Diff file is empty: {path}")
    changed_files = _parse_changed_files(raw_diff)
    return DiffResult(
        source="file",
        raw_diff=raw_diff,
        title=p.name,
        changed_files=changed_files,
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _parse_changed_files(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            fname = line[6:].strip()
            if fname and fname not in files:
                files.append(fname)
    return files


def _parse_additions_deletions(stat_output: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in stat_output.splitlines():
        if "insertion" in line or "deletion" in line:
            parts = line.split(",")
            for part in parts:
                part = part.strip()
                if "insertion" in part:
                    additions += int(part.split()[0])
                elif "deletion" in part:
                    deletions += int(part.split()[0])
    return additions, deletions


def _gitlab_diffs_to_unified(diffs: list[dict[str, Any]]) -> str:
    """Convert GitLab diff objects to a single unified diff string."""
    parts = []
    for d in diffs:
        old_path = d.get("old_path", "/dev/null")
        new_path = d.get("new_path", "/dev/null")
        parts.append(f"--- a/{old_path}")
        parts.append(f"+++ b/{new_path}")
        parts.append(d.get("diff", ""))
    return "\n".join(parts)


def _raise_for_status(response: httpx.Response, context: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(
            f"API error fetching {context}: HTTP {response.status_code} — {response.text[:300]}"
        )
