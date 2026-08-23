"""Probe: do the Phase 4 tools fire mid-conversation in PROD?

Two questions over fresh client_session_ids:
  1. Current-events question → Gemini must call web_search (Tavily) and
     summarize results verbally.
  2. Capture request → Gemini must call add_note; the note lands in
     Firestore (tool_notes) and every invocation lands in tool_audit.

Hard evidence is pulled from Cloud Run structured logs afterwards
(tool_called / tool_result events) — see verify commands in product_phases.md.
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from recall_test import converse, tts_to_pcm  # noqa: E402

RUN = uuid.uuid4().hex[:8]
CID_SEARCH = f"probe4-search-{RUN}"
CID_NOTE = f"probe4-note-{RUN}"
PCM = f"probe4_{RUN}.pcm"


async def ask(text: str, cid: str) -> tuple[str, str]:
    tts_to_pcm(text, PCM)
    print(f"[probe] {text}", flush=True)
    u, a = await converse(PCM, cid)
    print(f"[probe] user='{u}'", flush=True)
    print(f"[probe] asst='{a}'", flush=True)
    return u, a


async def main():
    which = sys.argv[2] if len(sys.argv) > 2 else "both"
    if which in ("both", "search"):
        await ask(
            "What is the latest news about India's high speed rail project?",
            CID_SEARCH,
        )
    if which in ("both", "note"):
        await ask(
            "Please note down that I want to try the new filter coffee "
            "place in Indiranagar this weekend.",
            CID_NOTE,
        )


asyncio.run(main())
