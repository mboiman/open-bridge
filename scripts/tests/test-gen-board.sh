#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the scripts/gen-board.py pytest suite.
#
# Run: bash scripts/tests/test-gen-board.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_gen_board.py -q
