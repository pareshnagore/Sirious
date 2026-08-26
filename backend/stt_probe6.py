"""Probe 6: THE C1-path test — StreamingRecognize + diarization + en-IN/hi-IN.
Feeds the synthetic 2-voice WAV like a mic (3200-byte chunks) and prints
diarized interim/final results. This is exactly what ambient mode will do.
"""

import asyncio
import os
import time

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"
CHUNK = 3200
GAP = 0.1


async def main() -> None:
    wav = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")
    with open(wav, "rb") as f:
        content = f.read()

    c = speech.SpeechClient(client_options={"api_endpoint": "us-speech.googleapis.com"})
    rid = f"sirious-stream-{int(time.time()*10)%1000000}"
    op = c.create_recognizer(
        request=speech.CreateRecognizerRequest(
            parent=f"projects/{PROJECT}/locations/us",
            recognizer_id=rid,
            recognizer=speech.Recognizer(model="short", language_codes=["en-IN", "hi-IN"]),
        )
    )
    rec = op.result()
    print(f"recognizer: {rec.name}")

    cfg = speech.RecognitionConfig(
        features=speech.RecognitionFeatures(
            diarization_config=speech.SpeakerDiarizationConfig(
                min_speaker_count=2, max_speaker_count=2
            ),
            enable_automatic_punctuation=True,
        )
    )
    streaming_cfg = speech.StreamingRecognizeRequest(
        recognizer=rec.name,
        streaming_config=speech.StreamingRecognitionConfig(
            config=cfg,
            streaming_features=speech.StreamingRecognitionFeatures(
                interim_results=True
            ),
        ),
    )

    def requests():
        yield streaming_cfg
        for i in range(0, len(content), CHUNK):
            yield speech.StreamingRecognizeRequest(audio=content[i : i + CHUNK])

    try:
        responses = c.streaming_recognize(requests=requests())
        got_words = False
        for resp in responses:
            for res in resp.results:
                if not res.alternatives:
                    continue
                alt = res.alternatives[0]
                words = list(alt.words) if alt.words else []
                if words:
                    got_words = True
                    tags = sorted({w.speaker_label for w in words})
                    txt = " ".join(w.word for w in words)
                    print(f"[{'final' if res.is_final else 'interim'}] speakers={tags} :: {txt[:110]}")
        if not got_words:
            print("stream closed without word-level results")
    except Exception as e:
        print(f"FAIL {str(e)[:300]}")
    finally:
        try:
            c.delete_recognizer(name=rec.name)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
