# 📺 Telegram Media Hub

**自托管的 Telegram 媒体下载中枢** - 统一下载 Telegram 和外部链接的视频、文档和媒体文件到本地存储。

[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0055FF?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io/)

---

## 🌟 功能特性

- **🎯 多来源支持**
  - Telegram 私聊/群组/频道消息监听
  - YouTube、TikTok、Bilibili 等外部链接下载
  - 自动识别消息类型（视频、文档、图片、音频）

- **⚡ 高性能下载**
  - 并行下载支持（Telegram 原生视频）
  - 分片下载（使用 aria2）
  - 多 Worker 并发处理

- **🔄 稳定可靠**
  - 自动重试机制（指数退避）
  - 代理池故障转移
  - 任务持久化（ PostgreSQL）
  - Redis 消息队列

- **🖥️ 用户友好**
  - Web UI（FastAPI + Vue.js）
  - RESTful API 文档
  - 实时任务状态监控
  - 下载完成通知

- **🐳 易于部署**
  - Docker Compose 一键启动
  - 配置文件管理
  - 卷挂载持久化

---

## 📋 快速开始

### 环境要求

| 项目           | 要求                        |
| -------------- | --------------------------- |
| 操作系统       | Windows 10/11, macOS, Linux |
| Docker         | 20.10+                      |
| Docker Compose | 2.0+                        |
| 内存           | ≥ 4GB（推荐 8GB+）          |
| 磁盘           | ≥ 10GB（按需扩展）          |

### 3 步启动

#### 1. 克隆并进入项目

```bash
cd g:\Projects\telegram_media_hub
```

#### 2. 配置环境变量

```bash
# 复制配置模板
cp config/.env.example config/.env
```

编辑 `config/.env`，填写你的 Telegram API 凭据：

```bash
# 必需：Telegram API 凭据
TG_API_ID=你的_API_ID
TG_API_HASH=你的_API_HASH
TG_SESSION_NAME=media_hub

# 数据库和 Redis（Docker 环境自动配置）
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/media_hub
REDIS_URL=redis://redis:6379/0

# 存储路径
STORAGE_ROOT=/media
TEMP_DIR=/media/temp
```

**如何获取 Telegram API 凭据：**

1. 访问 https://my.telegram.org
2. 登录你的 Telegram 账号
3. 进入 "API development tools"
4. 创建新应用，获取 API ID 和 API Hash

#### 3. 启动服务

```bash
docker compose up -d
```

### 访问应用

| 服务          | 地址                         | 说明                           |
| ------------- | ---------------------------- | ------------------------------ |
| **Web UI**    | http://localhost:8000        | 主界面（任务管理、配置、登录） |
| **API Docs**  | http://localhost:8000/docs   | Swagger UI 文档                |
| **Health**    | http://localhost:8000/health | 健康检查                       |
| MeTube (可选) | http://localhost:8081        | Web-based yt-dlp               |

---

## 📖 完整文档

### 🚀 部署指南

- **[快速启动](QUICK_START.md)** - Docker 一键部署（本文档）
- **[测试运行](TESTING_GUIDE.md)** - 完整测试流程和故障排除
- **[开发环境](doc/DEVELOPMENT_GUIDE.md)** - 本地开发设置

### 🔧 配置说明

- **[环境变量](config/.env.example)** - 完整配置选项说明
- **[配置管理](doc/QUICK_START.md)** - 配置文件最佳实践

### 📊 维护指南

- **[依赖升级](doc/DEPENDENCY_UPGRADE_GUIDE.md)** - 更新依赖版本
- **[升级测试报告](doc/UPGRADE_TEST_REPORT.md)** - 版本兼容性验证
- **[合规性检查](doc/COMPLIANCE_CHECK.md)** - 安全和合规性

### 🆘 troubleshooting

