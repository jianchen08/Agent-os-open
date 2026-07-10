@echo off
REM 启动包装器: 运行 start_web_cn.bat 并把全部输出保存到日志文件。
REM 闪退/报错后可查看 start_web_cn.log 排查。
REM 双击此文件即可,与双击 start_web_cn.bat 效果相同,但额外生成日志。
cd /d "%~dp0"
echo Agent OS 启动(带日志模式,输出保存到 start_web_cn.log)...
echo.
call start_web_cn.bat 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath 'start_web_cn.log'"
echo.
echo === 日志已保存到 start_web_cn.log ===
pause
