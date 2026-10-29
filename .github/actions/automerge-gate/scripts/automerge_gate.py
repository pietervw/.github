#!/usr/bin/env python3
"""Merge an opted-in pull request only when every gate passes; otherwise say why and do nothing.

Gates, all required, all evaluated against the PR head SHA read at the start:
  1. the PR is open, not a draft, from the same repo, and carries the opt-in label;
  2. every changed file (and every rename source) matches an allow glob and no block glob;
  3. the OpenCode review summary for exactly this SHA says clean (see post_review.py);
  4. every other check run is green and the combined commit status is not failing/pending;
  5. no review thread (CodeRabbit included) is unresolved.
The merge passes `--match-head-commit`, so a push that lands after the gates ran is not merged.

The block list is built in and wins over any allow glob: schema/migrations, workflows,
lockfiles, auth, payments, env and config files never auto-merge. Callers can only narrow
the allow list, not shrink the block list.

Needs GH_TOKEN with permission to merge. Use a PAT or app token, not GITHUB_TOKEN: a merge made
with GITHUB_TOKEN does not trigger the push workflows (deploys) that Pete's merge normally starts.
Standard library only; shells out to `gh`.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

RESULT_RE = re.compile(r"<!-- opencode-review:result sha=([0-9a-f]{40}) clean=([01]) -->")
SUMMARY_MARKER = "<!-- opencode-review:summary -->"
BOT = "github-actions[bot]"

# fnmatch semantics on the lower-cased repo path; `*` crosses `/`.
BLOCK_GLOBS = (
    "*migration*", "*supabase/*", "*schema*", "*prisma/*", "*drizzle/*", "*.sql",
    ".github/*",
    "*package-lock.json", "*pnpm-lock.yaml", "*yarn.lock", "*bun.lock*", "*cargo.lock", "*poetry.lock", "*uv.lock",
    "*package.json", "*auth*", "*payment*", "*billing*", "*stripe*", "*checkout*", "*webhook*",
    "*.env*", "*secret*", "*wrangler.*", "*dockerfile*", "*docker-compose*", "*.config.*", "*middleware.*",
)
DEFAULT_ALLOW_GLOBS = (
    "*.md", "*.mdx", "content/*", "src/content/*", "public/*",
    "*.css", "*.svg", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.avif", "*.ico",
)
OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def matches(path, globs):
    lowered = path.lower()
    return any(fnmatch.fnmatchcase(lowered, glob.lower()) for glob in globs)


def check_paths(files, allow_globs):
    """Reasons the changed files cannot auto-merge; empty means all are allowed."""
    reasons = []
    for entry in files:
        for path in filter(None, (entry.get("filename"), entry.get("previous_filename"))):
            if matches(path, BLOCK_GLOBS):
                reasons.append(f"`{path}` is on the never-auto-merge list")
            elif not matches(path, allow_globs):
                reasons.append(f"`{path}` is not on the allow list")
    return reasons


def check_review(comments, sha):
    """The newest bot summary must name this exact SHA and say clean."""
    for comment in comments:
        body = comment.get("body") or ""
        if SUMMARY_MARKER not in body or (comment.get("user") or {}).get("login") != BOT:
            continue
        found = RESULT_RE.search(body)
        if not found:
            return ["OpenCode review summary has no result marker (old action version?)"]
        if found.group(1) != sha:
            return [f"OpenCode review is for {found.group(1)[:7]}, not head {sha[:7]}"]
        return [] if found.group(2) == "1" else ["OpenCode review is not clean"]
    return ["no OpenCode review summary on this PR"]


def check_ci(check_runs, status, own_run_id):
    """Every check run except this workflow's own must have finished green."""
    reasons = []
    others = [run for run in check_runs if f"/actions/runs/{own_run_id}/" not in (run.get("details_url") or "")]
    if not others:
        reasons.append("no CI check runs on the head commit yet")
    for run in others:
        if run.get("status") != "completed":
            reasons.append(f"check `{run.get('name')}` is {run.get('status')}")
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            reasons.append(f"check `{run.get('name')}` concluded {run.get('conclusion')}")
    if status.get("total_count", 0) and status.get("state") != "success":
        reasons.append(f"commit status is {status.get('state')}")
    return reasons


def check_threads(threads, has_next_page):
    reasons = []
    if has_next_page:
        reasons.append("more than 100 review threads; not evaluating")
    open_count = sum(1 for thread in threads if not thread.get("isResolved"))
    if open_count:
        reasons.append(f"{open_count} unresolved review thread(s)")
    return reasons


def check_pr(pr, label, repo):
    reasons = []
    if pr.get("state") != "open":
        reasons.append("PR is not open")
    if pr.get("draft"):
        reasons.append("PR is a draft")
    if pr["head"]["repo"] is None or pr["head"]["repo"]["full_name"] != repo:
        reasons.append("PR is from a fork")
    if label not in [item["name"] for item in pr.get("labels", [])]:
        reasons.append(f"PR does not carry the `{label}` label")
    return reasons


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def api(self, args, payload=None):
        proc = subprocess.run(["gh", "api", *args], input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"gh api {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def get(self, path):
        return self.api([f"repos/{self.repo}/{path}"])

    def pages(self, path, key=None):
        data = self.api(["--paginate", "--slurp", f"repos/{self.repo}/{path}"])
        return [item for page in data for item in (page[key] if key else page)]

    def threads(self, number):
        owner, name = self.repo.split("/")
        query = ("query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p)"
                 "{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved}}}}}")
        data = self.api(["graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={number}"])
        block = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        return block["nodes"], block["pageInfo"]["hasNextPage"]


def evaluate(gh, number, label, allow_globs, own_run_id):
    pr = gh.get(f"pulls/{number}")
    sha = pr["head"]["sha"]
    reasons = check_pr(pr, label, gh.repo)
    if reasons:  # not opted in or not eligible: skip the expensive checks
        return sha, reasons
    files = gh.pages(f"pulls/{number}/files")
    if len(files) >= 3000:
        return sha, ["PR touches 3000+ files; not evaluating"]
    reasons += check_paths(files, allow_globs)
    reasons += check_review(list(reversed(gh.pages(f"issues/{number}/comments"))), sha)
    reasons += check_ci(gh.pages(f"commits/{sha}/check-runs", key="check_runs"), gh.get(f"commits/{sha}/status"), own_run_id)
    reasons += check_threads(*gh.threads(number))
    return sha, reasons


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--label", default="automerge")
    parser.add_argument("--allow-glob", action="append", default=[])
    parser.add_argument("--merge-method", choices=["squash", "merge", "rebase"], default="squash")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "0"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    gh = GitHub(args.repo)
    allow = args.allow_glob or list(DEFAULT_ALLOW_GLOBS)
    sha, reasons = evaluate(gh, args.pr, args.label, allow, args.run_id)
    if reasons:
        write_summary([f"### Automerge gate: PR #{args.pr} not merged (head `{sha[:7]}`)", ""] + [f"- {r}" for r in reasons])
        return 0
    if args.dry_run:
        write_summary([f"### Automerge gate: PR #{args.pr} would merge (head `{sha[:7]}`, dry run)"])
        return 0
    proc = subprocess.run(["gh", "pr", "merge", str(args.pr), "--repo", args.repo, f"--{args.merge_method}",
                           "--match-head-commit", sha], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        write_summary([f"### Automerge gate: PR #{args.pr} passed the gates but the merge failed", "", "```",
                       (proc.stderr or proc.stdout).strip()[:1500], "```"])
        return 1
    write_summary([f"### Automerge gate: PR #{args.pr} merged at `{sha[:7]}`"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
