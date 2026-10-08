"""对当前 Compose 服务验收持久化；只创建并清理自己的临时文件。"""
import asyncio
import json
import os
import subprocess
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://127.0.0.1:" + os.environ.get("RAG_PORT", "8010")


def wait_ready(client):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            response = client.get(BASE + "/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Docker 服务未在 40 秒内恢复")


async def verify_mcp(document_id):
    def direct_client(**kwargs):
        return httpx.AsyncClient(trust_env=False, **kwargs)
    async with streamablehttp_client(BASE + "/mcp/", httpx_client_factory=direct_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_document", {"document_id": document_id})
            assert not result.isError, result
            assert "Docker 卷重建后仍保留文件" in result.content[0].text


def run():
    document_id = None
    with httpx.Client(trust_env=False, timeout=10) as client:
        wait_ready(client)
        status = client.get(BASE + "/api/status").json()
        assert not status["config"]["configured"], "此脚本仅用于未配置模型的新部署，避免调用用户的付费接口。"
        enabled = status["enabled"]
        try:
            if not enabled:
                client.put(BASE + "/api/service", json={"enabled": True}).raise_for_status()
            filename = "验收临时文件-" + uuid.uuid4().hex[:8] + ".md"
            response = client.post(BASE + "/api/documents", files={"file": (filename, "Docker 卷重建后仍保留文件。".encode())})
            response.raise_for_status()
            document = response.json()
            document_id = document["id"]
            assert document["status"] == "pending"
            client.put(BASE + "/api/service", json={"enabled": False}).raise_for_status()
            assert client.post(BASE + "/api/search", json={"query": "持久化"}).status_code == 503
            client.put(BASE + "/api/service", json={"enabled": True}).raise_for_status()
            print("上传与暂停恢复已通过，正在重建容器验证持久卷。", flush=True)
            subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"], check=True)
            wait_ready(client)
            documents = client.get(BASE + "/api/documents").json()["documents"]
            assert any(item["id"] == document_id and item["filename"] == filename for item in documents)
            original = client.get(BASE + f"/api/documents/{document_id}/content").json()
            assert original["text"] == "Docker 卷重建后仍保留文件。"
            asyncio.run(verify_mcp(document_id))
            identity = subprocess.check_output(["docker", "compose", "exec", "-T", "rag", "id", "-u"], text=True).strip()
            assert identity == "10001", identity
            print(json.dumps({"结果": "通过", "检查": ["容器实际上传", "业务暂停与恢复", "容器重建后原文件与文本保留", "部署入口 MCP 读取", "非 root 用户运行"], "用户ID": identity}, ensure_ascii=False), flush=True)
        finally:
            if document_id:
                client.delete(BASE + f"/api/documents/{document_id}").raise_for_status()
            client.put(BASE + "/api/service", json={"enabled": enabled}).raise_for_status()


if __name__ == "__main__":
    run()
