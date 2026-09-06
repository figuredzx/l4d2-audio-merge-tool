@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 上传到 GitHub

where git >nul 2>nul
if errorlevel 1 goto :nogit

if not exist .git (
    git init
    git branch -M main
)

git remote get-url origin >nul 2>nul
if errorlevel 1 git remote add origin https://github.com/figuredzx/l4d2-audio-merge-tool.git

git add -A
git diff --cached --quiet
if not errorlevel 1 goto :push

set "msg="
set /p msg=请输入本次提交说明（直接回车则用“更新”）：
if "!msg!"=="" set "msg=更新"
git commit -m "!msg!"

:push
git branch -M main
echo.
echo 正在推送到 GitHub...
echo 首次推送会弹出 GitHub 登录窗口，用浏览器登录 figuredzx 账号授权即可。
echo 凭据会自动保存在 Windows 凭据管理器，以后推送免登录。
echo.
git push -u origin main
if errorlevel 1 goto :fail

echo.
echo === 推送完成！https://github.com/figuredzx/l4d2-audio-merge-tool ===
pause
exit /b 0

:nogit
echo 未检测到 git，请先安装 Git for Windows：https://git-scm.com/download/win
pause
exit /b 1

:fail
echo.
echo === 推送失败，请查看上方错误信息后重试 ===
pause
exit /b 1
