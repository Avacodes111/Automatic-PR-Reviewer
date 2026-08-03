#!/usr/bin/env python3
"""
pr_reviewer.py — Thorough automated code review for a GitHub Pull Request.

What it does
------------
1. Fetches PR metadata + the per-file diff from the GitHub API.
2. Splits the diff into size-bounded batches (so large PRs don't blow context).
3. Sends each batch to Claude with a strict "find real problems, propose the
   single best fix" prompt, and parses back structured JSON findings.
4. Aggregates everything into one Markdown report: executive summary, a
   findings table, and full detail (problem + best solution + why) per file.
5. Writes the report to disk and prints a summary to stdout.

It does NOT post anything to GitHub — it only reads the PR and produces a
local report (see README.md if you want to extend it to post a comment).

Usage
-----
    export GITHUB_TOKEN=...        # needs `repo` (or public_repo) read scope
    export ANTHROPIC_API_KEY=...
    python pr_reviewer.py --url https://github.com/owner/repo/pull/123
    python pr_reviewer.py --repo owner/repo --pr 123 --output report.md

Also runnable inside GitHub Actions — see .github/workflows/pr-review.yml,
which supplies --repo/--pr from the triggering event automatically.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

# Sensible default: strong reasoning for a thorough review. Override with
# --model or ANTHROPIC_MODEL if you want a cheaper/faster pass instead.
DEFAULT_MODEL = "claude-sonnet-5"

DEFAULT_MAX_BATCH_CHARS = 12_000
DEFAULT_IGNORE_GLOBS = [
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.woff*",
    "*.pdf",
]

SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]
SEVERITY_EMOJI = {
    "critical": "🟥",
    "high": "🟧",
    "medium": "🟨",
    "low": "🟦",
    "nit": "⬜",
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    file: str
    severity: str
    category: str
    problem: str
    best_solution: str
    why_best: str
    line_hint: str | None = None
    code_suggestion: str | None = None

    def normalized_severity(self) -> str:
        s = (self.severity or "").strip().lower()
        return s if s in SEVERITY_ORDER else "low"


@dataclass
class ReviewResult:
    pr_title: str = ""
    pr_url: str = ""
    pr_author: str = ""
    base_ref: str = ""
    head_ref: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    findings: list[Finding] = field(default_factory=list)
    batch_summaries: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def _http_json(url: str, headers: dict, method: str = "GET", data: bytes | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} for {url}: {detail[:500]}") from e


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Could not parse a PR URL out of: {url}")
    owner, repo, num = m.groups()
    return owner, repo, int(num)


class GitHubClient:
    def __init__(self, token: str | None):
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-reviewer-script",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        return _http_json(url, self.headers)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        files: list[dict] = []
        page = 1
        while True:
            url = (
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
                f"?per_page=100&page={page}"
            )
            batch = _http_json(url, self.headers)
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------

def should_ignore(filename: str, ignore_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(filename, pat) for pat in ignore_globs)


def build_batches(files: list[dict], ignore_globs: list[str], max_chars: int) -> tuple[list[str], list[str]]:
    """Return (batches, skipped_files). Each batch is a formatted text blob
    containing one or more files' diff patches, kept under max_chars."""
    batches: list[str] = []
    skipped: list[str] = []
    current = ""

    for f in files:
        filename = f.get("filename", "unknown")
        if should_ignore(filename, ignore_globs):
            skipped.append(f"{filename} (ignored by glob)")
            continue
        patch = f.get("patch")
        if not patch:
            # Binary file, or a diff too large for GitHub to include a patch for.
            skipped.append(f"{filename} (no textual diff available, status={f.get('status')})")
            continue

        entry = (
            f"### FILE: {filename}\n"
            f"status: {f.get('status')}, +{f.get('additions', 0)}/-{f.get('deletions', 0)}\n"
            f"```diff\n{patch}\n```\n\n"
        )

        if len(entry) > max_chars:
            # Single file's diff alone exceeds the batch budget — truncate it
            # rather than dropping it entirely, and say so.
            truncated_patch = patch[: max_chars - 500]
            entry = (
                f"### FILE: {filename} (TRUNCATED — diff too large, showing first part only)\n"
                f"status: {f.get('status')}, +{f.get('additions', 0)}/-{f.get('deletions', 0)}\n"
                f"```diff\n{truncated_patch}\n... [truncated]\n```\n\n"
            )

        if len(current) + len(entry) > max_chars and current:
            batches.append(current)
            current = entry
        else:
            current += entry

    if current:
        batches.append(current)

    return batches, skipped


# --------------------------------------------------------------------------
# Claude review
# --------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """You are a principal software engineer doing a rigorous, unforgiving code review \
of a GitHub pull request diff. You are thorough and skeptical: you actively look for bugs, \
security issues, correctness problems, race conditions, error-handling gaps, resource leaks, \
performance regressions, bad API design, missing/inadequate tests, and maintainability problems. \
You do not nitpick style unless it genuinely hurts readability or hides bugs.

