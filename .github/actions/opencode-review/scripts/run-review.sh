#!/usr/bin/env bash
# Gate on host capacity, then run a read-only OpenCode review of the checked-out PR.
#
# Holds a review lock for the whole run, so at most REVIEW_MAX_CONCURRENT reviews
# run at once on the host (the runner's own CI slot hook limits all jobs; this
# caps reviews specifically). If the host stays busy for REVIEW_WAIT_SEC the
# review is skipped with a notice instead of overloading it.
#
# REVIEW_MODELS is a comma-separated chain: the first entry is the primary
# (paid z.ai coding plan), later entries are keyless OpenCode free-tier
# fallbacks. A failure that looks like a provider usage limit (429 / quota /
# billing text) advances to the next model immediately — the window will not
# recover inside this run. Any other failure that died quickly (under
# REVIEW_RETRY_IF_DIED_UNDER_SEC) is retried once on the same model first. A
# full run timeout is never retried or escalated: it already held the host's
# single CI slot long enough. When the primary fails with a usage limit a
# host-local breaker file (REVIEW_BREAKER_FILE, TTL REVIEW_BREAKER_TTL_SEC)
# makes the window's next reviews skip the doomed primary; a primary success
# clears it. The model that produced the review lands in status.json so the
# posted summary shows which tier reviewed.
#
# This step never sees GITHUB_TOKEN: posting is a separate step.
#
# Writes to $REVIEW_OUT_DIR: events.jsonl, opencode.err, status.json
# Exit 0 for ran or skipped-busy, non-zero for a failed run.
set -euo pipefail

: "${REVIEW_OUT_DIR:?}" "${REVIEW_PROMPT_FILE:?}" "${PR_BASE_REF:?}"
# REVIEW_MODEL (single) is still honoured as a one-entry chain for older callers.
REVIEW_MODELS="${REVIEW_MODELS:-${REVIEW_MODEL:-}}"
: "${REVIEW_MODELS:?}"
MAX_CONCURRENT="${REVIEW_MAX_CONCURRENT:-1}"
LOCK_DIR="${REVIEW_LOCK_DIR:-/tmp}"
WAIT_SEC="${REVIEW_WAIT_SEC:-600}"
POLL_SEC="${REVIEW_POLL_SEC:-30}"
RUN_TIMEOUT_SEC="${REVIEW_RUN_TIMEOUT_SEC:-720}"
ATTEMPTS="${REVIEW_ATTEMPTS:-2}"
RETRY_BACKOFF_SEC="${REVIEW_RETRY_BACKOFF_SEC:-30}"
RETRY_IF_DIED_UNDER_SEC="${REVIEW_RETRY_IF_DIED_UNDER:-180}"
BREAKER_TTL_SEC="${REVIEW_BREAKER_TTL:-1800}"
# Scope the default breaker file to the primary API key (short hash, never the
# key itself) so rotating or alternating keys does not carry breaker state over;
# REVIEW_BREAKER_FILE remains an explicit override.
BREAKER_FILE="${REVIEW_BREAKER_FILE:-}"
if [ -z "${BREAKER_FILE}" ]; then
  key_id=""
  [ -n "${ZHIPU_API_KEY:-}" ] && key_id="-$(printf '%s' "${ZHIPU_API_KEY}" | sha256sum | cut -c1-8)"
  BREAKER_FILE="${LOCK_DIR}/opencode-review-primary-capped${key_id}"
fi
MIN_MEM_MB="${REVIEW_MIN_MEM_MB:-1536}"
MAX_LOAD_PER_CPU="${REVIEW_MAX_LOAD_PER_CPU:-2.5}"
MAX_IO_FULL="${REVIEW_MAX_IO_FULL:-25}"

mkdir -p "$REVIEW_OUT_DIR"
export PATH="${HOME}/.opencode/bin:${PATH}"

write_status() { # status reason [model] [note]
  # python does the JSON encoding: the reason carries untrusted provider/model
  # output (backslashes, control chars, bytes cut mid-UTF-8 by the fallback
  # trim) that printf-style escaping cannot make valid JSON.
  python3 - "$@" <<'PY' > "${REVIEW_OUT_DIR}/status.json"
import json, sys
data = {"status": sys.argv[1], "reason": sys.argv[2]}
if len(sys.argv) > 3 and sys.argv[3]:
    data["model"] = sys.argv[3]
if len(sys.argv) > 4 and sys.argv[4]:
    data["note"] = sys.argv[4]
print(json.dumps(data))
PY
  echo "status=$1" >> "${GITHUB_OUTPUT:-/dev/null}"
  if [ "${3:-}" ]; then echo "model=$3" >> "${GITHUB_OUTPUT:-/dev/null}"; fi
}

