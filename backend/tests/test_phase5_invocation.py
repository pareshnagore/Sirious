"""Phase 5 C2 tests: invocation handshake helpers + Deepgram keyword boost.

No network: pure string/param-level assertions on the C2 additions.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)  # backend root → app.main works with its relative imports

from app.main import _ambient_seed_block, _clean_query_text  # noqa: E402
from app.stt import DeepgramAmbient  # noqa: E402


# ── Invocation handshake helpers ─────────────────────────────────────────

def test_clean_query_text_trims_and_caps():
    assert _clean_query_text("  hello world  ", 500) == "hello world"
    assert _clean_query_text(None, 500) == ""
    assert _clean_query_text("", 500) == ""
    assert _clean_query_text("   ", 500) == ""


def test_clean_query_text_caps_length():
    raw = "x" * 5000
    assert len(_clean_query_text(raw, 100)) == 100
    assert len(_clean_query_text(raw, 4000)) == 4000


def test_ambient_seed_block_empty_for_no_seed():
    assert _ambient_seed_block("") == ""
    assert _ambient_seed_block("  ") == ""


def test_ambient_seed_block_includes_seed_text():
    block = _ambient_seed_block("S1: hello\nS2: world")
    assert "S1: hello" in block
    assert "do not repeat it back" in block


def test_ambient_seed_block_escapes_em_dash_only():
    # The block text is plain; ensure no brace formatting surprises in the
    # system-instruction concatenation path.
    block = _ambient_seed_block("S1: what is the longest train?")
    assert block.startswith("\n\nA room conversation")
    assert block.endswith("S1: what is the longest train?")


# ── Deepgram keyword boost (C2 spotter recall) ───────────────────────────

def test_deepgram_params_keyword_default():
    # Default env: keyword boost pins the product name on the wire.
    provider = DeepgramAmbient(on_segment=lambda seg: None)
    params = provider._params()
    assert params["keywords"] == "Sirious"
    # Baseline streaming contract stays intact.
    assert params["interim_results"] == "false"
    assert params["encoding"] == "linear16"
    assert params["sample_rate"] == "16000"
    assert params["diarize"] == "true"


def test_deepgram_params_keyword_env_override(monkeypatch):
    monkeypatch.setenv("SIRIOUS_STT_KEYWORD", "Sirious:5")
    provider = DeepgramAmbient(on_segment=lambda seg: None)
    assert provider._params()["keywords"] == "Sirious:5"


def test_deepgram_params_keyword_empty_env_disables(monkeypatch):
    monkeypatch.setenv("SIRIOUS_STT_KEYWORD", "   ")
    provider = DeepgramAmbient(on_segment=lambda seg: None)
    assert "keywords" not in provider._params()