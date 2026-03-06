#!/usr/bin/env python3
"""
Set up the .tools environment:
  1. Create a virtualenv at .tools/.venv
  2. Install dependencies from .tools/requirements.txt
  3. Copy .tools/templates/.agent and .tools/templates/do.py
     to the parent project directory (if they don't already exist)
"""

import os
import shutil
import subprocess
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOLS_DIR)
VENV_DIR = os.path.join(TOOLS_DIR, ".venv")
REQUIREMENTS = os.path.join(TOOLS_DIR, "requirements.txt")
TEMPLATES_DIR = os.path.join(TOOLS_DIR, "templates")


def create_venv():
    if os.path.isdir(VENV_DIR):
        print(f"Virtualenv already exists at {VENV_DIR}")
    else:
        print(f"Creating virtualenv at {VENV_DIR} ...")
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
        print("Virtualenv created.")


def install_requirements():
    if not os.path.isfile(REQUIREMENTS):
        print(f"No requirements.txt found at {REQUIREMENTS}, skipping install.")
        return

    pip = os.path.join(VENV_DIR, "Scripts" if sys.platform == "win32" else "bin", "pip")
    print(f"Installing requirements from {REQUIREMENTS} ...")
    subprocess.run([pip, "install", "-r", REQUIREMENTS], check=True)
    print("Requirements installed.")


def copy_if_absent(src, dst):
    if os.path.exists(dst):
        print(f"Already exists, skipping: {dst}")
        return
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    print(f"Copied: {src} -> {dst}")


def copy_templates():
    for name in [".agent", "do.py", "ci.py"]:
        src = os.path.join(TEMPLATES_DIR, name)
        dst = os.path.join(PROJECT_DIR, name)
        if not os.path.exists(src):
            print(f"Template not found, skipping: {src}")
            continue
        copy_if_absent(src, dst)


def main():
    create_venv()
    install_requirements()
    copy_templates()
    print("\nSetup complete.")


if __name__ == "__main__":
    main()