- **[tgcrypto 缺失](doc/TGCRYPTO_MISSING.md)** - Windows 编译问题解决方案

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        Web UI (8000)                        │
│              FastAPI + Static Vue.js frontend               │
└─────────────────┬───────────────────────────────────────────┘
                  │
          ┌───────▼───────┐
          │   App Service │
          │  (Main API +   │
          │  Telegram.List)│
          └───────┬───────┘
                  │
    ┌─────────────┼─────────────┐
    │             │             │
┌───▼───┐   ┌────▼────┐   ┌───▼───┐
│Worker │   │ Worker  │   │Worker │
│(TG)   │   │(External│   │(Retry)│
└───┬───┘   │(yt-dlp) │   └───────┘
    │       └─────────┘
    │             │
    └─────────────┼─────────────┐
                  │             │
          ┌───────▼───────┐ ┌───▼───┐
          │   Redis Queue │ │ Postgres│
          │  (Tasks)      │ │ (Data)  │
          └───────────────┘ └─────────┘
```

### 服务组件

| 服务                | 端口 | 说明                           |
| ------------------- | ---- | ------------------------------ |
| **app**             | 8000 | FastAPI 应用 + Telegram 监听器 |
| **tg-worker**       | -    | Telegram 媒体下载 Worker       |
| **external-worker** | -    | 外链下载 Worker (yt-dlp)       |
| **postgres**        | 5432 | 数据库存储                     |
| **redis**           | 6379 | 任务队列                       |
| **metube** (可选)   | 8081 | Web-based yt-dlp               |

---

## 💻 使用方式

### 方式 1：通过 Web UI（推荐）

1. **访问** http://localhost:8000
2. **Telegram 登录**
   - 点击 "TG Login" 标签
   - 输入手机号（包括国家代码）
   - 输入验证码完成登录
3. **监听消息**
   - 登录后自动开始监听配置的聊天
4. **下载媒体**
   - 向监听的聊天发送 YouTube/TikTok 链接
   - 或发送 Telegram 视频/文档
5. **查看任务**
   - 切换到 "Tasks" 标签
   - 查看下载进度和状态

### 方式 2：通过 API

```bash
# 查看 API 文档
# 访问 http://localhost:8000/docs

# 创建外链下载任务（示例）
curl -X POST http://localhost:8000/api/tasks/ \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "EXTERNAL_LINK",
    "source_url": "https://www.youtube.com/watch?v=xxx",
    "telegram_chat_id": 123456789,
    "telegram_message_id": 1
  }'
```

### 方式 3：通过 Telegram（自动化）

1. 配置要监听的聊天（在 Web UI 的 Settings 中）
2. 向这些聊天发送外部链接或媒体文件
3. 系统自动识别并创建下载任务
4. 下载完成后可选择是否通知

---

## ⚙️ 配置说明

### 必需配置

```bash
# Telegram API 凭据（必需）
TG_API_ID=你的_API_ID
TG_API_HASH=你的_API_HASH
```

### 推荐配置

```bash
# 存储路径
STORAGE_ROOT=/media          # 下载文件根目录
TEMP_DIR=/media/temp         # 临时文件目录

# Worker 并发数
TG_DOWNLOAD_WORKERS=3        # Telegram 下载并发
EXTERNAL_DOWNLOAD_WORKERS=5  # 外链下载并发

# 重试配置
MAX_RETRIES=5                # 最大重试次数
RETRY_BASE_DELAY=30          # 初始重试延迟（秒）
```

### 可选配置

```bash
# 代理池（提升稳定性）
PROXY_POOL=http://proxy1:7890,socks5://proxy2:1080

# yt-dlp 配置
YTDLP_FORMAT=bestvideo+bestaudio/best
YTDLP_USE_ARIA2=true

