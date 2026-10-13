use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use rusqlite::{params, Connection};
use serde_json::Value;

use crate::crypto::FernetCipher;

#[derive(Debug)]
pub struct AppError(pub String);

impl AppError {
    pub fn new(msg: impl Into<String>) -> Self {
        AppError(msg.into())
    }
}

#[cfg(test)]
mod sync_migration_tests {
    use super::*;
    #[test]
    fn existing_vectors_repair_zero_count_without_reindex() {
        let directory = std::env::temp_dir().join(format!("ld-sync-migration-{}", uuid::Uuid::new_v4().simple()));
        let storage = Storage::new(&directory).unwrap();
        storage.with_conn(|conn| {
            conn.execute("INSERT INTO documents(id,filename,suffix,size,text,created_at,status) VALUES('legacy','a.txt','.txt',3,'old','0','ready')", [])?;
            conn.execute("INSERT INTO chunks VALUES('legacy',1,'old','[1,0,0]')", [])?;
            Ok(())
        }).unwrap();
        drop(storage);
        let storage = Storage::new(&directory).unwrap();
        let (count, vector): (i64, String) = storage.with_conn(|conn| {
            Ok(conn.query_row("SELECT d.chunk_count,c.vector FROM documents d JOIN chunks c ON c.document_id=d.id WHERE d.id='legacy'",[],|r|Ok((r.get(0)?,r.get(1)?)))?)
        }).unwrap();
        assert_eq!(count, 1);
        assert_eq!(vector, "[1,0,0]");
        drop(storage);
        let resolved = directory.canonicalize().unwrap();
        assert_eq!(resolved.parent().unwrap(), std::env::temp_dir().canonicalize().unwrap());
        assert!(resolved.file_name().unwrap().to_string_lossy().starts_with("ld-sync-migration-"));
        std::fs::remove_dir_all(resolved).unwrap();
    }
}

impl From<rusqlite::Error> for AppError {
    fn from(e: rusqlite::Error) -> Self {
        AppError(e.to_string())
    }
}

impl From<&str> for AppError {
    fn from(s: &str) -> Self {
        AppError(s.to_string())
    }
}

impl From<String> for AppError {
    fn from(s: String) -> Self {
        AppError(s)
    }
}

#[derive(Clone)]
pub struct Storage {
    pub directory: PathBuf,
    pub uploads: PathBuf,
    pub preview_cache: PathBuf,
    pub cipher: FernetCipher,
    conn: Arc<Mutex<Connection>>,
}

