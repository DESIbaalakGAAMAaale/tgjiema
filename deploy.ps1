# TG文件解码器 — 一键部署脚本（PowerShell）
# 环形冗余 v2 架构
# 用法: .\deploy.ps1
# 或:  .\deploy.ps1 -Docker    （使用 Docker 部署）
# 或:  .\deploy.ps1 -Local     （本地 Python 部署）

param(
    [switch]$Docker,
    [switch]$Local
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " TG文件解码器 — 一键部署" -ForegroundColor Cyan
Write-Host " 架构: 环形冗余 v2" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ── 步骤 1：检查配置文件 ──
Write-Host "`n[1/5] 检查配置文件..." -ForegroundColor Yellow

if (!(Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Write-Host "  [信息] .env 不存在，从 .env.example 复制..." -ForegroundColor Gray
        Copy-Item ".env.example" ".env"
        Write-Host "  [警告] 请编辑 .env 填入你的 Bot Token 等信息！" -ForegroundColor Red
    } else {
        Write-Host "  [错误] .env.example 不存在" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  [OK] .env 已存在" -ForegroundColor Green
}

if (!(Test-Path "config\groups.yaml")) {
    Write-Host "  [错误] config\groups.yaml 不存在" -ForegroundColor Red
    Write-Host "  请编辑 config\groups.yaml 填入你的频道 ID" -ForegroundColor Yellow
    exit 1
} else {
    Write-Host "  [OK] config\groups.yaml 已存在" -ForegroundColor Green
}

# ── 步骤 2：生成拓扑 ──
Write-Host "`n[2/5] 生成拓扑配置..." -ForegroundColor Yellow
python config/generate_topology.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [错误] 拓扑生成失败" -ForegroundColor Red
    exit 1
}

# ── 步骤 3：数据库初始化 ──
Write-Host "`n[3/5] 写入数据库..." -ForegroundColor Yellow
python admin/seed_topology.py --yes
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [警告] 数据库写入失败，请检查 COCKROACHDB_URL 配置" -ForegroundColor Red
    Write-Host "  你可稍后手动执行: python admin/seed_topology.py" -ForegroundColor Yellow
}

# ── 步骤 4：安装依赖 ──
Write-Host "`n[4/5] 安装 Python 依赖..." -ForegroundColor Yellow
pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [错误] 依赖安装失败" -ForegroundColor Red
    exit 1
}

# ── 步骤 5：启动服务 ──
Write-Host "`n[5/5] 启动服务..." -ForegroundColor Yellow

if ($Docker) {
    Write-Host "  使用 Docker 部署..." -ForegroundColor Gray
    docker-compose up -d --build
    Write-Host "`n  查看日志: docker-compose logs -f" -ForegroundColor Cyan
    Write-Host "  停止服务: docker-compose down" -ForegroundColor Cyan
} else {
    Write-Host "  本地启动..." -ForegroundColor Gray
    Write-Host "`n  管理后台: http://localhost:8080" -ForegroundColor Cyan
    Write-Host "  按 Ctrl+C 停止所有服务" -ForegroundColor Cyan
    python run_all.py
}

Write-Host "`n部署完成!" -ForegroundColor Green