# 🚀 测试运行指南

本指南介绍如何使用 Docker 进行测试运行。

---

## 📋 准备工作

### 1. 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| 操作系统 | Windows 10/11 | Windows 11 |
| Docker | 20.10+ | 24.0+ |
| Docker Compose | 2.0+ | 2.20+ |
| 内存 | 4GB | 8GB+ |
| 磁盘 | 10GB | 20GB+ |

### 2. 检查 Docker 安装

```bash
docker --version
docker compose version
```

---

## 🛠️ 第一步：配置环境

### 1. 创建 .env 文件

```bash
cp config/.env.example config/.env
```

### 2. 编辑配置文件

编辑 `config/.env`：

```bash
# 必需的 Telegram API 凭据
TG_API_ID=你的_API_ID
TG_API_HASH=你的_API_HASH

# 数据库连接（Docker 环境使用服务名）
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/media_hub

# Redis 连接（Docker 环境使用服务名）
REDIS_URL=redis://redis:6379/0

# 存储路径（使用 Docker 卷）
STORAGE_ROOT=/media
TEMP_DIR=/media/temp
```

**注意：**
- `DATABASE_URL` 使用 `postgres:5432`（Docker 服务名）
- `REDIS_URL` 使用 `redis:6379`（Docker 服务名）
- `STORAGE_ROOT` 使用 `/media`（Docker 容器内路径）

---

## 🚀 第二步：启动依赖服务

使用 Docker Compose 启动 PostgreSQL 和 Redis：

```bash
cd g:\Projects\telegram_media_hub

# 启动 PostgreSQL 和 Redis
docker compose up -d postgres redis
```

**等待服务启动并健康检查通过：**

```bash
# 检查服务状态
docker compose ps

# 等待直到 postgres 和 redis 显示 "healthy"
```

**验证服务：**

```bash
# 检查 PostgreSQL
docker compose exec postgres pg_isready -U postgres

# 检查 Redis
docker compose exec redis redis-cli ping
# 应该返回: PONG
```

---

## 🏗️ 第三步：构建和运行应用

### 方案 A：完整部署（推荐）

启动所有服务：

```bash
docker compose up -d
```

检查状态：

```bash
docker compose ps
```

预期输出：

```
NAME                  STATUS                  PORTS
media-hub-app         up (healthy)            0.0.0.0:8000->8000/tcp
media-hub-tg-worker   up (healthy)
media-hub-external-worker up (healthy)
media-hub-postgres    up (healthy)            0.0.0.0:5432->5432/tcp
media-hub-redis       up (healthy)            0.0.0.0:6379->6379/tcp
```

### 方案 B：逐个启动（调试用）

```bash
# 1. 启动依赖服务
docker compose up -d postgres redis

# 2. 构建应用镜像
docker compose build

# 3. 启动 App 服务
docker compose up -d app

# 4. 等待 App 健康检查通过
docker compose logs -f app

# 5. 启动 Worker 服务
docker compose up -d tg-worker external-worker
```

---

## ✅ 第四步：验证部署

### 1. 检查服务日志

```bash
# 查看所有服务日志
docker compose logs -f

# 查看特定服务日志
docker compose logs -f app
docker compose logs -f tg-worker
docker compose logs -f external-worker
```

### 2. 检查 Web UI

访问：http://localhost:8000

**应该看到：**
- Telegram Media Hub 首页
- 三个标签页：Settings, TG Login, Tasks

### 3. 检查 API 健康

```bash
# 检查健康状态
curl http://localhost:8000/health
# 应该返回: {"status":"ok","service":"telegram-media-hub"}

# 检查 API 文档
# 访问 http://localhost:8000/docs
```

### 4. 检查数据库

```bash
# 连接到 PostgreSQL
docker compose exec postgres psql -U postgres -d media_hub

# 查看表
\dt

# 应该看到：
# - tasks
# - proxies
# - alembic_version
```

---

## 🧪 第五步：功能测试

### 1. Telegram 登录测试

**访问 Web UI：** http://localhost:8000

**步骤：**
1. 点击 "TG Login" 标签
2. 输入手机号（包括国家代码）
3. 点击 "Send Verification Code"
4. 输入收到的验证码
5. 如果启用 2FA，输入密码

**成功标志：**
- 页面显示 "Account Connected"
- "Badge" 显示 "Connected"（绿色）

### 2. 任务管理测试

**访问：** http://localhost:8000#pane-tasks

**检查：**
- 查看任务列表（应该为空）
- 点击 "Refresh" 刷新列表

