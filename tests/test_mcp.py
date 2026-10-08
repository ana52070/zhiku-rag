import asyncio
import json
import socket
import tempfile
import threading
import unittest
import os
from unittest.mock import patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.main import create_app


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_keys_isolate_concurrent_real_mcp_clients(self):
        class LocalEmbedding:
            async def embed(self, config, texts):
                return [[1.0, 0.0] for text in texts]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_ADMIN_TOKEN': 'admin-fixture', 'RAG_MCP_TOKEN': ''}):
            application = create_app(directory, LocalEmbedding())
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            base = f'http://127.0.0.1:{listener.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(application, log_level='critical', ws='none'))
            worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            worker.start()
            try:
                for _ in range(250):
                    if server.started:
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(server.started)
                async with httpx.AsyncClient(trust_env=False, headers={'Authorization': 'Bearer admin-fixture'}) as admin:
                    await admin.put(base + '/api/config', json={'base_url': 'https://example.org/v1', 'model': 'fixture'})
                    docs = []
                    keys = []
                    for library, name in [('faq', '公开说明.txt'), ('retrospective', '内部复盘.txt')]:
                        docs.append((await admin.post(base + '/api/documents', params={'library_id': library}, files={'file': (name, name.encode())})).json()['id'])
                        response = await admin.post(base + '/api/keys', json={'name': name, 'library_ids': [library]})
                        self.assertEqual(response.status_code, 201, response.text)
                        keys.append(response.json())
                    def direct_client(**kwargs):
                        return httpx.AsyncClient(trust_env=False, **kwargs)
                    async def query_client(index):
                        async with streamablehttp_client(base + '/mcp/', headers={'Authorization': 'Bearer ' + keys[index]['key']}, httpx_client_factory=direct_client) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                for _ in range(3):
                                    listed = await session.call_tool('list_documents', {})
                                    self.assertFalse(listed.isError, listed)
                                    payload = json.loads(listed.content[0].text)
                                    self.assertEqual([d['id'] for d in payload['documents']], [docs[index]])
                                    hits = await session.call_tool('search_knowledge', {'query': '说明'})
                                    self.assertFalse(hits.isError, hits)
                                    self.assertEqual([d['document_id'] for d in json.loads(hits.content[0].text)['results']], [docs[index]])
                                    blocked = await session.call_tool('get_document', {'document_id': docs[1-index]})
                                    self.assertTrue(blocked.isError)
                    await asyncio.gather(query_client(0), query_client(1))
                    await admin.delete(base + '/api/keys/' + keys[0]['id'])
                    async with httpx.AsyncClient(trust_env=False) as client:
                        self.assertEqual((await client.post(base + '/mcp/', headers={'Authorization': 'Bearer ' + keys[0]['key']}, json={})).status_code, 401)
                        self.assertEqual((await client.post(base + '/mcp/', json={})).status_code, 401)
            finally:
                server.should_exit = True
                await asyncio.to_thread(worker.join, 5)
                listener.close()

    async def test_real_client_discovers_and_calls_tools_and_respects_pause(self):
        class LocalEmbedding:
            async def embed(self, config, texts):
                return [[1.0, 0.0] for text in texts]
        with tempfile.TemporaryDirectory() as directory:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(create_app(directory, LocalEmbedding()), log_level="critical", ws="none"))
            worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            worker.start()
            try:
                for _ in range(250):
                    if server.started:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(server.started)
                base = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(trust_env=False) as client:
                    await client.put(base + "/api/config", json={"base_url": "https://example.org/v1", "model": "fixture"})
                    document = (await client.post(base + "/api/documents", files={"file": ("来源.md", "这是原文证据。".encode())})).json()
                    def direct_client(**kwargs):
                        return httpx.AsyncClient(trust_env=False, **kwargs)
                    async with streamablehttp_client(base + "/mcp/", httpx_client_factory=direct_client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual({tool.name for tool in tools.tools}, {"search_knowledge", "list_documents", "get_document"})
                            result = await session.call_tool("search_knowledge", {"query": "原文", "top_k": 1})
                            self.assertFalse(result.isError)
                            payload = json.loads(result.content[0].text)
                            self.assertEqual(payload["results"][0]["filename"], "来源.md")
                            self.assertIn(f"/api/documents/{document['id']}/download", payload["results"][0]["download_url"])
                            content = await session.call_tool("get_document", {"document_id": document["id"]})
                            self.assertIn("这是原文证据。", content.content[0].text)
                            await client.patch(base + f'/api/documents/{document["id"]}', json={"enabled": False})
                            forbidden = await session.call_tool("get_document", {"document_id": document["id"]})
                            self.assertTrue(forbidden.isError)
                            await client.put(base + "/api/service", json={"enabled": False})
                            paused = await session.call_tool("list_documents")
                            self.assertTrue(paused.isError)
            finally:
                server.should_exit = True
                await asyncio.to_thread(worker.join, 5)
                listener.close()
