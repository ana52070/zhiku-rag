use reqwest::multipart::{Form, Part};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::vector::validate_vectors;

#[derive(Clone)]
pub struct WorkerClient {
    client: Client,
    worker_url: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ExtractResponse {
    pub suffix: String,
    pub text: String,
    pub extraction_info: Value,
}

impl WorkerClient {
    pub async fn smb(&self, action: &str, payload: &Value) -> Result<reqwest::Response, String> {
        self.source("smb", action, payload).await
    }
    pub async fn source(&self, kind: &str, action: &str, payload: &Value) -> Result<reqwest::Response, String> {
        if !["smb", "local"].contains(&kind) || !["test", "scan", "read"].contains(&action) {
            return Err("来源操作无效。".into());
        }
        let token = std::env::var("RAG_WORKER_TOKEN").unwrap_or_default();
        if token.is_empty() { return Err("请配置 Rust 与 Worker 共用的 RAG_WORKER_TOKEN。".into()); }
        let response = self.client.post(format!("{}/{}/{}", self.worker_url, kind, action))
            .timeout(std::time::Duration::from_secs(3600))
            .header("X-Worker-Token", token).json(payload).send().await
            .map_err(|_| "SMB Worker 请求失败；本次不执行删除。".to_string())?;
        if !response.status().is_success() {
            let value: Value = response.json().await.unwrap_or_default();
            return Err(value["detail"].as_str().unwrap_or("SMB Worker 访问失败。").to_string());
        }
        Ok(response)
    }
    pub fn new(worker_url: &str) -> Self {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()
            .unwrap_or_default();
        Self {
            client,
            worker_url: worker_url.trim_end_matches('/').to_string(),
        }
    }

    pub async fn extract_document(
        &self,
        filename: &str,
        content: &[u8],
    ) -> Result<ExtractResponse, String> {
        let part = Part::bytes(content.to_vec()).file_name(filename.to_string());
        let form = Form::new().part("file", part);

        let url = format!("{}/extract", self.worker_url);
        let resp = self
            .client
            .post(&url)
            .multipart(form)
            .send()
            .await
            .map_err(|e| format!("调用文档解析 Worker 失败: {}", e))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err_body: Value = resp.json().await.unwrap_or_default();
            let msg = err_body
                .get("detail")
                .and_then(|v| v.as_str())
                .unwrap_or("未知解析错误");
            return Err(format!("Worker 错误 (HTTP {}): {}", status, msg));
        }

        resp.json::<ExtractResponse>()
            .await
            .map_err(|e| format!("解析 Worker 响应 JSON 失败: {}", e))
    }

    pub async fn render_preview(
        &self,
        document: &Value,
        content: &[u8],
    ) -> Result<String, String> {
        let doc_json = serde_json::to_string(document).unwrap_or_default();
        let part = Part::bytes(content.to_vec()).file_name("document");
        let form = Form::new()
            .text("document", doc_json)
            .part("file", part);

        let url = format!("{}/preview", self.worker_url);
        let resp = self
            .client
            .post(&url)
            .multipart(form)
            .send()
            .await
            .map_err(|e| format!("调用预览渲染 Worker 失败: {}", e))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err_body: Value = resp.json().await.unwrap_or_default();
            let msg = err_body
                .get("detail")
                .and_then(|v| v.as_str())
                .unwrap_or("预览渲染错误");
            return Err(format!("Worker 错误 (HTTP {}): {}", status, msg));
        }

