"""MCP 只读凭证；请求上下文中的集合是唯一可访问范围。"""
import hashlib
import base64
import hmac
import json
import os
import secrets
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pydantic import Field
from .catalog import NamedResource
from .service import BusinessError

# None 表示管理员或兼容的全库凭证，空集合表示无权限。
library_access = ContextVar('library_access', default=None)
download_identity = ContextVar('download_identity', default=None)


def source_url(document_id, storage):
    base = os.environ.get('RAG_PUBLIC_URL', 'http://127.0.0.1:8010').rstrip('/')
    url = f'{base}/api/documents/{document_id}/download'
    identity = download_identity.get()
    if identity:
        payload = base64.urlsafe_b64encode(json.dumps([identity, document_id, int(time.time()) + 600], separators=(',', ':')).encode()).decode().rstrip('=')
        signature = hmac.new(storage.directory.joinpath('secret.key').read_bytes(), payload.encode(), hashlib.sha256).hexdigest()
        url += '?grant=' + payload + '.' + signature
    return url


def permitted(library_id):
    allowed = library_access.get()
    return allowed is None or library_id in allowed


class KeyRequest(NamedResource):
    library_ids: list[str] = Field(default_factory=list, max_length=200)
    all_libraries: bool = False


class AccessKeys:
    def __init__(self, storage, catalog):
        self.storage, self.catalog = storage, catalog
        with storage.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS access_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL, library_ids TEXT NOT NULL, all_libraries INTEGER NOT NULL,
                created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)''')

    def validate(self, request):
        if not request.all_libraries and not request.library_ids:
            raise BusinessError('请选择至少一个知识库。')
        for library_id in request.library_ids:
            self.catalog.validate_location(library_id)

    @staticmethod
    def public(row):
        return {key: (json.loads(value) if key == 'library_ids' else bool(value) if key in {'all_libraries', 'revoked'} else value)
                for key, value in dict(row).items() if key != 'digest'}

    def list(self):
        with self.storage.connect() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM access_keys ORDER BY created_at DESC')]

    def configured(self):
        with self.storage.connect() as db:
            return bool(db.execute('SELECT 1 FROM access_keys LIMIT 1').fetchone()) or self.storage.get('key_auth_required', False)

    def create(self, request):
        self.validate(request)
        self.storage.put('key_auth_required', True)
        secret = 'zk_' + secrets.token_urlsafe(32)
        identifier = uuid.uuid4().hex
        with self.storage.connect() as db:
            db.execute('INSERT INTO access_keys VALUES (?,?,?,?,?,?,?,0)',
                       (identifier, request.name, hashlib.sha256(secret.encode()).hexdigest(), secret[:10],
                        json.dumps(sorted(set(request.library_ids))), int(request.all_libraries), datetime.now(timezone.utc).isoformat()))
        return {**next(key for key in self.list() if key['id'] == identifier), 'key': secret}

    def resolve(self, secret):
        with self.storage.connect() as db:
            row = db.execute('SELECT * FROM access_keys WHERE digest=? AND revoked=0', (hashlib.sha256(secret.encode()).hexdigest(),)).fetchone()
        if not row:
            raise BusinessError('API Key 无效或已撤销。', 401)
        return (None if row['all_libraries'] else frozenset(json.loads(row['library_ids'])), row['id'])

    def resolve_download(self, document_id, grant):
        try:
            payload, signature = grant.split('.')
            expected = hmac.new(self.storage.directory.joinpath('secret.key').read_bytes(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            identity, selected_document, expires = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            if selected_document != document_id or not isinstance(expires, int) or expires <= time.time():
                raise ValueError()
            with self.storage.connect() as db:
                row = db.execute('SELECT * FROM access_keys WHERE id=? AND revoked=0', (identity,)).fetchone()
            if not row:
                raise ValueError()
            return None if row['all_libraries'] else frozenset(json.loads(row['library_ids']))
        except (ValueError, TypeError, KeyError):
            raise BusinessError('下载链接无效、已过期或授权已撤销，请重新检索。', 401)

    def update(self, identifier, request):
        self.validate(request)
        with self.storage.connect() as db:
            changed = db.execute('UPDATE access_keys SET name=?,library_ids=?,all_libraries=? WHERE id=? AND revoked=0',
                                 (request.name, json.dumps(sorted(set(request.library_ids))), int(request.all_libraries), identifier)).rowcount
        if not changed:
            raise BusinessError('API Key 不存在或已撤销。', 404)
        return next(key for key in self.list() if key['id'] == identifier)

    def revoke(self, identifier):
        with self.storage.connect() as db:
            if not db.execute('UPDATE access_keys SET revoked=1 WHERE id=?', (identifier,)).rowcount:
                raise BusinessError('API Key 不存在。', 404)
        return {'revoked': True}

    def delete(self, identifier):
        self.storage.put('key_auth_required', True)
        with self.storage.connect() as db:
            if not db.execute('DELETE FROM access_keys WHERE id=?', (identifier,)).rowcount:
                raise BusinessError('API Key 不存在。', 404)
        return {'deleted': True}
