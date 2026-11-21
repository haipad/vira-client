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

    def test_spoken_extension_word_narrows_and_ranks_the_exact_file(self):
        self._touch('Hari_resume_updated.pdf')
        exact = self._touch('Hari_resume.pdf')

        matches = actions.find_file_matches('Hari resume PDF', [self.root])

        self.assertEqual(matches[0][2], exact)
        self.assertEqual(matches[0][0], 4)
        self.assertEqual(len(matches), 2)

    def test_optional_owner_hint_prefers_matching_files(self):
        self._touch('Baskar_QA_Engineer_Resume.pdf')
        own_resume = self._touch('Hari_resume.pdf')
        self._touch('Akshuni_Raghuwanshi_Resume.pdf')

        matches = actions.find_file_matches('resume', [self.root], owner_hint='Hari')

        self.assertEqual(matches[0][2], own_resume)
        self.assertGreater(matches[0][0], matches[1][0])

    def test_filename_pattern_narrows_candidates_but_open_still_checks_ambiguity(self):
        own = self._touch('Hari_resume.pdf')
        self._touch('Hari_resume_old.pdf')
        self._touch('Baskar_resume.pdf')
        pattern = r'^Hari.*resume.*\.pdf$'

        matches = actions.find_file_matches('resume file', [self.root], filename_pattern=pattern)
        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('resume file', filename_pattern=pattern)

        self.assertEqual({path.name for _, _, path in matches}, {'Hari_resume.pdf', 'Hari_resume_old.pdf'})
        self.assertEqual(result['status'], 'ambiguous')
        launch.assert_not_called()
        self.assertEqual(own.name, 'Hari_resume.pdf')

    def test_filename_pattern_rejects_paths_complex_syntax_and_oversized_patterns(self):
        self._touch('Hari_resume.pdf')
        invalid_patterns = (
            r'C:\Users\*\resume.pdf',
            r'(Hari|Baskar).*resume.*\.pdf',
            r'(a+)+\.pdf',
            'a' * 101,
        )
        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            for pattern in invalid_patterns:
                result = actions.open_file_by_name('resume', filename_pattern=pattern)
                self.assertFalse(result['ok'], pattern)
        launch.assert_not_called()

    def test_short_asr_fragment_is_ignored_instead_of_substring_matching(self):
        harithaa = self._touch('Harithaa_Resume_Nov3_2025.pdf')

        self.assertIsNone(actions.find_file('RE', [self.root]))
        self.assertEqual(actions.find_file('resume', [self.root]), harithaa)

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

    def test_open_file_asks_for_choice_when_multiple_candidates_match(self):
        first = self._touch('Hari_resume.pdf', mtime=1_000_000)
        second = self._touch('Harithaa_Resume_Nov3_2025.pdf', mtime=2_000_000)

        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('RE resume')

        launch.assert_not_called()
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['matches'], [second.name, first.name])
        self.assertIn('Say which to open', actions.outcome_text({'action': 'open_file'}, result))

    def test_explicit_full_name_ranks_exact_file_first_and_opens_it(self):
        self._touch('Harithaa_Resume_Nov3_2025.pdf')
        exact = self._touch('Hari_resume.pdf')

        with patch.object(actions, 'search_roots', return_value=[self.root]), patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('Hari resume.pdf')

        launch.assert_called_once_with(exact)
        self.assertEqual(result['file'], exact.name)

    def test_selected_folder_resolves_duplicate_resume_names(self):
        documents = self.root / 'Documents'
        downloads = self.root / 'Downloads'
        documents.mkdir()
        downloads.mkdir()
        self._touch('Documents/Hari_resume.pdf')
        selected = self._touch('Downloads/Hari_resume.pdf')

        with patch.object(actions, 'known_folder', side_effect=lambda folder: self.root / folder.title()), \
             patch.object(actions, '_launch') as launch:
            result = actions.open_file_by_name('Hari resume PDF', folder='downloads')

        launch.assert_called_once_with(selected)
        self.assertEqual(result['path'], str(selected))

    def test_candidate_search_is_read_only_and_selected_id_opens_exact_file(self):
        first = self._touch('Documents/Hari_resume.pdf')
        self._touch('Downloads/Other_resume.pdf')
        with patch.object(actions, 'search_roots', return_value=[self.root]), \
             patch.object(actions, '_launch') as launch:
            result = actions.search_file_candidates('resume', owner_hint='Hari')
            self.assertEqual(result['candidates'][0]['name'], first.name)
            self.assertNotIn(str(self.root), str(result))
            launch.assert_not_called()
            opened = actions.open_file_candidate(result['candidates'][0]['id'])
            launch.assert_called_once_with(first)
            self.assertTrue(opened['ok'])
            self.assertFalse(actions.open_file_candidate(result['candidates'][0]['id'])['ok'])

    def test_candidate_ref_rejects_changed_file(self):
        path = self._touch('resume.pdf')
        with patch.object(actions, 'search_roots', return_value=[self.root]), \
             patch.object(actions, '_launch') as launch:
            result = actions.search_file_candidates('resume')
            path.write_text('changed')
            opened = actions.open_file_candidate(result['candidates'][0]['id'])
            self.assertFalse(opened['ok'])
            launch.assert_not_called()

    def test_opened_candidate_can_be_read_by_reference_without_researching(self):
        path = self._touch('Documents/resume.txt')
        self._touch('Downloads/resume.txt')
        with patch.object(actions, 'search_roots', return_value=[self.root]), \
             patch.object(actions, '_launch') as launch:
            candidates = actions.search_file_candidates('resume')['candidates']
            choice = next(item for item in candidates if item['folder'].endswith('Documents'))
            opened = actions.open_file_candidate(choice['id'])
            local = actions.read_document_ref(opened['document']['document_ref'])

        launch.assert_called_once_with(path)
        self.assertEqual(opened['document']['file'], path.name)
        self.assertEqual(local['path'], path)


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

    def test_open_file_outcomes_are_grounded_in_the_actual_result(self):
        success = actions.outcome_text(
            {'action': 'open_file', 'name': 'resume', 'success_message': 'Found your passport.'},
            {'ok': True, 'file': 'Hari_resume.pdf'},
        )
        failure = actions.outcome_text(
            {'action': 'open_file', 'name': 'resume file', 'failure_message': 'Opened it.'},
            {'ok': False, 'error': 'not found'},
        )

        self.assertEqual(success, 'Opened Hari_resume.pdf.')
        self.assertIn('could not find a file matching "resume file"', failure)

    def test_ambiguous_open_file_asks_from_ranked_matches_not_a_generic_failure(self):
        result = actions.outcome_text(
            {'action': 'open_file', 'name': 'resume', 'failure_message': 'No problem.'},
            {'ok': False, 'status': 'ambiguous', 'matches': ['Hari_resume.pdf in Downloads', 'Other_resume.pdf in Documents']},
        )

        self.assertIn('Hari_resume.pdf in Downloads', result)
        self.assertIn('Other_resume.pdf in Documents', result)
        self.assertIn('which to open', result.lower())

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
            self.assertEqual(result['location'], 'Pictures/Screenshots')

    def test_screenshot_success_announcement_includes_the_folder(self):
        self.assertIn('Pictures/Screenshots', actions.outcome_text(
            {'action': 'take_screenshot', 'success_message': 'Done.'}, {'ok': True},
        ))


class WebSocketActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcript_event_reaches_the_caption_callback(self):
        import json
        import ws_client
        from unittest.mock import AsyncMock

        callback = AsyncMock()
        await ws_client._handle(json.dumps({'type': 'transcript', 'speaker': 'user', 'text': 'Hello'}), on_transcript=callback)
        callback.assert_awaited_once_with('user', 'Hello')

    async def test_execute_action_returns_correlated_client_acknowledgement(self):
        import json
        import ws_client

        with patch.object(ws_client, 'run_action', return_value={'ok': True, 'file': 'shot.png', 'path': 'private/path'}) as run:
            result = await ws_client._handle(json.dumps({
                'type': 'execute_action', 'id': 'action-id', 'action': {'action': 'take_screenshot'},
            }))

        run.assert_called_once_with({'action': 'take_screenshot'})
        self.assertEqual(result['id'], 'action-id')
        self.assertTrue(result['result']['ok'])
        self.assertNotIn('path', result['result'])

    async def test_file_search_and_selected_open_return_correlated_results(self):
        import json
        import ws_client

        search_result = {'ok': True, 'candidates': [{'id': 'opaque', 'name': 'resume.pdf', 'folder': 'Documents'}]}
        with patch.object(ws_client, 'search_file_candidates', return_value=search_result) as search, \
             patch.object(ws_client, 'open_file_candidate', return_value={'ok': True, 'file': 'resume.pdf'}) as open_choice:
            found = await ws_client._handle(json.dumps({
                'type': 'search_files', 'id': 'request-1', 'query': 'resume', 'owner_hint': 'Hari',
            }))
            opened = await ws_client._handle(json.dumps({
                'type': 'open_selected_file', 'id': 'request-2', 'candidate_id': 'opaque',
            }))

        self.assertEqual(found, {'type': 'document_result', 'id': 'request-1', 'result': search_result})
        self.assertEqual(opened['id'], 'request-2')
        self.assertTrue(opened['result']['ok'])
        search.assert_called_once_with('resume', 'Hari', '', '')
        open_choice.assert_called_once_with('opaque')

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

        self.assertEqual(order, [
            ('run', 'open_browser'),
            ('say', 'Opened site.'),
            ('run', 'open_file'),
            ('say', 'I could not find a file matching "x". Please say more of its filename or its folder.'),
        ])
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
