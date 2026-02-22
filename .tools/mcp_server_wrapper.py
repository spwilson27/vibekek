#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path

def main():
    script_dir = Path(__file__).parent.resolve()
    venv_dir = script_dir / ".venv"
    
    # OS-specific python paths
    if os.name == "nt":  # Windows
        python_exe = venv_dir / "Scripts" / "python.exe"
        pip_exe = venv_dir / "Scripts" / "pip.exe"
    else:  # Linux/Mac
        python_exe = venv_dir / "bin" / "python"
        pip_exe = venv_dir / "bin" / "pip"

    target = sys.argv[1] if len(sys.argv) > 1 else "rag"
    
    if target == "rag":
        req_file = script_dir / "rag" / "requirements.txt"
        server_script = script_dir / "rag" / "mcp_server.py"
        script_args = sys.argv[2:]
    elif target == "mapper":
        req_file = script_dir / "mapper" / "requirements.txt"
        server_script = script_dir / "mapper" / "repomap_server.py"
        script_args = sys.argv[2:]
    else:
        # Fallback to treat target as direct script path
        server_script = Path(target).resolve()
        req_file = None
        script_args = sys.argv[2:]

    # 1. Create venv if missing
    if not python_exe.exists():
        print(f"[wrapper] Creating .venv at {venv_dir}...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=False)

    # Since we dont' check environment creation, we need to wait for it to finish
    # (There may be multiple servers starting up at the same time)
    if not pip_exe.exists():
        i = 0
        import time
        while not pip_exe.exists():
            time.sleep(1)
            i += 1
            if i > 100:
                raise Exception("Timed out waiting for venv to be created")

    # 2. Check requirements
    if req_file and req_file.exists():
        install_stamp = venv_dir / f".install_stamp_{target}"
        stamp_mtime = install_stamp.stat().st_mtime if install_stamp.exists() else 0
        req_mtime = req_file.stat().st_mtime
        
        if not install_stamp.exists() or req_mtime > stamp_mtime:
            print(f"[wrapper] Installing/updating dependencies for {target}...", file=sys.stderr)
            try:
                print(f"[wrapper] Installing from {req_file}...", file=sys.stderr)
                subprocess.run([str(pip_exe), "install", "-q", "-r", str(req_file)], check=True)
                install_stamp.touch()
            except subprocess.CalledProcessError as e:
                print(f"[wrapper] Failed to install dependencies: {e}", file=sys.stderr)
                sys.exit(1)

    # 3. Launch MCP server
    if not server_script.exists():
        print(f"[wrapper] Server script {server_script} does not exist.", file=sys.stderr)
        sys.exit(1)
        
    args = [str(python_exe), str(server_script)] + script_args
    
    # Use os.execv on Unix to replace the current process (more efficient/better signal handling).
    # On Windows, os.execv can have issues, so we use subprocess.run instead.
    if os.name == "nt":
        sys.exit(subprocess.run(args).returncode)
    else:
        os.execv(str(python_exe), args)

if __name__ == "__main__":
    main()
