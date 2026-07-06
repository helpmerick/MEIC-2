@echo off
REM Launcher for the DXLink quote-token boundary observation (Phase-5 assumption 9).
REM Scheduled to run unattended tomorrow at market open; holds ~25h to cross the token boundary.
cd /d "C:\Users\ashle\MEIC_BOT_2.0"
".venv\Scripts\python.exe" "tools\observe_quote_token.py" --hours 25
