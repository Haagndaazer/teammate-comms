@echo off
rem Sets TC_BASH_EXE to Git Bash (never the WSL launcher in System32).
set "TC_BASH_EXE="
set "TC_GIT_EXE="
for /f "delims=" %%G in ('where git.exe 2^>nul') do if not defined TC_GIT_EXE set "TC_GIT_EXE=%%G"
if defined TC_GIT_EXE for %%G in ("%TC_GIT_EXE%\..\..\bin\bash.exe") do if exist "%%~fG" set "TC_BASH_EXE=%%~fG"
if not defined TC_BASH_EXE if exist "%ProgramFiles%\Git\bin\bash.exe" set "TC_BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if not defined TC_BASH_EXE if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "TC_BASH_EXE=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined TC_BASH_EXE if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "TC_BASH_EXE=%ProgramFiles(x86)%\Git\bin\bash.exe"
