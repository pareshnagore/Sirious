"""Probe 3: isolate why v2 rejects speaker_diarization.
Matrix: language count (1 vs 2) x word_time_offsets (on/off) x model.
"""

import os

from google.cloud import speech_v2 as speech

PROJECT = "sirious-2026"


def try_combo(model: str, loc: str, langs: list[str], wto: bool) -> None:
    wav = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "sirious_hinglish.wav")
    with open(wav, "rb") as f:
        content = f.read()
    ep = "speech.googleapis.com" if loc == "global" else f"{loc}-speech.googleapis.com"
    c = speech.SpeechClient(client_options={"api_endpoint": ep})
    req = speech.RecognizeRequest(
        recognizer=f"projects/{PROJECT}/locations/{loc}/recognizers/_",
        config=speech.RecognitionConfig(
            language_codes=langs,
            model=model,
            features=speech.RecognitionFeatures(
                diarization_config=speech.SpeakerDiarizationConfig(
                    min_speaker_count=2, max_speaker_count=2
                ),
                enable_word_time_offsets=wto,
                enable_automatic_punctuation=True,
            ),
        ),
        content=content,
    )
    tag = f"{model}/{loc} langs={len(langs)} wto={wto}"
    try:
        r = c.recognize(request=req)
        words = [w for res in r.results for w in res.alternatives[0].words]
        tags = sorted({w.speaker_label for w in words})
        print(f"OK   {tag}  speakers={tags}")
    except Exception as e:
        msg = str(e)
        if "does not support feature" in msg:
            print(f"FAIL {tag}  (feature unsupported)")
        else:
            print(f"FAIL {tag}  {msg[:100]}")


def main() -> None:
    try_combo("short", "us", ["en-IN"], False)
    try_combo("short", "us", ["en-IN"], True)
    try_combo("short", "us", ["en-IN", "hi-IN"], True)
    try_combo("latest_short", "us", ["en-IN"], True)
    try_combo("short", "global", ["en-IN"], True)
    try_combo("latest_long", "us", ["en-IN"], True)


if __name__ == "__main__":
    main()
