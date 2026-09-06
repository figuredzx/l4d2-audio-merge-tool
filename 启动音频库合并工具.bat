@echo off
chcp 936 >nul
cd /d "%~dp0"
rem 优先使用打包好的 exe（无需 Python）；其次使用系统 Python
if exist "%~dp0音频库合并工具.exe" (
    "%~dp0音频库合并工具.exe"
    exit /b
)
where python >nul 2>nul
if errorlevel 1 (
    echo 未检测到 Python，也没有打包好的 音频库合并工具.exe
    echo 两种解决方式任选其一：
    echo   1. 直接双击同目录的「音频库合并工具.exe」
    echo   2. 到 python.org 安装 Python 3.8+（默认安装即可，无需装任何库）
    pause
    exit /b 1
)
python -m merger.gui
if errorlevel 1 pause
