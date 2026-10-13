# 工程部手册采集与本地同步实施计划

执行方式：本会话按任务实施，使用 TDD 与 verification-before-completion；用户已经授权推荐架构及部署，采集器在新建子目录开发，不建立 worktree。

**目标：** 独立容器每周采集工程手册，原子发布本地 Markdown，定时导入专用知识库。

**架构：** 采集器负责网站适配，文件协议连接采集与知识库。Python Worker 只读来源，Rust 复用现有同步事务与调度。

**技术栈：** Python 3.12、Playwright Chromium、标准库、现有 FastAPI/Axum/SQLite。

**规格：** `docs/superpowers/specs/2026-10-13-engineering-handbook-design.md`。

## 全局约束

每周采集一次。源文件、快照、清单、日志、数据库及密钥不提交 Git。采集失败保留旧版本，索引失败保留旧文档；未变内容不重新索引。原始图片/Office 文件不在此次网页文本采集保证范围。

## 1. 独立采集器

文件：`collectors/engineering_handbook/{collector.py,Dockerfile,requirements.txt}`、`tests/test_handbook_collector.py`。

接口：`prepare_documents(dataset)` 输出路径到 UTF-8 字节的映射；`publish(root,dataset,html)` 校验后原子发布版本；命令 `python -m collectors.engineering_handbook.collector --once` 立即采集，默认循环每周执行。

- [x] 测试同来源多问题只存一份文本、同源差异全部保留、空文本拒绝、路径安全与稳定字节、失败保持 CURRENT、超过 20% 缩减保护。
- [x] 运行 `python -m unittest tests.test_handbook_collector -v`，确认接口缺失失败。
- [x] 按用户确认直接解析 HTTP 网页中的 JSON；实现 Markdown、不可变版本发布及每周调度，重复测试通过。
- [x] 使用真实网页运行首次采集及重复采集，确认 255 条问题、220 个来源、475 份文件，重复版本不变。
- [x] 提交此阶段：`e3b8d74`。

## 2. 本地来源与核心同步

文件：`app/local_source.py`、`app/worker.py`、`core/src/{sync.rs,client.rs,sync_api.rs}`、`tests/{test_local_source.py,test_sync_e2e.py}`。

接口：`/local/{test,scan,read}` 使用 source_id、snapshot、path、hash；scan 输出 complete/files/snapshot。RAG_LOCAL_SOURCES 为受控 JSON 映射。

- [x] 测试发布协议完整性、固定快照、路径逃逸/链接拒绝、令牌校验与缺失挂载失败。
- [x] 运行新测试确认缺少接口失败，再实现受控读取并通过测试。
- [x] Rust 增加默认 SMB 来源类型，按类型路由 Worker，并将首轮 snapshot 传给文件读取；源标识修改限制纳入测试。
- [x] 编写真实 Rust 本地来源增量集成测试；先确认旧二进制不支持，再构建并运行新测试与现有 SMB 测试。
- [x] 提交此阶段：`eee724a`。

## 3. 页面、部署与交付

文件：`app/static/{sync.js,index.html}`、`deploy/compose.handbook.yaml`、`docs/工程部手册采集使用说明.md`、`docs/工程部手册采集验收记录.md`。

- [x] 浏览器测试本地来源创建/编辑、连接、隐藏 SMB 字段、手机布局及原 SMB 行为。
- [x] 实现页面来源选择、受控挂载、独立采集服务及说明。
- [x] Rust 8 项通过；Python 87 项中 84 项通过、3 项因本机 OCR/Office 依赖缺失跳过；本地与线上浏览器及 Docker 验证通过。
- [x] 冷备份后发布，最终 475 份 ready、1,842 个片段；重复 475 份跳过，限定库 MCP、下载哈希及回滚证据已记录。
- [x] 记录结果，确认业务快照、原文、数据库、日志、环境文件及临时凭据不进入提交；阶段成果与最终文档提交至功能分支。
