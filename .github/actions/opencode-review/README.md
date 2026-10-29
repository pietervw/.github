# OpenCode PR review (reusable composite action)

Read-only, automatic review of ready-for-review pull requests, run by OpenCode
(`zai-coding-plan/glm-5.3`) on a self-hosted runner. Piloted on `pool-calculator`.

## What it does

1. The caller workflow triggers on `opened`, `reopened`, `synchronize`,
   `ready_for_review` and cancels superseded runs per PR; the reusable workflow
   skips drafts, fork PRs and Dependabot.
2. `scripts/run-review.sh` waits for host capacity (memory, load, IO pressure,
   and a review lock of `max-concurrent` slots, default 1). If the host is still
   busy after `wait-seconds` (default 600) it skips and says so on the PR.
3. OpenCode runs with read-only tools: no shell, no edits, no network, no paths
   outside the workspace. It reads the diff from `.opencode-review/diff.patch`
   (git-excluded). This step gets only the z.ai key, never `GITHUB_TOKEN`.
4. `scripts/post_review.py` posts the result with the PR-scoped token: one
   summary comment edited in place on every run, plus a single `COMMENT` review
   holding inline comments for new findings that land on diff lines. It never
   approves, requests changes, merges, or pushes.
5. The summary carries a hidden machine-readable result,
   `<!-- opencode-review:result sha=<head> clean=0|1 -->`. `clean=1` means the review
   ran, the verdict is `no_issues` and there are no P0-P2 findings. The automerge gate
   reads it; the "review / review" check itself still passes regardless of findings.

`review-prompt.md` is the reusable review prompt (credits to Codex and Claude
Code guidance are in the file).

## Using it

Repos call the reusable workflow `.github/workflows/opencode-review.yml` in this
repo; see the root README for the caller and the rollout requirements
(`RUNNER` variable, `ZHIPU_API_KEY` secret).

## Tuning

Inputs: `model`, `prompt-file`, `max-concurrent`, `wait-seconds`. Host gate
thresholds are environment variables on `run-review.sh`: `REVIEW_MIN_MEM_MB`
(1536), `REVIEW_MAX_LOAD_PER_CPU` (2.5), `REVIEW_MAX_IO_FULL` (25, percent of
IO "full" pressure over 10 s), `REVIEW_POLL_SEC` (30), `REVIEW_RUN_TIMEOUT_SEC`
(720), `REVIEW_LOCK_DIR` (`/tmp`).

## Tests

```
python3 -m unittest discover -s .github/actions/opencode-review/scripts
```
