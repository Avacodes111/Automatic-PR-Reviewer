# PR Reviewer

Thoroughly reviews a GitHub pull request and reports the problems it finds
along with the single best fix for each — as a Markdown report, not a PR
comment. Runs both as a local CLI and as a GitHub Action.

## How it works

1. Pulls the PR's metadata and per-file diff from the GitHub API.
2. Splits the diff into size-bounded batches and sends each to Claude with a
   strict "act as a principal engineer, find real problems, propose only the
   single best fix" prompt.
3. Parses the structured findings back out, aggregates them, and asks Claude
   for one short executive summary of overall risk and top priorities.
4. Renders everything into one Markdown report (severity table + full detail
   per finding) and writes it to disk.

Findings are grouped by severity: `critical`, `high`, `medium`, `low`, `nit`.
Binary files and files matching common lockfile/generated-asset patterns are
skipped automatically (see `DEFAULT_IGNORE_GLOBS` in `pr_reviewer.py`); add
more with `--ignore` if needed.

## Setup

No third-party dependencies — the script only uses the Python standard
library (`requirements.txt` is a placeholder documenting that).

You need:
- A **GitHub token** with read access to the repo (`GITHUB_TOKEN` env var).
  Public repos can often be reviewed without one, but you'll hit low rate
  limits.
- An **Anthropic API key** (`ANTHROPIC_API_KEY` env var).

## Running locally

```bash
export GITHUB_TOKEN=ghp_...
export ANTHROPIC_API_KEY=sk-ant-...

python pr_reviewer.py --url https://github.com/owner/repo/pull/123
# or
python pr_reviewer.py --repo owner/repo --pr 123 --output report.md
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--output` | `pr_review_report.md` | where the report is written |
| `--model` | `claude-sonnet-5` | Anthropic model ID; use `claude-opus-4-8` for the deepest possible review on high-stakes PRs |
| `--max-batch-chars` | `12000` | diff characters per batch sent to Claude |
| `--ignore` | (repeatable) | extra glob pattern(s) of files to skip, e.g. `--ignore "*.gen.go"` |

The script prints a one-line severity summary to stdout and writes the full
report to `--output`.

## Running in GitHub Actions

`.github/workflows/pr-review.yml` is included and triggers on
`opened` / `synchronize` / `reopened` / `ready_for_review`. It:

1. Runs `pr_reviewer.py` against the triggering PR.
2. Appends the report to the workflow's **job summary** (visible in the
   Actions tab — no PR comment is posted).
3. Uploads the report as a downloadable **artifact**.

To enable it:

1. Copy `pr_reviewer.py` and `.github/workflows/pr-review.yml` into the
   target repo (paths relative to the repo root).
2. Add a repository secret `ANTHROPIC_API_KEY` (Settings → Secrets and
   variables → Actions). `GITHUB_TOKEN` is supplied automatically by Actions
   — you don't need to set it yourself.
3. Open a PR — the workflow runs automatically.

## Notes / limitations

- This deliberately does **not** post PR comments or change PR status —
  it only produces a report, per your request. If you later want it to also
  comment on the PR, the natural extension point is `render_report()`'s
  output plus a `POST /repos/{owner}/{repo}/issues/{pr}/comments` call using
  the same `GitHubClient`.
- Very large PRs are batched, but each batch is still an independent model
  call, so cross-file issues (e.g. an interface change in file A breaking a
  caller in file B that ends up in a different batch) can be missed. Raising
  `--max-batch-chars` trades that off against per-call cost/latency.
- Findings are only as good as the diff content GitHub returns; if a file's
  patch is omitted (huge diffs, some binary/generated files), it's listed
  under "Skipped Files" rather than silently ignored.

  <!-- test -->
