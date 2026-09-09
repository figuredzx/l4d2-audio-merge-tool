@echo off
chcp 936 >nul
cd /d "%~dp0"
rem ����ʹ�ô���õ� exe������ Python�������ʹ��ϵͳ Python
if exist "%~dp0��Ƶ��ϲ�����.exe" (
    "%~dp0��Ƶ��ϲ�����.exe"
    exit /b
)
where python >nul 2>nul
if errorlevel 1 (
    echo δ��⵽ Python��Ҳû�д���õ� ��Ƶ��ϲ�����.exe
    echo ���ֽ����ʽ��ѡ��һ��
    echo   1. ֱ��˫��ͬĿ¼�ġ���Ƶ��ϲ�����.exe��
    echo   2. �� python.org ��װ Python 3.8+��Ĭ�ϰ�װ���ɣ�����װ�κο⣩
    pause
    exit /b 1
)
python -m merger.gui
if errorlevel 1 pause
