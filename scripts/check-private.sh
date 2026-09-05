#!/usr/bin/env bash
# Fail if any private file is tracked by git. Run by CI on every push and PR.
# docs/private/ holds private working documents; .env holds the tokens.
# Neither may ever enter the tree.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

tracked="$(git ls-files -- docs/private .env ':(glob)**/.env')"
if [ -n "$tracked" ]; then
    echo "ERROR: private files are tracked by git:" >&2
    echo "$tracked" >&2
    exit 1
fi
echo "ok: no private files tracked"
