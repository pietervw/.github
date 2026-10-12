"""Run with: python3 -m unittest discover -s .github/actions/opencode-review/scripts"""
import argparse
import json
import os
import tempfile
import unittest

import post_review as pr

PATCH = "@@ -1,3 +1,4 @@\n keep\n-old\n+new\n+added\n keep2\n@@ -20,2 +21,2 @@\n ctx\n+tail\n"


def finding(**overrides):
    base = {"severity": "P2", "category": "correctness", "title": "Bad compare", "body": "Why.",
            "path": "src/a.ts", "line": 3, "confidence": 0.9}
    base.update(overrides)
    return base


class FakeGitHub:
    def __init__(self, review_comments=()):
        self.review_comments = list(review_comments)
        self.writes = []

    def get(self, path, paginate=False):
        return self.review_comments

    def write(self, method, path, payload):
        self.writes.append((method, path, payload))
        return {}


class ParsingTests(unittest.TestCase):
    def test_collects_text_events_and_takes_last_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            events = [
                {"type": "tool_use", "part": {}},
                {"type": "text", "part": {"text": "draft <<<REVIEW_JSON\n{\"summary\": \"old\"}\nREVIEW_JSON>>>"}},
                {"type": "text", "part": {"text": "final\n<<<REVIEW_JSON\n```json\n{\"summary\": \"new\"}\n```\nREVIEW_JSON>>>"}},
            ]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json\n" + "\n".join(json.dumps(e) for e in events))
            review = pr.extract_review(pr.collect_text(path))
        self.assertEqual(review["summary"], "new")

    def test_unparseable_or_missing_block_is_none(self):
        self.assertIsNone(pr.extract_review("no markers"))
        self.assertIsNone(pr.extract_review("<<<REVIEW_JSON\n{broken\nREVIEW_JSON>>>"))
        self.assertIsNone(pr.extract_review("<<<REVIEW_JSON\n[1]\nREVIEW_JSON>>>"))

    def test_clean_findings_filters_sorts_and_caps(self):
        raw = {"findings": [
            finding(severity="P3", title="low"),
            finding(severity="P1", title="high", confidence=0.7),
            finding(severity="P0", title="low confidence", confidence=0.3),
            finding(severity="P9", title="bad severity"),
            finding(title="no path", path=""),
            finding(title="bad line", line=0),
            finding(title="dot path", path="./.github/x.yml"),
            "garbage",
        ]}
        kept = pr.clean_findings(raw)
        self.assertEqual([f["title"] for f in kept], ["high", "dot path", "low"])
        self.assertEqual(kept[1]["path"], ".github/x.yml")
        many = {"findings": [finding(title=f"t{i}") for i in range(30)]}
        self.assertEqual(len(pr.clean_findings(many)), pr.MAX_FINDINGS)

    def test_fingerprint_ignores_line_but_not_identity(self):
        self.assertEqual(pr.fingerprint(finding(line=3)), pr.fingerprint(finding(line=40)))
        self.assertNotEqual(pr.fingerprint(finding()), pr.fingerprint(finding(title="Other")))


class DiffTests(unittest.TestCase):
    def test_diff_lines_counts_added_and_context_lines(self):
        lines = pr.diff_lines([{"filename": "src/a.ts", "patch": PATCH}])["src/a.ts"]
        self.assertEqual(lines, {1, 2, 3, 4, 21, 22})

    def test_binary_file_without_patch(self):
        self.assertEqual(pr.diff_lines([{"filename": "x.png"}])["x.png"], set())


class PostingTests(unittest.TestCase):
    args = argparse.Namespace(repo="o/r", pr=7, sha="a" * 40, model="m", run_url="")

    def test_inline_only_new_findings_on_diff_lines(self):
        files = [{"filename": "src/a.ts", "patch": PATCH}]
        seen = finding(title="Seen")
        gh = FakeGitHub([{"body": f"x <!-- opencode-review:fp={pr.fingerprint(seen)} -->"}])
        cleaned = pr.clean_findings({"findings": [finding(line=3), seen, finding(title="Off diff", line=99),
                                                  finding(title="Second", line=22)]})
        pr.post_inline(gh, self.args, cleaned, files)
        (method, path, payload), = gh.writes
        self.assertEqual((method, path, payload["event"]), ("POST", "pulls/7/reviews", "COMMENT"))
        self.assertEqual([c["line"] for c in payload["comments"]], [3, 22])
        self.assertIn("opencode-review:fp=", payload["comments"][0]["body"])

    def test_no_review_posted_when_nothing_new(self):
        gh = FakeGitHub()
        pr.post_inline(gh, self.args, pr.clean_findings({"findings": [finding(line=99)]}), [{"filename": "src/a.ts", "patch": PATCH}])
        self.assertEqual(gh.writes, [])


class SummaryTests(unittest.TestCase):
    args = PostingTests.args

    def render(self, status="ran", review=None, reason="", raw=""):
        findings = pr.clean_findings(review) if review else []
        return pr.render_summary(self.args, status, reason, review, findings, raw)

    def test_marker_and_groups(self):
        text = self.render(review={"summary": "S", "verdict": "blocking_issues",
                                   "findings": [finding(severity="P1"), finding(severity="P3", title="n")],
                                   "not_verified": ["runtime"]})
        self.assertTrue(text.startswith(pr.SUMMARY_MARKER))
        self.assertIn("### P1 — High (1)", text)
        self.assertIn("### P3 — Low (1)", text)
        self.assertIn("blob/" + "a" * 40 + "/src/a.ts#L3", text)
        self.assertIn("Not verified", text)

    def test_clean_review(self):
        text = self.render(review={"summary": "Fine.", "verdict": "no_issues", "findings": []})
        self.assertIn("No issues found.", text)
        self.assertIn("No findings above the confidence bar.", text)

    def test_skipped_failed_and_unparsed_are_never_a_clean_review(self):
        self.assertIn("Review skipped", self.render("busy", reason="load5 high"))
        self.assertIn("did not complete", self.render("failed", reason="exit 1"))
        unparsed = self.render(review=None, raw="oops ```x``` ")
        self.assertIn("no parseable result", unparsed)
        self.assertIn("not a clean review", unparsed)
        self.assertNotIn("No issues found", unparsed)


if __name__ == "__main__":
    unittest.main()
