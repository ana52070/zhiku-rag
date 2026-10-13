use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use tracing::info;

pub mod api;
pub mod catalog;
pub mod client;
pub mod crypto;
pub mod keys;
pub mod mcp;
pub mod operations;
pub mod service;
pub mod storage;
pub mod vector;
pub mod sync_schedule;
pub mod sync;
pub mod sync_api;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rag_core=info,tower_http=info".into()),
        )
        .init();

    // 确定数据目录：如果显式指定环境变量则直接使用，否则探测默认目录
    let data_dir = match std::env::var("RAG_DATA_DIR") {
        Ok(dir) => PathBuf::from(dir),
        Err(_) => {
            if Path::new("data").exists() {
                PathBuf::from("data")
            } else {
                PathBuf::from("../data")
            }
        }
    };

    // 确定静态前端目录
    let mut static_dir = PathBuf::from("../app/static");
    if !static_dir.exists() && Path::new("app/static").exists() {
        static_dir = PathBuf::from("app/static");
    }

    let worker_url = std::env::var("RAG_WORKER_URL").unwrap_or_else(|_| "http://127.0.0.1:8002".to_string());
    let host = std::env::var("RAG_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port: u16 = std::env::var("RAG_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8010);

    info!("正在初始化 Rust 核心服务...");
    info!("数据目录: {}", data_dir.display());
    info!("前端资源目录: {}", static_dir.display());
    info!("文档解析 Worker 地址: {}", worker_url);

    let storage = storage::Storage::new(&data_dir)
        .map_err(|e| format!("初始化存储失败: {}", e))?;

    // 启动清理中断任务
    let _ = storage.with_conn(|conn| {
        conn.execute(
            "UPDATE documents SET status='failed', error='上次处理被中断，请重新索引。' WHERE status='processing' OR status='pending'",
            [],
        )?;
        Ok(())
    });

    let service = service::KnowledgeService::new(storage, &worker_url);
    sync::start_scheduler(service.clone());
    let app = api::create_router(service, static_dir);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    info!("知库 RAG 主控服务启动成功，监听地址: http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
