#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_workspace -v
