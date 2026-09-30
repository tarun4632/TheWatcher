from datetime import datetime, timedelta, timezone

from app import scrapers


def test_workday_phrases():
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert scrapers.parse_workday_posted_on("Posted Today", now) == now
    assert scrapers.parse_workday_posted_on("Posted Yesterday", now) == now - timedelta(days=1)
    assert scrapers.parse_workday_posted_on("Posted 3 Days Ago", now) == now - timedelta(days=3)
    assert scrapers.parse_workday_posted_on("Posted 30+ Days Ago", now) == now - timedelta(days=31)


def test_age_window():
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert scrapers.is_recent("", now)
    assert scrapers.is_recent(None, now)
    assert scrapers.is_recent("Posted Today", now)
    assert scrapers.is_recent("Posted 3 Days Ago", now)
    assert scrapers.is_recent("Posted 10 Days Ago", now)
    assert not scrapers.is_recent("Posted 11 Days Ago", now)
    assert not scrapers.is_recent("Posted 30+ Days Ago", now)
    assert scrapers.is_recent((now - timedelta(days=2)).isoformat(), now)
    assert not scrapers.is_recent((now - timedelta(days=40)).isoformat(), now)


def test_lever_milliseconds():
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    recent_ms = int((now - timedelta(days=1)).timestamp() * 1000)
    old_ms = int((now - timedelta(days=40)).timestamp() * 1000)
    assert scrapers.is_recent(recent_ms, now)
    assert not scrapers.is_recent(old_ms, now)