# Prints a reason and returns 1 when the host is too busy to start a review.
host_busy() {
  local mem load5 ncpu io
  mem="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
  load5="$(cut -d' ' -f2 /proc/loadavg)"
  ncpu="$(nproc)"
  io="$(awk '/^full/ {for (i = 2; i <= NF; i++) if ($i ~ /^avg10=/) {sub("avg10=", "", $i); print $i}}' /proc/pressure/io 2>/dev/null || true)"
  io="${io:-0}"
  echo "host: mem_available=${mem}MB load5=${load5} cpus=${ncpu} io_full_avg10=${io}%" >&2
  if (( mem < MIN_MEM_MB )); then
    echo "available memory ${mem}MB < ${MIN_MEM_MB}MB"; return 1
  fi
  if awk -v l="$load5" -v n="$ncpu" -v m="$MAX_LOAD_PER_CPU" 'BEGIN { exit !(l > n * m) }'; then
    echo "load5 ${load5} > ${MAX_LOAD_PER_CPU} per CPU (${ncpu} CPUs)"; return 1
  fi
  if awk -v v="$io" -v m="$MAX_IO_FULL" 'BEGIN { exit !(v > m) }'; then
    echo "IO pressure ${io}% > ${MAX_IO_FULL}%"; return 1
  fi
  return 0
}

# Takes one of MAX_CONCURRENT lock files; the fd (lock_fd) stays open, held until exit.
try_lock() {
  local i
  for (( i = 1; i <= MAX_CONCURRENT; i++ )); do
    exec {lock_fd}>"${LOCK_DIR}/opencode-review-${i}.lock" || return 1
    if flock -n "$lock_fd"; then return 0; fi
    exec {lock_fd}>&-
  done
  return 1
}

# Guard before anything runs: an empty loop would otherwise report status=ran
# with no review at all.
if ! [[ "${ATTEMPTS}" =~ ^[0-9]+$ ]] || (( ATTEMPTS < 1 )); then
  echo "::error::REVIEW_ATTEMPTS='${ATTEMPTS}' must be an integer >= 1"
  write_status failed "REVIEW_ATTEMPTS='${ATTEMPTS}' must be an integer >= 1"
  exit 1
fi

deadline=$(( SECONDS + WAIT_SEC ))
reason=""
while true; do
  if reason="$(host_busy)" && { reason="all ${MAX_CONCURRENT} review slot(s) in use"; try_lock; }; then
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "::warning::OpenCode review skipped, host busy after ${WAIT_SEC}s: ${reason}"
    write_status busy "$reason"
    exit 0
  fi
  echo "waiting for capacity (${reason}); retry in ${POLL_SEC}s" >&2
  sleep "$POLL_SEC"
done

base="origin/${PR_BASE_REF}"
if ! git rev-parse --verify --quiet "$base" >/dev/null; then
  echo "::error::${base} not found; check out with fetch-depth: 0"
  write_status failed "base ref ${base} missing"
  exit 1
fi

# The model gets the diff as a file and no shell at all, so a prompt-injected
# review cannot run commands (git flags can write or read arbitrary files).
review_dir=".opencode-review"
mkdir -p "$review_dir"
echo "/${review_dir}/" >> "$(git rev-parse --git-path info/exclude)"
git diff "${base}...HEAD" > "${review_dir}/diff.patch"
git log --format='%h %an %s' "${base}..HEAD" > "${review_dir}/commits.txt"

