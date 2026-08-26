"""Probe 2: create a named v2 recognizer (model baked in) and test diarization
against it. If the default `_` recognizer rejects features, a real recognizer
with model=latest_long should accept them.
"""

import os
import sys

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"
LOC = "us"
REC_ID = "sirious-smoke-diar"


def main() -> None:
    wav = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")
    with open(wav, "rb") as f:
        content = f.read()

    c = speech.SpeechClient(client_options={"api_endpoint": f"{LOC}-speech.googleapis.com"})
    parent = f"projects/{PROJECT}/locations/{LOC}"

    # 1) create-or-get recognizer with model + languages baked into default config
    rec_name = f"{parent}/recognizers/{REC_ID}"
    try:
        op = c.create_recognizer(
            request=speech.CreateRecognizerRequest(
                parent=parent,
                recognizer_id=REC_ID,
                recognizer=speech.Recognizer(
                    model="latest_long",
                    language_codes=["en-IN", "hi-IN"],
                ),
            )
        )
        rec = op.result() if hasattr(op, "result") else op
        print(f"created: {rec.name}")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("recognizer exists, reusing")
        else:
            print(f"create failed: {str(e)[:200]}")
            return

    # 2) recognize with recognizer defaults + diarization features per-request
    try:
        req = speech.RecognizeRequest(
            recognizer=rec_name,
            config=speech.RecognitionConfig(
                features=speech.RecognitionFeatures(
                    diarization_config=speech.SpeakerDiarizationConfig(
                        min_speaker_count=2,
                        max_speaker_count=2,
                    ),
                    enable_automatic_punctuation=True,
                ),
            ),
            content=content,
        )
        r = c.recognize(request=req)
        print("OK with named recognizer")
        for res in r.results:
            alt = res.alternatives[0]
            words = getattr(alt, "words", [])
            if words:
                tags = sorted({w.speaker_label for w in words})
                print(f"  speakers: {tags}")
                cur, buf = None, []
                for w in words:
                    if w.speaker_label != cur:
                        if buf:
                            print(f"  S{cur}: {' '.join(buf)}")
                        cur, buf = w.speaker_label, [w.word]
                    else:
                        buf.append(w.word)
                if buf:
                    print(f"  S{cur}: {' '.join(buf)}")
            else:
                print(f"  transcript: {alt.transcript[:120]} (no word list in result)")
    except Exception as e:
        print(f"FAIL: {str(e)[:300]}")


if __name__ == "__main__":
    main()
