"""采集失败不得污染已发布资料，重复采集不应改变文档字节。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path


def sample():
    return {'url': 'https://example.org/handbook', 'title': '工程手册', 'date': '2026-10-13',
            'expected_count': 2, 'expected_sources': 1,
            'items': [{'id': '1', 'question': '如何启动？', 'category': '软件', 'tags': ['启动'],
                       'answer': ['先检查电源。'], 'source': 'E:/资料/a.docx', 'full': ['第一章', '检查电源后启动。']},
                      {'id': '2', 'question': '如何停止？', 'category': '软件', 'tags': [],
                       'answer': ['按停止键。'], 'source': 'E:/资料/a.docx', 'full': ['第一章', '检查电源后启动。']}]}


class CollectorTests(unittest.TestCase):
    def test_busy_collector_does_not_overwrite_success_status(self):
        from filelock import FileLock
        from unittest.mock import patch
        from collectors.engineering_handbook.collector import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); status = root / 'status.json'
            status.write_text('{"state":"success","next_run":"2030-01-01T00:00:00+00:00"}')
            before = status.read_bytes()
            with FileLock(str(root / '.collector.lock')), patch('sys.argv', ['collector', '--output', directory]), patch('time.sleep', side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    main()
            self.assertEqual(status.read_bytes(), before)

    def test_weekly_schedule_uses_shanghai_monday(self):
        from datetime import datetime, timezone
        from collectors.engineering_handbook.collector import next_weekly
        self.assertEqual(next_weekly(datetime(2026, 10, 13, 8, tzinfo=timezone.utc)),
                         datetime(2026, 10, 18, 18, tzinfo=timezone.utc))

    def test_parse_embedded_json_without_executing_site_scripts(self):
        from collectors.engineering_handbook.collector import parse_html
        html = '''<html><h1>工程部手册</h1><script>
const FAQ_META = {"updated":"2026-10-13"};
const FAQ_DATA = [{"id":"soft","name":"软件","items":[
{"q":"如何启动？","a":["检查电源"],"src":"a.docx","tags":[],"full":["完整原文"]}]}];
throw new Error("禁止执行网站脚本");</script></html>'''
        result = parse_html(html, 'https://example.org/handbook')
        self.assertEqual(result['expected_count'], 1)
        self.assertEqual(result['items'][0]['category'], '软件')
        self.assertEqual(result['items'][0]['full'], ['完整原文'])
        with self.assertRaises(ValueError):
            parse_html(html.replace('const FAQ_DATA', 'const MISSING_DATA'), 'https://example.org/handbook')

    def test_deduplicate_fulltext_and_keep_question_relationship(self):
        from collectors.engineering_handbook.collector import prepare_documents
        docs = prepare_documents(sample())
        full = [v.decode() for k, v in docs.items() if k.startswith('全文/')]
        faq = [v.decode() for k, v in docs.items() if k.startswith('问答/')]
        self.assertEqual(len(full), 1)
        self.assertEqual(len(faq), 2)
        self.assertIn('检查电源后启动。', full[0])
        self.assertIn('如何启动？', full[0])
        self.assertIn('如何停止？', full[0])
        self.assertIn('网页整理要点', faq[0])
        self.assertIn('E:/资料/a.docx', faq[0])
        self.assertTrue(any(k.startswith('全文/a.docx__') for k in docs))
        self.assertTrue(any(k.startswith('问答/如何启动？__') for k in docs))

    def test_missing_fulltext_rejected_and_conflicting_text_preserved(self):
        from collectors.engineering_handbook.collector import prepare_documents
        data = sample(); data['items'][1]['full'] = []
        with self.assertRaises(ValueError):
            prepare_documents(data)
        data = sample(); data['items'][1]['full'] = ['不同全文']
        docs = prepare_documents(data)
        full = next(v.decode() for k, v in docs.items() if k.startswith('全文/'))
        self.assertIn('不同全文', full)
        self.assertIn('检查电源后启动。', full)
        self.assertIn('完整性未核实', full)

    def test_page_count_mismatch_rejected(self):
        from collectors.engineering_handbook.collector import prepare_documents
        data = sample(); data['expected_count'] = 3
        with self.assertRaises(ValueError):
            prepare_documents(data)

    def test_stable_documents_and_safe_names(self):
        from collectors.engineering_handbook.collector import prepare_documents
        data = sample(); first = prepare_documents(data)
        data['date'] = '2026-10-20'; data['items'].reverse()
        self.assertEqual(first, prepare_documents(data))
        data['items'][0]['question'] = '../../x:bad?'
        self.assertTrue(all('..' not in k and ':' not in k for k in prepare_documents(data)))

    def test_failed_publish_keeps_current_and_unchanged_reuses_version(self):
        from collectors.engineering_handbook.collector import publish
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = publish(root, sample(), '<html>证据</html>')
            self.assertEqual(first['files'], 3)
            pointer = (root / 'CURRENT.json').read_bytes()
            self.assertEqual(publish(root, sample(), '新快照')['version'], first['version'])
            data = sample(); data['items'][0]['full'] = []
            with self.assertRaises(ValueError):
                publish(root, data, '错误页面')
            self.assertEqual((root / 'CURRENT.json').read_bytes(), pointer)
            version = json.loads(pointer)['version']
            manifest = json.loads((root / 'versions' / version / 'manifest.json').read_text('utf-8'))
            self.assertTrue(manifest['complete'])
            self.assertEqual(len(manifest['files']), 3)

    def test_large_drop_requires_investigation(self):
        from collectors.engineering_handbook.collector import publish
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); publish(root, sample(), '完整页面')
            pointer = (root / 'CURRENT.json').read_bytes()
            data = sample(); data['items'].pop(); data['expected_count'] = 1
            with self.assertRaises(ValueError):
                publish(root, data, '少了一半')
            self.assertEqual((root / 'CURRENT.json').read_bytes(), pointer)
