"""C1 smoke test: synthesize a 2-voice Hinglish sample -> batch STT with diarization.

Verifies (before any client work): credentials, API enablement, model choice,
diarization output shape, and language behavior on code-switched audio.
Usage: python backend/stt_smoke.py [--keep-sample]
"""

import asyncio
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.stt import transcribe_file_with_diarization  # noqa: E402

# 2-voice Hinglish conversation (office flavor, deliberate code-switching)
TURNS = [
    ("en-IN-NeerjaNeural", "Hey, did you finish the deployment for the billing service?"),
    ("hi-IN-MadhurNeural", "Haan, ho gaya. Staging pe deploy kar diya hai."),
    ("en-IN-NeerjaNeural", "Nice. And what did the client say about the pricing change?"),
    ("hi-IN-MadhurNeural", "Unhone kaha ki naya pricing theek hai, lekin contract next week chahiye."),
    ("en-IN-NeerjaNeural", "Okay, I will send them the updated contract by tomorrow morning."),
    ("hi-IN-MadhurNeural", "Theek hai. Ek aur baat — sprint review Friday ko hai, mat bhoolna."),
]


async def synth(out_wav: str) -> None:
    import edge_tts

    tmp = tempfile.mkdtemp(prefix="sirious_stt_")
    parts = []
    for i, (voice, text) in enumerate(TURNS):
        mp3 = os.path.join(tmp, f"t{i}.mp3")
        await edge_tts.Communicate(text, voice).save(mp3)
        wav = os.path.join(tmp, f"t{i}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
             "-ar", "16000", "-ac", "1", wav],
            check=True,
        )
        parts.append(wav)

    # concat with 0.4s silence between turns
    sil = os.path.join(tmp, "sil.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "0.4", sil],
        check=True,
    )
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\nfile '{sil}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", lst, "-ar", "16000", "-ac", "1", out_wav],
        check=True,
    )
    print(f"sample: {out_wav}")


def main() -> None:
    out = os.path.join(tempfile.gettempdir(), "sirious_hinglish.wav")
    asyncio.run(synth(out))
    model, utts = transcribe_file_with_diarization(out)
    print(f"model: {model}\n--- diarized transcript ---")
    for u in utts:
        print(f"S{u.speaker_tag} [{u.start:5.1f}-{u.end:5.1f}] {u.text}")
    speakers = {u.speaker_tag for u in utts}
    print(f"--- {len(utts)} utterances, {len(speakers)} speaker tag(s): {sorted(speakers)}")


if __name__ == "__main__":
    main()
