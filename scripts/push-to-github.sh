#!/usr/bin/env bash
# Push the project to GitHub.
#
# The token is read from a file, never typed on the command line, so it does not
# land in your shell history or in any process listing.
#
# Usage:
#   1. Put a repo-scoped token in /workspace/.github-push-token (one line, no spaces)
#      chmod 600 /workspace/.github-push-token
#   2. ./scripts/push-to-github.sh
#   3. Delete the token file afterwards, and revoke the token on GitHub.
#
# Options:
#   --token-file PATH   read the token from PATH instead
#   --branch NAME       push to NAME instead of main
#   --keep              do not delete the token file when finished

set -u

REPO="github.com/rrajareddy234-gif/Shiva-sagar-hostel.git"
TOKEN_FILE="/workspace/.github-push-token"
BRANCH="main"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --branch)     BRANCH="$2";     shift 2 ;;
    --keep)       KEEP=1;          shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$TOKEN_FILE" ]; then
  echo "No token file at $TOKEN_FILE" >&2
  echo "Write a repo-scoped token there first, then run this again." >&2
  exit 1
fi

TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
if [ -z "$TOKEN" ]; then
  echo "The token file is empty." >&2
  exit 1
fi

echo "Checking the token can write to $REPO ..."
CHECK=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/rrajareddy234-gif/Shiva-sagar-hostel")
if [ "$CHECK" != "200" ]; then
  echo "The token cannot see the repository (HTTP $CHECK)." >&2
  echo "It needs the 'repo' scope, and it must belong to rrajareddy234-gif." >&2
  exit 1
fi

cd "$(dirname "$0")/.." || exit 1
echo "Pushing branch $BRANCH ..."
GIT_TERMINAL_PROMPT=0 git push "https://x-access-token:${TOKEN}@${REPO}" "$BRANCH" || {
  echo "Push failed." >&2
  exit 1
}

if [ "$KEEP" -eq 0 ]; then
  rm -f "$TOKEN_FILE"
  echo "Token file removed."
fi

echo "Done. Verify at: https://github.com/rrajareddy234-gif/Shiva-sagar-hostel"
echo "Now revoke the token at https://github.com/settings/tokens"
