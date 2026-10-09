@echo off
chcp 65001 >nul
echo ==========================================================
echo  正在启动联大研发部知识平台 (Rust + Python 混合架构)
echo ==========================================================

set PYTHONPATH=.
set RAG_WORKER_URL=http://127.0.0.1:8002
set RAG_PORT=8010
set RAG_HOST=127.0.0.1
set RAG_DATA_DIR=data

echo [1/2] 正在启动后台 Python 文档解析微服务 (端口: 8002)...
start /B python -m uvicorn app.worker:app --port 8002 --host 127.0.0.1

timeout /t 2 /nobreak >nul

if not exist "core\target\debug\rag-core.exe" (
    echo 正在编译 Rust 核心...
    cd core
    cargo build
    cd ..
)

echo [2/2] 正在启动 Rust 主控核心 (端口: 8010)...
echo 管理控制台地址: http://127.0.0.1:8010
echo MCP 协议接入点: http://127.0.0.1:8010/mcp

core\target\debug\rag-core.exe
