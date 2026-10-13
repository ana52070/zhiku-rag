//! 持久化同步任务与增量发布。来源清单成功完成之前绝不执行删除。
use crate::crypto::sha256_hex;
use crate::service::KnowledgeService;
use crate::sync_schedule::{next_run, ScheduleConfig};
use crate::vector::{split_text, validate_vectors};
use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::Arc;
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize)]
pub struct TaskConfig {
    pub name: String,
    pub server: String,
    pub share: String,
    #[serde(default)]
    pub subdir: String,
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub password: String,
    #[serde(default = "default_port")]
    pub port: u16,
    pub library_id: String,
    pub folder_id: Option<String>,
    #[serde(default)]
    pub enabled: bool,
    pub schedule: ScheduleConfig,
}
fn default_port() -> u16 {
    445
}

#[derive(Clone, Deserialize)]
struct SourceEntry {
    path: String,
    size: usize,
    hash: String,
}
#[derive(Deserialize)]
struct Manifest {
    complete: bool,
    files: Vec<SourceEntry>,
}
#[derive(Default, Serialize, Deserialize)]
pub struct RunSummary {
    pub added: usize,
    pub updated: usize,
    pub skipped: usize,
    pub deleted: usize,
    pub failed: usize,
    pub total: usize,
    pub completed: usize,
    pub errors: Vec<String>,
}

pub fn valid_path(path: &str) -> bool {
    !path.is_empty()
        && path.len() <= 4096
        && !path.contains(['\\', ':', '\0'])
        && path
            .split('/')
            .all(|p| !p.is_empty() && p != "." && p != "..")
}

