"""Harness tests for run-review.sh: the capacity gate and the model chain, driven end to end.

The memory probe reads REVIEW_MEMINFO_FILE (default /proc/meminfo), so the busy
path is exercised from a fixture file — never by starving the box. The load and
IO gate thresholds are pinned open for the run tests so a busy dev machine
cannot flake them. The `opencode` CLI is a scripted stub counted per model and
attempt, which also pins the retry / quota-fallback / breaker contracts.

Run with: python3 -m unittest discover -s .github/actions/opencode-review/scripts
"""
import json
import os
import subprocess
import tempfile
import threading
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
RUN_REVIEW = os.path.join(SCRIPTS, "run-review.sh")
REVIEW_PROMPT = os.path.join(os.path.dirname(SCRIPTS), "review-prompt.md")
ACTION_YML = os.path.join(os.path.dirname(SCRIPTS), "action.yml")

# 388MB available (Tickaboard #15 saw 398MB and 584MB): always under the 1536MB gate.
MEMINFO_BUSY = "MemTotal:       4000000 kB\nMemFree:         100000 kB\nMemAvailable:    398000 kB\n"
MEMINFO_OK = "MemTotal:      16000000 kB\nMemFree:        8000000 kB\nMemAvailable:   8000000 kB\n"

REVIEW_JSON = ("<<<REVIEW_JSON\n"
               '{"summary": "Looks fine.", "verdict": "no_issues", "findings": [], "not_verified": []}\n'
               "REVIEW_JSON>>>")
EVENTS_LINE = json.dumps({"type": "text", "part": {"text": REVIEW_JSON}})

# opencode stub: counts calls per model, supports scripted failure modes.
#   FAKE_QUOTA_MODELS=prov/a,prov/b   -> those models exit 1 with quota text
#   FAKE_TRANSIENT_MODEL=prov/a FAKE_TRANSIENT_TIMES=1 -> first N calls of that
#     model exit 1 with ECONNRESET text, later calls succeed
FAKE_OPENCODE = r"""#!/usr/bin/env bash
state="${FAKE_STATE_DIR:?}"
model=""
prev=""
for a in "$@"; do
  [ "$prev" = "--model" ] && model="$a"
  prev="$a"
done
key="${model//\//_}"
mkdir -p "$state/counts"
n=$(( $(cat "$state/counts/$key" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$state/counts/$key"
printf '%s\n' "$*" >> "$state/log"
if [ "${1:-}" = "--version" ]; then echo "1.9.0-fake"; exit 0; fi
if [ "${2:-}" = "--help" ]; then echo "usage: opencode run [prompt]"; exit 0; fi
if [ -n "${FAKE_TRANSIENT_MODEL:-}" ] && [ "${model:-}" = "${FAKE_TRANSIENT_MODEL}" ] \
   && [ "$n" -le "${FAKE_TRANSIENT_TIMES:-0}" ]; then
  echo "error: read tcp: connection reset by peer (ECONNRESET)" >&2
  exit 1
fi
for m in ${FAKE_QUOTA_MODELS//,/ }; do
  if [ "${model:-}" = "$m" ]; then
    echo "Error: 429 rate limit exceeded, usage limit reached for this window" >&2
    exit 1
  fi
done
cat "${FAKE_OPENCODE_EVENTS:?}"
exit 0
"""