impl Storage {
    pub fn new(directory: impl AsRef<Path>) -> Result<Self, String> {
        let directory = directory.as_ref().to_path_buf();
        std::fs::create_dir_all(&directory)
            .map_err(|e| format!("创建数据目录失败: {}", e))?;

        let uploads = directory.join("uploads");
        std::fs::create_dir_all(&uploads)
            .map_err(|e| format!("创建 uploads 目录失败: {}", e))?;

        let preview_cache = directory.join("preview-cache");
        std::fs::create_dir_all(&preview_cache)
            .map_err(|e| format!("创建 preview-cache 目录失败: {}", e))?;

        let key_path = directory.join("secret.key");
        let cipher = FernetCipher::load_or_create(&key_path)?;

        let db_path = directory.join("knowledge.sqlite");
        let conn = Connection::open(&db_path)
            .map_err(|e| format!("打开 SQLite 数据库失败: {}", e))?;

        conn.execute_batch(
            r#"
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            PRAGMA busy_timeout = 15000;

            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS libraries (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
                parent_id TEXT REFERENCES folders(id), name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, suffix TEXT NOT NULL,
                size INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0, dimension INTEGER NOT NULL DEFAULT 0,
                library_id TEXT REFERENCES libraries(id),
                folder_id TEXT REFERENCES folders(id),
                extraction_info TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS chunks (
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL, text TEXT NOT NULL, vector TEXT NOT NULL,
                PRIMARY KEY(document_id, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS access_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL, library_ids TEXT NOT NULL, all_libraries INTEGER NOT NULL,
                created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_tasks (
                id TEXT PRIMARY KEY, config TEXT NOT NULL, next_run TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_files (
                task_id TEXT NOT NULL REFERENCES sync_tasks(id) ON DELETE CASCADE,
                path TEXT NOT NULL, document_id TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
                PRIMARY KEY(task_id, path)
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES sync_tasks(id) ON DELETE CASCADE,
                state TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sync_runs_task ON sync_runs(task_id, started_at);
            CREATE TABLE IF NOT EXISTS document_blobs (
                document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                filename TEXT NOT NULL
            );
            "#,
        )
        .map_err(|e| format!("初始化数据库表结构失败: {}", e))?;

        // 兼容旧版本已写入向量但片段计数为零的记录；不重新生成向量。
        conn.execute(
            "UPDATE documents SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE document_id=documents.id) WHERE status='ready' AND chunk_count=0 AND EXISTS(SELECT 1 FROM chunks WHERE document_id=documents.id)",
            [],
        ).map_err(|e| format!("校准旧文档片段计数失败: {}", e))?;

        // 检查并初始化默认知识库
        let init_done: bool = conn
            .query_row(
                "SELECT 1 FROM settings WHERE key='catalog_initialized'",
                [],
                |_| Ok(true),
            )
            .unwrap_or(false);

        if !init_done {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO libraries VALUES ('default', '待整理资料', '0')",
                [],
            );
            let _ = conn.execute(
                "UPDATE libraries SET name='待整理资料' WHERE id='default' AND name='默认知识库'",
                [],
            );
            let default_libs = [
                ("faq", "技术组FAQ查询", "1"),
                ("retrospective", "错题本记录", "1"),
                ("projects", "AI查询机器人", "1"),
            ];
            for (id, name, created) in default_libs {
                let _ = conn.execute(
                    "INSERT OR IGNORE INTO libraries VALUES (?, ?, ?)",
                    params![id, name, created],
                );
            }
            let _ = conn.execute(
                "INSERT OR REPLACE INTO settings VALUES ('catalog_initialized', 'true')",
                [],
            );
        }

        Ok(Self {
            directory,
            uploads,
            preview_cache,
            cipher,
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    pub fn with_conn<F, R>(&self, f: F) -> Result<R, String>
    where
        F: FnOnce(&Connection) -> Result<R, AppError>,
    {
        let conn = self.conn.lock().map_err(|e| format!("获取数据库锁失败: {}", e))?;
        f(&conn).map_err(|e| e.0)
    }

    pub fn with_conn_mut<F, R>(&self, f: F) -> Result<R, String>
    where
        F: FnOnce(&mut Connection) -> Result<R, AppError>,
    {
        let mut conn = self.conn.lock().map_err(|e| format!("获取数据库锁失败: {}", e))?;
        f(&mut conn).map_err(|e| e.0)
    }

    pub fn get_setting(&self, key: &str) -> Option<Value> {
        self.with_conn(|conn| {
            let val: String = conn.query_row(
                "SELECT value FROM settings WHERE key = ?",
                params![key],
                |row| row.get(0),
            )?;
            Ok(serde_json::from_str(&val).ok())
        })
        .ok()
        .flatten()
    }

    pub fn put_setting(&self, key: &str, value: &Value) -> Result<(), String> {
        let serialized = serde_json::to_string(value).map_err(|e| e.to_string())?;
        self.with_conn(|conn| {
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                params![key, serialized],
            )?;
            Ok(())
        })
    }

    pub fn encrypt(&self, plain: &str) -> Result<String, String> {
        if plain.is_empty() {
            Ok("".to_string())
        } else {
            self.cipher.encrypt(plain.as_bytes())
        }
    }

    pub fn decrypt(&self, cipher_text: &str) -> Result<String, String> {
        if cipher_text.is_empty() {
            Ok("".to_string())
        } else {
            let bytes = self.cipher.decrypt(cipher_text)?;
            String::from_utf8(bytes).map_err(|e| format!("解密结果非合法 UTF-8: {}", e))
        }
    }

    pub fn remove_preview(&self, document_id: &str) {
        if !document_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
            return;
        }
        if let Ok(entries) = std::fs::read_dir(&self.preview_cache) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with(&format!("{}-", document_id)) && name.ends_with(".pdf") {
                    let _ = std::fs::remove_file(entry.path());
                }
            }
        }
    }
}