### 3. 配置测试

**访问：** http://localhost:8000#pane-settings

**测试：**
1. 检查当前配置是否正确显示
2. 修改某个配置（如 `MAX_RETRIES`）
3. 点击 "Save Configuration"
4. 检查保存是否成功

---

## 🧩 第六步：下载功能测试

### 方式 1：通过 Web UI（推荐）

1. **创建测试任务**
   - 访问 http://localhost:8000
   - 在 TG Login 中登录
   - 在 Tasks 标签中查看任务

2. **测试外链下载**
   - 添加一个 YouTube 视频链接到 Telegram 消息
   - 等待监听器识别
   - 在 Web UI 中查看任务状态

### 方式 2：通过 API（手动测试）

```bash
# 1. 获取 API 文档
# 访问 http://localhost:8000/docs

# 2. 创建测试任务（Python 示例）
python -c "
import httpx
import asyncio

async def create_test_task():
    async with httpx.AsyncClient() as client:
        # 创建外链下载任务
        response = await client.post(
            'http://localhost:8000/api/tasks/',
            json={
                'source_type': 'EXTERNAL_LINK',
                'source_url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                'telegram_chat_id': 123456789,
                'telegram_message_id': 1
            }
        )
        print(f'Status: {response.status_code}')
        print(f'Response: {response.json()}')

asyncio.run(create_test_task())
"
```

### 方式 3：通过 Telegram（真实测试）

1. **发送测试消息到监控的聊天**
   - 向监控的聊天发送YouTube视频链接
   - 等待监听器识别
   - 检查任务状态

2. **检查下载结果**
   - 访问 http://localhost:8000/tasks 查看任务
   - 检查 `local_path` 字段
   - 确认文件下载到 `./media/external/youtube/`

---

## 🐛 故障排除

### 问题 1：服务无法启动

**症状：**
```
ERROR: compose.menu.foldable.1 - Error response from daemon
```

**解决：**
```bash
# 检查是否端口冲突
netstat -ano | findstr :8000
netstat -ano | findstr :5432
netstat -ano | findstr :6379

# 停止占用端口的进程
taskkill /PID <PID> /F

# 重新启动
docker compose down
docker compose up -d
```

---

### 问题 2：数据库连接失败

**症状：**
```
sqlalchemy.exc.OperationalError: could not connect to server
```

**解决：**
```bash
# 检查 PostgreSQL 是否健康
docker compose ps postgres

# 查看 PostgreSQL 日志
docker compose logs postgres

# 等待 PostgreSQL 完全启动（需要几秒）
sleep 10

# 重启应用
docker compose restart app
```

---

### 问题 3：Redis 连接失败

**症状：**
```
redis.exceptions.ConnectionError: Error connecting to Redis
```

**解决：**
```bash
# 检查 Redis 是否运行
docker compose exec redis redis-cli ping

# 重启 Redis
docker compose restart redis

# 等待 Redis 启动
sleep 5

# 重启应用
docker compose restart app tg-worker external-worker
```

---

### 问题 4：ALEMBIC 迁移失败

**症状：**
```
alembic.util.exc.CommandError: Target database is not up-to-date
```

**解决：**
```bash
# 运行迁移
docker compose exec app alembic upgrade head

# 或者重置数据库（警告：会删除所有数据！）
docker compose down -v
docker compose up -d postgres redis
docker compose exec app alembic upgrade head
docker compose up -d
```

---

### 问题 5：Telegram 登录失败

**症状：**
- Web UI 显示 "Not Connected"
- API 返回 400 错误

**解决：**
```bash
# 1. 检查 .env 中的 API 凭据
cat config/.env | grep TG_API

# 2. 确认凭据正确
# - 访问 https://my.telegram.org 获取
# - API ID 必须是数字
# - API Hash 必须是字符串

# 3. 重启应用
docker compose restart app

# 4. 重新登录
# 访问 http://localhost:8000 -> TG Login
```

---

### 问题 6：Worker 没有处理任务

**症状：**
- 任务状态一直显示 "PENDING"
- Worker 日志中没有处理记录

**解决：**
```bash
# 1. 检查 Worker 是否运行
docker compose ps

# 2. 查看 Worker 日志
docker compose logs tg-worker
docker compose logs external-worker

# 3. 检查 Redis 队列
docker compose exec redis redis-cli
> LLEN tg_download
> LLEN external_download

# 4. 重启 Worker
docker compose restart tg-worker external-worker
```

---

## 📊 性能测试

