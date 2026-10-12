#!/usr/bin/env bash
# Gate on host capacity, then run a read-only OpenCode review of the checked-out PR.
#
# Holds a review lock for the whole run, so at most REVIEW_MAX_CONCURRENT reviews
# run at once on the host (the runner's own CI slot hook limits all jobs; this
# caps reviews specifically). If the host stays busy for REVIEW_WAIT_SEC the
# review is skipped with a notice instead of overloading it.
#
# This step never sees GITHUB_TOKEN: posting is a separate step.
#
# Writes to $REVIEW_OUT_DIR: events.jsonl, opencode.err, status.json
# Exit 0 for ran or skipped-busy, non-zero for a failed run.
set -euo pipefail

: "${REVIEW_OUT_DIR:?}" "${REVIEW_MODEL:?}" "${REVIEW_PROMPT_FILE:?}" "${PR_BASE_REF:?}"
MAX_CONCURRENT="${REVIEW_MAX_CONCURRENT:-1}"
LOCK_DIR="${REVIEW_LOCK_DIR:-/tmp}"
WAIT_SEC="${REVIEW_WAIT_SEC:-600}"
POLL_SEC="${REVIEW_POLL_SEC:-30}"
RUN_TIMEOUT_SEC="${REVIEW_RUN_TIMEOUT_SEC:-720}"
MIN_MEM_MB="${REVIEW_MIN_MEM_MB:-1536}"
MAX_LOAD_PER_CPU="${REVIEW_MAX_LOAD_PER_CPU:-2.5}"
MAX_IO_FULL="${REVIEW_MAX_IO_FULL:-25}"

mkdir -p "$REVIEW_OUT_DIR"
export PATH="${HOME}/.opencode/bin:${PATH}"

write_status() { # status reason
  printf '{"status":"%s","reason":"%s"}\n' "$1" "${2//\"/\'}" > "${REVIEW_OUT_DIR}/status.json"
  echo "status=$1" >> "${GITHUB_OUTPUT:-/dev/null}"
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

opencode --version
set +e
timeout --kill-after=30 "$RUN_TIMEOUT_SEC" opencode run --format json --pure \
  --model "$REVIEW_MODEL" "$(cat "${REVIEW_OUT_DIR}/prompt.md")" \
  {lock_fd}>&- < /dev/null > "${REVIEW_OUT_DIR}/events.jsonl" 2> "${REVIEW_OUT_DIR}/opencode.err"
rc=$?
set -e

if (( rc != 0 )); then
  echo "::error::opencode exited with ${rc}"
  tail -n 20 "${REVIEW_OUT_DIR}/opencode.err" >&2 || true
  write_status failed "opencode exited with ${rc}"
  exit 1
fi
write_status ran ""
