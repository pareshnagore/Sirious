"""Activity-window relay guard (Phase 6 step 5 — 31 Aug S2 hardening).

Pure state machine tracking the client-observed activity window so the
server only ever forwards RELAY-SAFE activityStart/activityEnd signals to
Gemini's activity state machine.

Why: in manual-VAD mode the client owns turn boundaries. The 31 Aug S2
failure (prod session 6f689950) showed a Gemini Live session abort with
websocket close 1007 "Precondition check failed" after rapid barge-ins —
in the incident, one activityEnd landed with NO open window, an
activityEnd→activityStart flip arrived 78 ms apart, and the final
activityStart was left open when the session died. This guard collapses
such protocol-violating signals to the nearest legal equivalent BEFORE
they reach Gemini. Pure logic, no audio, fully unit-testable.
"""

from dataclasses import dataclass


@dataclass
class RelayDecision:
    """Outcome of feeding one activity signal through the guard."""

    forward: bool
    reason: str  # "open" | "close" | "duplicate_start" | "unbalanced_end"


class ActivityWindowGuard:
    """Tracks whether an activity window is currently open (client view)."""

    def __init__(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def on_start(self) -> RelayDecision:
        """Client says speech started. Forward only if no window is open."""
        if self._open:
            # A second start while already open is a duplicate — Gemini saw
            # the first one. Forwarding it is the exact signal class present
            # right before the S2 1007 abort.
            return RelayDecision(forward=False, reason="duplicate_start")
        self._open = True
        return RelayDecision(forward=True, reason="open")

    def on_end(self) -> RelayDecision:
        """Client says speech ended. Forward only if a window is open."""
        if not self._open:
            # An end with no open window is unbalanced (arrived after a
            # previous end already closed it, or before any start).
            return RelayDecision(forward=False, reason="unbalanced_end")
        self._open = False
        return RelayDecision(forward=True, reason="close")

    def force_close(self) -> None:
        """Server-side reset (Gemini leg died / teardown): clear our view so
        the next signal after a recovery is evaluated fresh. Forwards
        nothing — this only fixes OUR state, never touches Gemini."""
        self._open = False
