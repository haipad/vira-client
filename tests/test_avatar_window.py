import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

from avatar_window import AvatarShape, TranscriptPanel


class AvatarAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_speaking_frames_have_aligned_visible_bounds(self):
        avatar = AvatarShape()
        avatar.resize(220, 259)

        def horizontal_bounds():
            rendered = QPixmap(avatar.size())
            rendered.fill(Qt.GlobalColor.transparent)
            avatar.render(rendered, flags=QWidget.RenderFlag.DrawChildren)
            image = rendered.toImage()
            visible = [x for x in range(image.width())
                       if any(image.pixelColor(x, y).alpha() > 0 for y in range(image.height()))]
            return min(visible), max(visible)

        avatar.set_state("speaking")
        closed_bounds = horizontal_bounds()
        avatar._advance_speech_frame()
        open_bounds = horizontal_bounds()
        avatar.set_state("idle")

        self.assertLessEqual(abs(closed_bounds[0] - open_bounds[0]), 1)
        self.assertLessEqual(abs(closed_bounds[1] - open_bounds[1]), 1)

    def test_transcript_displays_both_speakers_and_clears_the_previous_reply(self):
        panel = TranscriptPanel()
        panel.set_transcript('user', 'What is next?')
        panel.set_transcript('assistant', 'A meeting at noon.')
        self.assertEqual(panel.fields['user'].toPlainText(), 'What is next?')
        self.assertEqual(panel.fields['assistant'].toPlainText(), 'A meeting at noon.')
        panel.set_transcript('user', 'Thank you.')
        self.assertEqual(panel.fields['assistant'].toPlainText(), '')