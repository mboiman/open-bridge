#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the pytest suite of scripts/check-doc-routes.py.
#
# Run: bash scripts/tests/test-doc-routes.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_doc_routes.py -q
