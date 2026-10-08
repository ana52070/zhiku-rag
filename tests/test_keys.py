"""真实 HTTP 授权边界，不依赖前端隐藏按钮。"""
import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_service import FixtureEmbedding


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'RAG_ADMIN_TOKEN': 'admin-fixture', 'RAG_MCP_TOKEN': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.application = create_app(self.directory.name, FixtureEmbedding())
        self.client = TestClient(self.application, base_url='http://127.0.0.1')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.admin = {'Authorization': 'Bearer admin-fixture'}
        self.client.put('/api/config', headers=self.admin, json={'base_url': 'https://example.org/v1', 'model': 'fixture'})
        self.a = self.client.post('/api/libraries', headers=self.admin, json={'name': 'FAQ'}).json()['id']
        self.b = self.client.post('/api/libraries', headers=self.admin, json={'name': '错题本'}).json()['id']
        self.da = self.client.post('/api/documents', params={'library_id': self.a}, headers=self.admin, files={'file': ('FAQ.txt', '雷达说明'.encode())}).json()['id']
        self.db = self.client.post('/api/documents', params={'library_id': self.b}, headers=self.admin, files={'file': ('内部.txt', '研发秘密'.encode())}).json()['id']

    def key(self, libraries=None, all_libraries=False):
        response = self.client.post('/api/keys', headers=self.admin, json={'name': 'OpenClaw', 'library_ids': libraries or [], 'all_libraries': all_libraries})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_key_cannot_read_other_library_download_or_manage(self):
        key = self.key([self.a])
        headers = {'Authorization': 'Bearer ' + key['key']}
        listed = self.client.get('/api/documents', headers=headers).json()['documents']
        self.assertEqual([d['id'] for d in listed], [self.da])
        for tail in ['/content', '/download']:
            self.assertEqual(self.client.get('/api/documents/' + self.db + tail, headers=headers).status_code, 404)
        self.assertEqual(self.client.get('/api/documents/' + self.da + '/download', headers=headers).content, '雷达说明'.encode())
        hits = self.client.post('/api/search', headers=headers, json={'query': '雷达'}).json()['results']
        self.assertEqual([d['document_id'] for d in hits], [self.da])
        self.assertEqual(self.client.post('/api/search', headers=headers, json={'query': '秘密', 'library_id': self.b}).status_code, 403)
        self.assertEqual(self.client.delete('/api/documents/' + self.da, headers=headers).status_code, 401)
        self.assertEqual(self.client.get('/api/keys', headers=headers).status_code, 401)
        self.assertEqual(self.client.get('/api/documents/' + self.da + '/download').status_code, 401)

    def test_only_hash_is_saved_revocation_and_scope_update_take_effect(self):
        key = self.key([self.a])
        headers = {'Authorization': 'Bearer ' + key['key']}
        listing = self.client.get('/api/keys', headers=self.admin).text
        self.assertNotIn(key['key'], listing)
        self.assertNotIn(key['key'].encode(), self.application.state.service.storage.path.read_bytes())
        response = self.client.patch('/api/keys/' + key['id'], headers=self.admin, json={'name': '项目组', 'library_ids': [self.b], 'all_libraries': False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d['id'] for d in self.client.get('/api/documents', headers=headers).json()['documents']], [self.db])
        self.assertEqual(self.client.delete('/api/keys/' + key['id'], headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get('/api/documents', headers=headers).status_code, 401)

    def test_all_library_key_covers_future_libraries_and_invalid_scopes_fail(self):
        key = self.key(all_libraries=True)
        headers = {'Authorization': 'Bearer ' + key['key']}
        self.assertEqual(len(self.client.get('/api/documents', headers=headers).json()['documents']), 2)
        future = self.client.post('/api/libraries', headers=self.admin, json={'name': '后续知识库'}).json()['id']
        added = self.client.post('/api/documents', params={'library_id': future}, headers=self.admin, files={'file': ('新增.txt', b'future')}).json()['id']
        self.assertIn(added, [d['id'] for d in self.client.get('/api/documents', headers=headers).json()['documents']])
        self.assertEqual(self.client.post('/api/keys', headers=self.admin, json={'name': '错误', 'library_ids': ['missing']}).status_code, 404)
        self.assertEqual(self.client.post('/api/keys', headers=self.admin, json={'name': '空权限', 'library_ids': []}).status_code, 400)

    def test_download_link_needs_no_bearer_but_expires_and_follows_revocation(self):
        from urllib.parse import urlsplit
        key = self.key([self.a])
        headers = {'Authorization': 'Bearer ' + key['key']}
        data = self.client.get('/api/documents', headers=headers).json()['documents'][0]
        url = urlsplit(data['download_url'])
        self.assertTrue(url.query, 'MCP 客户端的文件链接应包含短期下载授权')
        path = url.path + '?' + url.query
        self.assertNotIn(key['key'], data['download_url'])
        self.assertEqual(self.client.get(path).content, '雷达说明'.encode())
        other = path.replace(self.da, self.db)
        self.assertEqual(self.client.get(other).status_code, 401)
        import time
        with patch('app.access_keys.time.time', return_value=time.time()+601):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.client.delete('/api/keys/' + key['id'], headers=self.admin)
        self.assertEqual(self.client.get(path).status_code, 401)
