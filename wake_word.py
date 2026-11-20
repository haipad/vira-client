"""Local Hey Jarvis wake-word detector for the desktop client."""

import os
import threading
import time
from collections.abc import Callable


class WakeWordDetector:
    def __init__(self, on_detected: Callable[[], None]):
        self._on_detected = on_detected
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        self._thread = threading.Thread(
            target=self._run,
            name="vira-wake-word",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        from onnx_wakeword import WakeWordEngine, get_demo_model

        try:
            engine = WakeWordEngine()
            engine.load(*get_demo_model())
            threshold = float(os.environ.get("JARVIS_WAKEWORD_THRESHOLD", "0.01"))
            device_index = int(os.environ.get("JARVIS_WAKEWORD_DEVICE", "1"))
            engine.set_threshold(threshold)
            engine.set_L1(False)
            engine.set_L3(True)
            engine.set_debug(True)
            print(f"[wake] listening locally for Hey Jarvis threshold={threshold} device={device_index}")

            def on_detect(word, probability, info):
                print(f"[wake] detected {word} ({probability:.0%})")
                self._on_detected()

            engine.start(on_detect, device=device_index)
            while not self._stop_event.is_set():
                time.sleep(0.2)
            engine.stop()
        except Exception as error:
            print(f"[wake] detector failed; Agora microphone stays muted: {error}")
