#!/bin/sh
# SPDX-License-Identifier: MIT
# Contract for the pre-push COMMIT SET — the input to the content net.
#
# The net can only inspect what it is handed. Hand it an empty set and it scans
# nothing and waves the push through, which is the one failure mode a leak guard
# must not have. Two ways it used to be empty:
#
#   1. `git rev-list <local> --not --remotes` subtracts EVERY remote-tracking
#      ref. An instance whose PRIVATE origin already holds all its work computes
#      zero commits when pushing a NEW branch to a PUBLIC remote — the private
#      remote has them — and a branch carrying identity/personas/ went through
#      with exit 0 and no output.
#
#   2. `<remote_sha>..<local_sha>` errors when the local repo lacks remote_sha
#      (force-pushed elsewhere, or never fetched). The `||` set it to empty.
#
# The setup below is the real shape: a private origin that HAS the work, a public
# remote that does not. Without it the bug does not reproduce, which is why a
# 180-line content-net suite on a live instance never caught it.
set -e
HOOK=$(cd "$(dirname "$0")/../.." && pwd)/scripts/hooks/pre-push
PUBLIC=https://github.com/bks-lab/open-bridge.git
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }

setup() {  # $1 = extra content ("pii" or "core")
  rm -rf "$TMP/w"; mkdir -p "$TMP/w"; cd "$TMP/w"
  git init -q --bare private.git; git init -q --bare public.git
  git init -q a; cd a
  git config user.email t@e.st; git config user.name t
  mkdir -p docs; echo "# core" > docs/x.md
  git add -A; git commit -qm core; git branch -M main
  git remote add origin ../private.git; git remote add pub ../public.git
  if [ "$1" = pii ]; then
    mkdir -p identity/personas; echo "steuer_id: 1" > identity/personas/p.yaml
    git add -A; git commit -qm pii
  fi
  # THE CRITICAL STEP: the private remote already has everything.
  git push -q origin main
  git checkout -q -b promote-x
  echo "more" >> docs/x.md; git add -A; git commit -qm more
}

probe() {  # -> exit code of the hook for a NEW branch on the public remote
  printf 'refs/heads/promote-x %s refs/heads/promote-x %s\n' \
    "$(git rev-parse HEAD)" "0000000000000000000000000000000000000000" \
    | sh "$HOOK" pub "$PUBLIC" >/dev/null 2>&1 && echo 0 || echo 1
}

# 1. USER content already on the private remote must still BLOCK on the public one
setup pii
[ "$(probe)" = 1 ] || fail "USER content reached a public remote: the commit set was empty"

# 2. A CORE-only push must still be allowed
setup core
[ "$(probe)" = 0 ] || fail "a CORE-only push was blocked; the guard is now too strict"

# 3. An unreadable remote_sha must FAIL CLOSED, not open
setup pii
printf 'refs/heads/promote-x %s refs/heads/promote-x %s\n' \
  "$(git rev-parse HEAD)" "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" \
  | sh "$HOOK" pub "$PUBLIC" >/dev/null 2>&1 \
  && fail "an unreadable remote sha let the push through; a net that cannot see is not a net"

echo "test-push-guard-commit-set: 3 assertions pass"
