use std::collections::HashSet;
use std::sync::Arc;
use chrono::Utc;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use uuid::Uuid;

use crate::catalog::{Catalog, Location};
use crate::client::{EmbeddingClient, WorkerClient};
use crate::crypto::sha256_hex;
use crate::keys::AccessKeys;
use crate::storage::Storage;
use crate::vector::{calculate_norm, cosine_similarity, split_text, validate_vectors};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ModelConfig {
    pub base_url: String,
    pub model: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub clear_api_key: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct PublicModelConfig {
    pub base_url: String,
    pub model: String,
    pub has_api_key: bool,
    pub configured: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SearchHit {
    pub document_id: String,
    pub filename: String,
    pub library_id: String,
    pub folder_id: Option<String>,
    pub chunk_index: i64,
    pub text: String,
    pub score: f32,
    pub download_url: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SearchResponse {
    pub query: String,
    pub results: Vec<SearchHit>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ContentResponse {
    pub document_id: String,
    pub filename: String,
    pub text: String,
    pub total: usize,
    pub offset: usize,
    pub next_offset: Option<usize>,
    pub download_url: String,
}

#[derive(Clone)]
pub struct KnowledgeService {
    pub storage: Storage,
    pub catalog: Catalog,
    pub keys: AccessKeys,
    pub worker: WorkerClient,
    pub embedder: EmbeddingClient,
    lock: Arc<RwLock<()>>,
}

impl KnowledgeService {
    pub fn new(storage: Storage, worker_url: &str) -> Self {
        let catalog = Catalog::new(storage.clone());
        let keys = AccessKeys::new(storage.clone(), catalog.clone());
        let worker = WorkerClient::new(worker_url);
        let embedder = EmbeddingClient::new();

        Self {
            storage,
            catalog,
            keys,
            worker,
            embedder,
            lock: Arc::new(RwLock::new(())),
        }
    }

    pub fn check_active(&self) -> Result<(), (u16, String)> {
        let enabled = self
            .storage
            .get_setting("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        if !enabled {
            Err((503, "业务已暂停，请在管理页面恢复服务。".to_string()))
        } else {
            Ok(())
        }
    }

    pub fn config(&self) -> ModelConfig {
        let setting = self.storage.get_setting("config").unwrap_or_else(|| {
            json!({
                "base_url": "https://api.openai.com/v1",
                "model": "",
                "encrypted_key": ""
            })
        });

        let base_url = setting
            .get("base_url")
            .and_then(|v| v.as_str())
            .unwrap_or("https://api.openai.com/v1")
            .to_string();
        let model = setting
            .get("model")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let enc_key = setting
            .get("encrypted_key")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let api_key = self.storage.decrypt(enc_key).unwrap_or_default();

        ModelConfig {
            base_url,
            model,
            api_key,
            clear_api_key: false,
        }
    }

    pub fn public_config(&self) -> PublicModelConfig {
        let cfg = self.config();
        PublicModelConfig {
            base_url: cfg.base_url,
            model: cfg.model.clone(),
            has_api_key: !cfg.api_key.is_empty(),
            configured: !cfg.model.is_empty(),
        }
    }

    pub async fn save_config(&self, req: ModelConfig) -> Result<PublicModelConfig, (u16, String)> {
        let _guard = self.lock.write().await;
        let old = self.config();
        let base_url = req.base_url.trim().trim_end_matches('/').to_string();
        if base_url.is_empty() {
            return Err((400, "接口地址不能为空。".to_string()));
        }
        let model = req.model.trim().to_string();
        if model.is_empty() {
            return Err((400, "模型名称不能为空。".to_string()));
        }

        let api_key = if req.clear_api_key {
            "".to_string()
        } else if !req.api_key.trim().is_empty() {
            req.api_key.trim().to_string()
        } else {
            old.api_key
        };

        let encrypted_key = self
            .storage
            .encrypt(&api_key)
            .map_err(|e| (500, format!("加密 API Key 失败: {}", e)))?;

        let val = json!({
            "base_url": base_url,
            "model": model,
            "encrypted_key": encrypted_key,
        });

        self.storage
            .put_setting("config", &val)
            .map_err(|e| (500, e))?;

        Ok(self.public_config())
    }

    pub fn fingerprint(&self) -> String {
        let cfg = self.config();
        let raw = format!("{}::{}", cfg.base_url, cfg.model);
        sha256_hex(raw.as_bytes())
    }

    pub fn status(&self) -> Value {
        let cfg = self.config();
        let fp = self.fingerprint();
        let enabled = self
            .storage
            .get_setting("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);

        let (total_docs, ready_docs, failed_docs) = self
            .storage
            .with_conn(|conn| {
                let total: i64 = conn.query_row("SELECT COUNT(1) FROM documents", [], |r| r.get(0))?;
                let ready: i64 = conn.query_row(
                    "SELECT COUNT(1) FROM documents WHERE status = 'ready' AND fingerprint = ?",
                    params![fp],
                    |r| r.get(0),
                )?;
                let failed: i64 = conn.query_row(
                    "SELECT COUNT(1) FROM documents WHERE status = 'failed'",
                    [],
                    |r| r.get(0),
                )?;
                Ok((total, ready, failed))
            })
            .unwrap_or((0, 0, 0));

        json!({
            "enabled": enabled,
            "configured": !cfg.model.is_empty(),
            "model": cfg.model,
            "base_url": cfg.base_url,
            "total_documents": total_docs,
            "ready_documents": ready_docs,
            "failed_documents": failed_docs,
        })
    }

    pub fn set_enabled(&self, enabled: bool) -> Result<Value, (u16, String)> {
        self.storage
            .put_setting("enabled", &Value::Bool(enabled))
            .map_err(|e| (500, e))?;
        Ok(self.status())
    }

    pub fn list_documents(&self, tool: bool, allowed_libs: Option<&HashSet<String>>) -> Result<Vec<Value>, (u16, String)> {
        let fp = self.fingerprint();
        self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                r#"
                SELECT id, filename, suffix, size, created_at, enabled, status, error, chunk_count, dimension, library_id, folder_id, fingerprint, extraction_info
                FROM documents ORDER BY created_at DESC
                "#
            )?;

            let rows = stmt.query_map([], |row| {
                let id: String = row.get(0)?;
                let filename: String = row.get(1)?;
                let suffix: String = row.get(2)?;
                let size: i64 = row.get(3)?;
                let created_at: String = row.get(4)?;
                let enabled_val: i64 = row.get(5)?;
                let raw_status: String = row.get(6)?;
                let error: String = row.get(7)?;
                let chunk_count: i64 = row.get(8)?;
                let dimension: i64 = row.get(9)?;
                let library_id: Option<String> = row.get(10)?;
                let folder_id: Option<String> = row.get(11)?;
                let doc_fp: String = row.get(12)?;
                let ext_info: String = row.get(13)?;

                let status = if raw_status == "ready" && doc_fp != fp {
                    "stale".to_string()
                } else {
                    raw_status
                };

                let extraction: Value = serde_json::from_str(&ext_info).unwrap_or_else(|_| json!({}));

                Ok((id, filename, suffix, size, created_at, enabled_val != 0, status, error, chunk_count, dimension, library_id, folder_id, extraction))
            })?;

            let mut docs = Vec::new();
            for r in rows {
                let (id, filename, suffix, size, created_at, enabled, status, error, chunk_count, dimension, library_id, folder_id, extraction) = r?;
                if let Some(lib_id) = &library_id {
                    if let Some(allowed) = allowed_libs {
                        if !allowed.contains(lib_id) {
                            continue;
                        }
                    }
                }
                if tool && !enabled {
                    continue;
                }

                let dl_url = self.keys.generate_source_url(&id, None);
                docs.push(json!({
                    "id": id,
                    "filename": filename,
                    "suffix": suffix,
                    "size": size,
                    "created_at": created_at,
                    "enabled": enabled,
                    "status": status,
                    "error": error,
                    "chunk_count": chunk_count,
                    "dimension": dimension,
                    "library_id": library_id,
                    "folder_id": folder_id,
                    "extraction_info": extraction,
                    "download_url": dl_url,
                }));
            }
            Ok(docs)
        }).map_err(|e| (500, e))
    }

    pub fn get_document(&self, document_id: &str) -> Result<Value, (u16, String)> {
        let fp = self.fingerprint();
        self.storage.with_conn(|conn| {
            let val = conn.query_row(
                "SELECT id, filename, suffix, size, created_at, enabled, status, error, chunk_count, dimension, library_id, folder_id, fingerprint, extraction_info, text FROM documents WHERE id = ?",
                params![document_id],
                |row| {
                    let id: String = row.get(0)?;
                    let filename: String = row.get(1)?;
                    let suffix: String = row.get(2)?;
                    let size: i64 = row.get(3)?;
                    let created_at: String = row.get(4)?;
                    let enabled_val: i64 = row.get(5)?;
                    let raw_status: String = row.get(6)?;
                    let error: String = row.get(7)?;
                    let chunk_count: i64 = row.get(8)?;
                    let dimension: i64 = row.get(9)?;
                    let library_id: Option<String> = row.get(10)?;
                    let folder_id: Option<String> = row.get(11)?;
                    let doc_fp: String = row.get(12)?;
                    let ext_info: String = row.get(13)?;
                    let text: String = row.get(14)?;

                    let status = if raw_status == "ready" && doc_fp != fp {
                        "stale".to_string()
                    } else {
                        raw_status
                    };

                    let extraction: Value = serde_json::from_str(&ext_info).unwrap_or_else(|_| json!({}));
                    let dl_url = self.keys.generate_source_url(&id, None);

                    Ok(json!({
                        "id": id,
                        "filename": filename,
                        "suffix": suffix,
                        "size": size,
                        "created_at": created_at,
                        "enabled": enabled_val != 0,
                        "status": status,
                        "error": error,
                        "chunk_count": chunk_count,
                        "dimension": dimension,
                        "library_id": library_id,
                        "folder_id": folder_id,
                        "extraction_info": extraction,
                        "download_url": dl_url,
                        "text": text,
                    }))
                },
            )?;
            Ok(val)
        }).map_err(|_| (404, "文档不存在。".to_string()))
    }

    pub async fn upload(
        &self,
        filename: &str,
        content: &[u8],
        library_id: Option<&str>,
        folder_id: Option<&str>,
    ) -> Result<Value, (u16, String)> {
        self.check_active()?;

        let lib_id = match library_id {
            Some(id) => id.to_string(),
            None => {
                let libs = self.catalog.libraries().map_err(|e| (500, e))?;
                if libs.is_empty() {
                    return Err((400, "请先创建知识库，再上传文件。".to_string()));
                }
                libs[0].id.clone()
            }
        };

        self.catalog
            .validate_location(&lib_id, folder_id)
            .map_err(|e| (400, e))?;

        let clean_filename = std::path::Path::new(filename)
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| "document".to_string());

        let extracted = self
            .worker
            .extract_document(&clean_filename, content)
            .await
            .map_err(|e| (400, e))?;

        let doc_id = Uuid::new_v4().simple().to_string();
        let target_file = self.storage.uploads.join(format!("{}{}", doc_id, extracted.suffix));
        std::fs::write(&target_file, content).map_err(|e| (500, format!("写入上传文件失败: {}", e)))?;

        let now = Utc::now().to_rfc3339();
        let ext_json = serde_json::to_string(&extracted.extraction_info).unwrap_or_default();

        let insert_res = self.storage.with_conn(|conn| {
            conn.execute(
                r#"
                INSERT INTO documents (
                    id, filename, suffix, size, text, created_at, status, library_id, folder_id, extraction_info
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                "#,
                params![
                    doc_id,
                    clean_filename,
                    extracted.suffix,
                    content.len() as i64,
                    extracted.text,
                    now,
                    lib_id,
                    folder_id,
                    ext_json
                ],
            )?;
            Ok(())
        });

        if let Err(e) = insert_res {
            let _ = std::fs::remove_file(target_file);
            return Err((500, e));
        }

        let cfg = self.config();
        if !cfg.model.is_empty() {
            let _ = self.index_document(&doc_id).await;
        }

        self.get_document(&doc_id)
    }

    pub async fn index_document(&self, doc_id: &str) -> Result<(), String> {
        let doc = self.get_document(doc_id).map_err(|(_, e)| e)?;
        let text = doc["text"].as_str().unwrap_or("");
        let pieces = split_text(text);

        if pieces.is_empty() {
            self.storage.with_conn(|conn| {
                conn.execute(
                    "UPDATE documents SET status = 'failed', error = '文档没有有效文本分块' WHERE id = ?",
                    params![doc_id],
                )?;
                Ok(())
            })?;
            return Ok(());
        }

        let cfg = self.config();
        let vectors = self
            .embedder
            .embed(&cfg.base_url, &cfg.model, &cfg.api_key, &pieces)
            .await
            .map_err(|e| {
                let _ = self.storage.with_conn(|conn| {
                    conn.execute(
                        "UPDATE documents SET status = 'failed', error = ? WHERE id = ?",
                        params![e, doc_id],
                    )?;
                    Ok(())
                });
                e
            })?;

        let dimension = validate_vectors(&vectors, pieces.len())?;
        let fp = self.fingerprint();

        self.storage.with_conn_mut(|conn| {
            let tx = conn.transaction()?;
            tx.execute("DELETE FROM chunks WHERE document_id = ?", params![doc_id])?;

            {
                let mut stmt = tx.prepare(
                    "INSERT INTO chunks (document_id, chunk_index, text, vector) VALUES (?, ?, ?, ?)"
                )?;
                for (idx, (p_text, vec)) in pieces.into_iter().zip(vectors.into_iter()).enumerate() {
                    let vec_json = serde_json::to_string(&vec).unwrap_or_default();
                    stmt.execute(params![doc_id, (idx + 1) as i64, p_text, vec_json])?;
                }
            }

            tx.execute(
                "UPDATE documents SET status = 'ready', error = '', fingerprint = ?, chunk_count = ?, dimension = ? WHERE id = ?",
                params![fp, doc["chunk_count"].as_i64().unwrap_or(0), dimension as i64, doc_id],
            )?;

            tx.commit()?;
            Ok(())
        })?;

        Ok(())
    }

    pub async fn reindex(&self, document_id: Option<&str>) -> Result<Value, (u16, String)> {
        self.check_active()?;
        let cfg = self.config();
        if cfg.model.is_empty() {
            return Err((400, "请先配置 embedding 模型。".to_string()));
        }

        let doc_ids: Vec<String> = match document_id {
            Some(id) => vec![id.to_string()],
            None => {
                let docs = self.list_documents(false, None)?;
                docs.into_iter()
                    .filter_map(|d| d["id"].as_str().map(|s| s.to_string()))
                    .collect()
            }
        };

        for id in &doc_ids {
            let _ = self.index_document(id).await;
        }

        if let Some(id) = document_id {
            self.get_document(id)
        } else {
            let docs = self.list_documents(false, None)?;
            let failed = docs.iter().filter(|d| d["status"] == "failed").count();
            Ok(json!({
                "documents": docs,
                "failed_count": failed,
            }))
        }
    }

    pub async fn search(
        &self,
        query: &str,
        top_k: usize,
        library_id: Option<&str>,
        allowed_libs: Option<&HashSet<String>>,
        identity: Option<&str>,
    ) -> Result<SearchResponse, (u16, String)> {
        self.check_active()?;
        let clean_query = query.trim();
        if clean_query.is_empty() {
            return Err((400, "检索问题不能为空。".to_string()));
        }

        if let Some(lib_id) = library_id {
            if let Some(allowed) = allowed_libs {
                if !allowed.contains(lib_id) {
                    return Err((403, "该 API Key 未获授权访问此知识库。".to_string()));
                }
            }
            self.catalog
                .validate_location(lib_id, None)
                .map_err(|e| (404, e))?;
        }

        let cfg = self.config();
        if cfg.model.is_empty() {
            return Err((400, "请先在模型配置页填写 embedding 接口和模型名称。".to_string()));
        }

        let fp = self.fingerprint();

        let candidate_chunks: Vec<(String, String, String, Option<String>, i64, String, Vec<f32>)> = self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                r#"
                SELECT c.document_id, d.filename, d.library_id, d.folder_id, c.chunk_index, c.text, c.vector
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.enabled = 1 AND d.status = 'ready' AND d.fingerprint = ?
                  AND (? IS NULL OR d.library_id = ?)
                "#
            )?;

            let rows = stmt.query_map(params![fp, library_id, library_id], |row| {
                let doc_id: String = row.get(0)?;
                let filename: String = row.get(1)?;
                let lib_id: String = row.get(2)?;
                let folder_id: Option<String> = row.get(3)?;
                let chunk_idx: i64 = row.get(4)?;
                let text: String = row.get(5)?;
                let vec_str: String = row.get(6)?;
                Ok((doc_id, filename, lib_id, folder_id, chunk_idx, text, vec_str))
            })?;

            let mut list = Vec::new();
            for r in rows {
                let (doc_id, filename, lib_id, folder_id, chunk_idx, text, vec_str) = r?;
                if let Some(allowed) = allowed_libs {
                    if !allowed.contains(&lib_id) {
                        continue;
                    }
                }
                let vec: Vec<f32> = serde_json::from_str(&vec_str).unwrap_or_default();
                list.push((doc_id, filename, lib_id, folder_id, chunk_idx, text, vec));
            }
            Ok(list)
        }).map_err(|e| (500, e))?;

        if candidate_chunks.is_empty() {
            return Ok(SearchResponse {
                query: clean_query.to_string(),
                results: Vec::new(),
            });
        }

        let query_vectors = self
            .embedder
            .embed(&cfg.base_url, &cfg.model, &cfg.api_key, &[clean_query.to_string()])
            .await
            .map_err(|e| (500, e))?;

        let query_vector = &query_vectors[0];
        let query_norm = calculate_norm(query_vector);

        let mut scored_hits = Vec::with_capacity(candidate_chunks.len());
        for (doc_id, filename, lib_id, folder_id, chunk_idx, text, target_vec) in candidate_chunks {
            if target_vec.len() != query_vector.len() {
                return Err((409, "模型向量维度已变化，请重建全部文件索引。".to_string()));
            }

            let score = cosine_similarity(query_vector, &target_vec, query_norm);
            let dl_url = self.keys.generate_source_url(&doc_id, identity);

            scored_hits.push(SearchHit {
                document_id: doc_id,
                filename,
                library_id: lib_id,
                folder_id,
                chunk_index: chunk_idx,
                text,
                score,
                download_url: dl_url,
            });
        }

        scored_hits.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        scored_hits.truncate(top_k);

        Ok(SearchResponse {
            query: clean_query.to_string(),
            results: scored_hits,
        })
    }

    pub fn content(&self, doc_id: &str, offset: usize, limit: usize) -> Result<ContentResponse, (u16, String)> {
        let doc = self.get_document(doc_id)?;
        let full_text = doc["text"].as_str().unwrap_or("");
        let chars: Vec<char> = full_text.chars().collect();
        let total = chars.len();

        let slice_end = (offset + limit).min(total);
        let slice: String = if offset < total {
            chars[offset..slice_end].iter().collect()
        } else {
            "".to_string()
        };

        let next_offset = if slice_end < total { Some(slice_end) } else { None };
        let dl_url = self.keys.generate_source_url(doc_id, None);

        Ok(ContentResponse {
            document_id: doc_id.to_string(),
            filename: doc["filename"].as_str().unwrap_or("").to_string(),
            text: slice,
            total,
            offset,
            next_offset,
            download_url: dl_url,
        })
    }

    pub fn update_document_enabled(&self, doc_id: &str, enabled: bool) -> Result<Value, (u16, String)> {
        self.get_document(doc_id)?;
        self.storage.with_conn(|conn| {
            conn.execute("UPDATE documents SET enabled = ? WHERE id = ?", params![enabled as i64, doc_id])?;
            Ok(())
        }).map_err(|e| (500, e))?;
        self.get_document(doc_id)
    }

    pub fn move_document(&self, doc_id: &str, loc: Location) -> Result<Value, (u16, String)> {
        self.get_document(doc_id)?;
        self.catalog
            .validate_location(&loc.library_id, loc.folder_id.as_deref())
            .map_err(|e| (400, e))?;

        self.storage.with_conn(|conn| {
            conn.execute(
                "UPDATE documents SET library_id = ?, folder_id = ? WHERE id = ?",
                params![loc.library_id, loc.folder_id, doc_id],
            )?;
            Ok(())
        }).map_err(|e| (500, e))?;
        self.get_document(doc_id)
    }

    pub fn delete_document(&self, doc_id: &str) -> Result<Value, (u16, String)> {
        let doc = self.get_document(doc_id)?;
        let suffix = doc["suffix"].as_str().unwrap_or("");
        let path = self.storage.uploads.join(format!("{}{}", doc_id, suffix));
        let _ = std::fs::remove_file(path);
        self.storage.remove_preview(doc_id);

        self.storage.with_conn(|conn| {
            conn.execute("DELETE FROM documents WHERE id = ?", params![doc_id])?;
            Ok(())
        }).map_err(|e| (500, e))?;

        Ok(json!({ "deleted": true }))
    }

    pub fn batch_move(&self, doc_ids: &[String], library_id: &str, folder_id: Option<&str>) -> Result<Value, (u16, String)> {
        self.catalog
            .validate_location(library_id, folder_id)
            .map_err(|e| (400, e))?;

        let mut count = 0;
        self.storage.with_conn_mut(|conn| {
            let tx = conn.transaction()?;
            for id in doc_ids {
                count += tx.execute(
                    "UPDATE documents SET library_id = ?, folder_id = ? WHERE id = ?",
                    params![library_id, folder_id, id],
                )?;
            }
            tx.commit()?;
            Ok(())
        }).map_err(|e| (500, e))?;

        Ok(json!({ "moved_count": count }))
    }

    pub fn batch_delete(&self, doc_ids: &[String]) -> Result<Value, (u16, String)> {
        for id in doc_ids {
            let _ = self.delete_document(id);
        }
        Ok(json!({ "deleted_count": doc_ids.len() }))
    }
}
