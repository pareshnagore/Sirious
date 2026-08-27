"""Probe 8b: batch diarization follow-up.

Questions:
  1. Does SDK 2.19.0 silently DROP 'transcription_config' from generation_config
     (GenerateContentConfig has no such field) — meaning the API never saw
     diarization_mode="speaker"?
  2. With diarization requested, do word-level speaker annotations appear anywhere
     in the interaction content (output_text may hide them)?
"""

import os
import re

from google import genai
from google.genai import types

WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")


def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # 1) pydantic extra-field behavior on GenerateContentConfig
    print("== pydantic behavior ==")
    try:
        gc = types.GenerateContentConfig(transcription_config={"mode": {"type": "verbatim"}})
        print("dict extra field ACCEPTED (extra=allow); serialized keys follow:")
        print("  ", list(gc.model_dump(exclude_none=True).keys()))
    except Exception as e:  # noqa: BLE001
        print(f"dict extra field REJECTED: {e!r}")

    # 2) re-run interactions.create with diarization; dump the full content shape
    print("\n== fresh interactions.create with diarization ==")
    f = client.files.upload(file=WAV)
    ia = client.interactions.create(
        model="gemini-3.5-transcribe",
        input=[{"type": "audio", "uri": f.uri, "mime_type": f.mime_type}],
        generation_config={
            "transcription_config": {
                "mode": {
                    "type": "verbatim",
                    "diarization_mode": "speaker",
                    "timestamp_granularities": ["word"],
                }
            }
        },
    )
    blob = repr(ia)
    print(f"repr length: {len(blob)}")
    for pat in ("spk_", "speaker", "Speaker", "start_offset", "end_offset", "word", "timestamp"):
        hits = [m.start() for m in re.finditer(pat, blob)]
        print(f"  pattern {pat!r}: {len(hits)} hits" + (f" first at {hits[0]}" if hits else ""))
    # show a sample around the first spk_/speaker hit
    m = re.search(r"spk_|Speaker|speaker", blob)
    if m:
        i = m.start()
        print(f"\ncontext around first speaker hit (chars {max(0,i-200)}..{i+300}):")
        print(blob[max(0, i - 200) : i + 300])
    print("\noutput_text:", (getattr(ia, "output_text", None) or "")[:1200])


if __name__ == "__main__":
    main()