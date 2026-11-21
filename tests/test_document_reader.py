import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import actions


class FakePage:
    def extract_text(self):
        raise AssertionError('metadata inspection must not extract page text')


class FakePdfReader:
    pages = []

    def __init__(self, path):
        self.pages = type(self).pages


class DocumentReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        module = ModuleType('pypdf')
        module.PdfReader = FakePdfReader
        self.pypdf = patch.dict(sys.modules, {'pypdf': module})
        self.pypdf.start()
        self.refs = patch.dict(actions._DOCUMENT_REFS, {})
        self.refs.start()

    def tearDown(self):
        self.refs.stop()
        self.pypdf.stop()
        self.temp.cleanup()

    def _file(self, name, data=b'private synthetic document'):
        path = self.root / name
        path.write_bytes(data)
        return path

    def _inspect(self, name):
        with patch.object(actions, 'search_roots', return_value=[self.root]):
            return actions.inspect_document_by_name(name)

    def test_pdf_inspection_returns_metadata_only_and_byte_read_uses_reference(self):
        path = self._file('report.pdf')
        FakePdfReader.pages = [FakePage(), FakePage()]

        metadata = self._inspect('report')
        local = actions.read_document_ref(metadata['document_ref'])

        self.assertEqual(metadata['status'], 'inspected')
        self.assertEqual(metadata['page_count'], 2)
        self.assertNotIn('content', metadata)
        self.assertEqual(local['data'], path.read_bytes())
        self.assertEqual(local['file'], 'report.pdf')

    def test_large_pdf_preflight_requests_page_selection_without_reading_content(self):
        self._file('manual.pdf')
        FakePdfReader.pages = [FakePage()] * (actions.MAX_FULL_PDF_PAGES + 1)

        metadata = self._inspect('manual')

        self.assertTrue(metadata['requires_page_selection'])
        self.assertEqual(metadata['page_count'], actions.MAX_FULL_PDF_PAGES + 1)
        self.assertNotIn('content', metadata)

    def test_oversized_file_is_rejected_during_metadata_preflight(self):
        self._file('huge.txt', b'x' * (actions.MAX_DOCUMENT_BYTES + 1))

        metadata = self._inspect('huge')

        self.assertEqual(metadata['status'], 'too_large')
        self.assertNotIn('document_ref', metadata)

    def test_document_ref_rejects_modified_file_and_expired_reference(self):
        path = self._file('report.txt')
        metadata = self._inspect('report')
        path.write_bytes(b'changed after inspection')

        result = actions.read_document_ref(metadata['document_ref'])

        self.assertEqual(result['status'], 'changed')
        self.assertNotIn('data', result)

    def test_unknown_and_unsupported_documents_are_not_inspected(self):
        self._file('script.py')

        self.assertEqual(self._inspect('missing')['status'], 'not_found')
        self.assertEqual(self._inspect('script')['status'], 'not_found')

    def test_local_reference_remains_available_until_expiry_after_processing(self):
        path = self._file('plan.docx')
        metadata = self._inspect('plan')
        reference = metadata['document_ref']
        actions.finish_document_ref(reference, path, read_succeeded=False, open_after_read=False)
        self.assertIn(reference, actions._DOCUMENT_REFS)
        with patch.object(actions, '_launch') as launch:
            actions.finish_document_ref(reference, path, read_succeeded=True, open_after_read=True)
        self.assertIn(reference, actions._DOCUMENT_REFS)
        launch.assert_called_once_with(path)


class DocumentWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_returns_metadata_without_uploading_content(self):
        import ws_client

        expected = {'ok': True, 'status': 'inspected', 'file': 'report.pdf', 'document_ref': 'opaque-ref'}
        with patch.object(ws_client, 'inspect_document_by_name', return_value=expected) as inspect:
            result = await ws_client._handle(json.dumps({
                'type': 'inspect_document', 'id': 'doc-1', 'name': 'report',
            }))

        inspect.assert_called_once_with('report')
        self.assertEqual(result, {'type': 'document_result', 'id': 'doc-1', 'result': expected})

    async def test_read_request_uploads_bytes_only_for_the_inspected_reference(self):
        import ws_client

        local_path = Path('report.pdf')
        local = {'ok': True, 'file': 'report.pdf', 'path': local_path, 'data': b'PDF bytes'}
        with patch.object(ws_client, 'read_document_ref', return_value=local) as read, \
             patch.object(ws_client, '_upload_document', return_value={'ok': True, 'status': 'read'}) as upload, \
             patch.object(ws_client, 'finish_document_ref') as finish:
            result = await ws_client._handle(json.dumps({
                'type': 'read_document', 'id': 'req-7', 'document_ref': 'opaque-ref',
                'open_after_read': True,
            }))

        read.assert_called_once_with('opaque-ref')
        upload.assert_called_once_with('req-7', b'PDF bytes')
        finish.assert_called_once_with('opaque-ref', local_path, True, True)
        self.assertIsNone(result)

    async def test_failed_local_ref_is_returned_without_upload(self):
        import ws_client

        failed = {'ok': False, 'status': 'changed', 'error': 'file changed'}
        with patch.object(ws_client, 'read_document_ref', return_value=failed), \
             patch.object(ws_client, '_upload_document') as upload:
            result = await ws_client._handle(json.dumps({
                'type': 'read_document', 'id': 'req-8', 'document_ref': 'expired-ref',
            }))

        upload.assert_not_called()
        self.assertEqual(result['result'], failed)


if __name__ == '__main__':
    unittest.main()