use crate::{api::AppState, sync::TaskConfig};
use axum::{
    extract::Request,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/sync/tasks", get(list).post(create))
        .route(
            "/api/sync/tasks/{id}",
            get(detail).put(update).delete(remove),
        )
        .route("/api/sync/tasks/{id}/run", post(run))
        .route("/api/sync/tasks/{id}/runs", get(runs))
        .route("/api/sync/test", post(test))
        .route("/api/sync/schedule", post(schedule))
        .route_layer(middleware::from_fn(auth))
}
async fn auth(headers: HeaderMap, request: Request, next: Next) -> Response {
    let expected = std::env::var("RAG_ADMIN_TOKEN").unwrap_or_default();
    if expected.is_empty() {
        return error(503, "请先为同步管理配置 RAG_ADMIN_TOKEN。");
    }
    let actual = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    let mut mac = Hmac::<Sha256>::new_from_slice(expected.as_bytes()).unwrap();
    mac.update(actual.as_bytes());
    let mut reference = Hmac::<Sha256>::new_from_slice(expected.as_bytes()).unwrap();
    reference.update(expected.as_bytes());
    if mac
        .verify_slice(&reference.finalize().into_bytes())
        .is_err()
    {
        return error(401, "管理员令牌无效。");
    }
    next.run(request).await
}
fn error(code: u16, message: impl Into<String>) -> Response {
    (
        StatusCode::from_u16(code).unwrap(),
        Json(json!({"detail":message.into()})),
    )
        .into_response()
}
fn result(value: Result<Value, String>) -> Response {
    match value {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(400, e),
    }
}
async fn list(State(svc): State<AppState>) -> Response {
    result(svc.sync_tasks().map(|v| json!(v)))
}
async fn detail(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    result(svc.public_task(&id))
}
async fn create(State(svc): State<AppState>, Json(cfg): Json<TaskConfig>) -> Response {
    let Ok(_guard) = svc.sync_lock.clone().try_lock_owned() else {
        return error(409, "同步运行中，请稍后修改任务。");
    };
    result(svc.save_sync_task(None, cfg))
}
async fn update(
    State(svc): State<AppState>,
    Path(id): Path<String>,
    Json(cfg): Json<TaskConfig>,
) -> Response {
    let Ok(_guard) = svc.sync_lock.clone().try_lock_owned() else {
        return error(409, "同步运行中，请稍后修改任务。");
    };
    result(svc.save_sync_task(Some(&id), cfg))
}
async fn remove(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    let Ok(_guard) = svc.sync_lock.clone().try_lock_owned() else {
        return error(409, "同步运行中，请稍后删除任务。");
    };
    result(svc.delete_sync_task(&id).map(|_| json!({"deleted":true})))
}
async fn run(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    match svc.start_sync(&id) {
        Ok(id) => (StatusCode::ACCEPTED, Json(json!({"id":id}))).into_response(),
        Err((code, e)) => error(code, e),
    }
}
async fn runs(State(svc): State<AppState>, Path(id): Path<String>) -> Response {
    result(svc.sync_runs(&id).map(|v| json!(v)))
}
async fn schedule(Json(cfg): Json<crate::sync_schedule::ScheduleConfig>) -> Response {
    result(
        crate::sync_schedule::next_run(&cfg, chrono::Utc::now())
            .map(|date| json!({"next_run":date.to_rfc3339()})),
    )
}
async fn test(State(svc): State<AppState>, Json(mut payload): Json<Value>) -> Response {
    if let Some(id) = payload["task_id"].as_str() {
        if payload["password"].as_str().unwrap_or("").is_empty() {
            match svc
                .sync_task(id)
                .and_then(|(cfg, _)| svc.storage.decrypt(&cfg.password))
            {
                Ok(password) => payload["password"] = json!(password),
                Err(e) => return error(400, e),
            }
        }
    }
    match svc.worker.smb("test", &payload).await {
        Ok(response) => result(
            response
                .json::<Value>()
                .await
                .map_err(|_| "连接测试响应无效。".into()),
        ),
        Err(e) => error(502, e),
    }
}