        resp.text()
            .await
            .map_err(|e| format!("读取预览响应失败: {}", e))
    }

    pub async fn preview_source(
        &self,
        document_id: &str,
        filename: &str,
        content: &[u8],
    ) -> Result<(Vec<u8>, String, Option<String>), String> {
        let part = Part::bytes(content.to_vec()).file_name(filename.to_string());
        let form = Form::new()
            .text("document_id", document_id.to_string())
            .text("filename", filename.to_string())
            .part("file", part);

        let url = format!("{}/preview-source", self.worker_url);
        let resp = self
            .client
            .post(&url)
            .multipart(form)
            .send()
            .await
            .map_err(|e| format!("调用 Word 预览转换失败: {}", e))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err_body: Value = resp.json().await.unwrap_or_default();
            let msg = err_body
                .get("detail")
                .and_then(|v| v.as_str())
                .unwrap_or("转换错误");
            return Err(format!("Worker 错误 (HTTP {}): {}", status, msg));
        }

        let media_type = resp
            .headers()
            .get("content-type")
            .and_then(|h| h.to_str().ok())
            .unwrap_or("application/octet-stream")
            .to_string();

        let preview_mode = resp
            .headers()
            .get("x-preview-mode")
            .and_then(|h| h.to_str().ok())
            .map(|s| s.to_string());

        let bytes = resp
            .bytes()
            .await
            .map_err(|e| format!("读取文件字节失败: {}", e))?
            .to_vec();

        Ok((bytes, media_type, preview_mode))
    }

    pub async fn ocr_status(&self) -> Result<Value, String> {
        let url = format!("{}/ocr/status", self.worker_url);
        let resp = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| format!("获取 OCR 状态失败: {}", e))?;
        resp.json::<Value>()
            .await
            .map_err(|e| format!("解析 OCR 状态失败: {}", e))
    }
}

#[derive(Clone)]
pub struct EmbeddingClient {
    client: Client,
}

#[derive(Serialize)]
struct EmbeddingPayload<'a> {
    model: &'a str,
    input: &'a [String],
}

#[derive(Deserialize)]
struct EmbeddingItem {
    index: usize,
    embedding: Vec<f32>,
}

#[derive(Deserialize)]
struct EmbeddingApiResponse {
    data: Vec<EmbeddingItem>,
}

impl EmbeddingClient {
    pub fn new() -> Self {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(45))
            .build()
            .unwrap_or_default();
        Self { client }
    }

    pub async fn embed(
        &self,
        base_url: &str,
        model: &str,
        api_key: &str,
        texts: &[String],
    ) -> Result<Vec<Vec<f32>>, String> {
        let mut results = Vec::with_capacity(texts.len());
        let url = format!("{}/embeddings", base_url.trim_end_matches('/'));

        for chunk in texts.chunks(32) {
            let payload = EmbeddingPayload {
                model,
                input: chunk,
            };

            let mut req = self.client.post(&url).json(&payload);
            if !api_key.trim().is_empty() {
                req = req.header("Authorization", format!("Bearer {}", api_key.trim()));
            }

            let resp = req.send().await.map_err(|e| {
                if e.is_timeout() {
                    "模型接口响应超时，请检查网络或缩小文件后重试。".to_string()
                } else {
                    format!("模型接口连接失败，请检查地址和网络: {}", e)
                }
            })?;

            let status = resp.status();
            if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
                return Err("模型接口拒绝访问，请检查 API Key 和权限。".to_string());
            }
            if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
                return Err("模型接口限流或额度不足，请稍后重试。".to_string());
            }
            if !status.is_success() {
                return Err(format!("模型接口返回 HTTP {}，请检查地址和模型名称。", status.as_u16()));
            }

            let api_resp: EmbeddingApiResponse = resp
                .json()
                .await
                .map_err(|_| "模型接口响应格式不兼容，需要返回 data/index/embedding。".to_string())?;

            let mut ordered = api_resp.data;
            ordered.sort_by_key(|item| item.index);

            if ordered.len() != chunk.len() {
                return Err("模型接口返回的向量数量与输入不一致。".to_string());
            }
            for (idx, item) in ordered.iter().enumerate() {
                if item.index != idx {
                    return Err("模型接口返回的向量索引不完整。".to_string());
                }
            }

            let mut batch_vectors: Vec<Vec<f32>> = ordered.into_iter().map(|it| it.embedding).collect();
            validate_vectors(&batch_vectors, chunk.len())?;
            results.append(&mut batch_vectors);
        }

        validate_vectors(&results, texts.len())?;
        Ok(results)
    }
}