# 通知配置
TG_NOTIFY_ON_COMPLETE=true
TG_NOTIFY_ON_FAIL=true
```

完整配置说明请参考：[config/.env.example](config/.env.example)

---

## 📁 目录结构

```
telegram_media_hub/
├── app/              # 应用代码（API、服务、Worker）
├── config/           # 配置文件（.env、settings）
├── alembic/          # 数据库迁移
├── doc/              # 项目文档
├── sessions/         # Telegram Session（运行时生成）
├── media/            # 下载文件存储（运行时生成）
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

**app 目录结构：**

```
app/
├── api/              # API 路由（auth、config、tasks）
├── core/             # 核心功能（database、redis）
├── models/           # 数据模型
├── schemas/          # Pydantic Schema
├── services/         # 业务服务（Telegram、下载器、通知）
├── workers/          # 后台 Worker（下载、重试）
├── static/           # Web UI 静态文件
└── main.py           # 应用入口
```

---

## 🔌 技术栈

### 后端

- **FastAPI** - 现代化 Web 框架
- **Pyrogram** - Telegram MTProto 客户端
- **SQLAlchemy (Async)** - 异步 ORM
- **Redis + RQ** - 任务队列
- **Pydantic** - 数据验证
- **Loguru** - 结构化日志

### 数据库

- **PostgreSQL** - 关系型数据库
- **Redis** - 内存数据库/队列

### 工具

- **yt-dlp** - 视频下载器
- **aria2** - 分片下载引擎
- **FFmpeg** - 媒体处理
- **Alembic** - 数据库迁移

### DevOps

- **Docker** - 容器化
- **Docker Compose** - 编排
- **Uvicorn** - ASGI 服务器

---

## 🚀 高级使用

### 1. 启用代理

编辑 `config/.env`：

```bash
PROXY_POOL=http://localhost:7890,socks5://127.0.0.1:1080
```

### 2. 自定义下载格式

```bash
# 最高画质
YTDLP_FORMAT=bestvideo[height<=4320]+bestaudio/best

# 1080p（平衡画质和大小）
YTDLP_FORMAT=bestvideo[height<=1080]+bestaudio/best

# 仅音频
YTDLP_FORMAT=bestaudio/best
```

### 3. 调整 Worker 并发

```bash
# 高性能服务器
TG_DOWNLOAD_WORKERS=5
EXTERNAL_DOWNLOAD_WORKERS=10

# 低配服务器
TG_DOWNLOAD_WORKERS=1
EXTERNAL_DOWNLOAD_WORKERS=2
```

### 4. 监视多个聊天

在 Web UI 的 **Settings** 标签中，添加要监听的聊天 ID：

```
123456789,-1001234567890,@channelusername
```

---

## 🧪 测试

运行完整测试流程：

```bash
# 1. 启动服务
docker compose up -d

# 2. 等待服务健康（约 10 秒）
sleep 10

# 3. 检查健康状态
curl http://localhost:8000/health

# 4. 访问 Web UI
# http://localhost:8000

# 5. 测试 Telegram 登录
# 点击 "TG Login" 标签，输入手机号

# 6. 测试下载
# 向监听聊天发送 YouTube 链接

# 7. 查看任务
# http://localhost:8000#pane-tasks
```

完整测试指南：[TESTING_GUIDE.md](TESTING_GUIDE.md)

---

## 🐛 故障排除

### 问题 1：服务无法启动

```bash
# 检查端口占用
netstat -ano | findstr :8000
netstat -ano | findstr :5432
netstat -ano | findstr :6379

# 停止占用进程
taskkill /PID <PID> /F

# 重启
docker compose down
docker compose up -d
```

### 问题 2：数据库连接失败

```bash
# 检查 PostgreSQL 状态
docker compose ps postgres

# 查看日志
docker compose logs postgres

# 重启数据库
docker compose restart postgres
sleep 10
docker compose restart app
```

### 问题 3：Redis 连接失败

```bash
# 测试 Redis
docker compose exec redis redis-cli ping

# 重启 Redis
docker compose restart redis
sleep 5
docker compose restart app tg-worker external-worker
```

### 问题 4：Telegram 登录失败

