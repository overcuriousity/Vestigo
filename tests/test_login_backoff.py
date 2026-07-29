"""Unit tests for the in-memory login backoff tracker (fake clock, no sleeps)."""

from vestigo.core.login_backoff import LoginBackoff


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _backoff(threshold=3, base=2.0, maximum=60.0, max_entries=10_000, clock=None):
    return LoginBackoff(
        threshold=threshold,
        base_seconds=base,
        max_seconds=maximum,
        max_entries=max_entries,
        now=clock or FakeClock(),
    )


def test_allowed_below_threshold():
    b = _backoff(threshold=3)
    b.register_failure("alice", "1.2.3.4")
    b.register_failure("alice", "1.2.3.4")
    assert b.retry_after("alice", "1.2.3.4") == 0.0


def test_delay_grows_exponentially_and_caps():
    clock = FakeClock()
    b = _backoff(threshold=3, base=2.0, maximum=16.0, clock=clock)
    delays = []
    for _ in range(6):
        b.register_failure("alice", "1.2.3.4")
        delays.append(b.retry_after("alice", "1.2.3.4"))
    # Failures 1-2: below threshold. 3rd: 2s, 4th: 4s, 5th: 8s, 6th: capped 16s.
    assert delays == [0.0, 0.0, 2.0, 4.0, 8.0, 16.0]


def test_lock_expires_with_time():
    clock = FakeClock()
    b = _backoff(threshold=1, base=5.0, clock=clock)
    b.register_failure("alice", "1.2.3.4")
    assert b.retry_after("alice", "1.2.3.4") == 5.0
    clock.t += 5.0
    assert b.retry_after("alice", "1.2.3.4") == 0.0


def test_keys_are_isolated_per_username_and_ip():
    b = _backoff(threshold=1)
    b.register_failure("alice", "1.2.3.4")
    assert b.retry_after("alice", "1.2.3.4") > 0
    assert b.retry_after("alice", "5.6.7.8") == 0.0
    assert b.retry_after("bob", "1.2.3.4") == 0.0


def test_username_is_case_insensitive():
    b = _backoff(threshold=1)
    b.register_failure("Alice", "1.2.3.4")
    assert b.retry_after("alice", "1.2.3.4") > 0


def test_reset_clears_state():
    b = _backoff(threshold=1)
    b.register_failure("alice", "1.2.3.4")
    b.reset("alice", "1.2.3.4")
    assert b.retry_after("alice", "1.2.3.4") == 0.0


def test_pruning_bounds_memory():
    clock = FakeClock()
    b = _backoff(threshold=1, base=1.0, max_entries=5, clock=clock)
    for i in range(5):
        b.register_failure(f"user{i}", "1.1.1.1")
    clock.t += 100.0  # all locks expired
    b.register_failure("fresh", "1.1.1.1")
    assert len(b._entries) <= 5
    assert b.retry_after("fresh", "1.1.1.1") > 0


def test_cap_holds_when_nothing_is_prunable():
    """All entries locked into the future: pruning frees nothing, so evict instead."""
    clock = FakeClock()
    b = _backoff(threshold=1, base=1000.0, maximum=1000.0, max_entries=5, clock=clock)
    for i in range(5):
        b.register_failure(f"user{i}", "1.1.1.1")
    b.register_failure("fresh", "1.1.1.1")
    assert len(b._entries) == 5
    assert b.retry_after("fresh", "1.1.1.1") > 0


def test_eviction_drops_the_earliest_expiring_lock():
    clock = FakeClock()
    b = _backoff(threshold=1, base=1000.0, maximum=1000.0, max_entries=3, clock=clock)
    for i in range(3):
        b.register_failure(f"user{i}", "1.1.1.1")
        clock.t += 1.0  # stagger locked_until so user0 expires first
    b.register_failure("fresh", "1.1.1.1")
    assert len(b._entries) == 3
    assert b.retry_after("user0", "1.1.1.1") == 0.0  # evicted
    assert b.retry_after("user2", "1.1.1.1") > 0  # latest lock survives


def test_repeated_failure_on_known_key_does_not_evict():
    """A key already tracked cannot grow the dict, so the cap must not fire."""
    clock = FakeClock()
    b = _backoff(threshold=1, base=1000.0, maximum=1000.0, max_entries=3, clock=clock)
    for i in range(3):
        b.register_failure(f"user{i}", "1.1.1.1")
    b.register_failure("user0", "1.1.1.1")
    assert len(b._entries) == 3
    assert b._entries[("user0", "1.1.1.1")].failures == 2
