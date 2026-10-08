import asyncio
import json
import socket
import tempfile
import threading
import unittest

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.main import create_app


class MCPTests(unittest.IsolatedAsyncioTestCase):
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