class RunReviewHarness(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.out = os.path.join(base, "out")
        self.state = os.path.join(base, "state")
        self.home = os.path.join(base, "home")
        self.events = os.path.join(base, "events.jsonl")
        for path in (self.out, self.state, os.path.join(self.home, ".opencode", "bin")):
            os.makedirs(path, exist_ok=True)
        with open(self.events, "w", encoding="utf-8") as handle:
            handle.write(EVENTS_LINE + "\n")
        self.bin = os.path.join(base, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "opencode")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(FAKE_OPENCODE)
        os.chmod(stub, 0o755)
        self.repo = self.make_repo()
        self.addCleanup(self.tmp.cleanup)

    def make_repo(self):
        repo = os.path.join(self.tmp.name, "repo")
        def git(*args):
            subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)
        os.makedirs(repo)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        with open(os.path.join(repo, "base.txt"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        # run-review.sh diffs against origin/<PR_BASE_REF>; fake the remote ref.
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        with open(os.path.join(repo, "change.txt"), "w", encoding="utf-8") as handle:
            handle.write("change\n")
        git("add", "-A")
        git("commit", "-qm", "change")
        return repo

    def meminfo(self, text):
        path = os.path.join(self.tmp.name, "meminfo")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def review_env(self, meminfo, **overrides):
        env = {
            **os.environ,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
            "REVIEW_OUT_DIR": self.out,
            "REVIEW_PROMPT_FILE": REVIEW_PROMPT,
            "PR_BASE_REF": "main",
            "GITHUB_REPOSITORY": "o/r",
            "PR_NUMBER": "7",
            "PR_HEAD_SHA": "a" * 40,
            "PR_TITLE": "A PR",
            "PR_BODY": "Body text.",
            "REVIEW_MODELS": "prov/m1,prov/m2",
            "REVIEW_MEMINFO_FILE": self.meminfo(meminfo),
            "REVIEW_LOCK_DIR": self.tmp.name,
            "REVIEW_BREAKER_FILE": os.path.join(self.tmp.name, "breaker"),
            "REVIEW_POLL_SEC": "1",
            "REVIEW_RETRY_BACKOFF_SEC": "0",
            "GITHUB_OUTPUT": os.path.join(self.tmp.name, "github_output"),
            # The load/IO probes read the real host; pin their gates open so
            # only the fixture-driven memory probe decides capacity here.
            "REVIEW_MAX_LOAD_PER_CPU": "1000000",
            "REVIEW_MAX_IO_FULL": "1000000",
            "FAKE_STATE_DIR": self.state,
            "FAKE_OPENCODE_EVENTS": self.events,
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def collect(self, proc, env):
        self.proc = proc
        output_path = env["GITHUB_OUTPUT"]
        self.github_output = ""
        if os.path.exists(output_path):
            with open(output_path, encoding="utf-8") as handle:
                self.github_output = handle.read()
        self.status = {}
        status_path = os.path.join(self.out, "status.json")
        if os.path.exists(status_path):
            with open(status_path, encoding="utf-8") as handle:
                self.status = json.load(handle)
        return proc

    def run_review(self, meminfo, **overrides):
        env = self.review_env(meminfo, **overrides)
        proc = subprocess.run(["bash", RUN_REVIEW], cwd=self.repo, env=env,
                              capture_output=True, text=True, timeout=120)
        return self.collect(proc, env)

    def calls(self, model):
        path = os.path.join(self.state, "counts", model.replace("/", "_"))
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())

    def test_busy_skip_fails_the_check_instead_of_exiting_green(self):
        proc = self.run_review(MEMINFO_BUSY, REVIEW_WAIT_SEC="0")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.status["status"], "busy")
        self.assertIn("available memory", self.status["reason"])
        self.assertIn("< 1536MB", self.status["reason"])
        self.assertIn("status=busy", self.github_output)
        self.assertNotIn("status=ran", self.github_output)
        # The skip must be visible as an error in the check output, not a warning.
        self.assertIn("::error::OpenCode review skipped, host busy", proc.stdout)
        self.assertNotIn("::warning::OpenCode review skipped", proc.stdout)
        # Nothing ran: no model was ever invoked.
        self.assertFalse(os.path.exists(os.path.join(self.state, "log")))

    def test_busy_skip_waits_for_capacity_before_giving_up(self):
        proc = self.run_review(MEMINFO_BUSY, REVIEW_WAIT_SEC="2", REVIEW_POLL_SEC="1")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.status["status"], "busy")
        self.assertGreaterEqual(proc.stderr.count("waiting for capacity"), 1)

    def test_capacity_recovery_during_wait_still_runs_the_review(self):
        # Busy at the first probe, plenty of memory one second later: the gate
        # must re-probe and run the review, not skip on the first sample.
        env = self.review_env(MEMINFO_BUSY, REVIEW_WAIT_SEC="5", REVIEW_POLL_SEC="1")
        meminfo_path = env["REVIEW_MEMINFO_FILE"]

        def relax():
            with open(meminfo_path, "w", encoding="utf-8") as handle:
                handle.write(MEMINFO_OK)

        timer = threading.Timer(1.0, relax)
        timer.start()
        try:
            proc = subprocess.Popen(["bash", RUN_REVIEW], cwd=self.repo, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=120)
        finally:
            timer.cancel()
        proc.stdout, proc.stderr = stdout, stderr
        self.collect(proc, env)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(self.status["status"], "ran")
        self.assertEqual(self.status["model"], "prov/m1")
        self.assertEqual(self.calls("prov/m1"), 1)
        self.assertGreaterEqual(stderr.count("waiting for capacity"), 1)

    def test_healthy_host_runs_the_review_and_exits_success(self):
        proc = self.run_review(MEMINFO_OK, REVIEW_WAIT_SEC="30")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.status["status"], "ran")
        self.assertEqual(self.status["model"], "prov/m1")
        self.assertNotIn("note", self.status)
        self.assertIn("status=ran", self.github_output)
        self.assertEqual(self.calls("prov/m1"), 1)
        self.assertEqual(self.calls("prov/m2"), 0)
        # The review prompt was built from the diff against the base ref.
        with open(os.path.join(self.out, "prompt.md"), encoding="utf-8") as handle:
            self.assertIn("change.txt", handle.read())

    def test_quota_failure_advances_chain_without_retry_and_opens_breaker(self):
        proc = self.run_review(MEMINFO_OK, REVIEW_WAIT_SEC="30", FAKE_QUOTA_MODELS="prov/m1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.status["status"], "ran")
        self.assertEqual(self.status["model"], "prov/m2")
        self.assertIn("reviewed on fallback prov/m2", self.status.get("note", ""))
        self.assertEqual(self.calls("prov/m1"), 1)  # quota: no same-model retry
        self.assertEqual(self.calls("prov/m2"), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "breaker")))

    def test_transient_failure_retries_same_model_before_falling_back(self):
        proc = self.run_review(MEMINFO_OK, REVIEW_WAIT_SEC="30",
                               FAKE_TRANSIENT_MODEL="prov/m1", FAKE_TRANSIENT_TIMES="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.status["status"], "ran")
        self.assertEqual(self.status["model"], "prov/m1")
        self.assertNotIn("note", self.status)
        self.assertEqual(self.calls("prov/m1"), 2)  # quick death retried once
        self.assertEqual(self.calls("prov/m2"), 0)  # retry succeeded: no fallback


class PostReviewMappingTests(unittest.TestCase):
    """The Post review step's outcome-to-status mapping, executed from action.yml itself.

    The busy skip now exits the run step non-zero on purpose; if this mapping
    ever relabelled busy as failed, the PR comment would lose its skip
    explanation and retry guidance — the same silent-regression class this
    change fixes. The block between the two anchors is the real shipped code.
    """

    def mapping(self, outcome, run_status):
        with open(ACTION_YML, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index('status="${RUN_STATUS:-failed}"')
        end = text.index('python3 "${GITHUB_ACTION_PATH}/scripts/post_review.py"', start)
        block = text[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "status.json"), "w", encoding="utf-8") as handle:
                json.dump({"status": run_status, "reason": "available memory 398MB < 1536MB"}, handle)
            script = (f'set -euo pipefail\nout_dir="{tmp}"\nRUN_OUTCOME="{outcome}"\n'
                      f'RUN_STATUS="{run_status}"\n' + block
                      + '\nprintf "STATUS=%s|REASON=%s" "$status" "$reason"')
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_busy_skip_keeps_busy_status_despite_failed_outcome(self):
        out = self.mapping("failure", "busy")
        self.assertIn("STATUS=busy", out)
        self.assertIn("available memory 398MB < 1536MB", out)

    def test_real_failures_still_map_to_failed(self):
        self.assertIn("STATUS=failed", self.mapping("failure", "failed"))
        self.assertIn("STATUS=failed", self.mapping("failure", ""))

    def test_success_keeps_ran(self):
        self.assertIn("STATUS=ran", self.mapping("success", "ran"))


if __name__ == "__main__":
    unittest.main()
