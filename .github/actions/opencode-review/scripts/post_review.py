#!/usr/bin/env python3
"""Turn an OpenCode review run into one updatable PR summary comment plus inline comments.

Never approves, requests changes, merges, or pushes. The summary is a single issue
comment found by a hidden marker and edited in place on every run. Inline comments are
posted as one COMMENT review containing only findings that are new (by fingerprint) and
that land on lines present in the PR diff; every finding is always in the summary.

Needs GH_TOKEN (pull-requests: write) and the `gh` CLI. Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

SUMMARY_MARKER = "<!-- opencode-review:summary -->"
# Completed review rounds posted for this PR; the auto-review cap counts these.
ROUND_MARKER_FMT = "<!-- opencode-review:round={n} -->"
ROUND_RE = re.compile(r"<!-- opencode-review:round=(\d+) -->")
FINGERPRINT_RE = re.compile(r"<!-- opencode-review:fp=([0-9a-f]{12}) -->")
REVIEW_BLOCK_RE = re.compile(r"<<<REVIEW_JSON\s*(.*?)\s*REVIEW_JSON>>>", re.DOTALL)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SEVERITY_LABEL = {"P0": "Blocker", "P1": "High", "P2": "Medium", "P3": "Low"}
MIN_CONFIDENCE = 0.6
MAX_FINDINGS = 10


def collect_text(events_path):
    """Concatenate the text parts of an `opencode run --format json` event stream."""
    parts = []
    try:
        with open(events_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "text":
                    parts.append(event.get("part", {}).get("text", ""))
    except OSError:
        pass
    return "\n".join(parts)


def extract_review(text):
    """Return the last marker-delimited JSON object in the model output, or None."""
    blocks = REVIEW_BLOCK_RE.findall(text)
    if not blocks:
        return None
    raw = blocks[-1].strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clean_findings(review):
    """Keep well-formed, confident findings, most severe first, capped."""
    kept = []
    for item in review.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).upper()
        path = item.get("path")
        line = item.get("line")
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if severity not in SEVERITY_ORDER or confidence < MIN_CONFIDENCE:
            continue
        if not isinstance(path, str) or not path or not isinstance(line, int) or line < 1:
            continue
        kept.append({
            "severity": severity,
            "category": str(item.get("category", "general")),
            "title": str(item.get("title", "")).strip()[:120] or "Untitled finding",
            "body": str(item.get("body", "")).strip(),
            "path": re.sub(r"^(\./|/)+", "", path),
            "line": line,
            "confidence": confidence,
        })
    kept.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], -f["confidence"]))
    return kept[:MAX_FINDINGS]


def fingerprint(finding):
    """Stable across pushes: line numbers move, the issue identity does not."""
    key = "|".join([finding["path"], finding["severity"], finding["category"],
                    re.sub(r"\W+", " ", finding["title"].lower()).strip()])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def diff_lines(files):
    """Map path -> set of new-file line numbers that appear in the PR diff."""
    result = {}
    for entry in files:
        lines = set()
        current = None
        for row in (entry.get("patch") or "").splitlines():
            hunk = HUNK_RE.match(row)
            if hunk:
                current = int(hunk.group(1))
                continue
            if current is None or row.startswith("\\"):
                continue
            if row.startswith("-"):
                continue
            lines.add(current)
            current += 1
        result[entry["filename"]] = lines
    return result


def finding_markdown(finding, link=None, with_fingerprint=False):
    where = f"[`{finding['path']}:{finding['line']}`]({link})" if link else f"`{finding['path']}:{finding['line']}`"
    text = (f"**{finding['severity']} · {finding['category']}** — **{finding['title']}**\n\n"
            f"{finding['body']}\n\n{where}")
    if with_fingerprint:
        text += f"\n\n<!-- opencode-review:fp={fingerprint(finding)} -->"
    return text


def render_summary(args, status, reason, review, findings, raw_text, round_num=None, round_cap=None):
    short = args.sha[:7]
    head = [SUMMARY_MARKER, "## OpenCode review", ""]
    if round_num is not None:
        head.insert(1, ROUND_MARKER_FMT.format(n=round_num))
    meta = f"Commit `{short}` · model `{args.model}`"
    if round_num is not None and round_num >= 1:
        cap = f" of {round_cap}" if round_cap else ""
        meta += f" · review round {round_num}{cap}"
    if args.run_url:
        meta += f" · [workflow run]({args.run_url})"
    head += [meta, ""]
    foot = ["", "---",
            "_Automated, advisory review. It does not approve, block, or change this PR, and it is "
            "refreshed on every push._"]

    if status == "capped":
        cap = getattr(args, "max_rounds", 0) or "the configured"
        foot = ["", "---", "_Automated, advisory review. It does not approve, block, or change this PR._"]
        body = [f"**Auto-review stopped after {round_num} completed review round"
                f"{'s' if round_num != 1 else ''}** (cap {cap}).",
                "Later pushes are not auto-reviewed. For another opinion, comment `@opencode review`, "
                "re-run the workflow, or raise `max-rounds` in this repo's `opencode-review.yml`."]
        return "\n".join(head + body + foot)
    if status == "busy":
        body = [f"**Review skipped:** the review host was busy ({reason}).",
                "Push a commit, mark the PR ready for review again, or re-run the workflow to retry."]
        return "\n".join(head + body + foot)
    if status == "failed":
        body = [f"**Review did not complete:** {reason or 'the review run failed'}.",
                "See the workflow run for details; this is not a clean review."]
        return "\n".join(head + body + foot)
    if review is None:
        body = ["**The reviewer produced no parseable result**, so there is nothing reliable to report. "
                "This is not a clean review."]
        if raw_text.strip():
            body += ["", "<details><summary>Raw output (truncated)</summary>", "",
                     "```", raw_text.strip()[:3000].replace("```", "'''"), "```", "", "</details>"]
        return "\n".join(head + body + foot)

    verdict = {"no_issues": "No issues found",
               "issues_found": "Issues found",
               "blocking_issues": "Blocking issues found"}.get(review.get("verdict"), "Review complete")
    if findings and review.get("verdict") == "no_issues":
        verdict = "Issues found"
    body = [f"**{verdict}.** {str(review.get('summary', '')).strip()}", ""]
    if not findings:
        body.append("No findings above the confidence bar.")
    for severity in SEVERITY_ORDER:
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        body += [f"### {severity} — {SEVERITY_LABEL[severity]} ({len(group)})", ""]
        for finding in group:
            link = f"https://github.com/{args.repo}/blob/{args.sha}/{finding['path']}#L{finding['line']}"
            body += [finding_markdown(finding, link), ""]
    unverified = [str(x) for x in (review.get("not_verified") or []) if str(x).strip()]
    if unverified:
        body += ["<details><summary>Not verified</summary>", ""] + [f"- {x}" for x in unverified] + ["", "</details>"]
    return "\n".join(head + body + foot)


def parse_pages(text):
    """Flatten the output of `gh api --paginate`: one JSON array per page, concatenated.

    Parsed here instead of using `--slurp`, which older gh releases (self-hosted runners) reject.
    """
    decoder = json.JSONDecoder()
    items, pos = [], 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return items
        page, pos = decoder.raw_decode(text, pos)
        if not isinstance(page, list):
            raise ValueError(f"expected a JSON array per page, got {type(page).__name__}")
        items.extend(page)


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def _run(self, method, path, payload=None, paginate=False):
        cmd = ["gh", "api", "--method", method, f"repos/{self.repo}/{path}"]
        if paginate:
            cmd += ["--paginate"]
        if payload is not None:
            cmd += ["--input", "-"]
        proc = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"gh api {method} {path} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        if paginate:
            return parse_pages(proc.stdout)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def get(self, path, paginate=False):
        return self._run("GET", path, paginate=paginate)

    def write(self, method, path, payload):
        return self._run(method, path, payload)


def find_summary(gh, pr):
    """The bot's single summary comment for this PR, or None."""
    existing = [c for c in gh.get(f"issues/{pr}/comments", paginate=True)
                if SUMMARY_MARKER in (c.get("body") or "")
                and (c.get("user") or {}).get("login") == "github-actions[bot]"]
    return existing[0] if existing else None