### 1. Gateway API 压力测试

```bash
# 安装 ab (Apache Bench)
# Windows 可以使用：https://httpd.apache.org/docs/current/platform/windows.html

# 或使用 PowerShell
Invoke-WebRequest -Uri "http://localhost:8000/health" -Method GET
```

### 2. 测试下载速度

```bash
# 查看下载速度统计
curl http://localhost:8000/api/tasks/stats/summary

# 或访问 http://localhost:8000/docs 查看统计端点
```

---

## 🔄 日常维护

### 1. 查看日志

```bash
# 实时查看所有日志
docker compose logs -f

# 查看特定服务日志
docker compose logs -f app
docker compose logs -f tg-worker

# 导出日志
docker compose logs > logs/docker-compose-$(date +%Y%m%d).log
```

### 2. 备份数据

```bash
# 备份 PostgreSQL
docker compose exec postgres pg_dump -U postgres media_hub > backup/$(date +%Y%m%d).sql

# 备份下载文件
tar -czvf backup/media-$(date +%Y%m%d).tar.gz ./media/
```

### 3. 清理

```bash
# 停止所有服务
docker compose down

# 删除所有卷（⚠️ 警告：会删除所有数据！）
docker compose down -v

# 重新启动
docker compose up -d
```

---

## 📈 监控和调试

### 1. 检查容器资源

```bash
# 查看容器资源使用
docker stats

# 查看特定容器
docker stats media-hub-app
```

### 2. 进入容器调试

```bash
# 进入应用容器
docker compose exec app bash

# 在容器内执行命令
docker compose exec app python -c "import app; print(app.__file__)"

# 进入 PostgreSQL
docker compose exec postgres psql -U postgres -d media_hub
```

### 3. 查看文件

```bash
# 查看配置文件
docker compose exec app cat /app/config/.env

# 查看下载的文件
docker compose exec app ls -la /media/external/youtube/
```

---

## 🎯 完整测试流程

### 快速测试（10 分钟）

```bash
# 1. 启动服务
docker compose up -d postgres redis
sleep 10  # 等待数据库启动
docker compose up -d

# 2. 等待服务健康
sleep 5

# 3. 检查健康状态
curl http://localhost:8000/health

# 4. 测试 API
curl http://localhost:8000/api/tasks/stats/summary

# 5. 浏览器访问
# http://localhost:8000
```

### 完整测试（30 分钟）

```bash
# 1. 启动并等待健康
docker compose up -d
sleep 15

# 2. 检查日志
docker compose logs -f | head -100

# 3. 测试 Telegram 登录
# 打开 http://localhost:8000 -> TG Login
# 输入手机号和验证码

# 4. 测试创建任务
# API 文档: http://localhost:8000/docs

# 5. 测试下载
# 发送 YouTube 链接到 Telegram
# 等待下载完成

# 6. 检查结果
# 访问 http://localhost:8000/tasks
# 检查 local_path 和 status
```

---

## 🔐 安全检查

### 1. 检查敏感信息

```bash
# 确认 .env 没有提交到 Git
cat .gitignore | grep env

# 检查是否包含敏感文件
ls -la config/
```

### 2. 检查防火墙

```bash
# Windows 防火墙
Get-NetFirewallRule | Where-Object { $_.LocalPort -eq 8000, 5432, 6379 }

# 确保只在本地访问（开发环境）
```

---

## 📝 测试清单

- [ ] Docker 已安装并运行
- [ ] `.env` 文件配置正确
- [ ] PostgreSQL 启动并健康
- [ ] Redis 启动并健康
- [ ] 应用服务启动
- [ ] Worker 服务启动
- [ ] Web UI 可访问 (http://localhost:8000)
- [ ] API 文档可访问 (http://localhost:8000/docs)
- [ ] 健康检查通过 (`/health`)
- [ ] 数据库表创建成功
- [ ] Telegram 登录功能正常
- [ ] 任务创建 API 正常
- [ ] Worker 处理任务
- [ ] 下载文件保存成功
- [ ] 日志输出正常
- [ ] 没有错误或警告

---

## 🎉 测试成功！

如果以上所有检查都通过了，那么你的系统已经可以正常运行了！

**下一步：**
1. 配置 Telegram 监听的聊天
2. 开始下载测试
3. 根据需要调整配置

**遇到问题？**
查看 "故障排除" 部分
或检查日志：`docker compose logs -f`

---

**最后更新：** 2026-04-24  
**测试版本：** V1.1 (Post-Upgrade)
