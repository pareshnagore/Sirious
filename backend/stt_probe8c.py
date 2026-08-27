"""Probe 8c: batch diarization via RAW REST (no SDK in the middle).

Uploads the fixture to the Files API, POSTs to /v1beta/interactions with the
exact documented transcription_config shape, and dumps the response JSON so we
can see whether speaker annotations (spk_1/spk_2, word timestamps) actually
come back — independent of google-genai SDK version.
"""

import json
import os

import httpx

WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")
BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ["GEMINI_API_KEY"]


def upload() -> str:
    with open(WAV, "rb") as f:
        r = httpx.post(
            f"{BASE}/files",
            params={"key": KEY},
            files={"file": ("sirious_hinglish.wav", f, "audio/wav")},
            timeout=120,
        )
    r.raise_for_status()
    d = r.json()
    print("upload:", d.get("file", {}).get("name"), d.get("file", {}).get("uri"))
    return d["file"]["uri"]


def main() -> None:
    known = "https://generativelanguage.googleapis.com/v1beta/files/5lnvjbbwsnkw"
    uri = os.environ.get("SIRIOUS_TEST_FILE_URI", known)
    if not uri or uri == known:
        # confirm the known file still exists; fall back to a fresh upload
        r = httpx.get(f"{BASE}/files/{known.split('/')[-1]}", params={"key": KEY}, timeout=60)
        if r.status_code != 200:
            print(f"known file gone ({r.status_code}); uploading fresh")
            uri = upload()
    print("using file:", uri)
    body = {
        "model": "gemini-3.5-transcribe",
        "input": [{"type": "audio", "uri": uri, "mime_type": "audio/wav"}],
        "generation_config": {
            "transcription_config": {
                "mode": {
                    "type": "verbatim",
                    "diarization_mode": "speaker",
                    "timestamp_granularities": ["word"],
                }
            }
        },
    }
    r = httpx.post(f"{BASE}/interactions", params={"key": KEY}, json=body, timeout=180)
    print(f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:2000])
        return
    d = r.json()
    text = json.dumps(d, indent=1)
    print(f"response JSON length: {len(text)}")
    for pat in ("spk_", "speaker", "Speaker", "start_offset", "end_offset", "timestamp"):
        n = text.count(pat)
        print(f"  pattern {pat!r}: {n} hits")
    # pretty-print the content parts compactly
    print("\n--- content parts ---")
    for c in d.get("content", []):
        for p in c.get("parts", []):
            if "text" in p:
                print(f"[{c.get('role')}] text: {p['text'][:800]}")
            else:
                print(f"[{c.get('role')}] part keys: {list(p.keys())}")
    m = text.find("spk_")
    if m >= 0:
        print("\n...spk context...")
        print(text[max(0, m - 300) : m + 500])


if __name__ == "__main__":
    main()