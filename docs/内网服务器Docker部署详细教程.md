# 联大 · 研发部智库 RAG 平台 & OpenClaw 内网服务器 Docker 部署指南

本指南专为将重构后的 **Rust + Python 混合架构智库 RAG 知识库平台** 以及 **OpenClaw 智能体网关（含飞书通道）** 部署到企业内网 Ubuntu / Debian 服务器设计。

---

## 一、 系统架构与部署模型

本套方案采用**多容器解耦与桥接网络架构**，兼顾高并发低延迟与复杂文档生态解析：

```mermaid
flowchart TD
    Client["内网终端浏览器 / 飞书客户端"] -->|端口: 8010| Core["zhiku-rag-core (Rust 核心)"]
    Feishu["飞书开放平台 WebSocket"] <-->|内网主动出网长连接| Claw["openclaw-gateway (OpenClaw 网关: 18789)"]
    
    subgraph DockerNet ["Docker 专用网络 (zhiku-net)"]
        Core -->|HTTP 内部调用 /8002| Worker["zhiku-rag-worker (Python 微服务)"]
        Claw -->|MCP 协议内部请求 /8010| Core
        Worker --> OCR["Tesseract-OCR (中英双语)"]
        Worker --> Office["MarkItDown / Office 解析器"]
        Core --> SQLite[("挂载宿主机持久化卷 ./data")]
    end

    Claw -->|调用 LLM API| LLMServer["内网/公网大模型接口 (如 DeepSeek)"]
```

### 容器职责分工：
1. **`zhiku-rag-core` (Rust)**：极轻量（内存占用仅约 2MB），负责提供 Web 管理界面、文档与目录管理、SIMD/AVX2 向量检索及标准 MCP Streamable HTTP 协议。
2. **`zhiku-rag-worker` (Python 3.12)**：负责接收上传文档，执行 Office (Word/Excel/PPT)、PDF、Markdown 解析提取，并调用内置 Tesseract 5.5 进行 OCR 识别。
3. **`openclaw-gateway` (Node.js 22)**：负责维系与飞书机器人的 WebSocket 双向连接，根据意图调度知识库 MCP 工具并驱动大模型回答。

---

## 二、 前置环境准备

### 1. 宿主机要求
- **操作系统**：Ubuntu 22.04 / 24.04 LTS 或 Debian 12 (x86_64)
- **硬件配置建议**：
  - CPU：2 核及以上
  - 内存：4GB 及以上（Python OCR 处理与模型分词需要基本运行内存）
  - 磁盘空间：20GB 以上可用空间
- **网络需求**：允许连接国内常见开源镜像源（用于首次拉取基础镜像和依赖包）。

### 2. 安装 Docker 与 Docker Compose
若内网服务器尚未安装 Docker，可执行以下命令快速安装：

```bash
# 1. 卸载旧版本并安装依赖
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release

# 2. 配置 Docker 官方国内镜像源 (以阿里云源为例)
sudo mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 3. 安装 Docker Engine 与 Compose 插件
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. 将当前用户加入 docker 组 (免 sudo)
sudo usermod -aG docker $USER
newgrp docker

# 5. 验证安装
docker --version
docker compose version
```

---

## 三、 项目代码与部署文件准备

### 1. 获取项目代码
将项目代码克隆或上传解压至内网服务器，例如 `/opt/zhiku-rag`：

```bash
cd /opt
# 通过 Git 克隆或解压本地代码包
git clone <your-git-repo-url> zhiku-rag
cd zhiku-rag
```

### 2. 初始化部署目录结构
进入 `deploy` 目录，准备环境与数据持久化目录：

```bash
cd /opt/zhiku-rag/deploy

# 创建持久化数据目录
mkdir -p data openclaw
```

### 3. 配置环境变量文件 (`deploy/.env`)
拷贝并编辑环境变量文件：

```bash
cp .env.example .env
vim .env
```

