use std::collections::HashSet;
use std::sync::Arc;
use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Multipart, Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, patch, post, put},
    Json, Router,
};
use serde::Deserialize;
use serde_json::json;
use tower_http::cors::CorsLayer;
use tower_http::services::ServeDir;

use crate::catalog::Location;
use crate::mcp::{handle_mcp_request, JsonRpcRequest};
use crate::operations::OperationItem;
use crate::service::{KnowledgeService, ModelConfig};

pub type AppState = Arc<KnowledgeService>;

pub fn create_router(service: KnowledgeService, static_dir: std::path::PathBuf) -> Router {
    let state = Arc::new(service);

    let api_routes = Router::new()
        .route("/health", get(health))
        .route("/api/status", get(get_status))
        .route("/api/service", put(update_service_state).post(update_service_state))
        .route("/api/status/enable", post(enable_service))
        .route("/api/status/disable", post(disable_service))
        .route(
            "/api/config",
            get(get_config).post(save_config).put(save_config),
        )
        .route("/api/config/test", post(test_config))
        .route("/api/ocr/status", get(get_ocr_status))
        .route("/api/documents", get(list_documents).post(upload_document))
        .route("/api/documents/batch/move", post(batch_move_documents))
        .route("/api/documents/batch/delete", post(batch_delete_documents))
        .route("/api/documents/reindex", post(reindex_all_documents))
        .route("/api/reindex", post(reindex_all_documents))
        .route("/api/operations/{id}", get(get_operation))
        .route(
            "/api/documents/{id}",
            get(get_document).delete(delete_document).patch(toggle_document),
        )
        .route("/api/documents/{id}/reindex", post(reindex_single_document))
        .route("/api/documents/{id}/toggle", post(toggle_document))
        .route("/api/documents/{id}/move", post(move_document))
        .route("/api/documents/{id}/location", patch(move_document).post(move_document))
        .route("/api/documents/{id}/download", get(download_document))
        .route("/api/documents/{id}/content", get(get_document_content))
        .route("/api/documents/{id}/admin-content", get(get_document_content))
        .route("/api/documents/{id}/preview", get(preview_document))
        .route("/api/documents/{id}/preview-source", get(preview_source_document))
        .route("/api/libraries", get(list_libraries).post(create_library))
        .route(
            "/api/libraries/{id}",
            put(update_library).patch(update_library).delete(delete_library),
        )
        .route("/api/folders", get(list_folders).post(create_folder))
        .route("/api/folders/{id}", delete(delete_folder))
        .route("/api/keys", get(list_keys).post(create_key))
        .route(
            "/api/keys/{id}",
            put(update_key).patch(update_key).delete(revoke_key),
        )
        .route("/api/keys/{id}/permanent", delete(delete_key))
        .route("/api/keys/{id}/revoke", post(revoke_key))
        .route("/api/search", post(search_knowledge))
        .route(
            "/mcp",
            post(mcp_endpoint)
                .get(mcp_get_endpoint)
                .options(mcp_options_endpoint),
        )
        .route(
            "/mcp/",
            post(mcp_endpoint)
                .get(mcp_get_endpoint)
                .options(mcp_options_endpoint),
        );

    let index_file = static_dir.join("index.html");

    Router::new()
        .merge(api_routes)
        .layer(axum::middleware::from_fn_with_state(state.clone(), sync_mutation_guard))
        .merge(crate::sync_api::routes())
        .nest_service("/static", ServeDir::new(&static_dir))
        .fallback(move |uri: axum::http::Uri| {
            let idx = index_file.clone();
            async move {
                if uri.path().starts_with("/api/") || uri.path() == "/mcp" || uri.path().starts_with("/mcp/") {
                    return Response::builder()
                        .status(StatusCode::NOT_FOUND)
                        .header(header::CONTENT_TYPE, "application/json")
                        .body(Body::from(json!({ "detail": "API 接口不存在。" }).to_string()))
                        .unwrap();
                }
                if let Ok(content) = tokio::fs::read(&idx).await {
                    Response::builder()
                        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                        .body(Body::from(content))
                        .unwrap()
                } else {
                    Response::builder()
                        .status(StatusCode::NOT_FOUND)
                        .body(Body::from("Not Found"))
                        .unwrap()
                }
            }
        })
        .layer(DefaultBodyLimit::max(100 * 1024 * 1024))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

async fn sync_mutation_guard(
    State(svc): State<AppState>, request: axum::extract::Request, next: axum::middleware::Next,
) -> Response {
    let path = request.uri().path();
    let mutation = !matches!(*request.method(), axum::http::Method::GET | axum::http::Method::HEAD | axum::http::Method::OPTIONS)
        && (path.starts_with("/api/documents") || path.starts_with("/api/libraries")
            || path.starts_with("/api/folders") || path == "/api/reindex");
    if mutation {
        let Ok(_guard) = svc.sync_lock.clone().try_lock_owned() else {
            return err_response(409, "同步正在执行，请完成后修改文件或目录。");
        };
        return next.run(request).await;
    }
    next.run(request).await
}

fn err_response(code: u16, msg: impl Into<String>) -> Response {
    let status = StatusCode::from_u16(code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let body = json!({ "detail": msg.into() });
    (status, Json(body)).into_response()
}

async fn health(State(svc): State<AppState>) -> Response {
    let ocr = svc.worker.ocr_status().await.unwrap_or(json!({ "ocr": "unknown" }));
    Json(json!({
        "status": "ok",
        "core": "rust",
        "worker": ocr
    })).into_response()
}

async fn get_status(State(svc): State<AppState>) -> Response {
    let mut status = svc.status();
    let ocr = svc.worker.ocr_status().await.unwrap_or(json!({
        "available": false,
        "engine": "Tesseract",
        "device": "CPU",
        "languages": "chi_sim+eng"
    }));

    let admin_auth = std::env::var("RAG_ADMIN_TOKEN")
        .map(|t| !t.trim().is_empty())
        .unwrap_or(false);
    let mcp_auth = std::env::var("RAG_MCP_TOKEN")
        .map(|t| !t.trim().is_empty())
        .unwrap_or(false)
        || svc.keys.list().map(|k| !k.is_empty()).unwrap_or(false);

    if let Some(obj) = status.as_object_mut() {
        obj.insert("ocr".to_string(), ocr);
        obj.insert("admin_auth".to_string(), json!(admin_auth));
        obj.insert("mcp_auth".to_string(), json!(mcp_auth));
    }

    Json(status).into_response()
}

async fn enable_service(State(svc): State<AppState>) -> Response {
    match svc.set_enabled(true) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn disable_service(State(svc): State<AppState>) -> Response {
    match svc.set_enabled(false) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn get_config(State(svc): State<AppState>) -> Response {
    Json(svc.public_config()).into_response()
}

async fn save_config(State(svc): State<AppState>, Json(payload): Json<ModelConfig>) -> Response {
    match svc.save_config(payload).await {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn test_config(State(svc): State<AppState>, Json(payload): Json<ModelConfig>) -> Response {
    match svc.test_config(payload).await {
        Ok(dim) => Json(json!({ "dimension": dim })).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct ServicePayload {
    enabled: bool,
}

async fn update_service_state(State(svc): State<AppState>, Json(payload): Json<ServicePayload>) -> Response {
    match svc.set_enabled(payload.enabled) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct ContentQuery {
    #[serde(default)]
    offset: usize,
    #[serde(default = "default_content_limit")]
    limit: usize,
}

fn default_content_limit() -> usize {
    8000
}

async fn get_document_content(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Query(query): Query<ContentQuery>,
) -> Response {
    let limit = if query.limit == 0 { 8000 } else { query.limit.min(8000) };
    match svc.content(&id, query.offset, limit) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn get_ocr_status(State(svc): State<AppState>) -> Response {
    match svc.worker.ocr_status().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err_response(500, e),
    }
}

async fn list_documents(State(svc): State<AppState>) -> Response {
    match svc.list_documents(false, None) {
        Ok(v) => Json(json!({ "documents": v })).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn upload_document(
    State(svc): State<AppState>,
    Query(query): Query<OpQuery>,
    mut multipart: Multipart,
) -> Response {
    let mut filename = "document.bin".to_string();
    let mut file_data: Option<Vec<u8>> = None;
    let mut library_id: Option<String> = None;
    let mut folder_id: Option<String> = None;

    while let Ok(Some(field)) = multipart.next_field().await {
        let name = field.name().unwrap_or("").to_string();
        if name == "file" {
            if let Some(fname) = field.file_name() {
                filename = fname.to_string();
            }
            if let Ok(bytes) = field.bytes().await {
                file_data = Some(bytes.to_vec());
            }
        } else if name == "library_id" {
            if let Ok(text) = field.text().await {
                if !text.trim().is_empty() {
                    library_id = Some(text.trim().to_string());
                }
            }
        } else if name == "folder_id" {
            if let Ok(text) = field.text().await {
                if !text.trim().is_empty() {
                    folder_id = Some(text.trim().to_string());
                }
            }
        }
    }

    let data = match file_data {
        Some(d) => d,
        None => return err_response(400, "未上传文件数据。"),
    };

    if let Some(ref op_id) = query.operation_id {
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "upload".to_string(),
            state: "running".to_string(),
            stage: "parsing".to_string(),
            filename: filename.clone(),
            total: 1,
            completed: 0,
            failed: 0,
            current: 0,
            stage_total: None,
            error: "".to_string(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    let res = svc
        .upload(
            &filename,
            &data,
            library_id.as_deref(),
            folder_id.as_deref(),
        )
        .await;

    if let Some(ref op_id) = query.operation_id {
        let is_ok = res.is_ok();
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "upload".to_string(),
            state: if is_ok { "complete".to_string() } else { "failed".to_string() },
            stage: "done".to_string(),
            filename,
            total: 1,
            completed: if is_ok { 1 } else { 0 },
            failed: if is_ok { 0 } else { 1 },
            current: 0,
            stage_total: None,
            error: res.as_ref().err().map(|e| e.1.clone()).unwrap_or_default(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    match res {
        Ok(doc) => Json(doc).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn get_document(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.get_document(&id) {
        Ok(doc) => Json(doc).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn delete_document(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.delete_document(&id) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct OpQuery {
    operation_id: Option<String>,
}

async fn get_operation(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    if let Some(op) = svc.operations.get(&id) {
        Json(op).into_response()
    } else {
        err_response(404, "操作进度不存在或已过期。")
    }
}

async fn reindex_single_document(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Query(query): Query<OpQuery>,
) -> Response {
    let doc = match svc.get_document(&id) {
        Ok(d) => d,
        Err((c, m)) => return err_response(c, m),
    };
    let filename = doc["filename"].as_str().unwrap_or("").to_string();

    if let Some(ref op_id) = query.operation_id {
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "reindex".to_string(),
            state: "running".to_string(),
            stage: "embedding".to_string(),
            filename: filename.clone(),
            total: 1,
            completed: 0,
            failed: 0,
            current: 0,
            stage_total: None,
            error: "".to_string(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    let res = svc.reindex(Some(&id)).await;

    if let Some(ref op_id) = query.operation_id {
        let is_ok = res.is_ok();
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "reindex".to_string(),
            state: if is_ok { "complete".to_string() } else { "failed".to_string() },
            stage: "done".to_string(),
            filename,
            total: 1,
            completed: if is_ok { 1 } else { 0 },
            failed: if is_ok { 0 } else { 1 },
            current: 0,
            stage_total: None,
            error: res.as_ref().err().map(|e| e.1.clone()).unwrap_or_default(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    match res {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn reindex_all_documents(
    State(svc): State<AppState>,
    Query(query): Query<OpQuery>,
) -> Response {
    let docs = svc.list_documents(false, None).unwrap_or_default();
    let total = docs.len() as i64;

    if let Some(ref op_id) = query.operation_id {
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "reindex".to_string(),
            state: "running".to_string(),
            stage: "embedding".to_string(),
            filename: "".to_string(),
            total,
            completed: 0,
            failed: 0,
            current: 0,
            stage_total: None,
            error: "".to_string(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    let res = svc.reindex(None).await;

    if let Some(ref op_id) = query.operation_id {
        let is_ok = res.is_ok();
        svc.operations.update(OperationItem {
            id: op_id.clone(),
            kind: "reindex".to_string(),
            state: if is_ok { "complete".to_string() } else { "failed".to_string() },
            stage: "done".to_string(),
            filename: "".to_string(),
            total,
            completed: if is_ok { total } else { 0 },
            failed: if is_ok { 0 } else { total },
            current: 0,
            stage_total: None,
            error: res.as_ref().err().map(|e| e.1.clone()).unwrap_or_default(),
            updated: chrono::Utc::now().timestamp() as f64,
        });
    }

    match res {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct TogglePayload {
    enabled: bool,
}

async fn toggle_document(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Json(payload): Json<TogglePayload>,
) -> Response {
    match svc.update_document_enabled(&id, payload.enabled) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn move_document(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Json(payload): Json<Location>,
) -> Response {
    match svc.move_document(&id, payload) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct DownloadQuery {
    grant: Option<String>,
}

async fn download_document(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Query(query): Query<DownloadQuery>,
) -> Response {
    let doc = match svc.get_document(&id) {
        Ok(d) => d,
        Err((c, m)) => return err_response(c, m),
    };

    if let Some(grant) = query.grant {
        if let Err(e) = svc.keys.resolve_download(&id, &grant) {
            return err_response(401, e);
        }
    }

    let filename = doc["filename"].as_str().unwrap_or("document");
    let suffix = doc["suffix"].as_str().unwrap_or("");
    let file_path = svc.document_path(&id, suffix);

    if let Ok(bytes) = tokio::fs::read(&file_path).await {
        let cd = format!("attachment; filename=\"{}\"", filename);
        Response::builder()
            .header(header::CONTENT_DISPOSITION, cd)
            .header(header::CONTENT_TYPE, "application/octet-stream")
            .body(Body::from(bytes))
            .unwrap()
    } else {
        err_response(404, "文件不存在。")
    }
}

async fn preview_document(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    let doc = match svc.get_document(&id) {
        Ok(d) => d,
        Err((c, m)) => return err_response(c, m),
    };
    let suffix = doc["suffix"].as_str().unwrap_or("");
    let file_path = svc.document_path(&id, suffix);
    let bytes = match tokio::fs::read(&file_path).await {
        Ok(b) => b,
        Err(_) => return err_response(404, "源文件不存在。"),
    };

    match svc.worker.render_preview(&doc, &bytes).await {
        Ok(html) => Response::builder()
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::from(html))
            .unwrap(),
        Err(e) => err_response(500, e),
    }
}

async fn preview_source_document(
    State(svc): State<AppState>,
    Path(id): Path<String>,
) -> Response {
    let doc = match svc.get_document(&id) {
        Ok(d) => d,
        Err((c, m)) => return err_response(c, m),
    };
    let filename = doc["filename"].as_str().unwrap_or("document");
    let suffix = doc["suffix"].as_str().unwrap_or("");
    let file_path = svc.document_path(&id, suffix);
    let bytes = match tokio::fs::read(&file_path).await {
        Ok(b) => b,
        Err(_) => return err_response(404, "源文件不存在。"),
    };

    match svc.worker.preview_source(&id, filename, &bytes).await {
        Ok((file_bytes, media_type, preview_mode)) => {
            let mut resp = Response::builder()
                .header(header::CONTENT_TYPE, media_type)
                .header(
                    header::CONTENT_DISPOSITION,
                    format!("inline; filename=\"{}\"", filename),
                );
            if let Some(mode) = preview_mode {
                resp = resp.header("X-Preview-Mode", mode);
            }
            resp.body(Body::from(file_bytes)).unwrap()
        }
        Err(e) => err_response(500, e),
    }
}

#[derive(Deserialize)]
struct BatchMovePayload {
    document_ids: Vec<String>,
    library_id: String,
    folder_id: Option<String>,
}

async fn batch_move_documents(
    State(svc): State<AppState>,
    Json(payload): Json<BatchMovePayload>,
) -> Response {
    match svc.batch_move(
        &payload.document_ids,
        &payload.library_id,
        payload.folder_id.as_deref(),
    ) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

#[derive(Deserialize)]
struct BatchDeletePayload {
    document_ids: Vec<String>,
}

async fn batch_delete_documents(
    State(svc): State<AppState>,
    Json(payload): Json<BatchDeletePayload>,
) -> Response {
    match svc.batch_delete(&payload.document_ids) {
        Ok(v) => Json(v).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn list_libraries(State(svc): State<AppState>) -> Response {
    match svc.catalog.libraries() {
        Ok(libs) => Json(json!({ "libraries": libs })).into_response(),
        Err(e) => err_response(500, e),
    }
}

#[derive(Deserialize)]
struct CreateLibraryPayload {
    name: String,
}

async fn create_library(
    State(svc): State<AppState>,
    Json(payload): Json<CreateLibraryPayload>,
) -> Response {
    match svc.catalog.create_library(&payload.name) {
        Ok(lib) => Json(lib).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn update_library(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Json(payload): Json<CreateLibraryPayload>,
) -> Response {
    match svc.catalog.update_library(&id, &payload.name) {
        Ok(lib) => Json(lib).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn delete_library(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.catalog.delete_library(&id) {
        Ok(_) => Json(json!({ "deleted": true })).into_response(),
        Err(e) => err_response(400, e),
    }
}

#[derive(Deserialize)]
struct FolderQuery {
    library_id: Option<String>,
}

async fn list_folders(
    State(svc): State<AppState>,
    Query(query): Query<FolderQuery>,
) -> Response {
    match svc.catalog.folders(query.library_id.as_deref()) {
        Ok(folders) => Json(json!({ "folders": folders })).into_response(),
        Err(e) => err_response(500, e),
    }
}

#[derive(Deserialize)]
struct CreateFolderPayload {
    library_id: String,
    parent_id: Option<String>,
    name: String,
}

async fn create_folder(
    State(svc): State<AppState>,
    Json(payload): Json<CreateFolderPayload>,
) -> Response {
    match svc.catalog.create_folder(
        &payload.library_id,
        payload.parent_id.as_deref(),
        &payload.name,
    ) {
        Ok(folder) => Json(folder).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn delete_folder(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.catalog.delete_folder(&id) {
        Ok(_) => Json(json!({ "deleted": true })).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn list_keys(State(svc): State<AppState>) -> Response {
    match svc.keys.list() {
        Ok(keys) => Json(json!({ "keys": keys })).into_response(),
        Err(e) => err_response(500, e),
    }
}

async fn create_key(
    State(svc): State<AppState>,
    Json(payload): Json<crate::keys::KeyRequest>,
) -> Response {
    match svc.keys.create(&payload) {
        Ok(key) => Json(key).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn update_key(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Json(payload): Json<crate::keys::KeyRequest>,
) -> Response {
    match svc.keys.update(&id, &payload) {
        Ok(key) => Json(key).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn revoke_key(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.keys.revoke(&id) {
        Ok(_) => Json(json!({ "revoked": true })).into_response(),
        Err(e) => err_response(400, e),
    }
}

async fn delete_key(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.keys.delete(&id) {
        Ok(_) => Json(json!({ "deleted": true })).into_response(),
        Err(e) => err_response(400, e),
    }
}

#[derive(Deserialize)]
struct SearchPayload {
    query: String,
    #[serde(default = "default_top_k")]
    top_k: usize,
    library_id: Option<String>,
}

fn default_top_k() -> usize {
    5
}

async fn search_knowledge(
    State(svc): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<SearchPayload>,
) -> Response {
    let (allowed_libs, identity) = resolve_auth(&svc, &headers);
    match svc
        .search(
            &payload.query,
            payload.top_k,
            payload.library_id.as_deref(),
            allowed_libs.as_ref(),
            identity.as_deref(),
        )
        .await
    {
        Ok(hits) => Json(hits).into_response(),
        Err((c, m)) => err_response(c, m),
    }
}

async fn mcp_endpoint(
    State(svc): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<JsonRpcRequest>,
) -> Response {
    let (allowed_libs, identity) = resolve_auth(&svc, &headers);
    if let Some(resp) = handle_mcp_request(&svc, payload, allowed_libs, identity).await {
        Json(resp).into_response()
    } else {
        StatusCode::OK.into_response()
    }
}

fn resolve_auth(
    svc: &KnowledgeService,
    headers: &HeaderMap,
) -> (Option<HashSet<String>>, Option<String>) {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");

    if let Some(secret) = auth.strip_prefix("Bearer ").map(|s| s.trim()) {
        if secret.starts_with("zk_") {
            if let Ok((libs, id)) = svc.keys.resolve(secret) {
                return (libs, Some(id));
            }
        }
    }

    (None, None)
}


async fn mcp_get_endpoint() -> Response {
    Response::builder()
        .status(StatusCode::METHOD_NOT_ALLOWED)
        .header(header::ALLOW, "POST, OPTIONS")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(
            json!({ "detail": "MCP 接口仅支持 POST JSON-RPC 调用或 OPTIONS 预检。" }).to_string(),
        ))
        .unwrap()
}

async fn mcp_options_endpoint() -> Response {
    Response::builder()
        .status(StatusCode::NO_CONTENT)
        .header(header::ALLOW, "GET, POST, OPTIONS")
        .body(Body::empty())
        .unwrap()
}
