#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the scripts/extract-frontmatter.py pytest suite.
#
# Run: bash scripts/tests/test-extract-frontmatter.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_extract_frontmatter.py -q
