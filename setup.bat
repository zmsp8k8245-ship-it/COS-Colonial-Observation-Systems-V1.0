@echo off
REM Install requests
pip install requests

REM Create start.bat
(
echo @echo off
echo python cos.py
echo pause
) > start.bat

echo start.bat has been created. Run start.bat to launch cos.py
pause
