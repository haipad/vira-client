# Local Wake Word Setup

The desktop uses the bundled `onnx-wakeword` Hey Jarvis demo.

The Agora agent stays joined, but its published microphone track remains muted
until Hey Jarvis is detected locally. After detection, the mic is unmuted for
30 seconds.

Install the desktop dependency:

```powershell
pip install -r requirements.txt
```

A future Hey Vira model should use a compatible `model_info.json`/ONNX bundle and
replace the demo in `WakeWordEngine.load(...)`.