`.env` 内容示例与详细说明：
```env
# 知识库服务对外暴露的宿主机端口 (默认 8010)
RAG_PORT=8010

# 管理员安全令牌 (用于访问 Web 管理后台，请修改为强随机密码)
RAG_ADMIN_TOKEN=h6_4009cuLn5FJgMXtHXUpz7q3XPm-GNzw4mwdUwqz8

# 知识库内网访问根地址 (用于拼接源文件下载链接，必须为内网其他机器可访问的 IP 或域名)
RAG_PUBLIC_URL=http://192.168.1.100:8010

# OpenClaw 网关对外暴露端口 (默认 18789)
OPENCLAW_PORT=18789
```

### 4. 配置 OpenClaw 配置文件 (`deploy/openclaw/openclaw.json`)
拷贝配置模板：

```bash
cp openclaw/openclaw.json.example openclaw/openclaw.json
chmod 700 openclaw
vim openclaw/openclaw.json
```

**关键配置项说明与替换**：
```json
{
  "gateway": {
    "mode": "local",
    "bind": "lan",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "生成一个长随机字符串作为网关内部Token"
    }
  },
  "models": {
    "mode": "merge",
    "providers": {
      "custom": {
        "baseUrl": "http://180.76.235.223:3000/v1", // 替换为内网或外部可访问的 OpenAI 兼容接口地址
        "apiKey": "sk-63zWF7aCOJSERVWMWG0iDOws1XxuQIPoGAoOLeyPV5gNh3v9", // 替换为您的模型 API Key
        "api": "openai-completions",
        "models": [
          {
            "id": "deepseek-flash", // 替换为您实际的模型名
            "name": "deepseek-flash",
            "contextWindow": 64000,
            "maxTokens": 4096,
            "input": ["text"]
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "custom/deepseek-flash"
      }
    }
  },
  "plugins": {
    "entries": {
      "feishu": {
        "enabled": true // 必须为 true，显式信任飞书官方插件
      }
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "domain": "feishu",
      "connectionMode": "websocket", // 采用 WebSocket 长连接模式，内网服务器无需公网域名与公网IP
      "dmPolicy": "open",
      "groupPolicy": "open",
      "allowFrom": ["*"],
      "groupAllowFrom": ["*"],
      "accounts": {
        "default": {
          "enabled": true,
          "appId": "cli_xxxxxxxxxxxx", // 飞书开放平台应用的 App ID
          "appSecret": "xxxxxxxxxxxxxxxxxxxxxxxx", // 飞书开放平台应用的 App Secret
          "dmPolicy": "open",
          "allowFrom": ["*"]
        }
      }
    }
  },
  "mcp": {
    "servers": {
      "zhiku-rag": {
        "url": "http://rag-core:8010/mcp", // 利用 Docker 内部域名直接互联
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer zk_9hPY7FIVpwkmLDuKLqhufqPfMbca5HA5zVIGx-hKS70" // 知识库只读 API Key
        }
      }
    }
  }
}
```

