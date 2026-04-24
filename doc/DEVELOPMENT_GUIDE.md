# 开发环境安装指南

## Windows 开发环境

由于 tgcrypto 在 Windows 上需要 Visual Studio Build Tools 才能编译，
我们提供两种方案：

### 方案 1：无加密版本（推荐用于开发测试）

Pyrogram 的大多数功能不需要 tgcrypto，只在处理某些加密媒体时需要。

```bash
# 安装主要依赖
pip install pyrogram==2.0.106
pip install fastapi==0.115.6 uvicorn[standard]==0.34.0
pip install sqlalchemy[asyncio]==2.0.36 asyncpg==0.30.0 alembic==1.14.0
pip install redis==5.2.1 rq==1.16.2
pip install yt-dlp==2024.12.23
pip install pydantic==2.10.3 pydantic-settings==2.7.0
pip install httpx==0.28.1 aiofiles==24.1.0 python-dotenv==1.0.1
pip install loguru==0.7.3
```

或者使用 requirements.txt（已注释掉 tgcrypto）：

```bash
pip install -r requirements.txt
```

**注意：** 不带 tgcrypto 时，Pyrogram 会正常工作，但某些高级加密功能可能受限。

### 方案 2：完整版本（需要编译工具）

如果需要完整功能（处理某些加密媒体），需要安装 Visual Studio Build Tools：

1. 下载并安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. 选择 "C++ build tools" 工作负载
3. 然后安装 tgcrypto：

```bash
pip install tgcrypto==1.2.5
```

---

## Linux/Mac 开发环境

```bash
# 安装系统依赖
sudo apt-get update
sudo apt-get install -y python3-dev libssl-dev libffi-dev

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

---

## Docker 部署（生产环境）

Docker 镜像中已经预编译了所有依赖：

```bash
# 构建镜像
docker-compose build

# 启动服务
docker-compose up -d
```

Dockerfile 已经包含 tgcrypto 的编译：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*
```

---

## 验证安装

安装完成后，运行以下命令验证：

```python
python -c "import pyrogram; print('Pyrogram version:', pyrogram.__version__)"
python -c "import fastapi; print('FastAPI version:', fastapi.__version__)"
python -c "import sqlalchemy; print('SQLAlchemy version:', sqlalchemy.__version__)"
```

---

## 常见问题

### Q: 为什么 tgcrypto 在 Windows 上难安装？
A: tgcrypto 是 Pyrogram 的加密扩展，需要编译 C 代码。Windows 上需要 Visual Studio Build Tools。

### Q: 不安装 tgcrypto 会影响功能吗？
A: 大部分功能不受影响。只有处理某些特定的加密媒体时会受限。对于下载视频、文档等日常使用完全够用。

### Q: Docker 中也需要处理这个问题吗？
A: 不需要。Dockerfile 中已经包含了编译 tgcrypto 所需的工具。

### Q: 本地开发必须用 Docker 吗？
A: 不是必须的。可以使用本地 Python 环境（方案1）或 Docker（推荐）。

---

## 推荐开发流程

### 使用 Docker（推荐）

```bash
# 1. 创建 .env 文件
cp config/.env.example config/.env
# 编辑 config/.env

# 2. 启动依赖服务
docker-compose up -d postgres redis

# 3. 运行 migrations
docker-compose run --rm app alembic upgrade head

# 4. 启动服务
docker-compose up -d

# 5. 查看日志
docker-compose logs -f app
```

### 本地开发（不使用 Docker）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置数据库
createdb media_hub
# 或使用 PostgreSQL GUI 工具创建数据库

# 3. 运行 migrations
alembic upgrade head

# 4. 启动服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

**最后更新：** 2026-04-24
