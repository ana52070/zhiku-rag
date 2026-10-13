"""验证远端文件读取的安全边界和扫描失败传播。"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SourceTests(unittest.TestCase):
    def setUp(self):
        from app.smb_source import Source
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = Source(str(self.root), os)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recursive_hash_and_ignored_files(self):
        (self.root / '子目录').mkdir()
        (self.root / '子目录' / '说明.md').write_bytes(b'abc')
        (self.root / '~$lock.docx').write_bytes(b'lock')
        (self.root / 'ignore.exe').write_bytes(b'ignored')
        result = self.source.scan()
        self.assertTrue(result['complete'])
        self.assertEqual(result['files'], [{'path': '子目录/说明.md', 'size': 3,
                                          'hash': hashlib.sha256(b'abc').hexdigest()}])

    def test_path_escape_rejected(self):
        for name in ['../secret.txt', '/secret.txt', 'a/../secret.txt', 'C:\\secret.txt', 'a\\b.txt', 'file.txt:secret']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.source.read(name)

    def test_changed_since_scan_rejected(self):
        p = self.root / 'a.txt'
        p.write_bytes(b'old')
        entry = self.source.scan()['files'][0]
        p.write_bytes(b'new')
        with self.assertRaises(ValueError):
            self.source.read(entry['path'], entry['hash'])

    def test_permission_error_cannot_be_complete_scan(self):
        (self.root / 'a.txt').write_bytes(b'abc')
        with patch.object(os, 'scandir', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.source.scan()

    def test_read_bound(self):
        (self.root / 'a.txt').write_bytes(b'12345')
        with self.assertRaises(ValueError):
            self.source.read('a.txt', max_bytes=4)

    def test_source_missing_fails_instead_of_empty(self):
        from app.smb_source import Source
        with self.assertRaises(OSError):
            Source(str(self.root / 'missing'), os).scan()

    def test_generator_scandir_supported(self):
        (self.root / 'a.txt').write_bytes(b'abc')
        original = os.scandir
        def iterator(path, **kwargs):
            with original(path, **kwargs) as entries:
                yield from entries
        with patch.object(os, 'scandir', iterator):
            self.assertEqual(self.source.scan()['files'][0]['path'], 'a.txt')

    def test_connection_validation(self):
        from app.smb_source import validate_connection
        self.assertEqual(validate_connection({'server': '192.168.1.2', 'share': '资料', 'subdir': '技术/文档'}),
                         '\\\\192.168.1.2\\资料\\技术\\文档')
        for cfg in [{'server': 'x/y', 'share': 's'}, {'server': 'x', 'share': '../s'},
                    {'server': 'x', 'share': 's', 'subdir': '../secret'}]:
            with self.assertRaises(ValueError):
                validate_connection(cfg)

    def test_anonymous_context_does_not_require_os_credentials(self):
        import spnego
        import inspect
        from smbclient._pool import get_smb_tree
        from app.smb_source import connect
        with patch('smbclient.register_session'), patch('smbprotocol.tree.TreeConnect'), connect({'server': 'test-server', 'share': '资料', 'username': '', 'password': ''}) as source:
            context = spnego.client(source.kwargs['username'], source.kwargs['password'], protocol='ntlm')
            self.assertTrue(context.step())
            inspect.signature(get_smb_tree).bind(source.root, **source.kwargs)


if __name__ == '__main__':
    unittest.main()
