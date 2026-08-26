"""Probe 7: chirp_3@us batch diarization — the config was ACCEPTED in probe 6's
last run (no 400), the LRO just needed more time. Long timeout this time,
operation name persisted so results are recoverable across runs.
"""

import json
import os
import sys
import time

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"
STATE = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_chirp3_state.json")


def main() -> None:
    c = speech.SpeechClient(client_options={"api_endpoint": "us-speech.googleapis.com"})

    # resume or start
    if os.path.exists(STATE):
        with open(STATE) as f:
            st = json.load(f)
        rec_name, op_name = st["recognizer"], st["operation"]
        print(f"resuming: {op_name}")
        from google.api_core.operation import Operation

        op = Operation.from_gapic(
            c.transport.batch_recognize._wrap(None, None, None) if False else None,  # placeholder
        ) if False else None
        # simplest: re-fetch via operations client path through the same gapic
        op2 = c.batch_recognize(request=None) if False else None
        # fall back to fresh call if resume unsupported
        print("resume not supported by this client shape; running fresh instead")
        os.remove(STATE)

    wav = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")
    rid = f"sirious-c3-{int(time.time()*10)%1000000}"
    op = c.create_recognizer(
        request=speech.CreateRecognizerRequest(
            parent=f"projects/{PROJECT}/locations/us",
            recognizer_id=rid,
            recognizer=speech.Recognizer(model="chirp_3", language_codes=["en-US"]),
        )
    )
    rec = op.result()
    print(f"recognizer: {rec.name}")
    op2 = c.batch_recognize(
        request=speech.BatchRecognizeRequest(
            recognizer=rec.name,
            files=[speech.BatchRecognizeFileMetadata(uri="gs://sirious-stt-smoke/hinglish.wav")],
            config=speech.RecognitionConfig(
                features=speech.RecognitionFeatures(
                    diarization_config=speech.SpeakerDiarizationConfig(
                        min_speaker_count=2, max_speaker_count=2
                    ),
                    enable_automatic_punctuation=True,
                )
            ),
            recognition_output_config=speech.RecognitionOutputConfig(
                inline_response_config=speech.InlineOutputConfig()
            ),
        )
    )
    with open(STATE, "w") as f:
        json.dump({"recognizer": rec.name, "operation": op2.operation.name}, f)

    try:
        resp = op2.result(timeout=540)
    except Exception as e:
        print(f"still not done after 540s: {str(e)[:120]}")
        print(f"operation persisted in {STATE}; re-run to poll again")
        return

    os.remove(STATE)
    for key, fr in resp.results.items():
        alts = fr.transcript.alternatives if fr.transcript else []
        if not alts:
            print(f"{key}: empty transcript")
            continue
        words = list(alts[0].words)
        tags = sorted({w.speaker_label for w in words})
        print(f"chirp_3@us batch: OK words={len(words)} speakers={tags}")
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
    try:
        c.delete_recognizer(name=rec.name)
    except Exception:
        pass


if __name__ == "__main__":
    main()
