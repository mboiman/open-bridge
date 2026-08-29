#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the pytest suite of scripts/measure-context.py.
#
# Run: bash scripts/tests/test-measure-context.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_measure_context.py -q
