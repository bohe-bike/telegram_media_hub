# Telegram Media Hub（自托管 TG 媒体下载中枢）

## 1. 项目背景
当前方案：

- entity["software","Telegram","messaging platform"] Bot 接收链接
- 转发到 entity["software","MeTube","self-hosted web UI for yt-dlp"] 下载
- TG 原生视频保存到 NAS

现有问题：

- Bot API 权限有限
- 无法稳定处理大量任务
- 缺少任务管理
- 缺少失败恢复
- 无法应对代理波动
- 没有统一下载中心

目标：构建一个 **高稳定、支持代理容灾的自托管媒体下载系统**。

---

# 2. 项目目标

统一处理以下输入：

### Telegram 来源
- 私聊消息
- 群组消息
- Channel 消息
- Saved Messages
- 转发消息
- TG 视频
- TG 文档
- TG 图片

### 外部链接来源
- entity["company","YouTube","video platform"]
- entity["company","TikTok","video platform"]
- entity["company","Bilibili","video platform"]
- entity["company","X","social platform"]
- 其他 entity["software","yt-dlp","command-line video downloader"] 支持网站

最终统一下载到 NAS。

---

# 3. 核心需求（PRD）

## 3.1 消息接入系统

使用：entity["software","Pyrogram","Telegram MTProto framework"]

原因：

- 支持 user account 登录
- 可访问私有群/频道
- 支持大文件
- 比 Bot API 限制更少

功能：

- 登录 TG 账号
- Session 持久化
- 自动监听消息
- 支持多个聊天来源

---

## 3.2 内容识别

系统自动判断消息类型：

### 文本链接
发送至：

entity["software","MeTube","self-hosted web UI for yt-dlp"]

---

### TG 视频
直接下载 NAS

---

### TG 文件
直接下载 NAS

---

### 图片/音频
V2 支持

---

# 4. 稳定性需求（核心）

## 4.1 自动重试

任务失败后：

- 自动重试
- 指数退避
- 最大重试次数限制

示例：

- 第1次失败：30秒
- 第2次失败：2分钟
- 第3次失败：10分钟

---

## 4.2 断点续传

### TG 下载
记录：

- file_id
- offset
- 临时文件

### 外链下载
利用 entity["software","yt-dlp","command-line video downloader"]：

- --continue
- --retries
- --fragment-retries

---

## 4.3 服务崩溃恢复

容器重启后自动恢复：

- downloading
- retrying
- pending

任务

---

## 4.4 临时文件机制

下载中：

```
filename.tmp
```

完成后：

```
rename -> final file
```

---

# 5. 性能需求

## 任务处理模型

### TG 下载 Worker
固定单 worker 串行处理，优先保证 Telegram 会话稳定

### 外链下载 Worker
独立队列处理，不与 TG 原生媒体下载共享会话

---

## 分片下载

外链使用：

entity["software","aria2","download utility"]

提高下载速度。

---

# 6. 代理容灾需求

系统支持代理池：

```yaml
proxy_pool:
  - http://proxy1:7890
  - http://proxy2:7890
  - socks5://proxy3:1080
```

功能：

- 自动切换代理
- 健康检查
- 自动剔除失效代理
- 自动恢复

---

# 7. 存储规划

```
/media
  /telegram
    /video
    /photo
    /document

  /external
    /youtube
    /tiktok
    /bilibili

  /temp
```

---

# 8. Web 管理后台（V2）

后端：entity["software","FastAPI","Python web framework"]

前端：entity["software","Next.js","React framework"]

功能：

- 查看任务
- 搜索任务
- 删除任务
- 手动重试
- 查看速度
- 查看日志
- 查看代理状态

---

# 9. 技术架构

## 核心技术栈

### TG 接入
entity["software","Pyrogram","Telegram MTProto framework"]

---

### API 层
entity["software","FastAPI","Python web framework"]

---

### 消息队列
entity["software","Redis","in-memory datastore"] + entity["software","RQ","job queue library"]

选择原因：

- 比 entity["software","Celery","distributed task queue"] 更轻
- 部署简单

---

### 数据库
entity["software","PostgreSQL","database"]

用于：

- 任务记录
- 状态管理
- 日志记录

---

### 外链下载
entity["software","yt-dlp","command-line video downloader"]

配合：

entity["software","aria2","download utility"]

---

### 容器部署
entity["software","Docker Compose","container orchestration tool"]

---

# 10. 系统架构图

```
Telegram
   ↓
Pyrogram Listener
   ↓
Task Dispatcher
   ↓
Redis Queue
   ↓
Workers
 ├── TG Download Worker
 ├── External Download Worker
 └── Retry Worker
   ↓
PostgreSQL
   ↓
NAS Storage
```

---

# 11. 数据库设计

## tasks

- id
- source_type
- source_url
- telegram_file_id
- status
- retry_count
- proxy_used
- speed
- file_size
- local_path
- created_at
- updated_at

---

## proxies

- id
- proxy_url
- status
- latency
- fail_count

---

# 12. API 设计

## 创建任务

POST /tasks

---

## 获取任务列表

GET /tasks

---

## 获取任务详情

GET /tasks/{id}

---

## 手动重试

POST /tasks/{id}/retry

---

## 删除任务

DELETE /tasks/{id}

---

# 13. Docker 架构

```yaml
services:
  app
  worker
  redis
  postgres
  metube
```

---

# 14. 第一阶段 MVP

必须完成：

- TG 登录
- 消息监听
- 外链下载
- TG 文件下载
- NAS 存储
- 自动重试
- 基础任务状态

---

# 15. 第二阶段

- Web UI
- 代理池
- 多账号支持
- 批量历史消息抓取

---

# 16. 第三阶段

- entity["software","Plex","media server"] / entity["software","Jellyfin","media server"] 联动
- 自动分类
- AI 标签
- OCR
- 字幕处理

---

# 17. 核心设计原则

> 稳定优先 > 下载速度 > 功能堆叠

目标：

即使连续提交 100+ 下载任务，在代理波动、容器重启、网络异常情况下，系统仍能最终完成下载。
