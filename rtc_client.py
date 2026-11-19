"""
RTC audio client — joins Agora channel, streams mic in, plays agent audio out.
Uses agora-python-server-sdk (agora_realtime_ai_api) + sounddevice.
"""

import asyncio
import os

import numpy as np
import sounddevice as sd
from agora_realtime_ai_api.rtc import Channel, RtcEngine, RtcOptions

_RTC_AVAILABLE = True

AGORA_APP_ID = os.environ["AGORA_APP_ID"]
AGORA_APP_CERTIFICATE = os.environ["AGORA_APP_CERTIFICATE"]

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 20  # 20ms frames
SAMPLES_PER_FRAME = int(SAMPLE_RATE * FRAME_MS / 1000)

_engine: RtcEngine | None = None
_channel: Channel | None = None


async def start(channel_name: str, uid: int, agent_uid: int):
    global _engine, _channel

    _engine = RtcEngine(appid=AGORA_APP_ID, appcert=AGORA_APP_CERTIFICATE)
    options = RtcOptions(
        channel_name=channel_name,
        uid=uid,
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    _channel = _engine.create_channel(options)
    await _channel.connect()
    asyncio.create_task(_mic_loop())


async def stop():
    global _engine, _channel
    if _channel:
        await _channel.disconnect()
        _channel = None
    if _engine:
        _engine.destroy()
        _engine = None


async def _mic_loop():
    """Capture mic and push PCM frames into the channel."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def callback(indata, frames, time, status):
        pcm = (indata * 32767).astype(np.int16).tobytes()
        loop.call_soon_threadsafe(queue.put_nowait, pcm)

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=SAMPLES_PER_FRAME,
        callback=callback,
    ):
        while _channel:
            pcm = await queue.get()
            await _channel.push_audio_frame(pcm)


async def _playback_loop(agent_uid: int):
    """Receive agent audio frames and play them out."""
    stream = sd.OutputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
    )
    stream.start()

    audio_stream = None
    while _channel and audio_stream is None:
        audio_stream = _channel.get_audio_frames(agent_uid)
        await asyncio.sleep(0.1)

    if audio_stream:
        async for frame in audio_stream:
            pcm = np.frombuffer(frame.data, dtype=np.int16)
            stream.write(pcm)

    stream.stop()
    stream.close()