def current_round(gh, pr):
    """Completed review rounds posted on this PR (0 when none yet)."""
    summary = find_summary(gh, pr)
    if not summary:
        return 0
    match = ROUND_RE.search(summary.get("body") or "")
    return int(match.group(1)) if match else 0


def round_cap_verdict(gh, pr, max_rounds):
    """(completed rounds, capped?) for the pre-review cap check; 0 or negative max never caps."""
    rounds = current_round(gh, pr)
    return rounds, max_rounds > 0 and rounds >= max_rounds


def upsert_summary(gh, pr, body, existing=None):
    if existing is None:
        existing = find_summary(gh, pr)
    if existing:
        gh.write("PATCH", f"issues/comments/{existing['id']}", {"body": body})
    else:
        gh.write("POST", f"issues/{pr}/comments", {"body": body})


def post_inline(gh, args, findings, files):
    if not findings:
        return
    in_diff = diff_lines(files)
    seen = set()
    for comment in gh.get(f"pulls/{args.pr}/comments", paginate=True):
        seen.update(FINGERPRINT_RE.findall(comment.get("body") or ""))
    comments = []
    for finding in findings:
        if fingerprint(finding) in seen or finding["line"] not in in_diff.get(finding["path"], set()):
            continue
        comment = {"path": finding["path"], "line": finding["line"], "side": "RIGHT",
                   "body": finding_markdown(finding, with_fingerprint=True)}
        comments.append(comment)
    if not comments:
        return
    try:
        gh.write("POST", f"pulls/{args.pr}/reviews", {
            "commit_id": args.sha, "event": "COMMENT",
            "body": f"OpenCode review: {len(comments)} inline comment(s). The full summary is in the \"OpenCode review\" comment.",
            "comments": comments})
    except RuntimeError as error:
        # The findings are already in the summary comment; surface the inline failure instead of hiding it.
        print(f"::warning::Inline comments could not be posted: {error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--sha", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--status", choices=["ran", "busy", "failed", "capped"], required=False)
    parser.add_argument("--reason", default="")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--max-rounds", type=int, default=0,
                        help="auto-review cap for this repo; 0 = unlimited (round display only)")
    parser.add_argument("--check-round-cap", type=int, default=None, metavar="MAX",
                        help="print 'round=N' and 'capped=true|false' for the pre-review cap check, then exit")
    args = parser.parse_args()

    gh = GitHub(args.repo)
    if args.check_round_cap is not None:
        round_num, capped = round_cap_verdict(gh, args.pr, args.check_round_cap)
        print(f"round={round_num}")
        print(f"capped={'true' if capped else 'false'}")
        return 0
    if not args.status:
        parser.error("--status is required unless --check-round-cap is given")

    review = raw_text = None
    findings = []
    if args.status == "ran":
        raw_text = collect_text(os.path.join(args.out_dir, "events.jsonl"))
        review = extract_review(raw_text)
        findings = clean_findings(review) if review else []

    pr = gh.get(f"pulls/{args.pr}")
    if pr.get("state") != "open" or pr["head"]["sha"] != args.sha:
        print(f"PR moved on (state={pr.get('state')}, head={pr['head']['sha'][:7]}); not posting a stale review.")
        return 0

    summary_comment = find_summary(gh, args.pr)
    prior_round = 0
    if summary_comment:
        match = ROUND_RE.search(summary_comment.get("body") or "")
        prior_round = int(match.group(1)) if match else 0
    # Only a completed review run consumes a round; busy/failed/capped posts keep the count.
    round_num = prior_round + 1 if args.status == "ran" else prior_round

    summary = render_summary(args, args.status, args.reason, review, findings, raw_text or "",
                             round_num=round_num, round_cap=args.max_rounds or None)
    upsert_summary(gh, args.pr, summary, existing=summary_comment)
    if args.status == "ran" and review is not None:
        post_inline(gh, args, findings, gh.get(f"pulls/{args.pr}/files", paginate=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
