"""启动真实 Rust 核心与 HTTP Worker/embedding 替身，验证同步业务。"""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import httpx


class SyncE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / 'share'
        cls.source.mkdir()
        cls.fail_scan = False
        cls.fail_embedding = False
        cls.extract_count = 0
        cls.scan_gate = None
        from app.smb_source import Source

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                try:
                    if self.path == '/extract':
                        cls.extract_count += 1
                        # Multipart 文件部分，用真实解析函数提取纯文本。
                        raw = body.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
                        from app.documents import extract_document
                        suffix, text, info = extract_document('a.md', raw)
                        result = {'suffix': suffix, 'text': text, 'extraction_info': info}
                    elif self.path.endswith('/embeddings'):
                        if cls.fail_embedding:
                            raise RuntimeError('模拟模型故障')
                        payload = json.loads(body)
                        result = {'data': [{'index': i, 'embedding': [1., 0., 0.]} for i, _ in enumerate(payload['input'])]}
                    else:
                        payload = json.loads(body)
                        if self.headers.get('X-Worker-Token') != 'worker-test':
                            raise RuntimeError('缺少内部令牌')
                        if self.path == '/smb/test':
                            result = {'connected': True, 'directories': []}
                        elif self.path == '/smb/scan':
                            if cls.scan_gate:
                                cls.scan_gate.wait(timeout=10)
                            if cls.fail_scan:
                                raise RuntimeError('模拟权限/网络故障')
                            result = Source(str(cls.source), os).scan()
                        elif self.path == '/smb/read':
                            raw = Source(str(cls.source), os).read(payload['path'], payload['hash'])
                            self.send_response(200); self.end_headers(); self.wfile.write(raw); return
                        else:
                            raise RuntimeError('未知端点')
                    self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(result).encode())
                except Exception as error:
                    self.send_response(502); self.end_headers(); self.wfile.write(json.dumps({'detail': str(error)}).encode())

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{port}'
        worker_url = f'http://127.0.0.1:{cls.server.server_port}'
        env = dict(os.environ, RAG_DATA_DIR=str(cls.root / 'data'), RAG_PORT=str(port),
                   RAG_HOST='127.0.0.1', RAG_WORKER_URL=worker_url,
                   RAG_ADMIN_TOKEN='admin-test', RAG_WORKER_TOKEN='worker-test')
        binary = Path('core/target/debug/rag-core.exe' if os.name == 'nt' else 'core/target/debug/rag-core').resolve()
        cls.log = open(cls.root / 'core.log', 'w', encoding='utf-8')
        cls.env, cls.binary = env, binary
        cls.proc = subprocess.Popen([str(binary)], env=env, stdout=cls.log, stderr=cls.log)
        cls.client = httpx.Client(base_url=cls.url, headers={'Authorization': 'Bearer admin-test'}, trust_env=False, timeout=20)
        for _ in range(100):
            try:
                if cls.client.get('/api/status').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            cls.proc.terminate(); raise RuntimeError('核心未启动')
        r = cls.client.put('/api/config', json={'base_url': worker_url + '/v1', 'model': 'test', 'api_key': ''})
        r.raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.client.close(); cls.proc.terminate(); cls.proc.wait(timeout=10)
        cls.server.shutdown(); cls.server.server_close(); cls.log.close(); cls.tmp.cleanup()

    def task(self, name='同步测试'):
        library = self.client.post('/api/libraries', json={'name': name}).json()
        config = {'name': name, 'server': 'test-server', 'share': '资料', 'subdir': '',
                  'username': 'read-user', 'password': 'secret-smb', 'port': 445,
                  'library_id': library['id'], 'folder_id': None, 'enabled': False,
                  'schedule': {'kind': 'daily', 'time': '02:00', 'timezone': 'Asia/Shanghai'}}
        response = self.client.post('/api/sync/tasks', json=config)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), library

    def run_task(self, task):
        response = self.client.post(f"/api/sync/tasks/{task['id']}/run")
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()['id']
        for _ in range(200):
            runs = self.client.get(f"/api/sync/tasks/{task['id']}/runs").json()
            result = next(r for r in runs if r['id'] == run_id)
            if result['state'] != 'running':
                return result
            time.sleep(.05)
        self.fail('同步未完成')

    def docs(self, library):
        return [d for d in self.client.get('/api/documents').json()['documents'] if d['library_id'] == library['id']]

    def test_01_incremental_restore_update_delete(self):
        (self.source / '子目录').mkdir(exist_ok=True)
        file = self.source / '子目录' / 'a.md'
        file.write_text('原始文档 alpha', encoding='utf-8')
        task, library = self.task()
        self.assertNotIn('secret-smb', json.dumps(task))
        first = self.run_task(task)
        self.assertEqual(first['state'], 'success', first)
        self.assertEqual(first['added'], 1)
        docs = self.docs(library); self.assertEqual(len(docs), 1)
        document_id = docs[0]['id']; self.assertEqual(docs[0]['status'], 'ready')
        self.assertIsNotNone(docs[0]['folder_id'])
        before = self.extract_count
        self.assertEqual(self.run_task(task)['skipped'], 1)
        self.assertEqual(self.extract_count, before)
        file.write_text('修改后 beta', encoding='utf-8')
        type(self).fail_embedding = True
        failed = self.run_task(task); self.assertEqual(failed['state'], 'failed')
        self.assertIn('alpha', self.client.get(f'/api/documents/{document_id}').json()['text'])
        self.assertEqual(self.docs(library)[0]['status'], 'ready')
        type(self).fail_embedding = False
        self.assertEqual(self.run_task(task)['updated'], 1)
        self.assertEqual(self.docs(library)[0]['id'], document_id)
        self.assertIn('beta', self.client.get(f'/api/documents/{document_id}').json()['text'])
        self.assertEqual(self.client.get(f'/api/documents/{document_id}/download').content, file.read_bytes())
        self.assertEqual(self.client.get(f'/api/documents/{document_id}/download?grant=invalid').status_code, 401)
        self.client.post(f'/api/documents/{document_id}/move', json={'library_id': library['id'], 'folder_id': None})
        self.run_task(task); self.assertIsNotNone(self.docs(library)[0]['folder_id'])
        self.client.delete(f'/api/documents/{document_id}')
        self.run_task(task); self.assertEqual(self.docs(library)[0]['id'], document_id)
        file.unlink()
        type(self).fail_scan = True
        self.assertEqual(self.run_task(task)['state'], 'failed'); self.assertEqual(len(self.docs(library)), 1)
        type(self).fail_scan = False
        self.assertEqual(self.run_task(task)['deleted'], 1); self.assertEqual(self.docs(library), [])

    def test_02_admin_auth_and_credentials(self):
        for method, path in [('GET', '/api/sync/tasks'), ('POST', '/api/sync/test')]:
            with httpx.Client(base_url=self.url, trust_env=False) as client:
                self.assertEqual(client.request(method, path, json={}).status_code, 401)
        task, _ = self.task('鉴权测试')
        self.assertTrue(task['has_password'])
        self.assertNotIn('password', task)
        config = dict(task, password='', name='已编辑')
        result = self.client.put(f"/api/sync/tasks/{task['id']}", json=config)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['has_password'])

    def test_03_adopt_and_preserve_unrelated(self):
        file = self.source / 'same.md'; file.write_text('共享版本', encoding='utf-8')
        task, library = self.task('接管测试')
        uploaded = self.client.post('/api/documents', data={'library_id': library['id']}, files={'file': ('same.md', b'old')}).json()
        other = self.client.post('/api/documents', data={'library_id': library['id']}, files={'file': ('manual.md', b'manual')}).json()
        result = self.run_task(task); self.assertEqual(result['state'], 'success', result)
        docs = self.docs(library)
        adopted = next(d for d in docs if d['filename'] == 'same.md')
        self.assertEqual(adopted['id'], uploaded['id'])
        file.unlink(); self.run_task(task)
        self.assertEqual([d['id'] for d in self.docs(library)], [other['id']])

    def test_04_run_exclusion_and_task_delete_keeps_documents(self):
        (self.source / 'a.md').write_text('文档内容', encoding='utf-8')
        task, library = self.task('并发测试')
        gate = threading.Event(); type(self).scan_gate = gate
        try:
            first = self.client.post(f"/api/sync/tasks/{task['id']}/run")
            self.assertEqual(first.status_code, 202)
            self.assertEqual(self.client.post(f"/api/sync/tasks/{task['id']}/run").status_code, 409)
            self.assertEqual(self.client.delete(f"/api/sync/tasks/{task['id']}").status_code, 409)
            self.assertEqual(self.client.post('/api/reindex').status_code, 409)
        finally:
            gate.set(); type(self).scan_gate = None
        for _ in range(100):
            if not self.client.get(f"/api/sync/tasks/{task['id']}").json()['running']:
                break
            time.sleep(.05)
        self.assertEqual(len(self.docs(library)), 1)

    def test_05_automatic_schedule_and_restart_recovery(self):
        import sqlite3
        (self.source / 'a.md').write_text('自动调度文档', encoding='utf-8')
        task, library = self.task('调度恢复测试')
        task = self.client.put(f"/api/sync/tasks/{task['id']}", json=dict(task, enabled=True)).json()
        database = self.root / 'data' / 'knowledge.sqlite'
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE sync_tasks SET next_run=? WHERE id=?', ('2020-01-01T00:00:00Z', task['id']))
        for _ in range(300):
            runs = self.client.get(f"/api/sync/tasks/{task['id']}/runs").json()
            if runs and runs[0]['state'] == 'success':
                break
            time.sleep(.05)
        else:
            self.fail('到期任务没有自动执行')
        self.assertEqual(len(self.docs(library)), 1)

        type(self).proc.terminate(); type(self).proc.wait(timeout=10)
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO sync_runs VALUES('interrupted',?,'running','2020-01-01T00:00:00Z',NULL,'{}')", (task['id'],))
        type(self).proc = subprocess.Popen([str(self.binary)], env=self.env, stdout=self.log, stderr=self.log)
        for _ in range(100):
            try:
                runs = self.client.get(f"/api/sync/tasks/{task['id']}/runs").json()
                if any(r['id'] == 'interrupted' and r['state'] == 'failed' for r in runs) and sum(r['state'] == 'success' for r in runs) == 2:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            self.fail('重启后没有标记中断任务并补执行')
        self.assertEqual(len(self.docs(library)), 1)
        self.client.put(f"/api/sync/tasks/{task['id']}", json=dict(task, enabled=False))
        self.assertEqual(self.client.delete(f"/api/sync/tasks/{task['id']}").status_code, 200)
        self.assertEqual(len(self.docs(library)), 1)

    def test_06_delete_library_removes_synced_blob(self):
        import sqlite3
        (self.source / 'a.md').write_text('删除清理文档', encoding='utf-8')
        task, library = self.task('删除库测试')
        self.assertEqual(self.run_task(task)['state'], 'success')
        document = self.docs(library)[0]
        with sqlite3.connect(self.root / 'data' / 'knowledge.sqlite') as connection:
            blob = connection.execute('SELECT filename FROM document_blobs WHERE document_id=?', (document['id'],)).fetchone()[0]
        path = self.root / 'data' / 'uploads' / blob
        self.assertTrue(path.exists())
        self.assertEqual(self.client.delete(f"/api/libraries/{library['id']}").status_code, 200)
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
