"""本地来源只读、固定发布版本及鉴权回归。"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.worker import app


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'RAG_LOCAL_SOURCES': json.dumps({'manual': str(self.root)}),
                                          'RAG_WORKER_TOKEN': 'private-worker'})
        self.env.start(); self.client = TestClient(app)
        self.headers = {'X-Worker-Token': 'private-worker'}

    def tearDown(self):
        self.client.close(); self.env.stop(); self.tmp.cleanup()

    def post(self, action, **values):
        return self.client.post('/local/' + action, json={'source_id': 'manual', **values}, headers=self.headers)

    def test_auth_and_unregistered_source(self):
        self.assertEqual(self.client.post('/local/scan', json={}).status_code, 401)
        self.assertEqual(self.post('scan', source_id='unknown').status_code, 400)

    def test_plain_directory_nested_scan_and_hash_guard(self):
        (self.root / '子目录').mkdir(); file = self.root / '子目录/a.md'; file.write_text('原文', encoding='utf-8')
        scan = self.post('scan'); self.assertEqual(scan.status_code, 200, scan.text)
        entry = scan.json()['files'][0]
        self.assertEqual(entry['path'], '子目录/a.md')
        self.assertEqual(self.post('read', path=entry['path'], hash=entry['hash']).content, file.read_bytes())
        file.write_text('新版', encoding='utf-8')
        self.assertEqual(self.post('read', path=entry['path'], hash=entry['hash']).status_code, 400)
        self.assertEqual(self.post('read', path='../outside.md').status_code, 400)
        self.assertEqual(self.post('read', path='CURRENT.json').status_code, 400)

    def test_pinned_version_does_not_mix_new_release(self):
        from collectors.engineering_handbook.collector import publish
        from tests.test_handbook_collector import sample
        publish(self.root, sample(), '快照')
        first = self.post('scan').json(); self.assertTrue(first['complete'])
        entry = first['files'][0]
        old = self.post('read', snapshot=first['snapshot'], path=entry['path'], hash=entry['hash'])
        data = sample()
        for item in data['items']:
            item['full'] = ['完全更新后的内容']
        publish(self.root, data, '新版快照')
        second = self.post('scan').json(); self.assertNotEqual(first['snapshot'], second['snapshot'])
        self.assertEqual(self.post('read', snapshot=first['snapshot'], path=entry['path'], hash=entry['hash']).content, old.content)
        self.assertEqual(self.post('read', snapshot='../outside', path=entry['path']).status_code, 400)

    def test_corrupt_release_refuses_complete_manifest(self):
        from collectors.engineering_handbook.collector import publish
        from tests.test_handbook_collector import sample
        result = publish(self.root, sample(), '快照')
        version = self.root / 'versions' / result['version']
        manifest = json.loads((version / 'manifest.json').read_text('utf-8'))
        (version / 'documents' / manifest['files'][0]['path']).write_text('损坏', encoding='utf-8')
        self.assertEqual(self.post('scan').status_code, 400)

    def test_symlink_and_missing_root_refused(self):
        from app.local_source import LocalSource
        missing = self.root / 'missing'
        with patch.dict(os.environ, {'RAG_LOCAL_SOURCES': json.dumps({'manual': str(missing)})}):
            self.assertEqual(self.post('scan').status_code, 400)
        try:
            (self.root / 'link.md').symlink_to(self.root / 'outside.md')
        except OSError:
            self.skipTest('当前 Windows 账号没有创建符号链接权限')
        with self.assertRaises(ValueError):
            LocalSource({'source_id': 'manual'}).read('link.md')