```bash
# 检查 API 凭据
cat config/.env | grep TG_API

# 确认凭据正确（访问 https://my.telegram.org）
# API ID 必须是数字
# API Hash 必须是字符串

# 重启应用
docker compose restart app

# 重新登录
# http://localhost:8000 -> TG Login
```

完整故障排除指南：[TESTING_GUIDE.md](TESTING_GUIDE.md)

---

## 📊 监控和调试

### 查看日志

```bash
# 所有服务日志
docker compose logs -f

# 特定服务
docker compose logs -f app
docker compose logs -f tg-worker
docker compose logs -f external-worker
docker compose logs -f postgres
docker compose logs -f redis
```

### 进入容器调试

```bash
# App 容器
docker compose exec app bash

# PostgreSQL
docker compose exec postgres psql -U postgres -d media_hub
docker compose exec postgres psql -U postgres -c "\dt"

# Redis
docker compose exec redis redis-cli
> keys *
> LLEN tg_download
```

### 检查文件

```bash
# 下载的文件
docker compose exec app ls -la /media/external/youtube/

# 临时文件
docker compose exec app ls -la /media/temp/

# 配置文件
docker compose exec app cat /app/config/.env
```

---

## 🔄 日常维护

### 备份数据

```bash
# 备份数据库
docker compose exec postgres pg_dump -U postgres media_hub > backup/$(date +%Y%m%d).sql

# 备份下载文件（在主机上）
tar -czvf backup/media-$(date +%Y%m%d).tar.gz ./media/
```

### 清理

```bash
# 停止服务
docker compose down

# 删除临时文件（保留媒体数据）
docker compose down
# 手动删除 media/temp/ 中的旧文件

# 重启
docker compose up -d
```

### 更新镜像

```bash
# 拉取最新代码
git pull

# 重建镜像
docker compose down
docker compose build
docker compose up -d
```

---

## 📈 性能优化

### 1. 提升下载速度

```bash
# 增加 Worker 并发
TG_DOWNLOAD_WORKERS=5
EXTERNAL_DOWNLOAD_WORKERS=10

# 启用 aria2 分片
YTDLP_USE_ARIA2=true

# 使用高速代理
PROXY_POOL=http://fast-proxy:7890
```

### 2. 减少资源占用

```bash
# 降低并发
TG_DOWNLOAD_WORKERS=1
EXTERNAL_DOWNLOAD_WORKERS=2

# 限制 yt-dlp 格式
YTDLP_FORMAT=bestvideo[height<=720]+bestaudio/best
```

### 3. 数据库优化

```bash
# 调整 PostgreSQL 连接池
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db?min_size=5&max_size=20
```

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 开发环境

```bash
# 1. 克隆项目
git clone <repo>
cd telegram_media_hub

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 运行开发服务器
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

详细开发指南：[doc/DEVELOPMENT_GUIDE.md](doc/DEVELOPMENT_GUIDE.md)

---

## 📄 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- [Pyrogram](https://docs.pyrogram.org/) - Telegram 客户端库
- [FastAPI](https://fastapi.tiangolo.com/) - Web 框架
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - 视频下载器
- [RQ](https://python-rq.org/) - 任务队列
- [SQLAlchemy](https://www.sqlalchemy.org/) - ORM

---

## 📞 联系方式

如有问题或建议，欢迎提交 Issue。

---

## ⭐ Star History

如果本项目对你有帮助，欢迎Star支持！

![Star History Chart](https://api.star-history.com/svg?repos=your-username/telegram-media-hub&type=Date)

---

**最后更新：** 2026-04-24
**版本：** v1.1 (Post-Upgrade)

---

<div align="center">

[🚀 快速启动](QUICK_START.md) | [📖 文档](doc/) | [🔧 测试指南](TESTING_GUIDE.md) | [🐛 报告问题](https://github.com/your-username/telegram-media-hub/issues)

</div>
