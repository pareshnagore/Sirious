"""Phase 5 C1 tests: ambient store (AmbientTurnStore) + Deepgram payload
parsing + segment grouping. No network: Deepgram messages are fixtures
captured from the 25 Aug live smoke.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "app"))

from stt import DiarizedUtterance, parse_deepgram_results, group_ambient_segments  # noqa: E402


# ── Deepgram payload parsing ─────────────────────────────────────────────

def test_parse_final_with_speakers():
    msg = {
        "type": "Results",
        "channel": {
            "alternatives": [
                {
                    "transcript": "हां, हो गया. Staging पर deploy कर दिया है.",
                    "words": [
                        {"word": "हां", "start": 5.1, "end": 5.4, "speaker": 1},
                        {"word": "deploy", "start": 6.0, "end": 6.5, "speaker": 1},
                    ],
                }
            ]
        },
    }
    seg = parse_deepgram_results(msg)
    assert seg is not None
    assert seg.speaker_tag == 1
    assert seg.start == pytest.approx(5.1)
    assert seg.end == pytest.approx(6.5)
    assert "deploy" in seg.text


def test_parse_majority_speaker_wins():
    msg = {
        "channel": {
            "alternatives": [
                {
                    "transcript": "a b c d",
                    "words": [
                        {"word": "a", "start": 0, "end": 1, "speaker": 0},
                        {"word": "b", "start": 1, "end": 2, "speaker": 0},
                        {"word": "c", "start": 2, "end": 3, "speaker": 1},
                        {"word": "d", "start": 3, "end": 4, "speaker": 0},
                    ],
                }
            ]
        }
    }
    assert parse_deepgram_results(msg).speaker_tag == 0


def test_parse_empty_and_non_results():
    assert parse_deepgram_results({"type": "Metadata"}) is None
    assert parse_deepgram_results({"channel": {"alternatives": [{"transcript": ""}]}}) is None
    assert parse_deepgram_results({}) is None


# ── Segment grouping ─────────────────────────────────────────────────────

def _seg(tag, text, start, end):
    return DiarizedUtterance(speaker_tag=tag, text=text, start=start, end=end)


def test_group_merges_same_speaker_within_gap():
    out = group_ambient_segments([_seg(0, "Hello", 0, 1), _seg(0, "world", 1.5, 2.5)])
    assert len(out) == 1
    assert out[0].text == "Hello world"
    assert out[0].end == 2.5


def test_group_splits_on_speaker_change():
    out = group_ambient_segments([_seg(0, "Hi", 0, 1), _seg(1, "Hello", 1.2, 2)])
    assert len(out) == 2
    assert [s.speaker_tag for s in out] == [0, 1]


def test_group_splits_on_gap():
    out = group_ambient_segments([_seg(0, "One", 0, 1), _seg(0, "Two", 5, 6)], gap_s=2.0)
    assert len(out) == 2


def test_group_empty():
    assert group_ambient_segments([]) == []