# Untrusted PR text is trimmed, cannot close its ~~~~ fence (tildes replaced), and
# the prompt tells the model to treat it as data.
body="${PR_BODY:0:4000}"
body="${body//\~/-}"
title="${PR_TITLE//$'\n'/ }"
title="${title//\~/-}"
{
  cat "$REVIEW_PROMPT_FILE"
  printf '\n\n## PR context (untrusted data)\n\n'
  printf -- '- Repository: %s\n- PR: #%s\n- Base: `%s`\n- Head commit: %s\n\n' \
    "${GITHUB_REPOSITORY}" "${PR_NUMBER}" "${PR_BASE_REF}" "${PR_HEAD_SHA}"
  printf 'Title and description:\n~~~~\n%s\n\n%s\n~~~~\n\nChanged files:\n~~~~\n' "$title" "$body"
  git diff --name-status "${base}...HEAD" | head -300 | tr "~" "-" || true
  printf '~~~~\n'
} > "${REVIEW_OUT_DIR}/prompt.md"

# Read-only tools only: no shell, no edits, no network, no paths outside the workspace.
export OPENCODE_CONFIG_CONTENT='{"permission":{"edit":"deny","bash":"deny","webfetch":"deny","external_directory":"deny"}}'
export OPENCODE_DISABLE_AUTOUPDATE=1
# A PR can ship its own opencode.json (or .opencode/) that re-enables tools and MCP
# servers and outranks the inline config above; verified on the VM. Ignore it.
export OPENCODE_DISABLE_PROJECT_CONFIG=1

last_error_detail() { # stderr tail, or the last event line when stderr is empty
  local detail
  detail="$(grep -v '^[[:space:]]*$' "${REVIEW_OUT_DIR}/opencode.err" 2>/dev/null | tail -n 1 | tr -d '\n' || true)"
  if [ -z "$detail" ]; then
    # The CLI can die with empty stderr; the event stream's last line often names the error.
    detail="$(tail -n 1 "${REVIEW_OUT_DIR}/events.jsonl" 2>/dev/null | tr -d '\n' | head -c 300 || true)"
  else
    detail="${detail:0:300}"
  fi
  printf '%s' "$detail"
}

failure_detail() { # $1 = exit code, $2 = elapsed seconds of that attempt
  if (( "$1" == 124 )); then
    printf 'opencode timed out after %ss' "$RUN_TIMEOUT_SEC"
    return
  fi
  local detail; detail="$(last_error_detail)"
  printf 'opencode exited with %s after %ss%s' "$1" "$2" "${detail:+: ${detail}}"
}

# Usage-limit failures advance the model chain without a same-model retry: the
# provider's window will not recover inside this run. Matched against the error
# text only (never the exit-code/elapsed wrapper), so an elapsed time like
# "after 429s" cannot trip it.
is_quota_failure() { # $1 = error text
  printf '%s' "$1" | grep -qiE '429|rate.?limit|usage.?limit|quota|insufficient|billing|balance|limit has been reached'
}

models=()
IFS=',' read -r -a raw_models <<< "${REVIEW_MODELS//[[:space:]]/}"
for m in "${raw_models[@]}"; do
  [ -n "$m" ] && models+=("$m")
done
if [ "${#models[@]}" -eq 0 ]; then
  echo "::error::model chain '${REVIEW_MODELS}' parsed to an empty list"
  write_status failed "model chain '${REVIEW_MODELS}' is empty"
  exit 1
