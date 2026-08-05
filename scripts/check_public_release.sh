#!/usr/bin/env bash

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

failed=0

tracked_sensitive="$({
  git ls-files --cached
} | grep -E '(^|/)\.env($|\.)|(^|/)(provider_settings\.json|tts_dataset\.sqlite3)(/|$)|\.(sqlite3|sqlite3-wal|sqlite3-shm|wav|mp3|flac|zip)$' \
  | grep -v -E '(^|/)\.env\.example$' || true)"
if [[ -n "${tracked_sensitive}" ]]; then
  echo "ERROR: runtime data or secret-bearing files are tracked:"
  echo "${tracked_sensitive}"
  failed=1
fi

personal_paths="$(
  git grep --cached -n -I -E \
    '(/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|/opt/[A-Za-z0-9._-]+/|[A-Za-z]:\\\\Users\\\\)' \
    -- ':!scripts/check_public_release.sh' \
    | awk -F: '{print $1 ":" $2}' \
    | sort -u \
    || true
)"
if [[ -n "${personal_paths}" ]]; then
  echo "ERROR: personal absolute paths found at these staged locations:"
  echo "${personal_paths}"
  failed=1
fi

credential_shapes="$(
  git grep --cached -n -I -E \
    '(AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,}|AKLT[0-9A-Za-z]{12,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN ([A-Z0-9]+ )?PRIVATE KEY-----)' \
    -- ':!scripts/check_public_release.sh' \
    | awk -F: '{print $1 ":" $2}' \
    | sort -u \
    || true
)"
if [[ -n "${credential_shapes}" ]]; then
  # Never print the matched line: doing so could copy a real secret into CI logs.
  echo "ERROR: credential-like value found at these staged locations:"
  echo "${credential_shapes}"
  failed=1
fi

if [[ "${failed}" -ne 0 ]]; then
  exit 1
fi

echo "Public release scan passed."
