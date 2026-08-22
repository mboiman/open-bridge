#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Thin wrapper for the rule-inventory pytest suite of scripts/validate-bridge.py.
#
# Run: bash scripts/tests/test-validate-bridge-rule-map.sh   (from repo root; non-zero on failure)
set -u
cd "$(dirname "$0")/../.."

exec python3 -m pytest scripts/tests/test_validate_bridge_rule_map.py -q
