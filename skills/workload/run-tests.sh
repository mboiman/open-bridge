#!/bin/bash
# Contract entry point. The runner itself lives in scripts/tests/run.sh, so
# there is one implementation and not two that drift apart.
exec "$(cd "$(dirname "$0")" && pwd)/scripts/tests/run.sh" "$@"
