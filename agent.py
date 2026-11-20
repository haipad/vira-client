"""
Agora voice pipeline — STT → custom LLM (veera-backend) → Murf TTS.
Edit this file to change vendors, models, or pipeline config.
"""

import os
import random

from agora_agent import Area, AsyncAgora
from agora_agent.agentkit import Agent as AgoraAgent
from agora_agent.agentkit.agent import (
    TurnDetectionConfig,
    TurnDetectionNestedConfig,
    StartOfSpeechConfig,
    StartOfSpeechVadConfig,
    EndOfSpeechConfig,
    EndOfSpeechVadConfig,
)
from agora_agent.agentkit.vendors.llm import OpenAI as AgoraOpenAI
from agora_agent.agentkit.vendors.stt import SarvamSTT
from agora_agent.agentkit.vendors.tts import MurfTTS
from agora_agent.agentkit.token import generate_convo_ai_token

AGORA_APP_ID = os.environ["AGORA_APP_ID"]
AGORA_APP_CERTIFICATE = os.environ["AGORA_APP_CERTIFICATE"]
SARVAM_API_KEY = os.environ["SARVAM_API_KEY"]
MURF_API_KEY = os.environ["MURF_API_KEY"]
VEERA_URL = os.environ.get("VEERA_PUBLIC_URL") or os.environ.get("VEERA_BACKEND_URL", "http://127.0.0.1:8010")
DESKTOP_KEY = os.environ.get("VEERA_DESKTOP_KEY", "")

_agora = AsyncAgora(area=Area.US, app_id=AGORA_APP_ID, app_certificate=AGORA_APP_CERTIFICATE)
_sessions: dict = {}

# VAD config — edit thresholds here
VAD_TURN_DETECTION = TurnDetectionConfig(
    config=TurnDetectionNestedConfig(
        start_of_speech=StartOfSpeechConfig(
            mode="vad",
            vad_config=StartOfSpeechVadConfig(
                prefix_padding_ms=300,
                interrupt_duration_ms=160,
            ),
        ),
        end_of_speech=EndOfSpeechConfig(
            mode="vad",
            vad_config=EndOfSpeechVadConfig(silence_duration_ms=480),
        ),
    )
)

def make_channel() -> dict:
    """Generate a fresh channel name + RTC token for the user."""
    uid = random.randint(1000, 9_999_999)
    channel = f"veera-{random.randint(1000, 9999)}"
    token = generate_convo_ai_token(
        app_id=AGORA_APP_ID,
        app_certificate=AGORA_APP_CERTIFICATE,
        channel_name=channel,
        uid=uid,
        token_expire=3600,
    )
    return {"app_id": AGORA_APP_ID, "channel": channel, "uid": uid, "token": token}


async def start(channel: str, user_uid: int, access_id: str, user_name: str = "User") -> str:
    """Start the voice pipeline and return the agent_id."""
    agent_uid = random.randint(10_000_000, 99_999_999)

    llm = AgoraOpenAI(
        api_key=DESKTOP_KEY or "veera-internal",
        base_url=f"{VEERA_URL}/v1/chat/completions",
        model="veera",
        headers={"ngrok-skip-browser-warning": "true"},
        system_messages=[{
            "role": "system",
            "content": f"User name: {user_name}. User access ID: {access_id}. Use this identity for this conversation.",
        }],
        greeting_message=None,
        failure_message="One moment please.",
        max_history=15,
    )
    stt = SarvamSTT(api_key=SARVAM_API_KEY, language="en-IN")
    tts = MurfTTS(
        key=MURF_API_KEY,
        voice_id="en-IN-abhinav",
        base_url="wss://global.api.murf.ai/v1/speech/stream-input",
        locale="en-IN",
        rate=0,
        pitch=0,
        model="FALCON",
        sample_rate=24000,
    )

    parameters = {
        "audio_scenario": "chorus",
        "data_channel": "rtm",
        "enable_error_message": True,
        "enable_metrics": True,
    }

    session = (
        AgoraAgent(
            client=_agora,
            instructions="You are Veera, a helpful voice assistant. Be concise.",
            greeting="Hi! I'm Veera. How can I help?",
            failure_message="One moment please.",
            max_history=50,
            turn_detection={
                "config": {
                    "speech_threshold": 0.5,
                    "start_of_speech": {
                        "mode": "vad",
                        "vad_config": {
                            "interrupt_duration_ms": 160,
                            "prefix_padding_ms": 300,
                        },
                    },
                    "end_of_speech": {
                        "mode": "vad",
                        "vad_config": {
                            "silence_duration_ms": 480,
                        },
                    },
                },
            },
            advanced_features={"enable_rtm": True, "enable_tools": True},
            parameters=parameters,
        )
        .with_stt(stt)
        .with_llm(llm)
        .with_tts(tts)
    ).create_async_session(
        channel=channel,
        agent_uid=str(agent_uid),
        remote_uids=[str(user_uid)],
        enable_string_uid=False,
        idle_timeout=60,
        expires_in=3600,
    )

    agent_id = await session.start()
    _sessions[agent_id] = {"session": session, "access_id": access_id, "agent_uid": agent_uid}
    return agent_id


async def say(agent_id: str, text: str, priority: str = "APPEND") -> None:
    """Speak text through the running agent's TTS voice; APPEND queues behind anything already being said."""
    entry = _sessions.get(agent_id)
    if entry:
        if priority not in ("INTERRUPT", "APPEND", "IGNORE"):
            priority = "APPEND"
        await entry["session"].say(text, priority=priority, interruptable=False)


async def stop(agent_id: str):
    """Stop a running voice session."""
    entry = _sessions.pop(agent_id, None)
    if entry:
        await entry["session"].stop()
    else:
        await _agora.stop_agent(agent_id)
