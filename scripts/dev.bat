@echo off
REM 开发期一键启动（Windows 双击用）：调用 Git Bash 运行 dev.sh
REM 前提：Git Bash 在 PATH 中（即 git 安装时勾选了 "Git Bash Here"/加入到 PATH）
setlocal
set "HERE=%~dp0"
where bash >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 bash，请先安装 Git for Windows 并确保 bash 在 PATH 中。
  pause
  exit /b 1
)
bash "%HERE%dev.sh" %*
endlocal
