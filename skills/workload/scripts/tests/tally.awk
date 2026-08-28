# The tally over one verbose unittest transcript: one line per case, then the
# verdict. Its own file, because the verdict is a claim about the run and a
# claim wants a test; `tests/test_runner.py` feeds it transcripts and reads
# both the last line and the exit code.
#
#   awk -f tally.awk <raw>     exit 0 only when something ran and nothing failed
#
# THE RULE THAT COST SOMETHING: a run in which every case was SKIPPED is not
# green. total used to be ok+bad+skipped, so `run-tests.sh scaffold` in a
# detached copy printed "0/2 green" and exited 0 with nothing having run. That
# matters most in a detached copy, because that is where the mutation battery
# works: a needle whose test can only skip there would have scored as red.

function ident(field,   s) {
  s = substr(field, 2, length(field) - 2)
  sub(/^tests\./, "", s)
  return s
}

# THE SECOND THING THAT COST SOMETHING: a case whose METHOD carries a docstring
# is printed by unittest over TWO lines. The identifier sits on the first, and
# the verdict lands on the second behind the docstring's opening line:
#
#   test_two (tests.test_reconcile.A.test_two)
#   The sentence next to it must not claim what it never looked at. ... ok
#
# Nothing in that second line says which case it belongs to. It used to be filed
# under the second WORD of the prose, so two cases opening with the same two
# words collapsed into one, and `seen[]` then dropped the later verdict as a
# duplicate. A FAIL behind a PASS disappeared and this file exited 0 over it,
# which is the one thing it exists not to do. Eleven cases in this suite carry
# such a docstring and two of them already collided.
function subject(   s) {
  if ($2 ~ /^\(.*\)$/) s = ident($2)
  else                  s = pending
  pending = ""
  # Never nothing: an unnamed line still has to be COUNTED, or the fix would
  # trade one silent loss for another.
  if (s == "") s = "unnamed-case-at-line-" NR
  return s
}

# The identifier line of a documented case, carried over to its verdict.
/^test[^ ]* \(.*\)$/ { pending = ident($2); next }
# One line per CASE. A case with failing subtests prints one verbose line per
# failing subtest, so the first verdict per identifier is the one that counts.
/\.\.\. ok$/      { id = subject(); if (seen[id]++) next
                   printf "  ok    %s\n", id; ok++;   next }
/\.\.\. FAIL$/    { id = subject(); if (seen[id]++) next
                   printf "  FAIL  %s\n", id; bad++;  next }
/\.\.\. ERROR$/   { id = subject(); if (seen[id]++) next
                   printf "  FAIL  %s  (error)\n", id; bad++; next }
/\.\.\. skipped/  { id = subject(); if (seen[id]++) next
                   printf "  skip  %s\n", id; skipped++; next }
END {
  printf "\n"
  total = ok + bad + skipped
  if (total == 0) {
    printf "no test cases were collected, which is itself a failure\n"
  } else if (bad > 0) {
    printf "%d of %d FAILED\n", bad, total
  } else if (ok == 0) {
    printf "0 of %d ran: every case was skipped, so this run says nothing\n", total
  } else if (skipped > 0) {
    printf "%d/%d green, %d skipped\n", ok, total, skipped
  } else {
    printf "%d/%d green\n", ok, total
  }
  exit (bad > 0 || ok == 0) ? 1 : 0
}
