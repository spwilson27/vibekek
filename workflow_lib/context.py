import os
import subprocess
import sys
import json
import re
from typing import List, Dict, Any, Optional

from .constants import TOOLS_DIR, GEN_STATE_FILE, DOCS
from .runners import AIRunner, GeminiRunner
class ProjectContext:
    def __init__(self, root_dir: str, runner: Optional[AIRunner] = None, jobs: int = 1):
        self.root_dir = root_dir
        self.jobs = jobs
        self.sandbox_dir = os.path.join(root_dir, ".sandbox")
        self.plan_dir = os.path.join(root_dir, "docs", "plan")
        self.specs_dir = os.path.join(self.plan_dir, "specs")
        self.research_dir = os.path.join(self.plan_dir, "research")
        self.prompts_dir = os.path.join(TOOLS_DIR, "prompts")
        self.state_file = GEN_STATE_FILE
        self.desc_file = os.path.join(TOOLS_DIR, "input", "project-description.md")
        
        self.requirements_dir = os.path.join(self.plan_dir, "requirements")
        
        self.runner = runner or GeminiRunner()
        
        self.ignore_file = os.path.join(root_dir, self.runner.ignore_file_name)
        self.backup_ignore = self.ignore_file + ".bak"
        
        # Ensures directories exist
        os.makedirs(self.sandbox_dir, exist_ok=True)
        os.makedirs(self.specs_dir, exist_ok=True)
        os.makedirs(self.research_dir, exist_ok=True)
        os.makedirs(self.requirements_dir, exist_ok=True)
        
        self.shared_components_file = os.path.join(self.plan_dir, "shared_components.md")

        self.has_existing_ignore = os.path.exists(self.ignore_file)
        self.state = self._load_state()
        self.description_ctx = self._load_description()

    def _load_state(self) -> Dict[str, Any]:
        state = {
            "generated": [], 
            "fleshed_out": [], 
            "fleshed_out_headers": {},
            "extracted_requirements": [],
            "final_review_completed": False,
            "requirements_extracted": False,
            "requirements_merged": False,
            "requirements_ordered": False,
            "phases_completed": False,
            "tasks_completed": False,
            "tasks_reviewed": False,
            "cross_phase_reviewed": False,
            "tasks_generated": [],
            "dag_completed": False,
            "dag_reviewed": False,
            "tdd_completed": False
        }
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                try:
                    loaded = json.load(f)
                    state.update(loaded)
                except json.JSONDecodeError:
                    pass
        return state

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=4)

    def _load_description(self) -> str:
        if not os.path.exists(self.desc_file):
            print(f"Error: {self.desc_file} not found.")
            sys.exit(1)
        with open(self.desc_file, "r", encoding="utf-8") as f:
            return f.read()

    def load_shared_components(self) -> str:
        if os.path.exists(self.shared_components_file):
            with open(self.shared_components_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def load_prompt(self, filename: str) -> str:
        prompt_path = os.path.join(self.prompts_dir, filename)
        if not os.path.exists(prompt_path):
            print(f"Error: Prompt template {prompt_path} not found.")
            sys.exit(1)
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def format_prompt(self, tmpl: str, **kwargs) -> str:
        # Single-pass replacement to avoid values containing {other_key}
        # from being substituted in subsequent iterations
        pattern = re.compile(r'\{(' + '|'.join(re.escape(k) for k in kwargs) + r')\}')
        return pattern.sub(lambda m: str(kwargs[m.group(1)]), tmpl)

    def backup_ignore_file(self):
        if self.has_existing_ignore:
            shutil.copy(self.ignore_file, self.backup_ignore)

    def restore_ignore_file(self):
        if self.has_existing_ignore:
            if os.path.exists(self.backup_ignore):
                shutil.move(self.backup_ignore, self.ignore_file)
        elif os.path.exists(self.ignore_file):
            os.remove(self.ignore_file)

    def get_document_path(self, doc: dict) -> str:
        out_folder = "specs" if doc["type"] == "spec" else "research"
        return os.path.join(self.plan_dir, out_folder, f"{doc['id']}.md")

    def get_target_path(self, doc: dict) -> str:
        out_folder = "docs/plan/specs" if doc["type"] == "spec" else "docs/plan/research"
        return f"{out_folder}/{doc['id']}.md"

    def get_accumulated_context(self, current_doc: dict, include_research: bool = True) -> str:
        accumulated_context = ""
        for prev_doc in DOCS:
            if prev_doc == current_doc:
                break
            # Skip research docs when building context for spec generation
            # to prevent hallucinated market data from influencing architecture
            if not include_research and prev_doc["type"] == "research":
                continue
            prev_file = self.get_document_path(prev_doc)
            if os.path.exists(prev_file):
                with open(prev_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    accumulated_context += f'\n\n<previous_document name="{prev_doc["name"]}">\n{content}\n</previous_document>\n'
        return accumulated_context

    def get_workspace_snapshot(self) -> Dict[str, float]:
        snapshot = {}
        for root, dirs, files in os.walk(self.root_dir):
            if ".git" in root or ".sandbox" in root:
                continue
            for file in files:
                if file == ".DS_Store":
                    continue
                filepath = os.path.join(root, file)
                try:
                    snapshot[filepath] = os.path.getmtime(filepath)
                except OSError:
                    pass
        return snapshot

    def stage_changes(self, file_paths: List[str]):
        if not file_paths:
            return
        clean_paths = [os.path.abspath(p) for p in file_paths if p]
        subprocess.run(["git", "add"] + clean_paths, cwd=self.root_dir, check=False)

    def verify_changes(self, before: Dict[str, float], allowed_files: List[str]):
        after = self.get_workspace_snapshot()
        allowed_set = set(os.path.abspath(f) for f in allowed_files)
        allowed_dirs = [os.path.abspath(f) for f in allowed_files if os.path.isdir(f) or f.endswith(os.sep)]
        
        # Check for new or modified files
        for path in after:
            if path not in before or after[path] > before.get(path, 0):
                abs_path = os.path.abspath(path)
                is_allowed = abs_path in allowed_set
                
                if not is_allowed:
                    for d in allowed_dirs:
                        if abs_path.startswith(d if d.endswith(os.sep) else d + os.sep):
                            is_allowed = True
                            break
                            
                if not is_allowed:
                    # Allow internal script files
                    if abs_path in [os.path.abspath(self.state_file), 
                                  os.path.abspath(self.ignore_file), 
                                  os.path.abspath(self.backup_ignore)]:
                        continue
                    print(f"\n[SANDBOX VIOLATION] Unauthorized change detected: {path}")
                    print(f"The agent was only allowed to modify: {allowed_files}")
                    sys.exit(1)
        
        # Check for deleted files
        for path in before:
            if path not in after:
                abs_path = os.path.abspath(path)
                is_allowed = abs_path in allowed_set
                if not is_allowed:
                    for d in allowed_dirs:
                        if abs_path.startswith(d if d.endswith(os.sep) else d + os.sep):
                            is_allowed = True
                            break
                            
                if not is_allowed:
                    print(f"\n[SANDBOX VIOLATION] Unauthorized deletion detected: {path}")
                    sys.exit(1)

    def strip_thinking_tags(self, filepath: str):
        if not os.path.exists(filepath):
            return
            
        if os.path.isdir(filepath):
            for filename in os.listdir(filepath):
                if filename.endswith(".md"):
                    self.strip_thinking_tags(os.path.join(filepath, filename))
            return
            
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            
        new_content = re.sub(r'<thinking>.*?</thinking>\s*', '', content, flags=re.DOTALL)
        
        if new_content != content:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)

    def run_ai(self, full_prompt: str, ignore_content: str, allowed_files: Optional[List[str]] = None, sandbox: bool = True) -> subprocess.CompletedProcess:
        before = self.get_workspace_snapshot()
        result = self.runner.run(self.root_dir, full_prompt, ignore_content, self.ignore_file)
        if allowed_files is not None:
            if sandbox:
                self.verify_changes(before, allowed_files)
            for f in allowed_files:
                self.strip_thinking_tags(os.path.abspath(f))
        return result

    # Legacy alias kept for backwards compat
    def run_gemini(self, full_prompt: str, ignore_content: str, allowed_files: Optional[List[str]] = None, sandbox: bool = True) -> subprocess.CompletedProcess:
        return self.run_ai(full_prompt, ignore_content, allowed_files, sandbox)

    def count_task_files(self, directory: str) -> int:
        count = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.endswith(".md") and f not in ("review_summary.md", "cross_phase_review_summary.md",
                                                     "reorder_tasks_summary.md",
                                                     "cross_phase_review_summary_pass_1.md",
                                                     "cross_phase_review_summary_pass_2.md",
                                                     "reorder_tasks_summary_pass_1.md",
                                                     "reorder_tasks_summary_pass_2.md"):
                    count += 1
        return count

    def parse_markdown_headers(self, filepath: str) -> List[str]:
        headers = []
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    # Only want to capture h1 and h2
                    if re.match(r'^#{1,2}\s+', line):
                        headers.append(line.strip())
        return headers

