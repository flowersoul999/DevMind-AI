param(
    [switch]$UseConfiguredLLM,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$dataDirectory = Join-Path $projectRoot ".data"
$databasePath = Join-Path $dataDirectory "devflow-local-demo.db"
$backendOutputLog = Join-Path $dataDirectory "devflow-backend.out.log"
$backendErrorLog = Join-Path $dataDirectory "devflow-backend.err.log"
$frontendOutputLog = Join-Path $dataDirectory "devflow-frontend.out.log"
$frontendErrorLog = Join-Path $dataDirectory "devflow-frontend.err.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "未找到 backend/.venv。请先创建虚拟环境并安装 backend/requirements.txt。"
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules") -PathType Container)) {
    throw "未找到 frontend/node_modules。请先在 frontend 目录执行 npm install。"
}

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$databaseUrlPath = $databasePath.Replace("\", "/")
$env:DATABASE_URL = "sqlite:///$databaseUrlPath"
$env:MILVUS_ENABLED = "false"
$env:EMBEDDING_PROVIDER = "deterministic"
$env:EMBEDDING_MODEL = "deterministic-demo"
$env:EMBEDDING_DIMENSIONS = "128"
$env:AUTO_SYNC_ENABLED = "false"
$env:DEMO_MODE = "true"
$env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:$BackendPort"
$env:PYTHONPATH = $backendDirectory

if (-not $UseConfiguredLLM) {
    $env:LLM_API_KEY = ""
    $env:RAG_LLM_ENABLED = "false"
    $env:RAGAS_ENABLED = "false"
}

$backendProcess = $null
$frontendProcess = $null

try {
    $backendProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $backendDirectory `
        -RedirectStandardOutput $backendOutputLog `
        -RedirectStandardError $backendErrorLog `
        -WindowStyle Hidden `
        -PassThru

    $backendReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
        if ($backendProcess.HasExited) {
            throw "后端进程提前退出，请检查 $backendErrorLog。"
        }
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 2 | Out-Null
            $backendReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $backendReady) {
        throw "后端在 30 秒内没有就绪，请检查 $backendErrorLog。"
    }

    Push-Location $backendDirectory
    try {
        & $pythonPath (Join-Path $projectRoot "scripts\seed_demo_data.py")
        if ($LASTEXITCODE -ne 0) {
            throw "演示数据初始化失败。"
        }
    }
    finally {
        Pop-Location
    }

    $demoRepositories = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/repos" -TimeoutSec 5
    $demoRepository = $demoRepositories | Where-Object { $_.full_name -eq "course-demo/devflow-sample-api" } | Select-Object -First 1
    if (-not $demoRepository) {
        throw "没有找到初始化后的演示仓库。"
    }
    Invoke-RestMethod `
        -Method Patch `
        -Uri "http://127.0.0.1:$BackendPort/api/rag/repositories/$($demoRepository.id)/config" `
        -ContentType "application/json" `
        -Body '{"retrieval_method":"hybrid","score_threshold_enabled":false}' `
        -TimeoutSec 10 | Out-Null

    $frontendProcess = Start-Process `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev", "--", "-p", "$FrontendPort") `
        -WorkingDirectory $frontendDirectory `
        -RedirectStandardOutput $frontendOutputLog `
        -RedirectStandardError $frontendErrorLog `
        -WindowStyle Hidden `
        -PassThru

    $frontendReady = $false
    for ($attempt = 0; $attempt -lt 120; $attempt += 1) {
        if ($frontendProcess.HasExited) {
            throw "前端进程提前退出，请检查 $frontendErrorLog。"
        }
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort" -TimeoutSec 2 | Out-Null
            $frontendReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $frontendReady) {
        throw "前端在 60 秒内没有就绪，请检查 $frontendErrorLog。"
    }

    Write-Host "DevMind AI 本地演示已启动："
    Write-Host "  前端：http://127.0.0.1:$FrontendPort"
    Write-Host "  后端：http://127.0.0.1:$BackendPort"
    Write-Host "  后端 PID：$($backendProcess.Id)"
    Write-Host "  前端 PID：$($frontendProcess.Id)"
    Write-Host "  停止命令：Stop-Process -Id $($backendProcess.Id),$($frontendProcess.Id)"
    Write-Host "说明：该脚本关闭 Milvus并停用演示库的分数阈值，只用于页面、结构化数据和关键词检索演示；向量检索状态会显示 degraded。"
}
catch {
    if ($frontendProcess -and -not $frontendProcess.HasExited) {
        Stop-Process -Id $frontendProcess.Id
    }
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id
    }
    throw
}
