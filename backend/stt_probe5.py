"""Probe 5: diarization via BatchRecognize (v2's batch LRO method).
The sync recognize() rejects diarization ("Recognize does not support
Speaker Diarization"); batch is where v2 keeps that feature.
"""

import os
import time

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"


def main() -> None:
    wav = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")
    with open(wav, "rb") as f:
        content = f.read()

    c = speech.SpeechClient(client_options={"api_endpoint": "us-speech.googleapis.com"})
    rid = f"sirious-batch-{int(time.time()*10)%1000000}"
    op = c.create_recognizer(
        request=speech.CreateRecognizerRequest(
            parent=f"projects/{PROJECT}/locations/us",
            recognizer_id=rid,
            recognizer=speech.Recognizer(model="long", language_codes=["en-IN", "hi-IN"]),
        )
    )
    rec = op.result()
    print(f"recognizer: {rec.name}")
    try:
        op2 = c.batch_recognize(
            request=speech.BatchRecognizeRequest(
                recognizer=rec.name,
                files=[speech.BatchRecognizeFileMetadata(content=content)],
                config=speech.RecognitionConfig(
                    features=speech.RecognitionFeatures(
                        diarization_config=speech.SpeakerDiarizationConfig(
                            min_speaker_count=2, max_speaker_count=2
                        ),
                        enable_automatic_punctuation=True,
                    )
                ),
            )
        )
        resp = op2.result(timeout=180)
        # response shape: results is a map file->BatchRecognizeFileResultResponse
        print("response keys:", list(resp.result_fields()) if hasattr(resp, "result_fields") else type(resp))
        results = resp.results
        for key, file_res in results.items():
            tr = file_res.transcript
            alts = tr.alternatives if tr is not None else []
            if not alts:
                print(f"{key}: no transcript")
                continue
            words = list(alts[0].words)
            tags = sorted({w.speaker_label for w in words})
            print(f"{key}: words={len(words)} speakers={tags}")
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
    except Exception as e:
        print(f"FAIL {str(e)[:300]}")
    finally:
        try:
            c.delete_recognizer(name=rec.name)
        except Exception:
            pass


if __name__ == "__main__":
    main()
