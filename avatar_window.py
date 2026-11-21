"""
Avatar desktop widget with a sprite-based face.
States: idle | listening | speaking
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlencode

# Apply before QWebEngineView/QApplication initializes Chromium.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--autoplay-policy=no-user-gesture-required")
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QUrl
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QPlainTextEdit
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

BACKEND = os.environ.get("VEERA_PUBLIC_URL", "http://localhost:8000")

# ── Avatar shape widget ────────────────────────────────────────────────────────

AVATAR_ASSETS = Path(__file__).resolve().parent / "assets" / "avatar"
OPEN_FRAME_X_OFFSET = 50

class AvatarShape(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._state = "idle"
        self._mouth_open = False
        self._frame_index = 0
        self._frame_delays = (130, 190, 120, 220)
        self._closed_frame = QPixmap(str(AVATAR_ASSETS / "face_closed.png"))
        self._open_frame = QPixmap(str(AVATAR_ASSETS / "face_open.png"))
        self._speech_timer = QTimer(self)
        self._speech_timer.timeout.connect(self._advance_speech_frame)

    def set_state(self, state: str):
        if state not in ("idle", "listening", "speaking") or state == self._state:
            return
        was_speaking = self._state == "speaking"
        self._state = state
        if state == "speaking":
            self._mouth_open = False
            self._frame_index = 0
            self._speech_timer.start(self._frame_delays[0])
        elif was_speaking:
            self._speech_timer.stop()
            self._mouth_open = False
        self.update()

    def _advance_speech_frame(self):
        if self._state != "speaking":
            self._speech_timer.stop()
            return
        self._mouth_open = not self._mouth_open
        self._frame_index = (self._frame_index + 1) % len(self._frame_delays)
        self._speech_timer.start(self._frame_delays[self._frame_index])
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        sprite = self._open_frame if self._state == "speaking" and self._mouth_open else self._closed_frame
        if sprite.isNull():
            return
        scaled = sprite.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        if self._state == "speaking" and self._mouth_open:
            x += round(OPEN_FRAME_X_OFFSET * scaled.width() / sprite.width())
        y = (self.height() - scaled.height()) // 2
        p.drawPixmap(x, y, scaled)


# ── Main window ────────────────────────────────────────────────────────────────

class AvatarWebPage(QWebEnginePage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.permissionRequested.connect(self._permission_requested)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        print(f"[vira-web:{source_id}:{line_number}] {message}")

    def _permission_requested(self, permission):
        if permission.permissionType() == permission.PermissionType.MediaAudioCapture:
            print("[vira-web] granting microphone permission for", permission.origin().toString())
            permission.grant()
        else:
            permission.deny()

    def featurePermissionRequested(self, security_origin, feature):
        if feature == QWebEnginePage.Feature.MediaAudioCapture:
            self.setFeaturePermission(
                security_origin,
                feature,
                QWebEnginePage.PermissionPolicy.PermissionGrantedByUser,
            )
        else:
            super().featurePermissionRequested(security_origin, feature)


class AgoraStateBridge(QObject):
    stateChanged = pyqtSignal(str)

    @pyqtSlot(str)
    def setState(self, state: str):
        if state in ("idle", "listening", "speaking"):
            self.stateChanged.emit(state)


class TranscriptPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            TranscriptPanel { background: rgba(25, 30, 38, 235); border-radius: 6px; }
            QLabel { color: #b7c8c9; font-size: 11px; font-weight: 600; }
            QPlainTextEdit { color: #ffffff; background: transparent; border: 0; font-size: 12px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        self.fields = {}
        for speaker, label in (("user", "You"), ("assistant", "Vira")):
            layout.addWidget(QLabel(label))
            field = QPlainTextEdit()
            field.setReadOnly(True)
            field.setFixedHeight(52)
            field.setPlaceholderText("Waiting..." if speaker == "user" else "")
            layout.addWidget(field)
            self.fields[speaker] = field

    def set_transcript(self, speaker: str, text: str):
        if speaker not in self.fields:
            return
        if speaker == "user":
            self.fields["assistant"].clear()
        field = self.fields[speaker]
        field.setPlainText(text)
        field.verticalScrollBar().setValue(0)


class AvatarWindow(QWidget):
    # emitted when background WebSocket callbacks need GUI-thread operations
    say_requested = pyqtSignal(str)
    state_requested = pyqtSignal(str)
    transcript_requested = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.say_requested.connect(self._play_say_slot)
        self.state_requested.connect(self._set_state_slot)
        self.transcript_requested.connect(self._set_transcript_slot)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(340, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.shape = AvatarShape(self)
        self.shape.setFixedSize(250, 260)
        layout.addWidget(self.shape, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.transcript = TranscriptPanel(self)
        layout.addWidget(self.transcript)

        # hidden WebEngine for Agora audio
        self._web = QWebEngineView(self)
        web_settings = self._web.settings()
        web_settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self._web.setPage(AvatarWebPage(self._web))
        self._web.loadFinished.connect(self._on_load_finished)
        self._web.setFixedSize(1, 1)
        layout.addWidget(self._web)
        self._web.lower()

        self._agora_bridge = AgoraStateBridge(self)
        self._agora_bridge.stateChanged.connect(self.set_state)
        self._web_channel = QWebChannel(self._web.page())
        self._web_channel.registerObject("viraBridge", self._agora_bridge)
        self._web.page().setWebChannel(self._web_channel)

        # drag support
        self._drag_pos = None

        # position bottom-right
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20,
                  screen.bottom() - self.height() - 20)

    def load_audio(self, client_id: str, rtc_config: dict | None = None, access_id: str = "vira-desktop", user_name: str = "User"):
        backend = os.environ.get("VEERA_BACKEND_URL", "http://127.0.0.1:8010")
        local_url = os.environ.get("VIRA_LOCAL_URL", "http://127.0.0.1:8021")
        access_id = os.environ.get("VEERA_ACCESS_ID", "vira-desktop")
        user_name = os.environ.get("VEERA_USER_NAME", "User")
        url = QUrl(f"{local_url}/vira.html")
        params = {
            "backend": backend,
            "client_id": client_id,
            "access_id": access_id,
            "user_name": user_name,
        }
        if rtc_config:
            print(
                "[vira-client] RTC app_id:", rtc_config["app_id"],
                "channel:", rtc_config["channel"],
                "uid:", rtc_config["uid"],
                "agent_uid:", rtc_config["agent_uid"],
            )
            params.update({
                "app_id": str(rtc_config["app_id"]),
                "channel": rtc_config["channel"],
                "uid": str(rtc_config["uid"]),
                "agent_uid": str(rtc_config["agent_uid"]),
                "token": rtc_config["token"],
            })
        # urlencode escapes '+' in the token; Qt would leave it raw and JS would decode it as a space.
        url.setQuery(urlencode(params))
        self._web.load(url)

    def _on_load_finished(self, success: bool):
        print(f"[vira-web] page load {'succeeded' if success else 'failed'}")

    def set_state(self, state: str):
        self.shape.set_state(state)

    @pyqtSlot(str)
    def _set_state_slot(self, state: str):
        self.set_state(state)

    @pyqtSlot(str, str)
    def _set_transcript_slot(self, speaker: str, text: str):
        self.transcript.set_transcript(speaker, text)

    def play_say(self, text: str):
        self.say_requested.emit(text)

    @pyqtSlot(str)
    def _play_say_slot(self, text: str):
        import json
        payload = json.dumps(text)
        self._web.page().runJavaScript(f"playFallbackSpeech({payload})")

    # drag to reposition
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e):
        QApplication.quit()
