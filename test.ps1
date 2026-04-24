<#
.SYNOPSIS
    Telegram Media Hub – 本地镜像构建与冒烟测试脚本

.DESCRIPTION
    1. 检查 Docker Desktop 是否运行
    2. 构建应用镜像
    3. 启动完整服务栈（app + workers + postgres + redis）
    4. 等待健康检查通过
    5. 执行 API 冒烟测试
    6. 打印测试结果摘要
    7. 可选：测试结束后自动清理容器

.PARAMETER NoBuild
    跳过镜像构建，直接使用已有镜像启动

.PARAMETER NoCache
    构建时加 --no-cache（强制重新下载所有层，默认不加）

.PARAMETER KeepRunning
    测试完成后不停止容器（方便手动查看 Web UI）

.PARAMETER Timeout
    等待服务就绪的超时时间（秒），默认 120

.EXAMPLE
    .\test.ps1                    # 完整构建 + 测试 + 清理
    .\test.ps1 -NoBuild           # 跳过构建
    .\test.ps1 -KeepRunning       # 测试后保持运行
#>

param(
    [switch]$NoBuild,
    [switch]$NoCache,
    [switch]$KeepRunning,
    [int]$Timeout = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ──────────────────────────────────────────────────────────────
# 颜色辅助函数
# ──────────────────────────────────────────────────────────────
function Write-Step { param($msg) Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-OK { param($msg) Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "  ✗ $msg" -ForegroundColor Red }
function Write-Info { param($msg) Write-Host "  · $msg" -ForegroundColor DarkGray }

$COMPOSE_FILES = @("-f", "docker-compose.yml", "-f", "docker-compose.test.yml")
$PROJECT_NAME = "mediahub-test"
$BASE_URL = "http://localhost:8000"
$PASS = 0
$FAIL = 0

# ──────────────────────────────────────────────────────────────
# 0. 前置检查
# ──────────────────────────────────────────────────────────────
Write-Step "前置检查"

# Docker Desktop 是否运行
try {
    $null = docker info 2>&1
    Write-OK "Docker Desktop 正在运行"
}
catch {
    Write-Fail "Docker Desktop 未启动，请先启动 Docker Desktop"
    exit 1
}

# config.toml 存在
if (-not (Test-Path "config\config.toml")) {
    Write-Fail "config\config.toml 不存在，请先创建配置文件"
    exit 1
}
Write-OK "config\config.toml 存在"

# 创建本地媒体目录
New-Item -ItemType Directory -Force -Path "data\media" | Out-Null
Write-OK "本地存储目录 data\media 就绪"

# ──────────────────────────────────────────────────────────────
# 1. 停止并清理旧测试容器
# ──────────────────────────────────────────────────────────────
Write-Step "清理旧测试容器"
docker compose @COMPOSE_FILES -p $PROJECT_NAME down --remove-orphans 2>&1 | Out-Null
Write-OK "旧容器已清理"

# ──────────────────────────────────────────────────────────────
# 2. 构建镜像
# ──────────────────────────────────────────────────────────────
if (-not $NoBuild) {
    Write-Step "构建应用镜像"
    $buildStart = Get-Date
    $buildArgs = @("compose") + $COMPOSE_FILES + @("-p", $PROJECT_NAME, "build")
    if ($NoCache) { $buildArgs += "--no-cache" }
    $buildArgs += "app"
    docker @buildArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "镜像构建失败（exit code $LASTEXITCODE）"
        exit 1
    }
    $buildSecs = [int]((Get-Date) - $buildStart).TotalSeconds
    Write-OK "镜像构建成功（耗时 ${buildSecs}s）"
}
else {
    Write-Info "跳过构建（-NoBuild）"
}

# ──────────────────────────────────────────────────────────────
# 3. 启动服务
# ──────────────────────────────────────────────────────────────
Write-Step "启动服务栈"
docker compose @COMPOSE_FILES -p $PROJECT_NAME up -d --wait 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "docker compose up 失败"
    docker compose @COMPOSE_FILES -p $PROJECT_NAME logs --tail 50
    exit 1
}
Write-OK "所有容器已启动"

# ──────────────────────────────────────────────────────────────
# 4. 等待 FastAPI 就绪
# ──────────────────────────────────────────────────────────────
Write-Step "等待 FastAPI 服务就绪（最多 ${Timeout}s）"
$deadline = (Get-Date).AddSeconds($Timeout)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "$BASE_URL/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    }
    catch { }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 3
}
Write-Host ""

if (-not $ready) {
    Write-Fail "服务在 ${Timeout}s 内未就绪"
    Write-Info "--- app 日志 ---"
    docker compose @COMPOSE_FILES -p $PROJECT_NAME logs --tail 40 app
    if (-not $KeepRunning) {
        docker compose @COMPOSE_FILES -p $PROJECT_NAME down
    }
    exit 1
}
Write-OK "FastAPI 服务已就绪"

