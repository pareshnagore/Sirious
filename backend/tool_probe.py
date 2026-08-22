"""Probe: does the agentic memory tool fire mid-conversation?

Asks about a topic that has NO memory (kangaroos). Expected behavior:
Gemini calls search_past_conversations → gets "No matching memories found"
→ answers "no". The prod log line `tool_called` proves the round-trip.
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from recall_test import converse, tts_to_pcm  # noqa: E402

BASE = os.environ.get("BASE", "https://sirious-api-635321277027.asia-south1.run.app")
TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
RUN = uuid.uuid4().hex[:8]
PCM = f"probe_{RUN}.pcm"
Q = "Did we ever have a conversation about kangaroos?"


async def main():
    tts_to_pcm(Q, PCM)
    print(f"[probe] {Q}", flush=True)
    u, a = await converse(PCM, f"probe-kangaroo-{RUN}")
    print(f"[probe] user='{u}'", flush=True)
    print(f"[probe] asst='{a}'", flush=True)
    low = a.lower()
    print("ANSWER_SAYS_NO:", "yes-ish" if ("yes" in low and "didn" not in low and "no," not in low) else "no-ish")


asyncio.run(main())
