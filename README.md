# pietervw/.github

Shared automation for Pete's repositories.

## OpenCode PR review

Automatic, read-only review of ready (non-draft) pull requests by OpenCode
(`zai-coding-plan/glm-5.3`) on the `ubuntu-agents` self-hosted runners.

- `.github/workflows/opencode-review.yml`: reusable workflow (guards, runner, permissions).
- `.github/actions/opencode-review/`: composite action (capacity gate, read-only run,
  posting). See its README for behaviour, tuning and the threat model.

### Add it to a repo

1. The repo needs the `RUNNER` Actions variable (`self-hosted`) and a `ZHIPU_API_KEY`
   secret (private repos only; see `personal-ai/memory/tools/github-actions-runner.md`).
2. Add `.github/workflows/opencode-review.yml`:

```yaml
name: OpenCode review

on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]

# One review per PR at a time; a newer push cancels the superseded run.
concurrency:
  group: opencode-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions: {}

jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
    uses: pietervw/.github/.github/workflows/opencode-review.yml@v1
    secrets:
      ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
```

3. Optional: cap how many times the bot auto-reviews one PR (default 3; a round
   is one completed review — busy/failed runs do not consume it). When the cap
   is reached the summary comment says so instead of re-running on every push:

```yaml
    with:
      max-rounds: "5"   # or "0" for unlimited
```

The cap applies to the automatic on-push review only; a requested
`@opencode review` still runs.

### Releasing a change

`v1` is a moving tag. Change this repo through a PR, then move the tag
(`git tag -f v1 <sha> && git push -f origin v1`) so every repo picks it up.
The action code never comes from the PR under review, which is why callers use
the central tag instead of a copy in the repo.

This repo must stay accessible to the account's other repos:
Settings, Actions, General, Access, "Accessible from repositories owned by the user".

### Tests

`python3 -m unittest discover -s .github/actions/opencode-review/scripts`
(no CI here: the private-repo runners are the only ones that work, and none is
registered for this repo).
