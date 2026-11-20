import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import actions


class UrlSafetyTests(unittest.TestCase):
    def test_public_sites_are_accepted_and_bare_names_get_https(self):
        self.assertEqual(actions.check_url('https://www.youtube.com'), 'https://www.youtube.com')
        self.assertEqual(actions.check_url('youtube.com/watch?v=1'), 'https://youtube.com/watch?v=1')

    def test_empty_url_opens_the_browser_home(self):
        self.assertEqual(actions.check_url(None), actions.BROWSER_HOME)
        self.assertEqual(actions.check_url('  '), actions.BROWSER_HOME)

    def test_dangerous_or_local_targets_are_rejected(self):
        for bad in (
            'file:///C:/Windows/System32/cmd.exe', 'javascript:alert(1)', 'ms-msdt:/id', 'ftp://x.com',
            'http://localhost:8010', 'http://127.0.0.1', 'http://192.168.1.1', 'http://10.0.0.5',
            'http://169.254.169.254/latest', 'https://user:pass@evil.com', 'http://printer.local',
            'https://intranet', 'http://[::1]/',
        ):
            with self.assertRaises(ValueError, msg=bad):
                actions.check_url(bad)

    def test_open_site_reports_failures_without_launching(self):
        with patch.object(actions.webbrowser, 'open') as opener:
            result = actions.open_site('file:///C:/secret.txt')
        self.assertFalse(result['ok'])
        opener.assert_not_called()

    def test_open_site_launches_a_validated_url(self):
        with patch.object(actions.webbrowser, 'open', return_value=True) as opener:
            result = actions.open_site('github.com')
        opener.assert_called_once_with('https://github.com')
        self.assertTrue(result['ok'])

    def test_open_site_reports_when_no_browser_launches(self):
        with patch.object(actions.webbrowser, 'open', return_value=False):
            self.assertFalse(actions.open_site('github.com')['ok'])


class FileSearchTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _touch(self, relative, mtime=None):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x')
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_exact_name_beats_partial_matches(self):
        self._touch('resume_old_draft.pdf')
        exact = self._touch('Resume.pdf')

        self.assertEqual(actions.find_file('resume', [self.root]), exact)

    def test_spoken_name_matches_despite_case_spaces_and_punctuation(self):
        target = self._touch('Q3 Budget-Final.xlsx')

        self.assertEqual(actions.find_file('q3 budget final', [self.root]), target)

    def test_newest_wins_among_equal_matches(self):
        self._touch('notes/report.pdf', mtime=1_000_000)
        newest = self._touch('archive/report.pdf', mtime=2_000_000)

        self.assertEqual(actions.find_file('report', [self.root]), newest)

    def test_an_extension_in_the_request_narrows_the_match(self):
        self._touch('budget.pdf')
        sheet = self._touch('budget.xlsx')

        self.assertEqual(actions.find_file('budget.xlsx', [self.root]), sheet)

    def test_executables_and_scripts_are_never_opened(self):
        for name in ('setup.exe', 'run.bat', 'task.ps1', 'link.lnk', 'macro.vbs', 'tool.py'):
            self._touch(name)

        for query in ('setup', 'run', 'task', 'link', 'macro', 'tool'):
            self.assertIsNone(actions.find_file(query, [self.root]), query)

    def test_junk_folders_and_deep_nesting_are_skipped(self):
        self._touch('node_modules/pkg/readme.md')
        self._touch('a/b/c/d/e/f/deep.pdf')

        self.assertIsNone(actions.find_file('readme', [self.root]))
        self.assertIsNone(actions.find_file('deep', [self.root]))

    def test_no_match_or_blank_query_finds_nothing(self):
        self._touch('resume.pdf')

        self.assertIsNone(actions.find_file('passport', [self.root]))
        self.assertIsNone(actions.find_file('  !! ', [self.root]))

    def test_open_file_launches_the_match_and_reports_the_name(self):
        target = self._touch('Resume.pdf')

        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('resume')

        launch.assert_called_once_with(target)
        self.assertEqual((result['ok'], result['file']), (True, 'Resume.pdf'))

    def test_open_file_failure_does_not_launch_anything(self):
        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('passport')

        launch.assert_not_called()
        self.assertFalse(result['ok'])


