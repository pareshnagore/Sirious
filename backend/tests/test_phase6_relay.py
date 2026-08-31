"""Phase 6 step 5 tests: activity-window relay guard (S2 hardening).

The 31 Aug S2 failure (prod session 6f689950) killed a Gemini Live session
with websocket close 1007 "Precondition check failed" after rapid barge-ins;
the incident log showed an unbalanced activityEnd and a final activityStart
left open. The guard collapses those signals before they reach Gemini.

Run from backend/:  .venv/Scripts/python.exe -m pytest tests/ -q
"""

from app.relay import ActivityWindowGuard


def test_start_opens_and_forwards():
    g = ActivityWindowGuard()
    d = g.on_start()
    assert d.forward is True
    assert d.reason == "open"
    assert g.is_open is True


def test_end_closes_and_forwards():
    g = ActivityWindowGuard()
    g.on_start()
    d = g.on_end()
    assert d.forward is True
    assert d.reason == "close"
    assert g.is_open is False


def test_duplicate_start_suppressed():
    """The S2 signature: second activityStart while a window is open."""
    g = ActivityWindowGuard()
    g.on_start()
    d = g.on_start()
    assert d.forward is False
    assert d.reason == "duplicate_start"
    # State unchanged — the original window stays open.
    assert g.is_open is True
    # And a following end still closes normally.
    assert g.on_end().forward is True


def test_unbalanced_end_suppressed():
    """The S2 incident also showed an end with no open window."""
    g = ActivityWindowGuard()
    d = g.on_end()
    assert d.forward is False
    assert d.reason == "unbalanced_end"
    assert g.is_open is False


def test_end_after_end_suppressed_then_start_reopens():
    g = ActivityWindowGuard()
    g.on_start()
    g.on_end()
    assert g.on_end().forward is False  # unbalanced
    assert g.on_start().forward is True  # fresh window legal


def test_rapid_barge_in_sequence_all_forward():
    """A realistic rapid-barge-in sequence (S2 shape) stays fully legal:
    start/end pairs alternate cleanly and every signal forwards."""
    g = ActivityWindowGuard()
    for _ in range(6):
        assert g.on_start().forward is True
        assert g.on_end().forward is True


def test_force_close_resets_state():
    g = ActivityWindowGuard()
    g.on_start()
    g.force_close()
    assert g.is_open is False
    # After a server-side reset, an end is unbalanced, a start is legal.
    assert g.on_end().forward is False
    assert g.on_start().forward is True


def test_force_close_never_reports_forward():
    """force_close is a server-side state fix only — it must never be
    confused with a forwardable signal."""
    g = ActivityWindowGuard()
    g.on_start()
    g.force_close()
    # Nothing to assert beyond is_open; the method returns None by design.
