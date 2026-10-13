"""受控本地目录来源：仅允许环境映射中的目录及完整发布版本。"""
import hashlib
import json
import os
import re
from pathlib import Path
from .smb_source import Source, SUFFIXES, safe_parts


def read_json(path):
    if path.is_symlink() or path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('来源清单无效。')
    try:
        return json.loads(path.read_text('utf-8'))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError('来源清单无法解析。') from error


class LocalSource:
    def __init__(self, payload):
        try:
            mapping = json.loads(os.environ.get('RAG_LOCAL_SOURCES', '{}'))
        except json.JSONDecodeError as error:
            raise ValueError('本地来源映射配置无效。') from error
        identifier = payload.get('source_id')
        if not isinstance(identifier, str) or identifier not in mapping:
            raise ValueError('本地来源未登记，请由服务器配置允许读取的目录。')
        root = Path(mapping[identifier])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError('本地来源目录不存在或不是受控目录。')
        self.root = root.resolve()
        self.snapshot = payload.get('snapshot') or ''
        if self.snapshot and not re.fullmatch('[0-9a-f]{64}', self.snapshot):
            raise ValueError('本地版本标识无效。')
        if not self.snapshot and (self.root / 'CURRENT.json').exists():
            pointer = read_json(self.root / 'CURRENT.json')
            self.snapshot = pointer.get('version', '')
            if pointer.get('schema_version') != 1 or not re.fullmatch('[0-9a-f]{64}', self.snapshot):
                raise ValueError('已发布版本指针无效。')
        self.manifest = None
        if self.snapshot:
            version = self.root / 'versions' / self.snapshot
            self._safe(version)
            manifest_path = version / 'manifest.json'
            self._safe(manifest_path)
            self.manifest = read_json(manifest_path)
            if (self.manifest.get('schema_version') != 1 or self.manifest.get('complete') is not True
                    or self.manifest.get('version') != self.snapshot):
                raise ValueError('本地版本尚未完整发布。')
            self.directory = version / 'documents'
            self._safe(self.directory)
            records = self.manifest.get('files', [])
            self.entries = {}
            for record in records:
                path = record.get('path', '')
                safe_parts(path)
                if Path(path).suffix.lower() not in SUFFIXES or path in self.entries:
                    raise ValueError('来源清单存在不支持或重复文件。')
                if not isinstance(record.get('size'), int) or not 0 < record['size'] <= 50 * 1024 * 1024:
                    raise ValueError('来源文件大小无效。')
                if not re.fullmatch('[0-9a-f]{64}', record.get('hash', '')):
                    raise ValueError('来源文件指纹无效。')
                self.entries[path] = record
            if not self.entries:
                raise ValueError('发布版本没有文档，取消同步。')
        else:
            # 版本目录未发布指针时不能当作普通目录；否则可能错误清空知识库。
            if (self.root / 'versions').exists():
                raise ValueError('来源尚未发布完整版本。')
            self.directory = self.root
        self.source = Source(str(self.directory), os)

    def _safe(self, path):
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('本地来源禁止符号链接或重解析点。')
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('本地来源路径越界。')

    def read(self, path, expected_hash=None):
        safe_parts(path)
        if Path(path).suffix.lower() not in SUFFIXES:
            raise ValueError('只允许读取知识库支持的文档。')
        self._safe(self.directory / path)
        record = self.entries.get(path) if self.manifest else None
        if self.manifest and record is None:
            raise ValueError('文件未列入已发布清单。')
        if record and expected_hash and record['hash'] != expected_hash:
            raise ValueError('请求与发布清单的指纹不一致。')
        content = self.source.read(path, record['hash'] if record else expected_hash)
        if record and len(content) != record['size']:
            raise ValueError('文件长度与发布清单不一致。')
        return content

    def scan(self):
        result = self.source.scan()
        if self.manifest:
            expected = sorted(self.entries.values(), key=lambda r: r['path'])
            if result['files'] != expected:
                raise ValueError('来源文件与完整发布清单不一致。')
        result['snapshot'] = self.snapshot
        return result

    def test(self):
        return {'connected': True, 'snapshot': self.snapshot,
                'directories': sorted(p.name for p in self.directory.iterdir() if p.is_dir() and not p.is_symlink())}
