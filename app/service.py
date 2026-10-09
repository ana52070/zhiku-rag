import asyncio
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit

from .documents import extract_document, split_text
from .embedding import HTTPEmbedding, validate_vectors
from .storage import Storage
from .progress import report, document_done


class BusinessError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=4096)
    clear_api_key: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("接口地址必须是无用户名、密码和查询参数的 HTTP/HTTPS 基础地址。")
        return value

    @field_validator("model")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("模型名称不能为空。")
        return value.strip()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    library_id: str | None = None

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("检索问题不能为空。")
        return value.strip()


class EnabledRequest(BaseModel):
    enabled: bool


class BatchDocuments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_ids: list[str] = Field(min_length=1, max_length=1000)


class BatchMove(BatchDocuments):
    library_id: str
    folder_id: str | None = None


class KnowledgeService:
    def __init__(self, directory, embedder=None):
        self.storage = Storage(directory)
        from .catalog import Catalog
        self.catalog = Catalog(self.storage)
        from .access_keys import AccessKeys
        self.keys = AccessKeys(self.storage, self.catalog)
        self.embedder = embedder or HTTPEmbedding()
        self.lock = asyncio.Lock()

    def active(self):
        if not self.storage.get("enabled", True):
            raise BusinessError("业务已暂停，请在管理页面恢复服务。", 503)

    def config(self):
        config = self.storage.get("config", {"base_url": "https://api.openai.com/v1", "model": "", "encrypted_key": ""})
        return {"base_url": config["base_url"], "model": config["model"], "api_key": self.storage.decrypt(config.get("encrypted_key", ""))}

    def public_config(self):
        config = self.config()
        return {"base_url": config["base_url"], "model": config["model"], "has_api_key": bool(config["api_key"]), "configured": bool(config["model"])}

    def resolve_config(self, requested):
        old = self.config()
        return {"base_url": requested.base_url, "model": requested.model,
                "api_key": "" if requested.clear_api_key else (requested.api_key.strip() or old["api_key"])}

    def fingerprint(self):
        config = self.config()
        return hashlib.sha256(json.dumps([config["base_url"], config["model"]]).encode()).hexdigest()

    async def save_config(self, requested):
        async with self.lock:
            old_fingerprint = self.fingerprint()
            resolved = self.resolve_config(requested)
            self.storage.put("config", {"base_url": resolved["base_url"], "model": resolved["model"], "encrypted_key": self.storage.encrypt(resolved["api_key"])})
            changed = old_fingerprint != self.fingerprint()
            if changed:
                with self.storage.connect() as connection:
                    connection.execute("UPDATE documents SET status='stale', error='' WHERE status='ready'")
            return {**self.public_config(), "index_invalidated": changed}

    def document(self, document_id):
        with self.storage.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        from .access_keys import permitted
        if not row or not permitted(row['library_id']):
            raise BusinessError("文件不存在。", 404)
        return dict(row)

    def public_document(self, document):
        doc_id = document.get("id", "")
        res = {key: (bool(value) if key == "enabled" else value) for key, value in document.items() if key not in {"text", "fingerprint", "suffix"}}
        res['extraction_info'] = json.loads(document.get('extraction_info', '{}'))
        if doc_id:
            from .access_keys import source_url
            res["download_url"] = source_url(doc_id, self.storage)
        return res

    def documents(self, tool=False):
        if tool:
            self.active()
        with self.storage.connect() as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        from .access_keys import permitted
        return [self.public_document(dict(row)) for row in rows if permitted(row['library_id']) and (not tool or row["enabled"])]

    def status(self):
        documents = self.documents()
        return {"enabled": self.storage.get("enabled", True), "document_count": len(documents),
                "ready_count": sum(doc["status"] == "ready" and doc["enabled"] for doc in documents),
                "stale_count": sum(doc["status"] == "stale" for doc in documents),
                "chunk_count": sum(doc["chunk_count"] for doc in documents if doc["status"] == "ready" and doc["enabled"]),
                "config": self.public_config()}

    async def upload(self, filename, content, library_id=None, folder_id=None):
        report(total=1,filename=Path(filename.replace('\\','/')).name[:240])
        async with self.lock:
            self.active()
            if library_id is None:
                libraries = self.catalog.libraries()
                if not libraries:
                    raise BusinessError('请先创建知识库，再上传文件。')
                library_id = libraries[0]['id']
            self.catalog.validate_location(library_id, folder_id)
            filename = Path(filename.replace("\\", "/")).name[:240]
            suffix, text, extraction = await asyncio.to_thread(extract_document, filename, content)
            document_id = uuid.uuid4().hex
            path = self.storage.uploads / (document_id + suffix)
            path.write_bytes(content)
            try:
                with self.storage.connect() as connection:
                    connection.execute("INSERT INTO documents(id,filename,suffix,size,text,created_at,status,library_id,folder_id,extraction_info) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                       (document_id, filename, suffix, len(content), text, datetime.now(timezone.utc).isoformat(), "pending", library_id, folder_id, json.dumps(extraction, ensure_ascii=False)))
            except Exception:
                path.unlink(missing_ok=True)
                raise
            if self.config()["model"]:
                await self._index(document_id)
            result=self.public_document(self.document(document_id))
            document_done(result['status']=='failed')
            return result

    async def _index(self, document_id):
        document = self.document(document_id)
        report(filename=document['filename'],stage='parsing',current=0,stage_total=None)
        from .ocr import EXTRACTION_VERSION
        extraction = json.loads(document.get('extraction_info', '{}'))
        if extraction.get('version') != EXTRACTION_VERSION or extraction.get('state') == 'partial':
            path = self.storage.uploads / (document_id + document['suffix'])
            try:
                _, text, extraction = await asyncio.to_thread(extract_document, document['filename'], path.read_bytes())
                with self.storage.connect() as connection:
                    connection.execute('UPDATE documents SET text=?,extraction_info=?,status=\'stale\' WHERE id=?', (text, json.dumps(extraction, ensure_ascii=False), document_id))
                document = self.document(document_id)
            except (ValueError, OSError) as error:
                with self.storage.connect() as connection:
                    connection.execute("UPDATE documents SET status='failed',error=? WHERE id=?", (str(error), document_id))
                return
        pieces = split_text(document["text"])
        report(stage='embedding',current=0,stage_total=len(pieces))
        with self.storage.connect() as connection:
            connection.execute("UPDATE documents SET status='processing',error='' WHERE id=?", (document_id,))
        try:
            vectors = await self.embedder.embed(self.config(), pieces)
            dimension = validate_vectors(vectors, len(pieces))
            report(current=len(pieces))
        except ValueError as error:
            with self.storage.connect() as connection:
                connection.execute("UPDATE documents SET status='failed',error=? WHERE id=?", (str(error), document_id))
            return
        report(stage='saving',current=0,stage_total=None)
        with self.storage.connect() as connection:
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            connection.executemany("INSERT INTO chunks(document_id,chunk_index,text,vector) VALUES(?,?,?,?)",
                                   [(document_id, index + 1, text, json.dumps(vector)) for index, (text, vector) in enumerate(zip(pieces, vectors))])
            connection.execute("UPDATE documents SET status='ready',error='',fingerprint=?,chunk_count=?,dimension=? WHERE id=?",
                               (self.fingerprint(), len(pieces), dimension, document_id))

    async def reindex(self, document_id=None):
        async with self.lock:
            self.active()
            if not self.config()["model"]:
                raise BusinessError("请先配置 embedding 模型。")
            ids = [document_id] if document_id else [doc["id"] for doc in self.documents()]
            report(total=len(ids))
            for selected_id in ids:
                await self._index(selected_id)
                document_done(self.document(selected_id)['status']=='failed')
            if document_id:
                return self.public_document(self.document(document_id))
            documents = self.documents()
            return {"documents": documents, "failed_count": sum(doc["status"] == "failed" for doc in documents)}

    async def update_document(self, document_id, enabled):
        async with self.lock:
            self.document(document_id)
            with self.storage.connect() as connection:
                connection.execute("UPDATE documents SET enabled=? WHERE id=?", (int(enabled), document_id))
            return self.public_document(self.document(document_id))

    async def delete(self, document_id):
        async with self.lock:
            document = self.document(document_id)
            (self.storage.uploads / (document_id + document["suffix"])).unlink(missing_ok=True)
            with self.storage.connect() as connection:
                connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
            self.storage.remove_preview(document_id)
            return {"deleted": True}

    async def set_enabled(self, enabled):
        async with self.lock:
            self.storage.put("enabled", enabled)
            return self.status()

    async def move_document(self, document_id, location):
        async with self.lock:
            self.document(document_id)
            self.catalog.validate_location(location.library_id, location.folder_id)
            with self.storage.connect() as db:
                db.execute('UPDATE documents SET library_id=?,folder_id=? WHERE id=?', (location.library_id, location.folder_id, document_id))
            return self.public_document(self.document(document_id))

    async def batch_move(self, request):
        async with self.lock:
            ids = list(dict.fromkeys(request.document_ids))
            for identifier in ids:
                self.document(identifier)
            self.catalog.validate_location(request.library_id, request.folder_id)
            with self.storage.connect() as db:
                db.executemany('UPDATE documents SET library_id=?,folder_id=? WHERE id=?',
                               [(request.library_id, request.folder_id, identifier) for identifier in ids])
            return {'moved_count': len(ids)}

    async def batch_delete(self, request):
        async with self.lock:
            documents = [self.document(identifier) for identifier in dict.fromkeys(request.document_ids)]
            with self.storage.connect() as db:
                db.executemany('DELETE FROM documents WHERE id=?', [(doc['id'],) for doc in documents])
            for doc in documents:
                (self.storage.uploads / (doc['id'] + doc['suffix'])).unlink(missing_ok=True)
                self.storage.remove_preview(doc['id'])
            return {'deleted_count': len(documents)}

    async def search(self, query, top_k=5, library_id=None):
        request = SearchRequest(query=query, top_k=top_k, library_id=library_id)
        from .access_keys import permitted
        if library_id and not permitted(library_id):
            raise BusinessError('该 API Key 未获授权访问此知识库。', 403)
        if library_id:
            self.catalog.validate_location(library_id)
        async with self.lock:
            self.active()
            config = self.config()
            if not config["model"]:
                raise BusinessError("请先在模型配置页填写 embedding 接口和模型名称。")
            with self.storage.connect() as connection:
                rows = connection.execute("""SELECT c.*,d.filename,d.library_id,d.folder_id FROM chunks c JOIN documents d ON d.id=c.document_id
                    WHERE d.enabled=1 AND d.status='ready' AND d.fingerprint=? AND (? IS NULL OR d.library_id=?)""", (self.fingerprint(), library_id, library_id)).fetchall()
            rows = [row for row in rows if permitted(row['library_id'])]
            if not rows:
                return {"query": request.query, "results": []}
            vectors = await self.embedder.embed(config, [request.query])
            validate_vectors(vectors, 1)
            query_vector = vectors[0]
            query_norm = math.sqrt(sum(value * value for value in query_vector))
            results = []
            from .access_keys import source_url
            for row in rows:
                vector = json.loads(row["vector"])
                if len(vector) != len(query_vector):
                    raise BusinessError("模型向量维度已变化，请重建全部文件索引。", 409)
                denominator = query_norm * math.sqrt(sum(value * value for value in vector))
                score = sum(a * b for a, b in zip(query_vector, vector)) / denominator
                results.append({
                    "document_id": row["document_id"],
                    "filename": row["filename"],
                    "library_id": row["library_id"],
                    "folder_id": row["folder_id"],
                    "chunk_index": row["chunk_index"],
                    "text": row["text"],
                    "score": round(max(-1.0, min(1.0, score)), 6),
                    "download_url": source_url(row['document_id'], self.storage)
                })
            results.sort(key=lambda item: item["score"], reverse=True)
            return {"query": request.query, "results": results[:request.top_k]}

    def content(self, document_id, offset=0, limit=8000, admin=False):
        if not admin:
            self.active()
        document = self.document(document_id)
        if not admin and not document["enabled"]:
            raise BusinessError("该文件已停止参与检索。", 404)
        if offset < 0 or not 1 <= limit <= 8000:
            raise BusinessError("读取范围无效。")
        text = document["text"]
        from .access_keys import source_url
        return {
            "document_id": document_id,
            "filename": document["filename"],
            "text": text[offset:offset + limit],
            "total_characters": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "download_url": source_url(document_id, self.storage)
        }
