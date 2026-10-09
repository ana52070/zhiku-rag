use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::Utc;
use hmac::{Hmac, Mac};
use rand::RngCore;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Sha256;
use uuid::Uuid;

use crate::catalog::Catalog;
use crate::crypto::sha256_hex;
use crate::storage::{AppError, Storage};

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Serialize, Deserialize)]
pub struct KeyRequest {
    pub name: String,
    #[serde(default)]
    pub library_ids: Vec<String>,
    #[serde(default)]
    pub all_libraries: bool,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AccessKeyPublic {
    pub id: String,
    pub name: String,
    pub prefix: String,
    pub library_ids: Vec<String>,
    pub all_libraries: bool,
    pub created_at: String,
    pub revoked: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,
}

#[derive(Clone)]
pub struct AccessKeys {
    storage: Storage,
    catalog: Catalog,
}

impl AccessKeys {
    pub fn new(storage: Storage, catalog: Catalog) -> Self {
        Self { storage, catalog }
    }

    pub fn configured(&self) -> bool {
        let has_keys: bool = self
            .storage
            .with_conn(|conn| {
                conn.query_row("SELECT 1 FROM access_keys LIMIT 1", [], |_| Ok(true))
                    .map_err(|e| AppError::new(e.to_string()))
            })
            .unwrap_or(false);

        let required = self
            .storage
            .get_setting("key_auth_required")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        has_keys || required
    }

    pub fn validate(&self, req: &KeyRequest) -> Result<(), String> {
        if !req.all_libraries && req.library_ids.is_empty() {
            return Err("请选择至少一个知识库。".to_string());
        }
        for lib_id in &req.library_ids {
            self.catalog.validate_location(lib_id, None)?;
        }
        Ok(())
    }

    pub fn list(&self) -> Result<Vec<AccessKeyPublic>, String> {
        self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, name, prefix, library_ids, all_libraries, created_at, revoked FROM access_keys ORDER BY created_at DESC",
            )?;

            let rows = stmt.query_map([], |row| {
                let lib_json: String = row.get(3)?;
                let lib_ids: Vec<String> = serde_json::from_str(&lib_json).unwrap_or_default();
                let all_libs: i64 = row.get(4)?;
                let revoked: i64 = row.get(6)?;

                Ok(AccessKeyPublic {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    prefix: row.get(2)?,
                    library_ids: lib_ids,
                    all_libraries: all_libs != 0,
                    created_at: row.get(5)?,
                    revoked: revoked != 0,
                    key: None,
                })
            })?;