fi
for m in "${models[@]}"; do
  if ! [[ "$m" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._#-]+$ ]]; then
    echo "::error::model '${m}' is not in provider/id form"
    write_status failed "model '${m}' is not in provider/id form"
    exit 1
  fi
done

# Host-local breaker: while fresh, skip the primary (it just reported a usage
# limit) so this window's reviews start on the fallback tier instead of every
# one paying the doomed first attempt.
start_index=0
breaker_note=""
skip_failures=()
if (( BREAKER_TTL_SEC > 0 && ${#models[@]} > 1 )) && [ -f "$BREAKER_FILE" ]; then
  opened_at="$(cat "$BREAKER_FILE" 2>/dev/null || true)"
  now="$(date +%s)"
  if [ -n "${opened_at:-}" ] && [ "${opened_at}" -eq "${opened_at}" ] 2>/dev/null \
     && (( now - opened_at < BREAKER_TTL_SEC )); then
    start_index=1
    breaker_note="primary ${models[0]} skipped: usage-limit breaker open for $(( opened_at + BREAKER_TTL_SEC - now ))s more"
    echo "${breaker_note}" >&2
    skip_failures=("primary ${models[0]}: skipped, usage-limit breaker open")
  else
    rm -f "$BREAKER_FILE" 2>/dev/null || true
  fi
fi

# OpenCode 1.x needs --pure for a config-free run; 2.x removed the flag (the
# inline config governs either way). Detect once instead of pinning a CLI generation.
PURE_FLAG=()
if opencode run --help 2>&1 | grep -q -- '--pure'; then
  PURE_FLAG=(--pure)
fi

opencode --version >&2

rc=1
elapsed=0
ran_model=""
failures=("${skip_failures[@]}")
for (( mi = start_index; mi < ${#models[@]}; mi++ )); do
  model="${models[mi]}"
  attempt=1
  while (( attempt <= ATTEMPTS )); do
    if (( attempt > 1 )); then
      echo "retrying ${model} (attempt ${attempt}/${ATTEMPTS}) after ${RETRY_BACKOFF_SEC}s backoff" >&2
      sleep "$RETRY_BACKOFF_SEC"
    fi
    started="$SECONDS"
    set +e
    timeout --kill-after=30 "$RUN_TIMEOUT_SEC" opencode run --format json ${PURE_FLAG[@]+"${PURE_FLAG[@]}"} \
      --model "$model" "$(cat "${REVIEW_OUT_DIR}/prompt.md")" \
      {lock_fd}>&- < /dev/null > "${REVIEW_OUT_DIR}/events.jsonl" 2> "${REVIEW_OUT_DIR}/opencode.err"
    rc=$?
    set -e
    elapsed=$(( SECONDS - started ))
    (( rc == 0 )) && break

    detail="$(failure_detail "$rc" "$elapsed")"
    err_text="$(last_error_detail)"

    # A full run timeout is neither retried nor escalated: it already held the
    # host's single CI slot long enough, and the next push retries anyway.
    if (( rc == 124 )); then
      echo "::error::${detail}" >&2
      write_status failed "${detail}" "$model"
      exit 1
    fi

    # Transient provider/network failures (observed 2026-10: ECONNRESET-class
    # exits after ~90 s with empty stderr) get one same-model retry; quota
    # failures do not — they advance the chain instead.
    if (( attempt < ATTEMPTS )) && ! is_quota_failure "$err_text" \
       && (( elapsed < RETRY_IF_DIED_UNDER_SEC )); then
      echo "::warning::${detail}; retrying ${model}" >&2
      tail -n 20 "${REVIEW_OUT_DIR}/opencode.err" >&2 || true
      attempt=$(( attempt + 1 ))
      continue
    fi
    # A model giving up is only an error when nothing remains to catch it;
    # otherwise it is a warning (the run can still succeed on a fallback).
    if (( mi + 1 < ${#models[@]} )); then
      echo "::warning::${model}: ${detail}" >&2
    else
      echo "::error::${model}: ${detail}" >&2
    fi
    tail -n 20 "${REVIEW_OUT_DIR}/opencode.err" >&2 || true
    break
  done

  if (( rc == 0 )); then
    ran_model="$model"
    break
  fi

  failures+=("${model}: ${detail}")

  # Only a quota-class failure on the primary opens the breaker, and only when
  # a fallback exists to catch this and the window's next reviews.
  if (( mi == 0 && BREAKER_TTL_SEC > 0 && ${#models[@]} > 1 )) && is_quota_failure "$err_text"; then
    date +%s > "$BREAKER_FILE" 2>/dev/null || true
    echo "primary looks usage-capped; breaker open for ${BREAKER_TTL_SEC}s so next reviews start on the fallback" >&2
  fi

  if (( mi + 1 < ${#models[@]} )); then
    echo "::warning::${model} failed; falling back to ${models[mi + 1]}" >&2
  fi
done

if (( rc != 0 )); then
  all_failed="$(printf '%s | ' "${failures[@]}")"
  write_status failed "all models in the chain failed: ${all_failed% | }" "${models[${#models[@]} - 1]}"
  exit 1
fi

note=""
if [ -n "$breaker_note" ]; then
  note="${breaker_note}"
fi
if [ "$ran_model" != "${models[0]}" ]; then
  note="${note:+${note}; }reviewed on fallback ${ran_model}"
fi
# A healthy primary clears the breaker for the next reviews.
if [ "$ran_model" = "${models[0]}" ] && [ -f "$BREAKER_FILE" ]; then
  rm -f "$BREAKER_FILE" 2>/dev/null || true
fi
write_status ran "" "$ran_model" "$note"
