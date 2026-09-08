@echo off
setlocal
call "%~dp0find-git-bash.cmd"
if not defined TC_BASH_EXE (
  echo {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "teammate-comms: Git for Windows (Git Bash) is required for this plugin's hooks on Windows but was not found. INSTRUCTION: tell the user to install it from https://git-scm.com/download/win and restart Codex."}}
  exit /b 0
)
set "TC_SH=%~dp0session-start.sh"
set "TC_SH=%TC_SH:\=/%"
"%TC_BASH_EXE%" "%TC_SH%"
exit /b %ERRORLEVEL%
