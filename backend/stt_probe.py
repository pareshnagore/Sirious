"""One-shot probe: which Google STT v2 (model, location) combos support
speaker diarization for our languages? Prints OK/FAIL per combo.
Read-only: one local WAV, four recognize() calls.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from google.cloud import speech_v2 as speech  # noqa: E402

WAV = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()) if (tempfile := __import__("tempfile")) else "", "sirious_hinglish.wav")
PROJECT = "sirious-2026"


def main() -> None:
    path = WAV if os.path.exists(WAV) else os.path.join(os.environ["TEMP"], "sirious_hinglish.wav")
    with open(path, "rb") as f:
        content = f.read()

    combos = [
        ("short", "global"),
        ("latest_long", "global"),
        ("short", "us"),
        ("latest_long", "us"),
    ]
    for model, loc in combos:
        ep = "speech.googleapis.com" if loc == "global" else f"{loc}-speech.googleapis.com"
        try:
            c = speech.SpeechClient(client_options={"api_endpoint": ep})
            req = speech.RecognizeRequest(
                recognizer=f"projects/{PROJECT}/locations/{loc}/recognizers/_",
                config=speech.RecognitionConfig(
                    language_codes=["en-IN", "hi-IN"],
                    model=model,
                    features=speech.RecognitionFeatures(
                        diarization_config=speech.SpeakerDiarizationConfig(
                            min_speaker_count=2,
                            max_speaker_count=2,
                        ),
                        enable_automatic_punctuation=True,
                    ),
                ),
            )
            r = c.recognize(request=req)
            txt = " | ".join(
                alt.transcript for res in r.results for alt in res.alternatives[:1]
            )
            print(f"OK   {model}/{loc}: {txt[:100]}")
        except Exception as e:
            print(f"FAIL {model}/{loc}: {str(e)[:120]}")


if __name__ == "__main__":
    main()
