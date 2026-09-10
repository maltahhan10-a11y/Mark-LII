"""The live conversation on a phone call, over the audio bridge.

A dedicated Gemini Live session, deliberately separate from the one running
the user's own voice loop. Two reasons: the call needs different audio devices
(the bridge, not the built-in mic and speakers), and it needs a different set
of instructions -- it is talking to a stranger on behalf of someone, not to
the person who owns the machine. Sharing one session would mean the user's
next sentence arrives in the middle of somebody else's phone call.

Audio here runs at telephone quality by necessity. The bridge carries whatever
the cellular codec already degraded, so there is nothing to gain from a wider
band and something to lose in latency.
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types as gtypes

from core import audio_devices, models

SEND_RATE = 16_000       # what Live wants from a microphone
RECV_RATE = 24_000       # what Live returns
CHUNK = 1_024

# Said by the other side, this ends the call. Matched on the transcript rather
# than on audio so a noisy line cannot hang up by accident.
_GOODBYE = re.compile(
    r"\b(goodbye|bye now|bye bye|talk (?:to you )?later|have a good (?:day|night)|"
    r"take care|thanks,? bye|that'?s all)\b", re.I)


def _voice() -> str:
    try:
        from core.voice import get_voice
        return get_voice()
    except Exception:
        return "Charon"


def run(display: str, purpose: str, player=None, timeout: float = 300.0) -> str:
    """Talk to whoever answered until the call ends. Returns a summary."""
    from actions import phone_call

    in_name, out_name = phone_call.bridge_devices()
    mic = audio_devices.resolve(in_name, "input")
    spk = audio_devices.resolve(out_name, "output")

    owner = str(models.config().get("owner_name", "")).strip()
    prompt = phone_call.call_instructions(display, purpose, owner)

    box: dict = {"transcript": [], "error": None, "ended": False}
    stop = threading.Event()

    def log(msg: str) -> None:
        print(f"[Call] {msg}")
        if player is not None:
            try:
                player.write_log(f"Jarvis: {msg}")
            except Exception:
                pass

    def worker() -> None:
        try:
            asyncio.run(_session(prompt, mic, spk, box, stop, timeout, log))
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=worker, daemon=True, name="phone-call")
    t.start()
    t.join(timeout + 20)
    stop.set()

    if box["error"]:
        return f"The call ran into trouble: {box['error']}"
    said = " ".join(box["transcript"]).strip()
    if not said:
        return f"The call to {display} connected but nothing was said."
    return f"Call with {display} finished. {said[:600]}"


async def _session(prompt, mic, spk, box, stop, timeout, log) -> None:
    keys = models.api_keys()
    if not keys:
        raise RuntimeError("no API key for the call")

    client = genai.Client(api_key=keys[0], http_options={"api_version": "v1beta"})
    config = gtypes.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        system_instruction=prompt,
        speech_config=gtypes.SpeechConfig(
            voice_config=gtypes.VoiceConfig(
                prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(voice_name=_voice())
            )
        ),
    )

    mic_q: queue.Queue = queue.Queue(maxsize=60)
    started = time.time()

    def on_audio(indata, _frames, _t, _status):
        try:
            mic_q.put_nowait(bytes(indata))
        except queue.Full:
            pass          # a dropped frame is better than a stalled call

    async with client.aio.live.connect(
            model=models.for_task("live"), config=config) as session:
        log("Connected — Jarvis is on the call.")

        # Speak first. The callee said "hello?" into silence, and a synthetic
        # voice that waits to be prompted sounds like a dead line.
        await session.send_client_content(
            turns={"parts": [{"text": "The call has been answered. Greet them now."}]},
            turn_complete=True)

        stream = sd.InputStream(samplerate=SEND_RATE, channels=1, dtype="int16",
                                blocksize=CHUNK, device=mic, callback=on_audio)
        out = sd.RawOutputStream(samplerate=RECV_RATE, channels=1, dtype="int16",
                                 blocksize=CHUNK, device=spk)
        stream.start(); out.start()

        async def pump_mic():
            while not stop.is_set() and time.time() - started < timeout:
                try:
                    chunk = await asyncio.to_thread(mic_q.get, True, 0.3)
                except queue.Empty:
                    continue
                except Exception:
                    break
                try:
                    await session.send_realtime_input(
                        audio=gtypes.Blob(data=chunk, mime_type=f"audio/pcm;rate={SEND_RATE}"))
                except Exception:
                    break

        async def pump_out():
            async for response in session.receive():
                if stop.is_set():
                    break
                if response.data:
                    await asyncio.to_thread(out.write, response.data)
                sc = response.server_content
                if not sc:
                    continue
                if sc.output_transcription and sc.output_transcription.text:
                    box["transcript"].append(sc.output_transcription.text.strip())
                if sc.input_transcription and sc.input_transcription.text:
                    heard = sc.input_transcription.text.strip()
                    if heard:
                        box["transcript"].append(f"[them] {heard}")
                        if _GOODBYE.search(heard):
                            box["ended"] = True
                            stop.set()
                            break

        try:
            await asyncio.wait_for(
                asyncio.gather(pump_mic(), pump_out()), timeout=timeout + 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            for s in (stream, out):
                try:
                    s.stop(); s.close()
                except Exception:
                    pass

    from actions import phone_call
    phone_call.hangup()
    log("Call ended.")