# ──────────────────────────────────────────────────────────────
# 5. 冒烟测试
# ──────────────────────────────────────────────────────────────
Write-Step "执行 API 冒烟测试"

function Test-Endpoint {
    param(
        [string]$Name,
        [string]$Method = "GET",
        [string]$Path,
        [int]$ExpectedStatus,
        [hashtable]$Body = $null
    )
    try {
        $params = @{
            Uri             = "$BASE_URL$Path"
            Method          = $Method
            UseBasicParsing = $true
            TimeoutSec      = 10
            ErrorAction     = "Stop"
            Headers         = @{ "Content-Type" = "application/json" }
        }
        if ($null -ne $Body) {
            $params["Body"] = ($Body | ConvertTo-Json -Compress)
        }
        $resp = Invoke-WebRequest @params
        if ($resp.StatusCode -eq $ExpectedStatus) {
            Write-OK "[$Method $Path] → $($resp.StatusCode) — $Name"
            $script:PASS++
            return $resp
        }
        else {
            Write-Fail "[$Method $Path] → 期望 $ExpectedStatus, 实际 $($resp.StatusCode) — $Name"
            $script:FAIL++
        }
    }
    catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -eq $ExpectedStatus) {
            Write-OK "[$Method $Path] → $statusCode — $Name"
            $script:PASS++
        }
        else {
            Write-Fail "[$Method $Path] → $($_.Exception.Message) — $Name"
            $script:FAIL++
        }
    }
    return $null
}

# 基础健康检查
Test-Endpoint -Name "健康检查"          -Path "/health"          -ExpectedStatus 200
Test-Endpoint -Name "前端页面"          -Path "/"                -ExpectedStatus 200

# 服务状态
Test-Endpoint -Name "服务状态"          -Path "/api/status"      -ExpectedStatus 200

# 配置 API
Test-Endpoint -Name "读取配置"          -Path "/api/config/"     -ExpectedStatus 200

# 任务 API
Test-Endpoint -Name "任务列表"          -Path "/api/tasks/"      -ExpectedStatus 200
Test-Endpoint -Name "任务统计"          -Path "/api/tasks/stats/summary" -ExpectedStatus 200

# 代理 API
Test-Endpoint -Name "代理列表"          -Path "/api/proxies/"    -ExpectedStatus 200

# TG 认证状态
Test-Endpoint -Name "TG 登录状态"       -Path "/api/auth/status" -ExpectedStatus 200

# 404 处理
Test-Endpoint -Name "不存在的路由"       -Path "/api/not-exist"   -ExpectedStatus 404

# ──────────────────────────────────────────────────────────────
# 6. 检查容器健康状态
# ──────────────────────────────────────────────────────────────
Write-Step "检查容器健康状态"
# 用 docker inspect 逐容器查询，避免 --format json 输出中未转义引号导致解析失败
$containerNames = @(
    "media-hub-app-test",
    "media-hub-tg-worker-test",
    "media-hub-external-worker-test",
    "media-hub-postgres-test",
    "media-hub-redis-test"
)
foreach ($name in $containerNames) {
    $state = docker inspect --format "{{.State.Status}}" $name 2>&1
    $health = docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}N/A{{end}}" $name 2>&1
    if ($state -eq "running") {
        Write-OK "${name}  状态=$state  健康=$health"
    }
    else {
        Write-Fail "${name}  状态=$state"
        $FAIL++
    }
}

# ──────────────────────────────────────────────────────────────
# 7. 结果摘要
# ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "═══════════════════════════════════════════" -ForegroundColor White
Write-Host "  测试结果：通过 $PASS  失败 $FAIL" -ForegroundColor $(if ($FAIL -eq 0) { "Green" } else { "Yellow" })
Write-Host "═══════════════════════════════════════════" -ForegroundColor White

if ($KeepRunning) {
    Write-Host ""
    Write-Host "  服务保持运行中。Web UI 地址：$BASE_URL" -ForegroundColor Cyan
    Write-Host "  查看日志: docker compose -f docker-compose.yml -f docker-compose.test.yml -p $PROJECT_NAME logs -f"
    Write-Host "  停止服务: docker compose -f docker-compose.yml -f docker-compose.test.yml -p $PROJECT_NAME down"
}
else {
    Write-Step "停止并清理测试容器"
    docker compose @COMPOSE_FILES -p $PROJECT_NAME down
    Write-OK "清理完成"
}

exit $(if ($FAIL -eq 0) { 0 } else { 1 })
