import os
import shutil
import subprocess
import sys
import time
import requests

DATA_DIR = "test_hybrid_data"
WORKER_PORT = 8092
CORE_PORT = 8090
WORKER_URL = f"http://127.0.0.1:{WORKER_PORT}"
CORE_URL = f"http://127.0.0.1:{CORE_PORT}"

def run_tests():
    print("=== 开始 Go/Rust + Python 混合架构端到端集成测试 ===", flush=True)
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    worker_log = open("test_worker.log", "w", encoding="utf-8")
    core_log = open("test_core.log", "w", encoding="utf-8")

    # 1. 启动 Python 解析 Worker
    print(f"[1/6] 启动 Python Worker (端口 {WORKER_PORT})...", flush=True)
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.worker:app", "--port", str(WORKER_PORT), "--host", "127.0.0.1"],
        stdout=worker_log,
        stderr=worker_log,
    )

    # 等待 Worker 就绪
    ready = False
    for i in range(20):
        if worker_proc.poll() is not None:
            raise RuntimeError(f"Python Worker 意外退出，查看 test_worker.log")
        try:
            r = requests.get(f"{WORKER_URL}/health", timeout=1)
            if r.status_code == 200:
                ready = True
                print("Python Worker 就绪:", r.json(), flush=True)
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        worker_proc.kill()
        raise RuntimeError("Python Worker 启动超时！")

    # 2. 启动 Rust 核心服务
    print(f"[2/6] 启动 Rust Core (端口 {CORE_PORT})...", flush=True)
    core_env = os.environ.copy()
    core_env["RAG_WORKER_URL"] = WORKER_URL
    core_env["RAG_PORT"] = str(CORE_PORT)
    core_env["RAG_HOST"] = "127.0.0.1"
    core_env["RAG_DATA_DIR"] = DATA_DIR

    core_bin = os.path.join("core", "target", "debug", "rag-core.exe")
    core_proc = subprocess.Popen(
        [core_bin],
        env=core_env,
        stdout=core_log,
        stderr=core_log,
    )

    # 等待 Core 就绪
    core_ready = False
    for _ in range(20):
        if core_proc.poll() is not None:
            raise RuntimeError(f"Rust Core 意外退出，查看 test_core.log")
        try:
            r = requests.get(f"{CORE_URL}/health", timeout=1)
            if r.status_code == 200:
                core_ready = True
                print("Rust Core 就绪:", r.json(), flush=True)
                break
        except Exception:
            pass
        time.sleep(0.5)

    try:
        if not core_ready:
            raise RuntimeError("Rust Core 启动超时！")

        # 3. 验证基础状态与知识库管理
        print("[3/6] 测试知识库与目录结构...", flush=True)
        status = requests.get(f"{CORE_URL}/api/status").json()
        print("  初始状态:", status, flush=True)
        assert status["enabled"] is True

        libs = requests.get(f"{CORE_URL}/api/libraries").json()
        print(f"  默认知识库数量: {len(libs)}", flush=True)
        assert len(libs) >= 3

        new_lib = requests.post(f"{CORE_URL}/api/libraries", json={"name": "自动化测试知识库"}).json()
        print("  创建新知识库:", new_lib, flush=True)
        assert new_lib["name"] == "自动化测试知识库"

        folder = requests.post(
            f"{CORE_URL}/api/folders",
            json={"library_id": new_lib["id"], "name": "核心文档目录", "parent_id": None},
        ).json()
        print("  创建新文件夹:", folder, flush=True)

        # 4. 上传文档（验证 Rust 调用 Python Worker 进行抽取）
        print("[4/6] 测试文档上传与无缝解析...", flush=True)
        test_content = b"# Rust Hybrid Architecture\nThis is a benchmark document for hybrid Rust + Python RAG core."
        files = {"file": ("benchmark.md", test_content, "text/markdown")}
        data = {"library_id": new_lib["id"], "folder_id": folder["id"]}

        upload_resp = requests.post(f"{CORE_URL}/api/documents", files=files, data=data)
        assert upload_resp.status_code == 200, f"Upload failed: {upload_resp.text}"
        doc = upload_resp.json()
        print("  上传并解析成功:", doc["id"], doc["filename"], "文本长度:", len(doc["text"]), flush=True)
        assert doc["filename"] == "benchmark.md"
        assert "Rust Hybrid Architecture" in doc["text"]

        # 验证预览接口 (Rust 路由 -> Python Worker 渲染 HTML)
        preview_resp = requests.get(f"{CORE_URL}/api/documents/{doc['id']}/preview")
        assert preview_resp.status_code == 200, f"Preview failed: {preview_resp.status_code} {preview_resp.text}"
        assert "html" in preview_resp.headers.get("content-type", "").lower()
        print("  文档预览 HTML 成功返回", flush=True)

        # 5. 测试 API Key 授权与 MCP 协议端点
        print("[5/6] 测试 MCP 协议与 API Key 鉴权...", flush=True)
        key_resp = requests.post(
            f"{CORE_URL}/api/keys",
            json={"name": "MCP 智能体密钥", "library_ids": [new_lib["id"]], "all_libraries": False},
        )
        assert key_resp.status_code == 200
        key_data = key_resp.json()
        api_key = key_data["key"]
        print("  生成 MCP API Key:", key_data["prefix"] + "...", flush=True)

        headers = {"Authorization": f"Bearer {api_key}"}

        # MCP initialize
        mcp_init = requests.post(
            f"{CORE_URL}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=headers,
        ).json()
        assert mcp_init["result"]["serverInfo"]["name"] == "知库 RAG"
        print("  MCP initialize 成功", flush=True)

        # MCP tools/list
        mcp_tools = requests.post(
            f"{CORE_URL}/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        ).json()
        tool_names = [t["name"] for t in mcp_tools["result"]["tools"]]
        print("  MCP tools:", tool_names, flush=True)
        assert "search_knowledge" in tool_names
        assert "list_documents" in tool_names
        assert "get_document" in tool_names

        # MCP tools/call -> list_documents
        mcp_call = requests.post(
            f"{CORE_URL}/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_documents"}},
            headers=headers,
        ).json()
        assert "result" in mcp_call
        print("  MCP tools/call (list_documents) 成功返回", flush=True)

        # 6. 文档删除与清理
        print("[6/6] 测试文档清理...", flush=True)
        del_resp = requests.delete(f"{CORE_URL}/api/documents/{doc['id']}")
        assert del_resp.status_code == 200
        print("  文档删除成功", flush=True)

        print("\n==========================================", flush=True)
        print("🎉 全部端到端集成测试圆满通过！🎉", flush=True)
        print("Rust Core + Python Worker 混合架构验证无误！", flush=True)
        print("==========================================", flush=True)

    finally:
        print("正在关闭测试服务进程...", flush=True)
        try:
            core_proc.kill()
            worker_proc.kill()
            worker_proc.wait(timeout=2)
            core_proc.wait(timeout=2)
        except Exception:
            pass
        worker_log.close()
        core_log.close()
        if os.path.exists(DATA_DIR):
            shutil.rmtree(DATA_DIR, ignore_errors=True)

if __name__ == "__main__":
    run_tests()
