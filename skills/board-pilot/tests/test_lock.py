"""Lock liveness — reclaim a holder ONLY when it is both pid-dead AND
heartbeat-stale. A live worker (even one that is swap-starved and hasn't
heartbeated) keeps its lock — that is the whole point of the lock.
"""
import os

from engine.lock import Lock

DEAD_PID = 2**30  # never a live process


def test_live_fresh_holder_blocks(tmp_path):
    assert Lock(tmp_path, "k").acquire(pid=os.getpid(), now=100.0) is True
    assert Lock(tmp_path, "k").acquire(pid=os.getpid(), now=101.0) is False


def test_live_but_stale_holder_still_blocks(tmp_path):
    # a live worker that is merely heartbeat-stale (the 16 GB swap-starve case)
    # must KEEP its lock — never stolen.
    assert Lock(tmp_path, "k", stale_seconds=10).acquire(pid=os.getpid(), now=0.0) is True
    assert Lock(tmp_path, "k", stale_seconds=10).acquire(pid=os.getpid(), now=1000.0) is False


def test_dead_but_fresh_holder_is_held(tmp_path):
    # a just-died holder is held until the heartbeat goes stale (conservative).
    assert Lock(tmp_path, "k").acquire(pid=DEAD_PID, now=100.0) is True
    assert Lock(tmp_path, "k").acquire(pid=os.getpid(), now=101.0) is False


def test_dead_and_stale_holder_is_reclaimed(tmp_path):
    # only when the holder is BOTH dead AND stale may a new worker take over.
    assert Lock(tmp_path, "k", stale_seconds=10).acquire(pid=DEAD_PID, now=0.0) is True
    assert Lock(tmp_path, "k", stale_seconds=10).acquire(pid=os.getpid(), now=1000.0) is True


def test_release_frees_the_lock(tmp_path):
    a = Lock(tmp_path, "k")
    assert a.acquire(pid=os.getpid(), now=5.0) is True
    a.release()
    assert Lock(tmp_path, "k").acquire(pid=os.getpid(), now=6.0) is True