> [!IMPORTANT]
> **飞书开放平台权限与配置要求**：
> 1. 登录 [飞书开放平台](https://open.feishu.cn/)，进入该企业自建应用。
> 2. 进入 **「开发配置」 -> 「事件与回调」**：
>    - 订阅方式必须选择：**「使用长连接接收事件/回调」**（WebSocket 模式，无需内网穿透或回调配置）。
>    - 添加事件：`im.message.receive_v1`（接收消息）。
> 3. 进入 **「权限管理」**：
>    - 开启 **「获取与发送单聊/群聊消息」**（`im:message`, `im:message:send_as_bot` 等相关权限）。
> 4. 发布应用最新版本。

---

## 四、 一键构建与容器启动

在 `/opt/zhiku-rag/deploy` 目录下运行：

```bash
# 1. 检查配置无误后，后台构建并启动全部容器
docker compose -f docker-compose.yml up -d --build

# 2. 查看容器运行状态
docker compose -f docker-compose.yml ps
```

预期正常输出：
```text
NAME                IMAGE                     COMMAND                  SERVICE             CREATED         STATUS                   PORTS
openclaw-gateway    deploy-openclaw-gateway   "openclaw gateway ru…"   openclaw-gateway    1 minute ago    Up 1 minute (healthy)    0.0.0.0:18789->18789/tcp
zhiku-rag-core      deploy-rag-core           "/usr/local/bin/rag-…"   rag-core            1 minute ago    Up 1 minute (healthy)    0.0.0.0:8010->8010/tcp
zhiku-rag-worker    deploy-rag-worker         "python -m uvicorn a…"   rag-worker          1 minute ago    Up 1 minute (healthy)    8002/tcp
```

---

## 五、 服务验证与自测清单

### 1. 验证 RAG 平台服务状态
在内网终端或使用 `curl` 请求知识库健康与状态接口：

```bash
# 检查健康接口 (包含 OCR 引擎信息)
curl http://127.0.0.1:8010/health

# 预期返回包含 core: "rust", worker.engine: "Tesseract"
```

浏览器访问知识库工作台：
- 打开：`http://<服务器内网IP>:8010/`
- 输入管理员令牌（在 `.env` 中配置的 `RAG_ADMIN_TOKEN`）登录管理后台。
- 测试上传一份 Word/Excel/PDF 文档，观察状态是否成功转为“已就绪”，并测试右上角的原图/正文预览。

### 2. 验证 OpenClaw 与飞书通道连通性
查看 OpenClaw 容器内部状态：

```bash
docker compose -f docker-compose.yml exec openclaw-gateway openclaw channels status --probe
```

预期输出：
```text
Gateway reachable.
- Feishu default: enabled, configured, running, connected, works
```
> 若显示 `works` 且为 `connected`，说明与飞书网关已建立长连接。

### 3. 验证 MCP 知识库工具链
```bash
docker compose -f docker-compose.yml exec openclaw-gateway openclaw mcp probe zhiku-rag
```
预期输出：
```text
MCP probe:
- zhiku-rag: 3 tools
```

### 4. 飞书端到端实测
在飞书中向该应用机器人私聊发送：
- *“你好，请介绍一下你自己”*
- *“请在知识库中帮我检索关于 [某文件/业务主题] 的内容”*

机器人应调用 `zhiku-rag` 检索并在文末附带原文件下载链接，完成有据问答。

---

## 六、 运维保障与常见故障排障

### 1. 数据持久化与定期备份
所有知识库 SQLite 数据库、上传的真实源文件、加密密钥与索引均保存在宿主机 `deploy/data` 目录下。

**备份指令（定时备份脚本范例）**：
```bash
# 备份知识库核心数据
BACKUP_DIR="/backup/zhiku-rag/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -r /opt/zhiku-rag/deploy/data "$BACKUP_DIR/"
echo "备份已存储至: $BACKUP_DIR"
```

### 2. 常见问题排查手册

| 常见现象 / 报错 | 根本原因 | 解决办法 |
| :--- | :--- | :--- |
| **`openclaw channels status` 提示 `not-running` 或 `stopped`** | 1. 飞书 AppID/Secret 配置有误<br>2. 飞书开放平台未开启“长连接”接收模式<br>3. `plugins.entries.feishu.enabled` 未开启 | 检查 `openclaw.json`，确保插件已开启，确认飞书后台事件订阅已切换为**「使用长连接接收」**。 |
| **上传文档提示 Worker 错误 / OCR 超时** | 宿主机 CPU 或内存受限，图片解析排队 | 在 `docker-compose.yml` 中适当调大 Worker 的资源配额，或在工作台高级设置中调整并发线程。 |
| **飞书机器人回复不带文件下载链接** | 提示词或工作台基准地址未配对 | 检查 `.env` 中的 `RAG_PUBLIC_URL` 是否设置为用户能访问的内网真实 IP。 |
| **容器无法解析外部 LLM 接口域名** | Docker 默认 DNS 解析失败 | 在 `docker-compose.yml` 中对应的 service 下添加 `dns: [223.5.5.5, 114.114.114.114]` 或公司内网 DNS 服务器。 |

### 3. 服务日常维护常用命令
```bash
# 查看实时日志
docker compose -f docker-compose.yml logs -f --tail=100 rag-core
docker compose -f docker-compose.yml logs -f --tail=100 openclaw-gateway

# 重启某个组件
docker compose -f docker-compose.yml restart openclaw-gateway

# 优雅停止所有服务
docker compose -f docker-compose.yml down
```
