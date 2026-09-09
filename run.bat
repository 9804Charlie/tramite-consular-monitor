@echo off
REM Lanzado por el Programador de tareas de Windows.
cd /d "%~dp0"
py -3 monitor.py >> monitor.log 2>&1
