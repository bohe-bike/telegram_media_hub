# 🚀 快速启动指南

## 3 步启动服务

### 1️⃣ 配置环境
```bash
cd g:\Projects\telegram_media_hub
cp config/.env.example config/.env
# 编辑 config/.env 填写你的 Telegram API 凭据
```

### 2️⃣ 启动服务
```bash
docker compose up -d
```

### 3️⃣ 访问 Web UI
打开 http://localhost:8000

---

## 📋 完整准备清单

### ✅ 必需项
- [ ] Docker 已安装
- [ ] Docker Compose 已安装
- [ ] `.env` 文件已创建
- [ ] Telegram API ID 和 Hash 已配置
- [ ] PostgreSQL 和 Redis 已启动并健康

### ⏳ 启动步骤
```bash
# 启动数据库依赖
docker compose up -d postgres redis

# 等待 10 秒让数据库启动
timeout /t 10

# 启动完整服务
docker compose up -d

# 查看状态
docker compose ps
```

---

## 🌐 访问地址

| 服务 | 地址 | 说明 |
|------|------|------|
| Web UI | http://localhost:8000 | 主界面 |
| API 文档 | http://localhost:8000/docs | Swagger UI |
| API 健康 | http://localhost:8000/health | 健康检查 |
| MeTube (可选) | http://localhost:8081 | Web-based yt-dlp |

---

## 📊 服务状态检查

```bash
# 查看所有服务状态
docker compose ps

# 查看日志
docker compose logs -f

# 检查健康
curl http://localhost:8000/health
```

---

## 🐛 快速修复

### 服务启动失败
```bash
docker compose down
docker compose up -d
```

### 数据库连接失败
```bash
docker compose restart postgres
sleep 5
docker compose restart app
```

### Redis 连接失败
```bash
docker compose exec redis redis-cli ping
docker compose restart redis
```

---

## 📖 详细文档

- **完整测试指南：** `TESTING_GUIDE.md`
- **依赖升级指南：** `DEPENDENCY_UPGRADE_GUIDE.md`
- **开发指南：** `DEVELOPMENT_GUIDE.md`
- **符合性检查：** `COMPLIANCE_CHECK.md`

---

**开始使用：** 访问 http://localhost:8000
