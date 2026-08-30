#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the pytest suite of scripts/check-reachability.py.
#
# Run: bash scripts/tests/test-reachability.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_reachability.py -q
