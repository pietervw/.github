import unittest

import automerge_gate as g

SHA = "a" * 40
OTHER = "b" * 40


def files(*names):
    return [{"filename": n} for n in names]


def summary(sha=SHA, clean=1, login="github-actions[bot]", marker=True):
    result = f"<!-- opencode-review:result sha={sha} clean={clean} -->\n" if marker else ""
    return {"user": {"login": login}, "body": f"{g.SUMMARY_MARKER}\n{result}## OpenCode review"}


class PathTests(unittest.TestCase):
    def test_allowed_marketing_files(self):
        self.assertEqual(g.check_paths(files("content/blog/post.md", "public/hero.webp", "src/styles/site.css"), g.DEFAULT_ALLOW_GLOBS), [])

    def test_unlisted_file_is_refused(self):
        self.assertEqual(len(g.check_paths(files("src/components/Hero.tsx"), g.DEFAULT_ALLOW_GLOBS)), 1)

    def test_block_list_beats_allow_list(self):
        for path in ("docs/migrations/001.md", "supabase/seed.sql", ".github/workflows/x.md", "content/auth/login.md",
                     "public/.env.svg", "db/schema.md", "docs/Billing.MD"):
            with self.subTest(path=path):
                reasons = g.check_paths(files(path), ("*",))
                self.assertEqual(len(reasons), 1)
                self.assertIn("never-auto-merge", reasons[0])

    def test_rename_source_is_checked(self):
        entry = [{"filename": "content/a.md", "previous_filename": "supabase/migrations/001.md"}]
        self.assertTrue(g.check_paths(entry, g.DEFAULT_ALLOW_GLOBS))

    def test_caller_allow_list_narrows_only(self):
        self.assertTrue(g.check_paths(files("public/a.png"), ("content/*",)))


class ReviewTests(unittest.TestCase):
    def test_clean_for_head(self):
        self.assertEqual(g.check_review([summary()], SHA), [])

    def test_not_clean(self):
        self.assertEqual(g.check_review([summary(clean=0)], SHA), ["OpenCode review is not clean"])

    def test_stale_sha(self):
        self.assertIn("not head", g.check_review([summary(sha=OTHER)], SHA)[0])

    def test_missing_marker(self):
        self.assertIn("no result marker", g.check_review([summary(marker=False)], SHA)[0])

    def test_marker_from_a_human_is_ignored(self):
        self.assertEqual(g.check_review([summary(login="someone")], SHA), ["no OpenCode review summary on this PR"])

    def test_newest_summary_wins(self):
        self.assertEqual(g.check_review([summary(), summary(clean=0)], SHA), [])
        self.assertTrue(g.check_review([summary(clean=0), summary()], SHA))

    def test_no_summary(self):
        self.assertTrue(g.check_review([], SHA))


class CiTests(unittest.TestCase):
    def run_(self, name="ci", status="completed", conclusion="success", url="https://x/actions/runs/1/job/2"):
        return {"name": name, "status": status, "conclusion": conclusion, "details_url": url}

    def test_green(self):
        self.assertEqual(g.check_ci([self.run_(), self.run_(conclusion="skipped")], {"total_count": 0}, "9"), [])

    def test_failure_and_pending(self):
        reasons = g.check_ci([self.run_(conclusion="failure"), self.run_(name="b", status="in_progress", conclusion=None)], {"total_count": 0}, "9")
        self.assertEqual(len(reasons), 2)

    def test_own_run_is_ignored(self):
        own = self.run_(status="in_progress", conclusion=None, url="https://x/actions/runs/9/job/2")
        self.assertEqual(g.check_ci([self.run_(), own], {"total_count": 0}, "9"), [])

    def test_no_checks_is_not_green(self):
        self.assertTrue(g.check_ci([], {"total_count": 0}, "9"))

    def test_failing_commit_status(self):
        self.assertTrue(g.check_ci([self.run_()], {"total_count": 1, "state": "pending"}, "9"))


class ThreadAndPrTests(unittest.TestCase):
    def test_threads(self):
        self.assertEqual(g.check_threads([{"isResolved": True}], False), [])
        self.assertEqual(len(g.check_threads([{"isResolved": False}], False)), 1)
        self.assertEqual(len(g.check_threads([], True)), 1)

    def pr(self, **kw):
        base = {"state": "open", "draft": False, "head": {"repo": {"full_name": "o/r"}}, "labels": [{"name": "automerge"}]}
        base.update(kw)
        return base

    def test_eligible(self):
        self.assertEqual(g.check_pr(self.pr(), "automerge", "o/r"), [])

    def test_ineligible(self):
        self.assertTrue(g.check_pr(self.pr(labels=[]), "automerge", "o/r"))
        self.assertTrue(g.check_pr(self.pr(draft=True), "automerge", "o/r"))
        self.assertTrue(g.check_pr(self.pr(state="closed"), "automerge", "o/r"))
        self.assertTrue(g.check_pr(self.pr(head={"repo": {"full_name": "x/fork"}}), "automerge", "o/r"))
        self.assertTrue(g.check_pr(self.pr(head={"repo": None}), "automerge", "o/r"))


if __name__ == "__main__":
    unittest.main()
