#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the pytest suite of scripts/context-index.py.
#
# Run: bash scripts/tests/test-context-index.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_context_index.py -q
