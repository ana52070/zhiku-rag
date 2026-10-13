# Samba 增量同步实施计划

执行方式：在本会话逐项实施，使用 executing-plans、TDD 和 verification-before-completion；用户已授权开始代码开发，无需重复询问实施许可。

**目标：** 网页配置远端共享定时任务，增量合并到知识库并在内网部署验证。

**架构：** Rust 管理任务、时间、文件映射及原子索引发布。Python Worker 作为 SMB 客户端只读访问远端文件。

**技术栈：** Axum、SQLite、Tokio、chrono-tz、cron、Python smbprotocol/smbclient、现有 HTML/JS。

**规格：** `docs/superpowers/specs/2026-10-13-samba-sync-design.md`。

## 约束

文档、界面、提交使用简体中文。文件类型和容量限制沿用现有解析器。凭据加密且不提交。准备失败保留原版本；完整扫描失败不执行删除。阶段验证后提交。

## 1. SMB 只读客户端

文件：`app/smb_source.py`、`app/worker.py`、`requirements.txt`、`tests/test_smb_source.py`。

接口：POST `/smb/test`、`/smb/scan`、`/smb/read`；连接配置含 `server/share/subdir/username/password/port`，读取额外包含 `path/hash`。内部令牌通过 `X-Worker-Token` 传入。扫描返回 `complete=true, files=[{path,size,hash}]`；读取返回文件字节，确认读取前后文件稳定。

- [ ] 用临时目录适配器测试路径穿越、嵌套扫描、SHA-256、锁文件、读取变化、扫描权限失败。
- [ ] `python -m unittest tests.test_smb_source -v` 观察缺少接口失败。
- [ ] 实现客户端及 Worker 端点，重复上述命令验证。

## 2. Rust 同步状态、发布和调度

文件：`core/src/sync.rs`、`core/src/sync_api.rs`、`core/src/storage.rs`、`core/src/service.rs`、`core/src/client.rs`、`core/src/api.rs`、`core/src/main.rs`、`core/Cargo.toml`。

接口：`/api/sync/tasks` CRUD、`/{id}/run`、`/{id}/runs`、`/api/sync/test`。配置含 SMB 信息、目标及 `schedule={kind,time,weekdays,interval_hours,cron,timezone}`。`sync_files` 唯一对应任务/相对路径与文档；`document_blobs` 指向不可变原文件。

- [ ] 编写调度、幂等、接管、更新故障、删除保护、人工恢复和凭据隐藏测试，运行 `cargo test --manifest-path core/Cargo.toml sync` 观察失败。
- [ ] 实现表结构、验证、加密凭据及内部 HTTP 客户端。
- [ ] 实现先准备后事务发布、完整扫描后的删除阶段、串行运行和重启恢复。
- [ ] 实现受管理员令牌保护的路由和常用模式/五字段 Cron 调度。
- [ ] 执行 Rust 单测及真实 HTTP 测试替身集成验证后提交。

## 3. 网页任务管理与部署

文件：`app/static/index.html`、`app/static/sync.js`、`app/static/style.css`、`deploy/docker-compose.yml`、Dockerfiles、同步使用文档及浏览器验收脚本。

- [ ] 网页提供连接测试、任务新增/编辑、时间预览、知识库/文件夹选择、立即运行、启停及执行记录。
- [ ] 运行浏览器桌面/手机尺寸验收和鉴权、密码不回显检查。
- [ ] 全量行为测试、Rust 编译、镜像构建；记录通过及未验证项。
- [ ] 核实内网运行版本与路径，备份后发布；真实共享信息缺失时通过结构化提问对齐，完成实际 SMB 增改删验收。