impl KnowledgeService {
    pub fn sync_task(&self, id: &str) -> Result<(TaskConfig, String), String> {
        let (raw, next): (String, String) = self.storage.with_conn(|conn| {
            Ok(conn.query_row(
                "SELECT config,next_run FROM sync_tasks WHERE id=?",
                params![id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?)
        })?;
        Ok((
            serde_json::from_str(&raw).map_err(|_| "同步配置损坏。")?,
            next,
        ))
    }
    pub fn public_task(&self, id: &str) -> Result<Value, String> {
        let (cfg, next) = self.sync_task(id)?;
        let mut value = serde_json::to_value(&cfg).map_err(|e| e.to_string())?;
        let object = value.as_object_mut().unwrap();
        object.remove("password");
        object.insert("has_password".into(), json!(!cfg.password.is_empty()));
        object.insert("id".into(), json!(id));
        object.insert("next_run".into(), json!(next));
        object.insert("running".into(), json!(self.task_running(id)?));
        Ok(value)
    }
    pub fn sync_tasks(&self) -> Result<Vec<Value>, String> {
        let ids = self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare("SELECT id FROM sync_tasks ORDER BY rowid")?;
            let result = stmt
                .query_map([], |r| r.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(result)
        })?;
        ids.iter().map(|id| self.public_task(id)).collect()
    }
    pub fn task_running(&self, id: &str) -> Result<bool, String> {
        self.storage.with_conn(|conn| {
            Ok(conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM sync_runs WHERE task_id=? AND state='running')",
                params![id],
                |r| r.get(0),
            )?)
        })
    }
    pub fn save_sync_task(&self, id: Option<&str>, mut cfg: TaskConfig) -> Result<Value, String> {
        cfg.name = crate::catalog::validate_name(&cfg.name)?;
        cfg.server = cfg.server.trim().into();
        cfg.share = cfg.share.trim().into();
        cfg.subdir = cfg.subdir.replace('\\', "/").trim_matches('/').into();
        if cfg.server.is_empty()
            || cfg.server.len() > 253
            || cfg
                .server
                .chars()
                .any(|c| c.is_whitespace() || "/\\:@\0".contains(c))
            || !valid_path(&cfg.share)
            || cfg.share.contains('/')
            || (!cfg.subdir.is_empty() && !valid_path(&cfg.subdir))
            || cfg.port == 0
        {
            return Err("共享连接参数无效，请填写主机、共享名及合法相对子目录。".into());
        }
        self.catalog
            .validate_location(&cfg.library_id, cfg.folder_id.as_deref())?;
        let next = next_run(&cfg.schedule, Utc::now())?.to_rfc3339();
        if let Some(id) = id {
            if self.task_running(id)? {
                return Err("任务运行中，请完成后编辑。".into());
            }
            let (old, _) = self.sync_task(id)?;
            let count: i64 = self.storage.with_conn(|conn| {
                Ok(conn.query_row(
                    "SELECT COUNT(*) FROM sync_files WHERE task_id=?",
                    params![id],
                    |r| r.get(0),
                )?)
            })?;
            if count > 0
                && (old.server != cfg.server
                    || old.share != cfg.share
                    || old.subdir != cfg.subdir
                    || old.port != cfg.port
                    || old.library_id != cfg.library_id
                    || old.folder_id != cfg.folder_id)
            {
                return Err("已有来源映射，不能修改源目录或目标位置；请新建任务。".into());
            }
            cfg.password = if cfg.password.is_empty() {
                old.password
            } else {
                self.storage.encrypt(&cfg.password)?
            };
        } else {
            cfg.password = self.storage.encrypt(&cfg.password)?;
        }
        // 阻止任务管理同一目标子树，避免互相覆盖和删除。
        for other in self.sync_tasks()? {
            if other["id"].as_str() == id || other["library_id"] != cfg.library_id {
                continue;
            }
            let (other_cfg, _) = self.sync_task(other["id"].as_str().unwrap())?;
            if self.locations_overlap(cfg.folder_id.as_deref(), other_cfg.folder_id.as_deref())? {
                return Err("该知识库目录与其他同步任务重叠，请选择独立子目录。".into());
            }
        }
        let id = id
            .map(String::from)
            .unwrap_or_else(|| Uuid::new_v4().simple().to_string());
        let raw = serde_json::to_string(&cfg).map_err(|e| e.to_string())?;
        self.storage.with_conn(|conn| {
            conn.execute("INSERT INTO sync_tasks(id,config,next_run) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,next_run=excluded.next_run",params![id,raw,next])?;
            Ok(())
        })?;
        self.public_task(&id)
    }
    fn locations_overlap(&self, a: Option<&str>, b: Option<&str>) -> Result<bool, String> {
        if a.is_none() || b.is_none() {
            return Ok(true);
        }
        let folders = self.catalog.folders(None)?;
        let ancestor = |start: Option<&str>, target: Option<&str>| {
            let mut current = start;
            for _ in 0..=folders.len() {
                if current == target {
                    return true;
                }
                current = current
                    .and_then(|id| folders.iter().find(|f| f.id == id))
                    .and_then(|f| f.parent_id.as_deref());
                if current.is_none() {
                    break;
                }
            }
            false
        };
        Ok(ancestor(a, b) || ancestor(b, a))
    }
    pub fn delete_sync_task(&self, id: &str) -> Result<(), String> {
        if self.task_running(id)? {
            return Err("运行中的任务不能删除。".into());
        }
        self.sync_task(id)?;
        self.storage.with_conn(|conn| {
            conn.execute("DELETE FROM sync_tasks WHERE id=?", params![id])?;
            Ok(())
        })
    }
    pub fn sync_runs(&self, task_id: &str) -> Result<Vec<Value>, String> {
        self.sync_task(task_id)?;
        self.storage.with_conn(|conn| {
            let mut stmt = conn.prepare("SELECT id,state,started_at,finished_at,summary FROM sync_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 100")?;
            let rows = stmt.query_map(params![task_id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,String>(4)?)))?;
            let mut output = Vec::new();
            for row in rows {
                let (id,state,start,end,raw)=row?;
                let mut value: Value = serde_json::from_str(&raw).unwrap_or(json!({}));
                let obj=value.as_object_mut().unwrap(); obj.insert("id".into(),json!(id)); obj.insert("state".into(),json!(state));
                obj.insert("started_at".into(),json!(start));obj.insert("finished_at".into(),json!(end));output.push(value);
            }
            Ok(output)
        })
    }
    pub fn smb_payload(&self, cfg: &TaskConfig) -> Result<Value, String> {
        Ok(
            json!({"server":cfg.server,"share":cfg.share,"subdir":cfg.subdir,"port":cfg.port,
            "username":cfg.username,"password":self.storage.decrypt(&cfg.password)?}),
        )
    }
    pub fn start_sync(self: &Arc<Self>, task_id: &str) -> Result<String, (u16, String)> {
        self.check_active()?;
        let guard = self
            .sync_lock
            .clone()
            .try_lock_owned()
            .map_err(|_| (409, "已有同步或模型配置操作正在执行，请稍后重试。".into()))?;
        let (cfg, _) = self
            .sync_task(task_id)
            .map_err(|_| (404, "任务不存在。".into()))?;
        let run_id = Uuid::new_v4().simple().to_string();
        let now = Utc::now();
        let next = next_run(&cfg.schedule, now)
            .map_err(|e| (400, e))?
            .to_rfc3339();
        let summary = serde_json::to_string(&RunSummary::default()).unwrap();
        self.storage
            .with_conn_mut(|conn| {
                let tx = conn.transaction()?;
                tx.execute(
                    "INSERT INTO sync_runs VALUES(?,?,'running',?,NULL,?)",
                    params![run_id, task_id, now.to_rfc3339(), summary],
                )?;
                tx.execute(
                    "UPDATE sync_tasks SET next_run=? WHERE id=?",
                    params![next, task_id],
                )?;
                tx.commit()?;
                Ok(())
            })
            .map_err(|e| (500, e))?;
        let svc = self.clone();
        let id = task_id.to_string();
        let rid = run_id.clone();
        tokio::spawn(async move {
            let _guard = guard;
            let mut summary = RunSummary::default();
            if let Err(error) = svc.execute_sync(&id, &rid, &cfg, &mut summary).await {
                summary.failed += 1;
                summary.errors.push(error);
            }
            let state = if summary.failed == 0 {
                "success"
            } else {
                "failed"
            };
            let raw = serde_json::to_string(&summary).unwrap();
            if let Err(error) = svc.storage.with_conn(|conn| {
                conn.execute(
                    "UPDATE sync_runs SET state=?,finished_at=?,summary=? WHERE id=?",
                    params![state, Utc::now().to_rfc3339(), raw, rid],
                )?;
                Ok(())
            }) {
                tracing::error!("保存同步结果失败: {}", error);
            }
        });
        Ok(run_id)
    }
    fn progress_sync(&self, rid: &str, summary: &RunSummary) -> Result<(), String> {
        let raw = serde_json::to_string(summary).map_err(|e| e.to_string())?;
        self.storage.with_conn(|conn| {
            conn.execute(
                "UPDATE sync_runs SET summary=? WHERE id=?",
                params![raw, rid],
            )?;
            Ok(())
        })
    }
    async fn execute_sync(
        &self,
        id: &str,
        rid: &str,
        cfg: &TaskConfig,
        result: &mut RunSummary,
    ) -> Result<(), String> {
        self.catalog
            .validate_location(&cfg.library_id, cfg.folder_id.as_deref())?;
        let payload = self.smb_payload(cfg)?;
        let manifest: Manifest = self
            .worker
            .smb("scan", &payload)
            .await?
            .json()
            .await
            .map_err(|_| "共享扫描响应无效。")?;
        if !manifest.complete {
            return Err("扫描未完整完成，取消同步及删除。".into());
        }
        let mut seen = HashSet::new();
        for entry in &manifest.files {
            if !valid_path(&entry.path)
                || entry.size > 50 * 1024 * 1024
                || entry.hash.len() != 64
                || !entry.hash.bytes().all(|b| b.is_ascii_hexdigit())
                || !seen.insert(entry.path.clone())
            {
                return Err("共享扫描返回无效或重复的文件信息，取消同步。".into());
            }
        }
        result.total = manifest.files.len();
        self.progress_sync(rid, result)?;
        for entry in &manifest.files {
            match self.sync_one(id, cfg, entry, &payload).await {
                Ok("added") => result.added += 1,
                Ok("updated") => result.updated += 1,
                Ok(_) => result.skipped += 1,
                Err(error) => {
                    result.failed += 1;
                    if result.errors.len() < 100 {
                        result.errors.push(format!("{}：{}", entry.path, error));
                    }
                }
            }
            result.completed += 1;
            self.progress_sync(rid, result)?;
        }
        if result.failed > 0 {
            return Err("部分文件处理失败，本轮取消删除阶段。".into());
        }
        // 删除之前再次完整扫描，目录在处理期间变化则留到下一轮。
        let verify: Manifest = self
            .worker
            .smb("scan", &payload)
            .await?
            .json()
            .await
            .map_err(|_| "删除前复核扫描失败。")?;
        let first: HashSet<_> = manifest.files.iter().map(|e| (&e.path, &e.hash)).collect();
        let second: HashSet<_> = verify.files.iter().map(|e| (&e.path, &e.hash)).collect();
        if !verify.complete || first != second {
            return Err("同步期间源目录发生变化，取消删除阶段，下次重试。".into());
        }
        let stale = self.storage.with_conn(|conn| {
            let mut stmt =
                conn.prepare("SELECT path,document_id FROM sync_files WHERE task_id=?")?;
            let rows = stmt
                .query_map(params![id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })?;
        for (path, doc_id) in stale {
            if seen.contains(&path) {
                continue;
            }
            if self.get_document(&doc_id).is_ok() {
                self.delete_document(&doc_id).map_err(|(_, e)| e)?;
                result.deleted += 1;
            }
            self.storage.with_conn(|conn| {
                conn.execute(
                    "DELETE FROM sync_files WHERE task_id=? AND path=?",
                    params![id, path],
                )?;
                Ok(())
            })?;
        }
        Ok(())
    }
    fn source_folder(&self, cfg: &TaskConfig, path: &str) -> Result<Option<String>, String> {
        let mut parent = cfg.folder_id.clone();
        let parts: Vec<_> = path.split('/').collect();
        for name in &parts[..parts.len() - 1] {
            let existing = self
                .catalog
                .folders(Some(&cfg.library_id))?
                .into_iter()
                .find(|f| f.parent_id == parent && f.name == *name);
            parent = Some(match existing {
                Some(f) => f.id,
                None => {
                    self.catalog
                        .create_folder(&cfg.library_id, parent.as_deref(), name)?
                        .id
                }
            });
        }
        Ok(parent)
    }
    async fn sync_one(
        &self,
        task_id: &str,
        cfg: &TaskConfig,
        entry: &SourceEntry,
        payload: &Value,
    ) -> Result<&'static str, String> {
        let folder = self.source_folder(cfg, &entry.path)?;
        let filename = entry.path.rsplit('/').next().unwrap();
        let previous: Option<(String, String)> = self.storage.with_conn(|conn| {
            Ok(conn
                .query_row(
                    "SELECT document_id,hash FROM sync_files WHERE task_id=? AND path=?",
                    params![task_id, entry.path],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?)
        })?;
        let mut doc_id = previous.as_ref().map(|p| p.0.clone());
        let mut old_doc = doc_id.as_ref().and_then(|id| self.get_document(id).ok());
        if previous.is_none() {
            let candidates: Vec<String> = self.storage.with_conn(|conn| {
                let mut stmt = conn.prepare(
                    "SELECT id FROM documents WHERE library_id=? AND folder_id IS ? AND filename=?",
                )?;
                let ids = stmt
                    .query_map(params![cfg.library_id, folder, filename], |r| r.get(0))?
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(ids)
            })?;
            if candidates.len() > 1 {
                return Err("目标目录存在多份同名文档，无法确定接管对象。".into());
            }
            if let Some(id) = candidates.first() {
                doc_id = Some(id.clone());
                old_doc = self.get_document(id).ok();
            }
        }
        let doc_id = doc_id.unwrap_or_else(|| Uuid::new_v4().simple().to_string());
        let owned: bool = self.storage.with_conn(|conn| {
            Ok(conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM sync_files WHERE document_id=? AND task_id<>?)",
                params![doc_id, task_id],
                |r| r.get(0),
            )?)
        })?;
        if owned {
            return Err("同名文档已由其他同步任务管理。".into());
        }
        if let Some(old) = &old_doc {
            if previous.as_ref().is_some_and(|p| p.1 == entry.hash) && old["status"] == "ready" {
                // 人工移动后恢复位置；不重新解析未变文件。
                self.move_document(
                    &doc_id,
                    crate::catalog::Location {
                        library_id: cfg.library_id.clone(),
                        folder_id: folder,
                    },
                )
                .map_err(|(_, e)| e)?;
                self.update_document_enabled(&doc_id, true)
                    .map_err(|(_, e)| e)?;
                return Ok("skipped");
            }
        }
        let mut request = payload.clone();
        request["path"] = json!(entry.path);
        request["hash"] = json!(entry.hash);
        let content = self
            .worker
            .smb("read", &request)
            .await?
            .bytes()
            .await
            .map_err(|_| "读取共享文件失败。")?;
        if content.len() != entry.size || sha256_hex(&content) != entry.hash {
            return Err("读取内容与扫描指纹不一致。".into());
        }
        let extracted = self.worker.extract_document(filename, &content).await?;
        let pieces = split_text(&extracted.text);
        if pieces.is_empty() || pieces.len() > crate::vector::MAX_CHUNKS {
            return Err("文档文本为空或切片数量超限。".into());
        }
        let model = self.config();
        if model.model.is_empty() {
            return Err("请先配置 embedding 模型。".into());
        }
        let fp = self.fingerprint();
        let vectors = self
            .embedder
            .embed(&model.base_url, &model.model, &model.api_key, &pieces)
            .await?;
        let dimension = validate_vectors(&vectors, pieces.len())?;
        if self.fingerprint() != fp {
            return Err("处理中模型配置变化，请重试。".into());
        }
        if ![".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".csv"]
            .contains(&extracted.suffix.as_str())
        {
            return Err("文档后缀无效。".into());
        }
        let blob = format!("{}{}", Uuid::new_v4().simple(), extracted.suffix);
        let new_path = self.storage.uploads.join(&blob);
        std::fs::write(&new_path, &content).map_err(|_| "写入新版本失败。")?;
        let old_path = old_doc
            .as_ref()
            .map(|d| self.document_path(&doc_id, d["suffix"].as_str().unwrap_or("")));
        let extraction = serde_json::to_string(&extracted.extraction_info).unwrap();
        let commit=self.storage.with_conn_mut(|conn|{
            let tx=conn.transaction()?;
            tx.execute("INSERT INTO documents(id,filename,suffix,size,text,created_at,status,library_id,folder_id,extraction_info,fingerprint,chunk_count,dimension) VALUES(?,?,?,?,?,?,'ready',?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,suffix=excluded.suffix,size=excluded.size,text=excluded.text,status='ready',enabled=1,error='',library_id=excluded.library_id,folder_id=excluded.folder_id,extraction_info=excluded.extraction_info,fingerprint=excluded.fingerprint,chunk_count=excluded.chunk_count,dimension=excluded.dimension",
                params![doc_id,filename,extracted.suffix,content.len() as i64,extracted.text,Utc::now().to_rfc3339(),cfg.library_id,folder,extraction,fp,pieces.len() as i64,dimension as i64])?;
            tx.execute("DELETE FROM chunks WHERE document_id=?",params![doc_id])?;
            for (i,(text,vector)) in pieces.iter().zip(vectors.iter()).enumerate(){
                tx.execute("INSERT INTO chunks VALUES(?,?,?,?)",params![doc_id,(i+1) as i64,text,serde_json::to_string(vector).unwrap()])?;
            }
            tx.execute("INSERT INTO document_blobs VALUES(?,?) ON CONFLICT(document_id) DO UPDATE SET filename=excluded.filename",params![doc_id,blob])?;
            tx.execute("INSERT INTO sync_files VALUES(?,?,?,?) ON CONFLICT(task_id,path) DO UPDATE SET document_id=excluded.document_id,hash=excluded.hash",params![task_id,entry.path,doc_id,entry.hash])?;
            tx.commit()?;Ok(())
        });
        if let Err(e) = commit {
            let _ = std::fs::remove_file(new_path);
            return Err(e);
        }
        if let Some(path) = old_path {
            let _ = std::fs::remove_file(path);
        }
        self.storage.remove_preview(&doc_id);
        Ok(if old_doc.is_some() {
            "updated"
        } else {
            "added"
        })
    }
}

