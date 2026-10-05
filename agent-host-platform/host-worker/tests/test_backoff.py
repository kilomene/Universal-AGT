"""Tests for agent.backoff.backoff_delay (pure function)."""
import pytest

from agent.backoff import backoff_delay


def no_jitter(*args, **kwargs):
    """Deterministic draw: jitter factor exactly 1.0."""
    return backoff_delay(*args, rand=lambda: 0.5, **kwargs)


def test_first_failure_is_base():
    assert no_jitter(1) == pytest.approx(5.0)


def test_doubling_sequence():
    assert no_jitter(1) == pytest.approx(5.0)
    assert no_jitter(2) == pytest.approx(10.0)
    assert no_jitter(3) == pytest.approx(20.0)
    assert no_jitter(4) == pytest.approx(40.0)


def test_cap_applies():
    # 5 * 2**6 = 320 > 300, so failures=7 saturates at the cap.
    assert no_jitter(7) == pytest.approx(300.0)
    assert no_jitter(100) == pytest.approx(300.0)


def test_custom_base_and_cap():
    assert backoff_delay(1, base=2.0, cap=100.0, rand=lambda: 0.5) == pytest.approx(2.0)
    assert backoff_delay(3, base=2.0, cap=100.0, rand=lambda: 0.5) == pytest.approx(8.0)
    assert backoff_delay(20, base=2.0, cap=100.0, rand=lambda: 0.5) == pytest.approx(100.0)


def test_jitter_bounds():
    # rand=0.0 -> delay * 0.75 ; rand -> 1.0 -> delay * 1.25
    low = backoff_delay(2, rand=lambda: 0.0)   # base value 10.0
    high = backoff_delay(2, rand=lambda: 1.0)
    assert low == pytest.approx(7.5)
    assert high == pytest.approx(12.5)


def test_jitter_is_symmetric_around_base_value():
    for i in range(200):
        d = backoff_delay(3)  # base value 20.0
        assert 15.0 <= d <= 25.0


def test_zero_failures_rejected():
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_never_negative():
    assert backoff_delay(1, rand=lambda: 0.0) >= 0.0
