"""Verify cooldown, concurrent reservations, expiry, and bounded memory."""

import pytest
from breezetts_test.speech_policy import SpeechFrequency


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("breezetts_test.speech_policy.monotonic", lambda: now[0])
    return now


def test_session_cooldown_boundaries_and_same_turn(clock):
    policy = SpeechFrequency()
    first, other = object(), object()
    assert policy.acquire("chat", first, 90)
    assert not policy.can_speak("chat", other, 0)
    policy.finish("chat", first, True)
    assert policy.can_speak("chat", first, 90)
    assert not policy.can_speak("chat", other, 90)
    clock[0] += 89.99
    assert not policy.acquire("chat", other, 90)
    clock[0] += 0.01
    assert policy.acquire("chat", other, 90)
    assert policy.acquire("different-chat", object(), 90)


def test_failure_releases_only_the_owning_reservation(clock):
    policy = SpeechFrequency()
    first, other = object(), object()
    assert policy.acquire("chat", first, 90)
    policy.finish("chat", other, True)
    assert not policy.acquire("chat", other, 0)
    policy.finish("chat", first, False)
    assert policy.acquire("chat", other, 90)
    policy.finish("chat", other, False)
    assert not policy.sessions


def test_zero_interval_preserves_inflight_protection(clock):
    policy = SpeechFrequency()
    first, other = object(), object()
    assert policy.acquire("chat", first, 0)
    assert not policy.acquire("chat", other, 0)
    policy.finish("chat", first, True)
    assert policy.acquire("chat", other, 0)


def test_failed_later_attempt_retains_previous_success(clock):
    policy = SpeechFrequency()
    first, second = object(), object()
    assert policy.acquire("chat", first, 90)
    policy.finish("chat", first, True)
    clock[0] += 90
    assert policy.acquire("chat", second, 90)
    policy.finish("chat", second, False)
    assert not policy.can_speak("chat", object(), 3600)


def test_bounded_cache_and_idle_expiry_preserve_inflight(clock):
    policy = SpeechFrequency()
    policy.max_sessions = 2
    waiting, completed = object(), object()
    assert policy.acquire("waiting", waiting, 90)
    assert policy.acquire("completed", completed, 90)
    policy.finish("completed", completed, True)
    assert not policy.acquire("third", object(), 90)
    clock[0] += 3600
    assert policy.acquire("third", object(), 90)
    assert set(policy.sessions) == {"waiting", "third"}


def test_unscoped_calls_do_not_share_global_cooldown(clock):
    policy = SpeechFrequency()
    first, second = object(), object()
    assert policy.acquire("", first, 90)
    policy.finish("", first, True)
    assert policy.acquire("", second, 90)
    assert policy.sessions == {}
