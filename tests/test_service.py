import os
import logging
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.documents import MAX_UPLOAD

logging.disable(logging.CRITICAL)


class FixtureEmbedding:
    """仅代替外部付费网络接口；数据库、解析和业务均运行真实实现。"""
    def __init__(self):
        self.fail = False

    async def embed(self, config, texts):
        if self.fail:
            raise ValueError("模型接口连接失败，请检查地址和网络。")
        return [[1.0, 0.0] if "雷达" in text else [0.0, 1.0] for text in texts]


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        from app.main import create_app
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.embedding = FixtureEmbedding()
        self.client = TestClient(create_app(self.folder.name, self.embedding), base_url="http://127.0.0.1")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.config = {"base_url": "https://example.org/v1", "model": "fixture-model", "api_key": "secret-test-key"}
        response = self.client.put("/api/config", json=self.config)
        self.assertEqual(response.status_code, 200, response.text)

    def upload(self, name="雷达说明.md", content="雷达用于测距，安装前请检查网口。"):
        response = self.client.post("/api/documents", files={"file": (name, content.encode("utf-8"), "text/plain")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def search(self):
        return self.client.post("/api/search", json={"query": "雷达", "top_k": 3})

    def test_upload_search_returns_ranked_source_and_persists(self):
        document = self.upload()
        self.upload("食谱.txt", "鸡蛋需要煮熟。")
        hits = self.search().json()["results"]
        self.assertEqual(hits[0]["document_id"], document["id"])
        self.assertEqual(hits[0]["filename"], "雷达说明.md")
        self.assertEqual(hits[0]["chunk_index"], 1)
        self.assertEqual(hits[0]["score"], 1.0)
        self.assertIn(f"/api/documents/{document['id']}/download", hits[0]["download_url"])

        # Test download endpoint
        dl_resp = self.client.get(f"/api/documents/{document['id']}/download")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertIn("雷达用于测距", dl_resp.text)

        content_resp = self.client.get(f"/api/documents/{document['id']}/content").json()
        self.assertIn(f"/api/documents/{document['id']}/download", content_resp["download_url"])

        from app.main import create_app
        with TestClient(create_app(self.folder.name, self.embedding), base_url="http://127.0.0.1") as restarted:
            self.assertEqual(len(restarted.get("/api/documents").json()["documents"]), 2)
            self.assertTrue(restarted.get("/api/config").json()["has_api_key"])

    def test_disabled_document_is_excluded_then_delete_removes_file(self):
        document = self.upload()
        response = self.client.patch(f'/api/documents/{document["id"]}', json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search().json()["results"], [])
        self.assertEqual(self.client.get(f'/api/documents/{document["id"]}/content').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/documents/{document["id"]}').status_code, 200)
        self.assertEqual(self.client.get("/api/documents").json()["documents"], [])
        from pathlib import Path
        self.assertEqual(list((Path(self.folder.name) / "uploads").iterdir()), [])

    def test_model_change_invalidates_existing_vectors_and_reindex_restores(self):
        document = self.upload()
        config = dict(self.config, model="different-model", api_key="")
        self.assertEqual(self.client.put("/api/config", json=config).status_code, 200)
        self.assertEqual(self.search().json()["results"], [])
        self.assertEqual(self.client.get("/api/documents").json()["documents"][0]["status"], "stale")
        self.assertEqual(self.client.post(f'/api/documents/{document["id"]}/reindex').status_code, 200)
        self.assertEqual(len(self.search().json()["results"]), 1)

    def test_api_key_preserved_masked_and_encrypted_at_rest(self):
        from pathlib import Path
        config = self.client.get("/api/config").json()
        self.assertNotIn("secret-test-key", str(config))
        self.assertNotIn("api_key", config)
        self.client.put("/api/config", json=dict(self.config, api_key=""))
        self.assertTrue(self.client.get("/api/config").json()["has_api_key"])
        for path in Path(self.folder.name).glob("*.sqlite*"):
            self.assertNotIn(b"secret-test-key", path.read_bytes())
        self.client.put("/api/config", json=dict(self.config, api_key="", clear_api_key=True))
        self.assertFalse(self.client.get("/api/config").json()["has_api_key"])

    def test_pause_blocks_business_but_management_can_resume(self):
        self.upload()
        self.assertEqual(self.client.put("/api/service", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.search().status_code, 503)
        self.assertEqual(self.client.post("/api/documents", files={"file": ("a.txt", b"abc")}).status_code, 503)
        self.assertEqual(self.client.post("/api/reindex").status_code, 503)
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.client.put("/api/service", json={"enabled": True})
        self.assertEqual(len(self.search().json()["results"]), 1)

    def test_embedding_failure_keeps_retryable_file_without_partial_vectors(self):
        self.embedding.fail = True
        document = self.upload()
        self.assertEqual(document["status"], "failed")
        self.embedding.fail = False
        self.assertEqual(self.search().json()["results"], [])
        response = self.client.post(f'/api/documents/{document["id"]}/reindex')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(len(self.search().json()["results"]), 1)

    def test_invalid_files_rejected_without_creating_documents(self):
        for name, body in [("a.exe", b"payload"), ("empty.txt", b" "), ("broken.pdf", b"not a PDF")]:
            response = self.client.post("/api/documents", files={"file": (name, body)})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/api/documents").json()["documents"], [])

    def test_invalid_vectors_do_not_enter_index(self):
        async def malformed(config, texts):
            return [[float("nan"), 1.0] for text in texts]
        self.embedding.embed = malformed
        document = self.upload()
        self.assertEqual(document["status"], "failed")

    def test_config_validation_and_search_limits(self):
        self.assertEqual(self.client.put("/api/config", json=dict(self.config, base_url="file:///tmp")).status_code, 422)
        self.assertEqual(self.client.post("/api/search", json={"query": " ", "top_k": 3}).status_code, 422)
        self.assertEqual(self.client.post("/api/search", json={"query": "x", "top_k": 100}).status_code, 422)

    def test_upload_without_model_waits_for_configuration(self):
        from app.main import create_app
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(directory, self.embedding), base_url="http://127.0.0.1") as client:
            response = client.post("/api/documents", files={"file": ("note.txt", "未配置也能保存文字。".encode())})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["status"], "pending")
            self.assertEqual(client.post("/api/search", json={"query": "文字"}).status_code, 400)
            client.put("/api/config", json=self.config)
            self.assertEqual(client.post("/api/reindex").status_code, 200)
            self.assertEqual(client.get("/api/documents").json()["documents"][0]["status"], "ready")

    def test_file_size_limit_and_unknown_host(self):
        response = self.client.post("/api/documents", files={"file": ("huge.txt", b"x" * (MAX_UPLOAD + 1))})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/documents").json()["documents"], [])
        self.assertEqual(self.client.get("/api/status", headers={"Host": "untrusted.example"}).status_code, 400)


class AuthenticationTests(unittest.TestCase):
    def test_separate_tokens_and_cross_origin_mutations(self):
        from app.main import create_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RAG_ADMIN_TOKEN": "admin-token", "RAG_MCP_TOKEN": "tool-token"}):
            with TestClient(create_app(directory, FixtureEmbedding()), base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/status").status_code, 401)
                self.assertEqual(client.get("/api/status", headers={"Authorization": "Bearer tool-token"}).status_code, 401)
                headers = {"Authorization": "Bearer admin-token"}
                self.assertEqual(client.get("/api/status", headers=headers).status_code, 200)
                self.assertEqual(client.post("/mcp", json={}, headers=headers).status_code, 401)
                response = client.put("/api/service", json={"enabled": False}, headers=dict(headers, Origin="https://untrusted.example"))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(client.get("/health").status_code, 200)
