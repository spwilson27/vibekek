#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path

def main():
    script_dir = Path(__file__).parent.resolve()
    venv_dir = script_dir / ".venv"
    requirements_file = script_dir / "requirements.txt"
    install_stamp = venv_dir / ".install_stamp"
    
    # OS-specific python paths
    if os.name == "nt":  # Windows
        python_exe = venv_dir / "Scripts" / "python.exe"
        pip_exe = venv_dir / "Scripts" / "pip.exe"
    else:  # Linux/Mac
        python_exe = venv_dir / "bin" / "python"
        pip_exe = venv_dir / "bin" / "pip"

    # 1. Create venv if missing
    if not python_exe.exists():
        print(f"[wrapper] Creating .venv at {venv_dir}...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    # 2. Install deps if requirements.txt is newer than the stamp file
    req_mtime = requirements_file.stat().st_mtime if requirements_file.exists() else 0
    stamp_mtime = install_stamp.stat().st_mtime if install_stamp.exists() else 0

    if not install_stamp.exists() or req_mtime > stamp_mtime:
        print(f"[wrapper] Installing/updating dependencies from {requirements_file}...", file=sys.stderr)
        try:
            subprocess.run([str(pip_exe), "install", "-q", "-r", str(requirements_file)], check=True)
            install_stamp.touch()
        except subprocess.CalledProcessError as e:
            print(f"[wrapper] Failed to install dependencies: {e}", file=sys.stderr)
            sys.exit(1)

    # 3. Launch MCP server
    server_script = script_dir / "mcp_server.py"
    args = [str(python_exe), str(server_script)] + sys.argv[1:]
    
    # Use os.execv to replace the current process with the server process
    # This is more efficient and handles signals correctly
    os.execv(str(python_exe), args)

if __name__ == "__main__":
    main()
