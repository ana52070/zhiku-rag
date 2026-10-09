# 启动 Go/Rust + Python 混合架构知识库服务
# 1. 后台文档解析 Worker (Python FastAPI, 端口 8002)
# 2. 前台主控高性能核心 (Rust Axum + SIMD, 端口 8010)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " 正在启动联大研发部知识平台 (Rust + Python 混合架构)" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

$env:PYTHONPATH = "."
$workerProcess = Start-Process -FilePath "python" -ArgumentList "-m uvicorn app.worker:app --port 8002 --host 127.0.0.1" -PassThru -NoNewWindow

Write-Host "[1/2] 文档解析 Worker 已启动 (PID: $($workerProcess.Id), 端口: 8002)" -ForegroundColor Yellow
Start-Sleep -Seconds 1

$coreExe = ".\core\target\debug\rag-core.exe"
if (-not (Test-Path $coreExe)) {
    Write-Host "未找到编译完成的 rag-core.exe，正在自动编译..." -ForegroundColor Cyan
    Push-Location core
    cargo build
    Pop-Location
}

Write-Host "[2/2] 正在启动 Rust 主控核心 (端口: 8010)..." -ForegroundColor Green
Write-Host "访问控制台: http://127.0.0.1:8010" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 可停止主服务。" -ForegroundColor Gray

$env:RAG_WORKER_URL = "http://127.0.0.1:8002"
$env:RAG_PORT = "8010"
$env:RAG_HOST = "127.0.0.1"
$env:RAG_DATA_DIR = "data"

try {
    & $coreExe
} finally {
    Write-Host "`n正在停止后台 Python Worker..." -ForegroundColor Yellow
    Stop-Process -Id $workerProcess.Id -Force -ErrorAction SilentlyContinue
    Write-Host "已全部退出。" -ForegroundColor Green
}
