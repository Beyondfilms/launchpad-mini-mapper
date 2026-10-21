@echo off
where pyw >nul 2>nul && (start "" pyw "%~dp0launchpad_mapper.py") || (start "" pythonw "%~dp0launchpad_mapper.py")
