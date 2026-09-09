@echo off
chcp 936 >nul
cd /d "%~dp0"
rem ���� exe����� Python
for %%f in ("%~dp0*.exe") do (
    "%%f"
    exit /b
)
where python >nul 2>nul
if errorlevel 1 (
    echo δ�ҵ� exe �� Python
    echo   1. ֱ��˫��ͬĿ¼�� exe
    echo   2. �� python.org ��װ Python 3.8+
    pause
    exit /b 1
)
python -m merger.gui
if errorlevel 1 pause
