"""知识库目录的真实持久化及索引稳定性。"""
import tempfile
import unittest

from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_service import FixtureEmbedding


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = TestClient(create_app(self.directory.name, FixtureEmbedding()), base_url="http://127.0.0.1")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.put('/api/config', json={'base_url': 'https://example.org/v1', 'model': 'fixture'})

    def library(self, name):
        response = self.client.post('/api/libraries', json={'name': name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_move_preserves_file_id_ready_index_and_source(self):
        library = self.library('目录测试知识库')
        folder = self.client.post('/api/folders', json={'name': '调试', 'library_id': library}).json()
        original = self.client.post('/api/documents', files={'file': ('说明.md', '雷达安装说明'.encode())}).json()
        response = self.client.patch('/api/documents/' + original['id'] + '/location', json={'library_id': library, 'folder_id': folder['id']})
        self.assertEqual(response.status_code, 200, response.text)
        moved = response.json()
        self.assertEqual(moved['status'], 'ready')
        self.assertEqual(moved['id'], original['id'])
        self.assertEqual(moved['folder_id'], folder['id'])
        self.assertEqual(self.client.get('/api/documents/' + original['id'] + '/download').content, '雷达安装说明'.encode())
        hits = self.client.post('/api/search', json={'query': '雷达', 'library_id': library}).json()['results']
        self.assertEqual([hit['document_id'] for hit in hits], [original['id']])
        self.assertEqual(self.client.delete('/api/folders/' + folder['id']).status_code, 409)

    def test_folders_cannot_cross_libraries_and_legacy_library_is_ordinary(self):
        a, b = self.library('FAQ'), self.library('错题本')
        parent = self.client.post('/api/folders', json={'name': '设备', 'library_id': a}).json()
        response = self.client.post('/api/folders', json={'name': '子目录', 'library_id': b, 'parent_id': parent['id']})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/folders', json={'name': '../逃逸', 'library_id': a})
        self.assertEqual(response.status_code, 422)
        default = self.client.get('/api/libraries').json()['libraries'][0]
        document = self.client.post('/api/documents', files={'file': ('旧资料.txt', b'original')}).json()
        self.assertEqual(document['library_id'], default['id'])
        self.assertEqual(self.client.delete('/api/libraries/' + default['id']).status_code, 200)

    def test_upgrade_existing_nonempty_database_keeps_vectors_and_document_id(self):
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        from app.storage import Storage
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / 'knowledge.sqlite')) as db:
                db.executescript('''CREATE TABLE documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, suffix TEXT NOT NULL,
                    size INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
                    chunk_count INTEGER NOT NULL DEFAULT 0, dimension INTEGER NOT NULL DEFAULT 0);
                    INSERT INTO documents VALUES ('legacy','旧文件.txt','.txt',3,'原文','2026',1,'ready','','same',1,2);
                    CREATE TABLE chunks (document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,text TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(document_id,chunk_index));
                    INSERT INTO chunks VALUES ('legacy',1,'原文','[1.0,0.0]');''')
            store = Storage(directory)
            Storage(directory)
            with store.connect() as db:
                self.assertEqual(dict(db.execute("SELECT * FROM documents WHERE id='legacy'").fetchone())['library_id'], 'default')
                self.assertEqual(db.execute("SELECT vector FROM chunks WHERE document_id='legacy'").fetchone()[0], '[1.0,0.0]')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
