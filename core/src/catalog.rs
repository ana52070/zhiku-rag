use chrono::Utc;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::storage::{AppError, Storage};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Location {
    pub library_id: String,
    pub folder_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Library {
    pub id: String,
    pub name: String,
    pub created_at: String,
    pub document_count: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Folder {
    pub id: String,
    pub library_id: String,
    pub parent_id: Option<String>,
    pub name: String,
}

pub fn validate_name(name: &str) -> Result<String, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() || trimmed.len() > 100 || trimmed == "." || trimmed == ".." || trimmed.contains('/') || trimmed.contains('\\') || trimmed.contains('\0') {
        return Err("名称不能为空，长度不超过100，且不能包含路径分隔符。".to_string());
    }
    Ok(trimmed.to_string())
}

#[derive(Clone)]
pub struct Catalog {
    storage: Storage,
}

impl Catalog {
    pub fn new(storage: Storage) -> Self {
        Self { storage }
    }

    pub fn validate_location(&self, library_id: &str, folder_id: Option<&str>) -> Result<(), String> {
        self.storage.with_conn(|conn| {
            let lib_exists: bool = conn
                .query_row(
                    "SELECT 1 FROM libraries WHERE id = ?",
                    params![library_id],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            if !lib_exists {
                return Err(AppError::new("知识库不存在。"));
            }

            if let Some(fid) = folder_id {
                let folder_valid: bool = conn
                    .query_row(
                        "SELECT 1 FROM folders WHERE id = ? AND library_id = ?",
                        params![fid, library_id],
                        |_| Ok(true),
                    )
                    .unwrap_or(false);

                if !folder_valid {
                    return Err(AppError::new("文件夹不存在或属于其他知识库。"));
                }
            }

            Ok(())
        })
    }

    pub fn libraries(&self) -> Result<Vec<Library>, String> {
        self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                r#"
                SELECT l.id, l.name, l.created_at, COUNT(d.id) AS document_count
                FROM libraries l
                LEFT JOIN documents d ON d.library_id = l.id
                GROUP BY l.id
                ORDER BY
                    CASE l.id
                        WHEN 'default' THEN 0
                        WHEN 'faq' THEN 1
                        WHEN 'retrospective' THEN 2
                        WHEN 'projects' THEN 3
                        ELSE 4
                    END,
                    l.created_at
                "#,
            )?;

            let rows = stmt.query_map([], |row| {
                Ok(Library {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    created_at: row.get(2)?,
                    document_count: row.get(3)?,
                })
            })?;

            let mut list = Vec::new();
            for r in rows {
                list.push(r?);
            }
            Ok(list)
        })
    }

    pub fn folders(&self, library_id: Option<&str>) -> Result<Vec<Folder>, String> {
        self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, library_id, parent_id, name FROM folders WHERE (? IS NULL OR library_id = ?) ORDER BY name",
            )?;

            let rows = stmt.query_map(params![library_id, library_id], |row| {
                Ok(Folder {
                    id: row.get(0)?,
                    library_id: row.get(1)?,
                    parent_id: row.get(2)?,
                    name: row.get(3)?,
                })
            })?;

            let mut list = Vec::new();
            for r in rows {
                list.push(r?);
            }
            Ok(list)
        })
    }

    pub fn create_library(&self, name: &str) -> Result<Library, String> {
        let clean_name = validate_name(name)?;
        let id = Uuid::new_v4().simple().to_string();
        let now = Utc::now().to_rfc3339();

        self.storage.with_conn(|conn| {
            let exists: bool = conn
                .query_row(
                    "SELECT 1 FROM libraries WHERE name = ?",
                    params![clean_name],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            if exists {
                return Err(AppError::new("知识库名称已存在。"));
            }

            conn.execute(
                "INSERT INTO libraries (id, name, created_at) VALUES (?, ?, ?)",
                params![id, clean_name, now],
            )?;

            Ok(())
        })?;

        Ok(Library {
            id,
            name: clean_name,
            created_at: now,
            document_count: 0,
        })
    }

    pub fn update_library(&self, library_id: &str, name: &str) -> Result<Library, String> {
        let clean_name = validate_name(name)?;
        self.validate_location(library_id, None)?;

        self.storage.with_conn(|conn| {
            let conflict: bool = conn
                .query_row(
                    "SELECT 1 FROM libraries WHERE name = ? AND id <> ?",
                    params![clean_name, library_id],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            if conflict {
                return Err(AppError::new("知识库名称已存在。"));
            }

            conn.execute(
                "UPDATE libraries SET name = ? WHERE id = ?",
                params![clean_name, library_id],
            )?;

            Ok(())
        })?;

        self.libraries()?
            .into_iter()
            .find(|l| l.id == library_id)
            .ok_or_else(|| "知识库不存在。".to_string())
    }

    pub fn delete_library(&self, library_id: &str) -> Result<(), String> {
        self.validate_location(library_id, None)?;

        let docs_to_clean: Vec<(String, String)> = self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare("SELECT d.id, COALESCE(b.filename, d.id || d.suffix) FROM documents d LEFT JOIN document_blobs b ON b.document_id=d.id WHERE d.library_id = ?")?;
            let rows = stmt.query_map(params![library_id], |row| Ok((row.get(0)?, row.get(1)?)))?;
            let mut list = Vec::new();
            for r in rows {
                list.push(r?);
            }
            Ok(list)
        })?;

        self.storage.with_conn_mut(|conn| {
            let tx = conn.transaction()?;
            tx.execute("DELETE FROM documents WHERE library_id = ?", params![library_id])?;
            tx.execute("UPDATE folders SET parent_id = NULL WHERE library_id = ?", params![library_id])?;
            tx.execute("DELETE FROM folders WHERE library_id = ?", params![library_id])?;
            tx.execute("DELETE FROM libraries WHERE id = ?", params![library_id])?;
            tx.commit()?;
            Ok(())
        })?;

        for (doc_id, filename) in docs_to_clean {
            let file_path = self.storage.uploads.join(filename);
            let _ = std::fs::remove_file(file_path);
            self.storage.remove_preview(&doc_id);
        }

        Ok(())
    }

    pub fn create_folder(
        &self,
        library_id: &str,
        parent_id: Option<&str>,
        name: &str,
    ) -> Result<Folder, String> {
        let clean_name = validate_name(name)?;
        self.validate_location(library_id, parent_id)?;

        let id = Uuid::new_v4().simple().to_string();

        self.storage.with_conn(|conn| {
            let conflict: bool = conn
                .query_row(
                    "SELECT 1 FROM folders WHERE library_id = ? AND parent_id IS ? AND name = ?",
                    params![library_id, parent_id, clean_name],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            if conflict {
                return Err(AppError::new("同级文件夹名称已存在。"));
            }

            conn.execute(
                "INSERT INTO folders (id, library_id, parent_id, name) VALUES (?, ?, ?, ?)",
                params![id, library_id, parent_id, clean_name],
            )?;

            Ok(())
        })?;

        Ok(Folder {
            id,
            library_id: library_id.to_string(),
            parent_id: parent_id.map(|s| s.to_string()),
            name: clean_name,
        })
    }

    pub fn delete_folder(&self, folder_id: &str) -> Result<(), String> {
        self.storage.with_conn(|conn| {
            let exists: bool = conn
                .query_row("SELECT 1 FROM folders WHERE id = ?", params![folder_id], |_| Ok(true))
                .unwrap_or(false);
            if !exists {
                return Err(AppError::new("文件夹不存在。"));
            }

            let has_subfolders: bool = conn
                .query_row(
                    "SELECT 1 FROM folders WHERE parent_id = ? LIMIT 1",
                    params![folder_id],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            let has_docs: bool = conn
                .query_row(
                    "SELECT 1 FROM documents WHERE folder_id = ? LIMIT 1",
                    params![folder_id],
                    |_| Ok(true),
                )
                .unwrap_or(false);

            if has_subfolders || has_docs {
                return Err(AppError::new("请先移走文件和子文件夹，再删除空文件夹。"));
            }

            conn.execute("DELETE FROM folders WHERE id = ?", params![folder_id])?;
            Ok(())
        })
    }
}
