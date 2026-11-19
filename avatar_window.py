"""
Avatar desktop widget.
Frameless, always-on-top window with an animated shape.
States: idle | listening | speaking

Swap out AvatarShape.paintEvent() with any 2D character renderer later.
"""

import os
import sys

# Apply before QWebEngineView/QApplication initializes Chromium.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--autoplay-policy=no-user-gesture-required")
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty, pyqtSignal, pyqtSlot, QObject, QUrl, QUrlQuery
from PyQt6.QtGui import QPainter, QColor, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

BACKEND = os.environ.get("VEERA_PUBLIC_URL", "http://localhost:8000")

# ── Avatar shape widget ────────────────────────────────────────────────────────

STATE_COLORS = {
    "idle":      QColor(80, 120, 220),
    "listening": QColor(60, 200, 120),
    "speaking":  QColor(220, 100, 60),
}

class AvatarShape(QWidget):
    """
    A pulsing circle. Replace paintEvent() with your 2D character later.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._state = "idle"
        self._pulse = 0.0          # 0.0 → 1.0, drives size oscillation

        self._anim = QPropertyAnimation(self, b"pulse")
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.SineCurve)
        self._anim.setLoopCount(-1)
        self._anim.start()

    @pyqtProperty(float)
    def pulse(self):
        return self._pulse

    @pulse.setter
    def pulse(self, v):
        self._pulse = v
        self.update()

    def set_state(self, state: str):
        self._state = state
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2

        base_r = min(w, h) * 0.30
        pulse_r = base_r + self._pulse * base_r * 0.18

        color = STATE_COLORS.get(self._state, STATE_COLORS["idle"])

        # outer glow
        grad = QRadialGradient(cx, cy, pulse_r * 1.4)
        glow = QColor(color)
        glow.setAlpha(40)
        grad.setColorAt(0, glow)
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(cx - pulse_r * 1.4), int(cy - pulse_r * 1.4),
                      int(pulse_r * 2.8), int(pulse_r * 2.8))

        # main circle
        p.setBrush(color)
        p.drawEllipse(int(cx - pulse_r), int(cy - pulse_r),
                      int(pulse_r * 2), int(pulse_r * 2))

        # face dots (eyes) — remove when you add a real character
        eye_color = QColor(255, 255, 255, 200)
        p.setBrush(eye_color)
        eye_r = pulse_r * 0.10
        p.drawEllipse(int(cx - pulse_r * 0.25 - eye_r), int(cy - pulse_r * 0.15 - eye_r),
                      int(eye_r * 2), int(eye_r * 2))
        p.drawEllipse(int(cx + pulse_r * 0.25 - eye_r), int(cy - pulse_r * 0.15 - eye_r),
                      int(eye_r * 2), int(eye_r * 2))

        # mouth — wiggles when speaking
        mouth_w = pulse_r * 0.4
        mouth_y = cy + pulse_r * 0.25
        p.setPen(QColor(255, 255, 255, 180))
        if self._state == "speaking":
            mouth_h = pulse_r * 0.12 + self._pulse * pulse_r * 0.10
            p.setBrush(QColor(255, 255, 255, 180))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(int(cx - mouth_w / 2), int(mouth_y),
                          int(mouth_w), int(mouth_h * 2))
        else:
            from PyQt6.QtCore import QRectF
            from PyQt6.QtGui import QPen
            pen = QPen(QColor(255, 255, 255, 180), 2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(int(cx - mouth_w / 2), int(mouth_y - mouth_w * 0.3),
                      int(mouth_w), int(mouth_w * 0.6), 0, -180 * 16)


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


class AvatarWindow(QWidget):
    # emitted when background WebSocket callbacks need GUI-thread operations
    say_requested = pyqtSignal(str)
    state_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.say_requested.connect(self._play_say_slot)
        self.state_requested.connect(self._set_state_slot)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(220, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.shape = AvatarShape(self)
        layout.addWidget(self.shape)

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

    def load_audio(self, client_id: str):
        backend = os.environ.get("VEERA_BACKEND_URL", "http://127.0.0.1:8000")
        url = QUrl(f"{backend}/vira.html")
        query = QUrlQuery()
        query.addQueryItem("backend", backend)
        query.addQueryItem("client_id", client_id)
        url.setQuery(query)
        self._web.load(url)

    def _on_load_finished(self, success: bool):
        print(f"[vira-web] page load {'succeeded' if success else 'failed'}")

    def set_state(self, state: str):
        self.shape.set_state(state)

    @pyqtSlot(str)
    def _set_state_slot(self, state: str):
        self.set_state(state)

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
