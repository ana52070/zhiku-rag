"""独立网页采集：完整版本发布后才允许知识库读取。"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_URL = 'https://jcni8xb0obr4.doubaoapps.com/app/app_17fwjp9ecfp'
LOG = logging.getLogger('handbook')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def prepare_documents(dataset):
    items = dataset['items']
    if not items or len(items) != dataset['expected_count']:
        raise ValueError('网页条目数量不完整，取消发布。')
    sources, questions, ids = {}, set(), set()
    for item in items:
        if not all(item.get(k) for k in ('id', 'question', 'category', 'source', 'answer', 'full')):
            raise ValueError('条目缺少问题、分类、来源、答案或全文，取消发布。')
        if item['id'] in ids or item['question'] in questions:
            raise ValueError('问题标识或标题重复，取消发布。')
        if not all(isinstance(p, str) and p.strip() for p in item['answer'] + item['full']):
            raise ValueError('条目文字无效，取消发布。')
        ids.add(item['id']); questions.add(item['question'])
        source = item['source']
        existing = sources.setdefault(source, {'items': []})
        existing['items'].append(item)
    if len(sources) != dataset['expected_sources']:
        raise ValueError('引用资料数量与网页标示不一致，取消发布。')
    output = {}
    for source, group in sorted(sources.items()):
        identifier = digest(source.encode('utf-8'))
        associated = sorted(group['items'], key=lambda i: i['question'])
        full = '# ' + Path(source.replace('\\', '/')).name + '\n\n'
        full += '内容类型：网页内嵌原文或节选，完整性未核实（不包含原始文件及图片保证）\n\n'
        full += f"网页来源：{dataset['url']}\n\n原资料位置：{source}\n\n"
        full += '关联问题：\n' + '\n'.join('- ' + i['question'] for i in associated)
        variants = {}
        for item in associated:
            variant = variants.setdefault(tuple(item['full']), [])
            variant.append(item['question'])
        for paragraphs, related in variants.items():
            full += '\n\n## 内嵌文本\n\n关联问题：' + '；'.join(related) + '\n\n'
            full += '\n\n'.join(paragraphs) + '\n'
        output[f'全文/{identifier}.md'] = full.encode('utf-8')
        for item in associated:
            key = digest(item['question'].encode('utf-8'))
            body = f"# {item['question']}\n\n内容类型：网页整理要点\n\n分类：{item['category']}\n\n"
            body += '标签：' + '、'.join(item['tags']) + '\n\n'
            body += f"网页来源：{dataset['url']}\n\n原资料位置：{source}\n\n全文文件：全文/{identifier}.md\n\n"
            body += '## 回答要点\n\n' + '\n\n'.join(item['answer']) + '\n'
            output[f'问答/{key}.md'] = body.encode('utf-8')
    return output


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(encoded(value)); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def publish(root, dataset, html):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    documents = prepare_documents(dataset)
    records = [{'path': p, 'size': len(b), 'hash': digest(b)} for p, b in sorted(documents.items())]
    version = digest(encoded(records))
    pointer = root / 'CURRENT.json'
    previous = json.loads(pointer.read_text('utf-8')) if pointer.exists() else None
    if previous:
        manifest = json.loads((root / 'versions' / previous['version'] / 'manifest.json').read_text('utf-8'))
        for key, count in [('item_count', len(dataset['items'])), ('source_count', dataset['expected_sources'])]:
            if count < manifest[key] * .8:
                raise ValueError('资料数量较上一版减少超过 20%，保留旧版并等待排查。')
        if previous['version'] == version:
            return {'version': version, 'files': len(records), 'changed': False}
    versions = root / 'versions'; versions.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
    try:
        for relative, content in documents.items():
            destination = staging / 'documents' / relative
            destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(content)
        manifest = {'schema_version': 1, 'complete': True, 'version': version, 'files': records,
                    'item_count': len(dataset['items']), 'source_count': dataset['expected_sources'],
                    'collected_at': datetime.now(timezone.utc).isoformat(), 'url': dataset['url'], 'page_date': dataset['date']}
        (staging / 'manifest.json').write_bytes(encoded(manifest))
        (staging / 'dataset.json').write_bytes(encoded(dataset))
        (staging / 'page.html').write_text(html, encoding='utf-8')
        target = versions / version
        if not target.exists():
            staging.rename(target)
        atomic_json(pointer, {'schema_version': 1, 'version': version})
        return {'version': version, 'files': len(records), 'changed': True}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_script = False; self.in_title = False
        self.scripts = []; self.title = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.in_script = True; self.scripts.append('')
        if tag == 'h1': self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'script': self.in_script = False
        if tag == 'h1': self.in_title = False

    def handle_data(self, data):
        if self.in_script: self.scripts[-1] += data
        if self.in_title: self.title += data


def parse_html(html, url):
    parser = PageParser(); parser.feed(html)
    def variable(name):
        values = []
        for script in parser.scripts:
            for match in re.finditer(r'\bconst\s+' + name + r'\s*=\s*', script):
                try:
                    value, _ = json.JSONDecoder().raw_decode(script[match.end():])
                    values.append(value)
                except json.JSONDecodeError as error:
                    raise ValueError('???????????') from error
        if len(values) != 1: raise ValueError('??????????????')
        return values[0]
    metadata, categories = variable('FAQ_META'), variable('FAQ_DATA')
    if not isinstance(metadata, dict) or not isinstance(categories, list) or not categories:
        raise ValueError('?????????????')
    items, category_keys = [], set()
    for category in categories:
        if not category.get('name') or not isinstance(category.get('items'), list) or category.get('id') in category_keys:
            raise ValueError('??????????')
        category_keys.add(category.get('id'))
        for original in category['items']:
            items.append({'id': str(len(items) + 1), 'question': original.get('q'), 'category': category['name'],
                          'tags': original.get('tags', []), 'answer': original.get('a'),
                          'source': original.get('src'), 'full': original.get('full')})
    dataset = {'url': url, 'title': parser.title.strip(), 'date': metadata.get('updated'),
               'expected_count': len(items), 'expected_sources': len({i['source'] for i in items}),
               'categories': [{'key': c['id'], 'name': c['name'], 'count': len(c['items'])} for c in categories], 'items': items}
    if not dataset['date'] or not dataset['title']: raise ValueError('????????????')
    prepare_documents(dataset)
    return dataset


def fetch(url):
    proxy = os.environ.get('HANDBOOK_PROXY')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy} if proxy else {}))
    request = urllib.request.Request(url, headers={'User-Agent': 'EngineeringHandbookArchive/1.0'})
    with opener.open(request, timeout=120) as response:
        raw = response.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024: raise ValueError('???? 20 MiB ?????')
    html = raw.decode('utf-8')
    return parse_html(html, url), html


def next_weekly(now):
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    candidate = local.replace(hour=2, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(0 - candidate.weekday()) % 7)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description='工程部手册独立采集器')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--output', default=os.environ.get('HANDBOOK_OUTPUT', '/output'))
    parser.add_argument('--url', default=os.environ.get('HANDBOOK_URL', DEFAULT_URL))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    root = Path(args.output); root.mkdir(parents=True, exist_ok=True)
    # OS 文件锁避免命令行手工执行与常驻采集同时发布。
    from filelock import FileLock
    while True:
        now = datetime.now(timezone.utc)
        state_path = root / 'status.json'
        try:
            with FileLock(str(root / '.collector.lock'), timeout=0):
                state = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
                due = datetime.fromisoformat(state['next_run']) if state.get('next_run') else now
                if args.once or due <= now:
                    dataset, html = fetch(args.url)
                    result = publish(root, dataset, html)
                    atomic_json(state_path, {'state': 'success', 'last_success': now.isoformat(),
                                           'next_run': next_weekly(now).isoformat(), **result})
                    LOG.info('采集完成：条目=%s 来源=%s 文件=%s 有变化=%s', len(dataset['items']), dataset['expected_sources'], result['files'], result['changed'])
        except Exception as error:
            LOG.error('采集失败，旧版本保留：%s', type(error).__name__)
            if args.once:
                raise
            atomic_json(state_path, {'state': 'failed', 'last_attempt': now.isoformat(),
                                    'next_run': (now + timedelta(hours=1)).isoformat(), 'error': str(error)[:300]})
        if args.once:
            return
        time.sleep(30)


if __name__ == '__main__':
    main()
