"""进度必须反映实际处理，且仅管理员可读取。"""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_service import FixtureEmbedding


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.environment=patch.dict(os.environ,{'RAG_ADMIN_TOKEN':'progress-admin','RAG_MCP_TOKEN':'progress-read'})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.embedder=FixtureEmbedding()
        self.client=TestClient(create_app(self.directory.name,self.embedder),base_url='http://127.0.0.1')
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'Authorization':'Bearer progress-admin'}
        self.client.put('/api/config',headers=self.headers,json={'base_url':'https://example.org/v1','model':'fixture'})

    def test_live_upload_progress_is_readable_during_embedding_and_private(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        original=self.embedder.embed
        async def blocking(config,texts):
            import asyncio
            entered.set();await asyncio.to_thread(release.wait,5)
            return await original(config,texts)
        self.embedder.embed=blocking
        identifier='a'*32;results=[]
        thread=threading.Thread(target=lambda:results.append(self.client.post('/api/documents',headers=self.headers,params={'operation_id':identifier},files={'file':('fixture.txt',b'progress text')})))
        thread.start();self.addCleanup(thread.join,6)
        self.assertTrue(entered.wait(4))
        try:
            response=self.client.get('/api/operations/'+identifier,headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            progress=response.json();self.assertEqual(progress['stage'],'embedding');self.assertEqual(progress['completed'],0)
            self.assertEqual(progress['filename'],'fixture.txt')
            self.assertEqual(self.client.get('/api/operations/'+identifier).status_code,401)
            self.assertEqual(self.client.get('/api/operations/'+identifier,headers={'Authorization':'Bearer progress-read'}).status_code,401)
        finally:release.set();thread.join(6)
        self.assertEqual(results[0].status_code,201)
        final=self.client.get('/api/operations/'+identifier,headers=self.headers).json()
        self.assertEqual(final['state'],'complete');self.assertEqual(final['completed'],1)
        self.assertEqual(self.client.post('/api/documents',headers=self.headers,params={'operation_id':identifier},files={'file':('duplicate.txt',b'text')}).status_code,409)

    def test_rebuild_counts_actual_files_and_reports_failed_index(self):
        for name in ['one.txt','two.txt']:
            self.assertEqual(self.client.post('/api/documents',headers=self.headers,files={'file':(name,b'fixture')}).status_code,201)
        identifier='b'*32
        self.embedder.fail=True
        response=self.client.post('/api/reindex',headers=self.headers,params={'operation_id':identifier})
        self.assertEqual(response.status_code,200)
        progress=self.client.get('/api/operations/'+identifier,headers=self.headers).json()
        self.assertEqual(progress['total'],2);self.assertEqual(progress['completed'],2);self.assertEqual(progress['failed'],2)
        self.assertEqual(progress['state'],'failed')

    def test_invalid_upload_has_failed_progress_and_identifier_validation(self):
        identifier='c'*32
        response=self.client.post('/api/documents',headers=self.headers,params={'operation_id':identifier},files={'file':('bad.exe',b'not a document')})
        self.assertEqual(response.status_code,400)
        progress=self.client.get('/api/operations/'+identifier,headers=self.headers).json()
        self.assertEqual(progress['state'],'failed');self.assertTrue(progress['error'])
        self.assertEqual(self.client.get('/api/operations/'+'d'*32,headers=self.headers).status_code,404)
        self.assertEqual(self.client.post('/api/reindex',headers=self.headers,params={'operation_id':'../oops'}).status_code,422)

    def test_image_ocr_reports_stage_and_source_from_worker_thread(self):
        from tests.test_rich_documents import office_fixture
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        identifier='e'*32;results=[]
        def recognizer(raw):
            entered.set();release.wait(5);return 'OCR sample text'
        with patch('app.ocr.recognize_image',side_effect=recognizer):
            thread=threading.Thread(target=lambda:results.append(self.client.post('/api/documents',headers=self.headers,params={'operation_id':identifier},files={'file':('image.docx',office_fixture('.docx'))})))
            thread.start()
            try:
                self.assertTrue(entered.wait(4))
                progress=self.client.get('/api/operations/'+identifier,headers=self.headers).json()
                self.assertEqual(progress['stage'],'ocr');self.assertEqual(progress['current'],0)
                self.assertIn('Word',progress['source'])
            finally:release.set();thread.join(6)
        self.assertEqual(results[0].status_code,201)