For every real problem you find, think through more than one possible fix internally, then report \
ONLY the single best solution — not a list of options. "Best" means: correct, minimal, idiomatic \
for the language/framework in evidence, and unlikely to introduce new problems.

If a diff segment has no real problems, do not invent any — it is fine to return zero findings for it.

Respond with ONLY a JSON object (no prose, no markdown fences) matching exactly this schema:
{
  "summary": "one sentence describing what this batch of diff does",
  "findings": [
    {
      "file": "path/to/file",
      "line_hint": "approximate location, e.g. a function name or line range from the diff",
      "severity": "critical|high|medium|low|nit",
      "category": "bug|security|performance|error-handling|testing|design|maintainability|other",
      "problem": "clear, specific description of what is wrong and why it matters",
      "best_solution": "the single recommended fix, concrete enough to act on",
      "why_best": "brief justification for why this is the best fix versus plausible alternatives",
      "code_suggestion": "optional short code snippet implementing the fix, or null"
    }
  ]
}

Severity guide:
- critical: will cause data loss, a crash, a security vulnerability, or broken core functionality
- high: significant bug or design flaw likely to cause problems in production
- medium: real issue, but limited blast radius or edge-case only
- low: worth fixing, minor correctness/robustness/maintainability improvement
- nit: cosmetic, use sparingly and only if genuinely worth mentioning
"""


def call_claude(api_key: str, model: str, user_content: str, max_tokens: int = 4000, retries: int = 3) -> str:
    body = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": REVIEW_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        }
    ).encode("utf-8")

    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ANTHROPIC_API, headers=headers, method="POST", data=body)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(text_parts)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"Anthropic API HTTP {e.code}: {detail[:500]}")
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise last_err
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 ** attempt)
    raise last_err  # type: ignore[misc]


def parse_model_json(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned.strip())
    cleaned = re.sub(r"```$", "", cleaned.strip())
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...} block.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def review_batches(batches: list[str], api_key: str, model: str, result: ReviewResult) -> None:
    for i, batch in enumerate(batches, start=1):
        prompt = (
            f"Pull request: {result.pr_title!r} (author: {result.pr_author})\n"
            f"Reviewing diff batch {i} of {len(batches)}:\n\n{batch}"
        )
        try:
            raw = call_claude(api_key, model, prompt)
            parsed = parse_model_json(raw)
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"Batch {i}/{len(batches)} failed: {e}")
            continue

        if parsed.get("summary"):
            result.batch_summaries.append(parsed["summary"])

        for f in parsed.get("findings", []):
            try:
                result.findings.append(
                    Finding(
                        file=f.get("file", "unknown"),
                        severity=f.get("severity", "low"),
                        category=f.get("category", "other"),
                        problem=f.get("problem", "").strip(),
                        best_solution=f.get("best_solution", "").strip(),
                        why_best=f.get("why_best", "").strip(),
                        line_hint=f.get("line_hint"),
                        code_suggestion=f.get("code_suggestion") or None,
                    )
                )
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"Could not parse a finding in batch {i}: {e}")


def build_executive_summary(result: ReviewResult, api_key: str, model: str) -> str:
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in result.findings:
        counts[f.normalized_severity()] += 1

    if not result.findings:
        return "No significant problems were found in this diff."

    findings_brief = "\n".join(
        f"- [{f.normalized_severity()}] {f.file}: {f.problem}" for f in result.findings[:60]
    )
    prompt = (
        f"Pull request {result.pr_title!r} was reviewed and produced these findings "
        f"(severity counts: {counts}):\n\n{findings_brief}\n\n"
        "Write a 3-5 sentence executive summary for a reviewer who has not read the details: "
        "overall risk level, whether this PR is safe to merge as-is, and the top 1-3 things "
        "that must be fixed first. Plain prose only, no markdown, no JSON."
    )
    try:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        req = urllib.request.Request(ANTHROPIC_API, headers=headers, method="POST", data=body)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
    except Exception:  # noqa: BLE001
        # Fall back to a programmatic summary if the extra call fails for any reason.
        top = sorted(result.findings, key=lambda f: SEVERITY_ORDER.index(f.normalized_severity()))[:3]
        lines = [f"Found {len(result.findings)} issue(s): " + ", ".join(f"{k}={v}" for k, v in counts.items() if v)]
        for f in top:
            lines.append(f"Priority: [{f.normalized_severity()}] {f.file} — {f.problem}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------

def render_report(result: ReviewResult, executive_summary: str) -> str:
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in result.findings:
        counts[f.normalized_severity()] += 1

    lines: list[str] = []
    lines.append(f"# PR Review: {result.pr_title}")
    lines.append("")
    lines.append(f"- **PR:** {result.pr_url}")
    lines.append(f"- **Author:** {result.pr_author}")
    lines.append(f"- **Branch:** `{result.head_ref}` → `{result.base_ref}`")
    lines.append(f"- **Changes:** +{result.additions}/-{result.deletions} across {result.changed_files} file(s)")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(executive_summary)
    lines.append("")
    lines.append("## Findings at a Glance")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|---|---|")
    for sev in SEVERITY_ORDER:
        if counts[sev]:
            lines.append(f"| {SEVERITY_EMOJI[sev]} {sev} | {counts[sev]} |")
    if not result.findings:
        lines.append("| — | 0 issues found |")
    lines.append("")

    if result.findings:
        lines.append("## Detailed Findings")
        lines.append("")
        ordered = sorted(result.findings, key=lambda f: SEVERITY_ORDER.index(f.normalized_severity()))
        for i, f in enumerate(ordered, start=1):
            sev = f.normalized_severity()
            lines.append(f"### {i}. {SEVERITY_EMOJI[sev]} [{sev.upper()}] {f.file}" + (f" — {f.line_hint}" if f.line_hint else ""))
            lines.append("")
            lines.append(f"**Category:** {f.category}")
            lines.append("")
            lines.append(f"**Problem:** {f.problem}")
            lines.append("")
            lines.append(f"**Best solution:** {f.best_solution}")
            lines.append("")
            lines.append(f"**Why this is the best fix:** {f.why_best}")
            if f.code_suggestion:
                lines.append("")
                lines.append("```")
                lines.append(f.code_suggestion)
                lines.append("```")
            lines.append("")

    if result.skipped_files:
        lines.append("## Skipped Files")
        lines.append("")
        for s in result.skipped_files:
            lines.append(f"- {s}")
        lines.append("")

    if result.errors:
        lines.append("## Review Errors")
        lines.append("")
        for e in result.errors:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("---")
    lines.append("*Generated automatically by pr_reviewer.py.*")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Thoroughly review a GitHub PR with Claude and report the best fixes.")
    ap.add_argument("--url", help="Full PR URL, e.g. https://github.com/owner/repo/pull/123")
    ap.add_argument("--repo", help="owner/repo (use with --pr instead of --url)")
    ap.add_argument("--pr", type=int, help="PR number (use with --repo instead of --url)")
    ap.add_argument("--output", default="pr_review_report.md", help="Path to write the Markdown report")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    ap.add_argument("--max-batch-chars", type=int, default=DEFAULT_MAX_BATCH_CHARS)
    ap.add_argument("--ignore", action="append", default=[], help="Extra glob pattern(s) of files to skip")
    args = ap.parse_args()

    if args.url:
        owner, repo, pr_number = parse_pr_url(args.url)
    elif args.repo and args.pr:
        owner, repo = args.repo.split("/", 1)
        pr_number = args.pr
    else:
        ap.error("Provide either --url, or both --repo and --pr")
        return 2

    github_token = os.environ.get("GITHUB_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is required.", file=sys.stderr)
        return 1

    gh = GitHubClient(github_token)

    print(f"Fetching PR {owner}/{repo}#{pr_number} ...")
    pr = gh.get_pr(owner, repo, pr_number)
    files = gh.get_pr_files(owner, repo, pr_number)

    result = ReviewResult(
        pr_title=pr.get("title", ""),
        pr_url=pr.get("html_url", args.url or ""),
        pr_author=(pr.get("user") or {}).get("login", "unknown"),
        base_ref=(pr.get("base") or {}).get("ref", "?"),
        head_ref=(pr.get("head") or {}).get("ref", "?"),
        additions=pr.get("additions", 0),
        deletions=pr.get("deletions", 0),
        changed_files=pr.get("changed_files", len(files)),
    )

    ignore_globs = DEFAULT_IGNORE_GLOBS + args.ignore
    batches, skipped = build_batches(files, ignore_globs, args.max_batch_chars)
    result.skipped_files = skipped

    if not batches:
        print("No reviewable diff content found (all files skipped/binary).")
    else:
        print(f"Reviewing {len(files) - len(skipped)} file(s) across {len(batches)} batch(es) with {args.model} ...")
        review_batches(batches, anthropic_key, args.model, result)

    print("Writing executive summary ...")
    executive_summary = build_executive_summary(result, anthropic_key, args.model)

    report = render_report(result, executive_summary)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)

    counts = {sev: sum(1 for x in result.findings if x.normalized_severity() == sev) for sev in SEVERITY_ORDER}
    print("\n=== Review complete ===")
    print(f"Report written to: {args.output}")
    print("Findings: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "Findings: none")
    if result.errors:
        print(f"({len(result.errors)} batch error(s) — see report for details)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
