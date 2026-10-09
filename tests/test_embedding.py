import json
import unittest
from unittest.mock import patch

import httpx

from app.embedding import HTTPEmbedding


class EmbeddingHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_advances_only_after_completed_embedding_batch(self):
        from app.progress import Operations
        operations=Operations();identifier='f'*32;seen=[]
        def handle(request):
            seen.append(operations.get(identifier).get('current',0))
            size=len(json.loads(request.content)['input'])
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.0,0.0]} for i in range(size)]})
        real_client=httpx.AsyncClient
        with operations.track(identifier,'reindex'),patch('app.embedding.httpx.AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw)):
            await HTTPEmbedding().embed({'base_url':'https://example.org/v1','model':'fixture','api_key':''},['text']*40)
            progress=operations.get(identifier)
            self.assertEqual(progress['current'],40);self.assertEqual(progress['stage_total'],40)
        self.assertEqual(seen,[0,32])

    async def test_reorders_vectors_and_sends_model_and_authorization(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "list", "model": "example-model", "data": [
                {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 2, "total_tokens": 2}})
        real_client = httpx.AsyncClient
        with patch("app.embedding.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)):
            vectors = await HTTPEmbedding().embed({"base_url": "https://example.org/v1", "model": "example-model", "api_key": "test-secret"}, ["甲", "乙"])
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(str(requests[0].url), "https://example.org/v1/embeddings")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-secret")
        self.assertEqual(json.loads(requests[0].content), {"model": "example-model", "input": ["甲", "乙"]})

    async def test_upstream_error_does_not_echo_sensitive_response(self):
        real_client = httpx.AsyncClient
        with patch("app.embedding.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="secret-test-key")), **kw)):
            with self.assertRaisesRegex(ValueError, "API Key") as result:
                await HTTPEmbedding().embed({"base_url": "https://example.org/v1", "model": "m", "api_key": "secret-test-key"}, ["文字"])
        self.assertNotIn("secret-test-key", str(result.exception))