class ActionRunnerTests(unittest.TestCase):
    def test_unknown_and_malformed_actions_fail_safely(self):
        for bad in ({'action': 'run_command', 'cmd': 'calc'}, {}, 'open_browser', None):
            self.assertFalse(actions.run_action(bad)['ok'], bad)

    def test_an_exception_inside_an_action_becomes_a_failure(self):
        with patch.object(actions, 'take_screenshot', side_effect=RuntimeError('boom')):
            result = actions.run_action({'action': 'take_screenshot'})
        self.assertEqual(result, {'ok': False, 'error': 'boom'})

    def test_outcome_speaks_the_matching_message_and_fills_in_the_file_name(self):
        action = {'success_message': 'Opening {file} for you.', 'failure_message': "Couldn't find it."}

        self.assertEqual(actions.outcome_text(action, {'ok': True, 'file': 'Resume.pdf'}), 'Opening Resume.pdf for you.')
        self.assertEqual(actions.outcome_text(action, {'ok': False}), "Couldn't find it.")

    def test_failure_message_names_the_requested_file_when_nothing_was_matched(self):
        action = {'name': 'passport', 'failure_message': 'I could not find {file}.'}

        self.assertEqual(actions.outcome_text(action, {'ok': False}), 'I could not find passport.')

    def test_outcome_has_safe_defaults_and_a_length_cap(self):
        self.assertEqual(actions.outcome_text({}, {'ok': True}), 'Done.')
        self.assertEqual(actions.outcome_text({}, {'ok': False}), "Sorry, that didn't work.")
        self.assertEqual(len(actions.outcome_text({'success_message': 'x' * 900}, {'ok': True})), actions.MAX_MESSAGE_CHARS)

    def test_screenshot_is_saved_in_the_pictures_folder(self):
        with tempfile.TemporaryDirectory() as pictures, patch.object(actions, 'known_folder', return_value=Path(pictures)):
            result = actions.take_screenshot()

            if not result['ok']:
                self.skipTest(result['error'])
            self.assertTrue(Path(result['path']).is_file())
            self.assertEqual(Path(result['path']).parent, Path(pictures) / 'Screenshots')
            self.assertTrue(result['file'].startswith('screenshot-'))


class WebSocketActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_action_is_run_in_order_and_announced_after_it_finishes(self):
        import ws_client

        spoken, order = [], []

        def fake_run(action):
            order.append(('run', action['action']))
            return {'ok': action['action'] != 'open_file', 'file': 'x.pdf'}

        async def on_say(text, priority):
            order.append(('say', text))
            spoken.append((text, priority))

        batch = [
            {'action': 'open_browser', 'success_message': 'Opened site.', 'failure_message': 'No site.'},
            {'action': 'open_file', 'name': 'x', 'success_message': 'Opened file.', 'failure_message': 'No file.'},
        ]
        with patch.object(ws_client, 'run_action', side_effect=fake_run):
            await ws_client._handle('{"type": "actions", "actions": %s}' % __import__('json').dumps(batch), on_say=on_say)

        self.assertEqual(order, [('run', 'open_browser'), ('say', 'Opened site.'), ('run', 'open_file'), ('say', 'No file.')])
        self.assertTrue(all(priority == 'APPEND' for _, priority in spoken))

    async def test_garbage_action_payloads_are_ignored(self):
        import ws_client

        async def on_say(text, priority):
            raise AssertionError('nothing should be spoken')

        for payload in ('{"type": "actions"}', '{"type": "actions", "actions": "x"}', 'not json'):
            await ws_client._handle(payload, on_say=on_say)

    async def test_at_most_five_actions_run_per_message(self):
        import ws_client

        ran = []
        with patch.object(ws_client, 'run_action', side_effect=lambda a: ran.append(a) or {'ok': True}):
            await ws_client._run_actions([{'action': 'take_screenshot'}] * 9, None)

        self.assertEqual(len(ran), ws_client.MAX_ACTIONS)


if __name__ == '__main__':
    unittest.main()
