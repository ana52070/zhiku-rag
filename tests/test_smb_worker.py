import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.worker import app
from app.smb_source import Source


class WorkerTests(unittest.TestCase):
    def test_internal_auth_and_complete_scan(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'RAG_WORKER_TOKEN': 'worker-only'}):
            @contextmanager
            def connection(cfg):
                yield Source(root, os)
            with patch('app.smb_source.connect', connection), TestClient(app) as client:
                self.assertEqual(client.post('/smb/scan', json={}).status_code, 401)
                result = client.post('/smb/scan', json={}, headers={'X-Worker-Token': 'worker-only'})
                self.assertEqual(result.json(), {'complete': True, 'files': []})
                result = client.post('/smb/test', json={}, headers={'X-Worker-Token': 'worker-only'})
                self.assertTrue(result.json()['connected'])

    def test_errors_do_not_disclose_password(self):
        @contextmanager
        def connection(cfg):
            raise OSError('password=very-secret')
            yield
        with patch.dict(os.environ, {'RAG_WORKER_TOKEN': 'worker-only'}), patch('app.smb_source.connect', connection), TestClient(app) as client:
            result = client.post('/smb/scan', json={'password': 'very-secret'}, headers={'X-Worker-Token': 'worker-only'})
            self.assertEqual(result.status_code, 502)
            self.assertNotIn('very-secret', result.text)

    def test_preview_cache_changes_with_content(self):
        from pathlib import Path
        def convert(path, directory, identifier):
            output = Path(directory) / 'result.pdf'
            output.write_bytes(b'PDF:' + path.read_bytes())
            return output
        with tempfile.TemporaryDirectory() as root, patch('app.worker.needs_pdf_preview', return_value=True), patch('app.worker.word_pdf', side_effect=convert), TestClient(app) as client:
            options = {'document_id': 'document', 'filename': 'a.docx', 'preview_cache_dir': root}
            first = client.post('/preview-source', data=options, files={'file': ('a.docx', b'old')})
            second = client.post('/preview-source', data=options, files={'file': ('a.docx', b'new')})
            self.assertEqual(first.content, b'PDF:old')
            self.assertEqual(second.content, b'PDF:new')