            let mut list = Vec::new();
            for r in rows {
                list.push(r?);
            }
            Ok(list)
        })
    }

    pub fn create(&self, req: &KeyRequest) -> Result<AccessKeyPublic, String> {
        self.validate(req)?;
        self.storage.put_setting("key_auth_required", &Value::Bool(true))?;

        let mut random_bytes = [0u8; 24];
        rand::thread_rng().fill_bytes(&mut random_bytes);
        let secret = format!("zk_{}", URL_SAFE_NO_PAD.encode(random_bytes));
        let digest = sha256_hex(secret.as_bytes());
        let prefix = secret.chars().take(10).collect::<String>();
        let id = Uuid::new_v4().simple().to_string();
        let now = Utc::now().to_rfc3339();

        let mut sorted_libs = req.library_ids.clone();
        sorted_libs.sort();
        sorted_libs.dedup();
        let lib_json = serde_json::to_string(&sorted_libs).unwrap_or_default();

        self.storage.with_conn(|conn| {
            conn.execute(
                "INSERT INTO access_keys (id, name, digest, prefix, library_ids, all_libraries, created_at, revoked) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                params![id, req.name, digest, prefix, lib_json, req.all_libraries as i64, now],
            )?;
            Ok(())
        })?;

        Ok(AccessKeyPublic {
            id,
            name: req.name.clone(),
            prefix,
            library_ids: sorted_libs,
            all_libraries: req.all_libraries,
            created_at: now,
            revoked: false,
            key: Some(secret),
        })
    }

    pub fn resolve(&self, secret: &str) -> Result<(Option<HashSet<String>>, String), String> {
        let digest = sha256_hex(secret.as_bytes());
        self.storage.with_conn(|conn| {
            let row = conn.query_row(
                "SELECT id, library_ids, all_libraries FROM access_keys WHERE digest = ? AND revoked = 0",
                params![digest],
                |r| {
                    let id: String = r.get(0)?;
                    let libs_str: String = r.get(1)?;
                    let all_libs: i64 = r.get(2)?;
                    Ok((id, libs_str, all_libs))
                },
            );

            match row {
                Ok((id, libs_str, all_libs)) => {
                    if all_libs != 0 {
                        Ok((None, id))
                    } else {
                        let ids: Vec<String> = serde_json::from_str(&libs_str).unwrap_or_default();
                        Ok((Some(ids.into_iter().collect()), id))
                    }
                }
                Err(_) => Err(AppError::new("API Key 无效或已撤销。")),
            }
        })
    }

    pub fn update(&self, id: &str, req: &KeyRequest) -> Result<AccessKeyPublic, String> {
        self.validate(req)?;
        let mut sorted_libs = req.library_ids.clone();
        sorted_libs.sort();
        sorted_libs.dedup();
        let lib_json = serde_json::to_string(&sorted_libs).unwrap_or_default();

        self.storage.with_conn(|conn| {
            let count = conn.execute(
                "UPDATE access_keys SET name = ?, library_ids = ?, all_libraries = ? WHERE id = ? AND revoked = 0",
                params![req.name, lib_json, req.all_libraries as i64, id],
            )?;
            if count == 0 {
                return Err(AppError::new("API Key 不存在或已撤销。"));
            }
            Ok(())
        })?;

        self.list()?
            .into_iter()
            .find(|k| k.id == id)
            .ok_or_else(|| "API Key 不存在。".to_string())
    }

    pub fn revoke(&self, id: &str) -> Result<(), String> {
        self.storage.with_conn(|conn| {
            let count = conn.execute(
                "UPDATE access_keys SET revoked = 1 WHERE id = ?",
                params![id],
            )?;
            if count == 0 {
                return Err(AppError::new("API Key 不存在。"));
            }
            Ok(())
        })
    }

    pub fn delete(&self, id: &str) -> Result<(), String> {
        self.storage.with_conn(|conn| {
            let count = conn.execute("DELETE FROM access_keys WHERE id = ?", params![id])?;
            if count == 0 {
                return Err(AppError::new("API Key 不存在。"));
            }
            Ok(())
        })
    }

    pub fn generate_source_url(&self, document_id: &str, identity: Option<&str>) -> String {
        let base = std::env::var("RAG_PUBLIC_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8010".to_string())
            .trim_end_matches('/')
            .to_string();

        let mut url = format!("{}/api/documents/{}/download", base, document_id);
        if let Some(ident) = identity {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let expires = now + 600;

            let payload_json = format!(r#"["{}", "{}", {}]"#, ident, document_id, expires);
            let payload = URL_SAFE_NO_PAD.encode(payload_json.as_bytes());

            let key_path = self.storage.directory.join("secret.key");
            let key_bytes = std::fs::read(&key_path).unwrap_or_default();

            if let Ok(mut mac) = HmacSha256::new_from_slice(&key_bytes) {
                mac.update(payload.as_bytes());
                let signature = hex::encode(mac.finalize().into_bytes());
                url = format!("{}?grant={}.{}", url, payload, signature);
            }
        }
        url
    }

    pub fn resolve_download(&self, document_id: &str, grant: &str) -> Result<Option<HashSet<String>>, String> {
        let parts: Vec<&str> = grant.split('.').collect();
        if parts.len() != 2 {
            return Err("下载链接无效、已过期或授权已撤销，请重新检索。".to_string());
        }
        let payload = parts[0];
        let signature = parts[1];

        let key_path = self.storage.directory.join("secret.key");
        let key_bytes = std::fs::read(&key_path).map_err(|_| "读取密钥失败".to_string())?;

        let mut mac = HmacSha256::new_from_slice(&key_bytes).map_err(|e| e.to_string())?;
        mac.update(payload.as_bytes());
        let expected_sig = hex::encode(mac.finalize().into_bytes());

        if signature != expected_sig {
            return Err("下载签名校验失败。".to_string());
        }

        let payload_bytes = URL_SAFE_NO_PAD
            .decode(payload.trim_end_matches('='))
            .map_err(|_| "Base64 解码失败".to_string())?;

        let items: Vec<Value> = serde_json::from_slice(&payload_bytes)
            .map_err(|_| "Payload 格式错误".to_string())?;

        if items.len() != 3 {
            return Err("Payload 字段数量不符".to_string());
        }

        let identity = items[0].as_str().ok_or("缺少身份")?;
        let doc_id = items[1].as_str().ok_or("缺少文件 ID")?;
        let expires = items[2].as_u64().ok_or("缺少过期时间")?;

        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        if doc_id != document_id || expires <= now {
            return Err("下载链接已过期或文件 ID 不匹配。".to_string());
        }

        self.storage.with_conn(|conn| {
            let row = conn.query_row(
                "SELECT library_ids, all_libraries FROM access_keys WHERE id = ? AND revoked = 0",
                params![identity],
                |r| {
                    let libs_str: String = r.get(0)?;
                    let all_libs: i64 = r.get(1)?;
                    Ok((libs_str, all_libs))
                },
            );

            match row {
                Ok((libs_str, all_libs)) => {
                    if all_libs != 0 {
                        Ok(None)
                    } else {
                        let ids: Vec<String> = serde_json::from_str(&libs_str).unwrap_or_default();
                        Ok(Some(ids.into_iter().collect()))
                    }
                }
                Err(_) => Err(AppError::new("API Key 已撤销。")),
            }
        })
    }
}
