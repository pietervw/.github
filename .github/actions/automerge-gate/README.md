# Automerge gate

Merges a pull request only when every gate passes; otherwise it writes the reasons to the job
summary and does nothing. Intended for low-risk repos (marketing sites). Pete's manual review stays
the gate everywhere else.

## Gates (all required)

1. PR is open, not a draft, from the same repo, and carries the opt-in label (`automerge`).
2. Every changed path (and every rename source) matches the allow list and **not** the block list.
3. The OpenCode summary comment names the PR's exact head SHA with `clean=1`.
4. Every other check run on the head commit finished green, the combined status is not failing, and at
   least one check exists.
5. No unresolved review thread (CodeRabbit included).

The merge uses `--match-head-commit`, so a push that lands after the gates ran is not merged.

## "Review clean"

Defined in `opencode-review/scripts/post_review.py` (`is_clean`): the review ran, the verdict is
`no_issues`, and there are no P0, P1 or P2 findings. It is published as a hidden marker in the
summary comment: `<!-- opencode-review:result sha=<head> clean=0|1 -->`. A busy/failed/skipped
review is never clean. The gate only trusts comments authored by `github-actions[bot]`.

The model's opinion is not the safety net for migrations. The path block list is.

## Block list (built in, cannot be overridden)

Migrations, `supabase/`, schema, prisma/drizzle, `*.sql`, `.github/`, lockfiles, `package.json`,
and paths containing auth, payment, billing, stripe, checkout, webhook, secret, `.env`, wrangler,
Dockerfile, `*.config.*`, middleware. Matching is case-insensitive and deliberately broad: a false
block costs one manual merge, a false allow ships unreviewed code.

## Default allow list

Markdown/MDX, `content/`, `public/`, CSS, and common image formats. Callers can only narrow it with
`allow-globs`; they cannot widen past the block list.

## Token

Pass `MERGE_TOKEN` (fine-grained PAT or app token with Contents and Pull requests write).
`GITHUB_TOKEN` is not usable: merges made with it do not trigger the push workflows, so the deploy
would silently not run.

## Tests

`python3 -B -m unittest discover -s .github/actions/automerge-gate/scripts`
