"""Probe 4: named recognizers with model baked in (docs: feature support is
evaluated against the recognizer's model, not per-request overrides).
Tests: chirp_3/global (release notes say it has diarization) and long/us.
"""

import os
import time

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"
WAV = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")


def test(model: str, loc: str, langs: list[str]) -> None:
    with open(WAV, "rb") as f:
        content = f.read()
    ep = "speech.googleapis.com" if loc == "global" else f"{loc}-speech.googleapis.com"
    c = speech.SpeechClient(client_options={"api_endpoint": ep})
    parent = f"projects/{PROJECT}/locations/{loc}"
    rec_id = f"sirious-{model.replace('_','')}-{int(time.time())}"

    op = c.create_recognizer(
        request=speech.CreateRecognizerRequest(
            parent=parent,
            recognizer_id=rec_id,
            recognizer=speech.Recognizer(model=model, language_codes=langs),
        )
    )
    rec = op.result()
    print(f"[{model}/{loc}] recognizer: {rec.name}")

    try:
        r = c.recognize(
            request=speech.RecognizeRequest(
                recognizer=rec.name,
                config=speech.RecognitionConfig(
                    features=speech.RecognitionFeatures(
                        diarization_config=speech.SpeakerDiarizationConfig(
                            min_speaker_count=2, max_speaker_count=2
                        ),
                        enable_automatic_punctuation=True,
                    )
                ),
                content=content,
            )
        )
        words = [w for res in r.results for w in res.alternatives[0].words]
        tags = sorted({w.speaker_label for w in words})
        print(f"[{model}/{loc}] OK  words={len(words)} speakers={tags}")
        cur, buf = None, []
        for w in words:
            if w.speaker_label != cur:
                if buf:
                    print(f"   S{cur}: {' '.join(buf)}")
                cur, buf = w.speaker_label, [w.word]
            else:
                buf.append(w.word)
        if buf:
            print(f"   S{cur}: {' '.join(buf)}")
    except Exception as e:
        msg = str(e).replace("\n", " | ")
        print(f"[{model}/{loc}] FAIL {msg[:200]}")
    finally:
        try:
            c.delete_recognizer(name=rec.name)
        except Exception:
            pass


def main() -> None:
    test("chirp_3", "global", ["en-IN", "hi-IN"])
    test("long", "us", ["en-IN", "hi-IN"])


if __name__ == "__main__":
    main()