pub fn start_scheduler(service: KnowledgeService) {
    let service = Arc::new(service);
    if let Err(error)=service.storage.with_conn_mut(|conn|{
        let tx=conn.transaction()?;
        tx.execute("UPDATE sync_tasks SET next_run=? WHERE id IN (SELECT task_id FROM sync_runs WHERE state='running')",params![Utc::now().to_rfc3339()])?;
        tx.execute("UPDATE sync_runs SET state='failed',finished_at=?,summary=? WHERE state='running'",params![Utc::now().to_rfc3339(),serde_json::to_string(&RunSummary{failed:1,errors:vec!["上次同步被中断，将在恢复后重试。".into()],..Default::default()}).unwrap()])?;
        tx.commit()?;Ok(())
    }) {tracing::error!("恢复同步状态失败: {}",error);return;}
    tokio::spawn(async move {
        let mut timer = tokio::time::interval(std::time::Duration::from_secs(10));
        loop {
            timer.tick().await;
            if service.check_active().is_err() {
                continue;
            }
            match service.sync_tasks() {
                Ok(tasks) => {
                    for task in tasks {
                        if task["enabled"] != true {
                            continue;
                        }
                        let due = task["next_run"]
                            .as_str()
                            .and_then(|v| chrono::DateTime::parse_from_rfc3339(v).ok())
                            .is_some_and(|date| date <= Utc::now());
                        if due {
                            let _ = service.start_sync(task["id"].as_str().unwrap());
                        }
                    }
                }
                Err(error) => tracing::warn!("读取同步调度失败: {}", error),
            }
        }
    });
}
