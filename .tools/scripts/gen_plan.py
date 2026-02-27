#!/usr/bin/env python3
import os
import subprocess
import sys
import shutil
import json
import re
from typing import List, Dict, Any, Optional
import threading
import concurrent.futures

ignore_file_lock = threading.Lock()

DOCS = [
    # Research
    {"id": "market_research", "type": "research", "name": "Market Research Report", "desc": "Analyze the problem space and create a market research report.", "prompt_file": "research_market.md"},
    {"id": "competitive_analysis", "type": "research", "name": "Competitive Analysis Report", "desc": "Analyze the competition and create a competitive analysis report.", "prompt_file": "research_competitive_analysis.md"},
    {"id": "tech_landscape", "type": "research", "name": "Technology Landscape Report", "desc": "Analyze the available technologies and create a technology landscape report.", "prompt_file": "research_technical_analysis.md"},
    {"id": "user_research", "type": "research", "name": "User Research Report", "desc": "Analyze potential users and create a user research report.", "prompt_file": "research_user_research.md"},
    
    # Specs
    {"id": "1_prd", "type": "spec", "name": "PRD (Product Requirements Document)", "desc": "Create a Product Requirements Document (PRD).", "prompt_file": "spec_prd.md"},
    {"id": "2_tas", "type": "spec", "name": "TAS (Technical Architecture Specification)", "desc": "Create a Technical Architecture Specification (TAS).", "prompt_file": "spec_tas.md"},
    {"id": "3_mcp_design", "type": "spec", "name": "MCP and AI Development Design", "desc": "Create an MCP and AI Development Design document.", "prompt_file": "spec_mcp_design.md"},
    {"id": "4_user_features", "type": "spec", "name": "User Features", "desc": "Create a User Features document describing user journeys and expectations.", "prompt_file": "spec_user_features.md"},
    {"id": "5_security_design", "type": "spec", "name": "Security Design", "desc": "Create a Security Design document detailing risks and security architectures.", "prompt_file": "spec_security_design.md"},
    {"id": "6_ui_ux_architecture", "type": "spec", "name": "UI/UX Architecture", "desc": "Create a UI/UX Architecture document.", "prompt_file": "spec_ui_ux_architecture.md"},
    {"id": "7_ui_ux_design", "type": "spec", "name": "UI/UX Design", "desc": "Create a UI/UX Design document.", "prompt_file": "spec_ui_ux_design.md"},
    {"id": "8_risks_mitigation", "type": "spec", "name": "Risks and Mitigation", "desc": "Create a Risks and Mitigation document.", "prompt_file": "spec_risks_mitigation.md"},
    {"id": "9_project_roadmap", "type": "spec", "name": "Project Roadmap", "desc": "Create a Project Roadmap.", "prompt_file": "spec_project_roadmap.md"}
]

class AIRunner:
    """Abstract base for AI CLI runners."""
    def write_ignore_file(self, ignore_file: str, ignore_content: str):
        with ignore_file_lock:
            should_write = True
            if os.path.exists(ignore_file):
                with open(ignore_file, "r", encoding="utf-8") as f:
                    if f.read() == ignore_content:
                        should_write = False
            if should_write:
                with open(ignore_file, "w", encoding="utf-8") as f:
                    f.write(ignore_content)

    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        raise NotImplementedError()

    @property
    def ignore_file_name(self) -> str:
        raise NotImplementedError()


class GeminiRunner(AIRunner):
    """Wraps the gemini CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)
        return subprocess.run(
            ["gemini", "-y"],
            input=full_prompt,
            cwd=cwd,
            capture_output=True,
            text=True
        )

    @property
    def ignore_file_name(self) -> str:
        return ".geminiignore"


class ClaudeRunner(AIRunner):
    """Wraps the claude CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)
        return subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions"],
            input=full_prompt,
            cwd=cwd,
            capture_output=True,
            text=True
        )

    @property
    def ignore_file_name(self) -> str:
        return ".claudeignore"


class CopilotRunner(AIRunner):
    """Wraps the GitHub Copilot CLI subprocess call."""
    def run(self, cwd: str, full_prompt: str, ignore_content: str, ignore_file: str) -> subprocess.CompletedProcess:
        self.write_ignore_file(ignore_file, ignore_content)

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=True) as f:
            f.write(full_prompt)
            #print(full_prompt)
            prompt_file = f.name
            candidates = [
                ["copilot", "-p", f"Follow the instructions in @{prompt_file}", "--yolo"],
            ]
            last_exc = None
            last_result = None
            for cmd in candidates:
                try:
                    last_result = subprocess.run(cmd, input=full_prompt, cwd=cwd, capture_output=True, text=True)
                    # If the command ran (found) and returned 0, return immediately.
                    if last_result.returncode == 0:
                        return last_result
                except FileNotFoundError as e:
                    last_exc = e
                    continue

        # If none succeeded, return the last result if available, else raise the FileNotFoundError
        if last_result is not None:
            return last_result
        raise last_exc if last_exc is not None else RuntimeError("Failed to invoke copilot CLI")

    @property
    def ignore_file_name(self) -> str:
        return ".copilotignore"


class ProjectContext:
    def __init__(self, root_dir: str, runner: Optional[AIRunner] = None, jobs: int = 1):
        self.root_dir = root_dir
        self.jobs = jobs
        self.sandbox_dir = os.path.join(root_dir, ".sandbox")
        self.plan_dir = os.path.join(root_dir, "docs", "plan")
        self.specs_dir = os.path.join(self.plan_dir, "specs")
        self.research_dir = os.path.join(self.plan_dir, "research")
        self.prompts_dir = os.path.join(root_dir, "scripts", "prompts")
        self.state_file = os.path.join(root_dir, "scripts", ".gen_state.json")
        self.desc_file = os.path.join(self.plan_dir, "input", "description.md")
        
        self.requirements_dir = os.path.join(self.plan_dir, "requirements")
        
        self.runner = runner or GeminiRunner()
        
        self.ignore_file = os.path.join(root_dir, self.runner.ignore_file_name)
        self.backup_ignore = self.ignore_file + ".bak"
        
        # Ensures directories exist
        os.makedirs(self.sandbox_dir, exist_ok=True)
        os.makedirs(self.specs_dir, exist_ok=True)
        os.makedirs(self.research_dir, exist_ok=True)
        os.makedirs(self.requirements_dir, exist_ok=True)
        
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

    def load_prompt(self, filename: str) -> str:
        prompt_path = os.path.join(self.prompts_dir, filename)
        if not os.path.exists(prompt_path):
            print(f"Error: Prompt template {prompt_path} not found.")
            sys.exit(1)
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def format_prompt(self, tmpl: str, **kwargs) -> str:
        result = tmpl
        for k, v in kwargs.items():
            result = result.replace(f"{{{k}}}", str(v))
        return result

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

    def get_accumulated_context(self, current_doc: dict) -> str:
        accumulated_context = ""
        for prev_doc in DOCS:
            if prev_doc == current_doc:
                break
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

    def parse_markdown_headers(self, filepath: str) -> List[str]:
        headers = []
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    # Only want to capture h1 and h2
                    if re.match(r'^#{1,2}\s+', line):
                        headers.append(line.strip())
        return headers

class BasePhase:
    def execute(self, ctx: ProjectContext):
        raise NotImplementedError()

class Phase1GenerateDoc(BasePhase):
    def __init__(self, doc: dict):
        self.doc = doc

    def execute(self, ctx: ProjectContext):
        if self.doc["id"] in ctx.state.get("generated", []):
            print(f"Skipping initial generation for {self.doc['name']} (already generated).")
            return
            
        target_path = ctx.get_target_path(self.doc)
        expected_file = ctx.get_document_path(self.doc)
        out_folder = "specs" if self.doc["type"] == "spec" else "research"
        
        accumulated_context = ctx.get_accumulated_context(self.doc)
        base_prompt_template = ctx.load_prompt(self.doc["prompt_file"])
        
        base_prompt = base_prompt_template.replace("{target_path}", target_path)
        base_prompt = base_prompt.replace("{document_name}", self.doc["name"])
        base_prompt = base_prompt.replace("{document_description}", self.doc["desc"])
        
        full_prompt = (
            f"{base_prompt}\n\n"
            f"# CONTEXT (Project Description)\n"
            f"{ctx.description_ctx}\n\n"
            f"# PREVIOUS PROJECT CONTEXT\n"
            f"{accumulated_context}\n\n"
            f"# FINAL INSTRUCTIONS\n"
            f"1. Read the Context provided above carefully.\n"
            f"2. Execute the Task as described in the Persona section.\n"
            f"3. Ensure the document is written to '{target_path}'.\n"
            f"4. You MUST end your turn immediately after writing the file.\n"
        )
        
        ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/{out_folder}/\n"
        print(f"\n=> [Phase 1: Generate] {self.doc['name']} into docs/plan/{out_folder}/{self.doc['id']}.md ...")
        
        allowed_files = [expected_file]
        result = ctx.run_gemini(full_prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0 or not os.path.exists(expected_file):
            print(f"\n[!] Error generating {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
            
        ctx.stage_changes(allowed_files)
        ctx.state.setdefault("generated", []).append(self.doc["id"])
        ctx.save_state()

class Phase2FleshOutDoc(BasePhase):
    def __init__(self, doc: dict):
        self.doc = doc

    def execute(self, ctx: ProjectContext):
        if self.doc["type"] != "spec":
            return
            
        if self.doc["id"] in ctx.state.get("fleshed_out", []):
            print(f"Skipping fleshing out for {self.doc['name']} (already fleshed out).")
            return
            
        expected_file = ctx.get_document_path(self.doc)
        target_path = ctx.get_target_path(self.doc)
        out_folder = "specs"
        accumulated_context = ctx.get_accumulated_context(self.doc)
        
        headers = ctx.parse_markdown_headers(expected_file)
        flesh_prompt_tmpl = ctx.load_prompt("flesh_out.md")
        
        ctx.state.setdefault("fleshed_out_headers", {})
        ctx.state["fleshed_out_headers"].setdefault(self.doc["id"], [])
        
        for header in headers:
            header_clean = header.strip()
            if header_clean == "":
                continue
                
            if header_clean in ctx.state["fleshed_out_headers"][self.doc["id"]]:
                print(f"   -> [Phase 2: Flesh Out Section] Skipping '{header_clean}' in {self.doc['name']} (already fleshed out).")
                continue
                
            print(f"   -> [Phase 2: Flesh Out Section] {header_clean} in {self.doc['name']} ...")
            flesh_prompt = ctx.format_prompt(flesh_prompt_tmpl,
                header=header_clean,
                target_path=target_path,
                description_ctx=ctx.description_ctx,
                accumulated_context=accumulated_context
            )
            
            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/{out_folder}/\n"
            allowed_files = [expected_file]
            result = ctx.run_gemini(flesh_prompt, ignore_content, allowed_files=allowed_files)
            
            if result.returncode != 0:
                print(f"\n[!] Error fleshing out section {header_clean} in {self.doc['name']}.")
                print(result.stdout)
                print(result.stderr)
                sys.exit(1)
            
            ctx.stage_changes(allowed_files)
            ctx.state["fleshed_out_headers"][self.doc["id"]].append(header_clean)
            ctx.save_state()
        
        ctx.state.setdefault("fleshed_out", []).append(self.doc["id"])
        ctx.save_state()

class Phase3FinalReview(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("final_review_completed", False):
            print("Final alignment review already completed.")
            return
            
        print("\n=> [Phase 3: Final Alignment Review] Reviewing all documents for consistency...")
        final_prompt_tmpl = ctx.load_prompt("final_review.md")
        final_prompt = ctx.format_prompt(final_prompt_tmpl, description_ctx=ctx.description_ctx)
        ignore_content = "/*\n!/.sandbox/\n!/docs/plan/specs/\n!/docs/plan/research/\n"
        
        # Final review can modify all existing specs and research files
        allowed_files = [ctx.get_document_path(d) for d in DOCS]
        result = ctx.run_gemini(final_prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print("\n[!] Error during final alignment review.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
            
        ctx.stage_changes(allowed_files)
        ctx.state["final_review_completed"] = True
        ctx.save_state()
        print("Successfully completed the Final Alignment Review.")

class Phase4AExtractRequirements(BasePhase):
    def __init__(self, doc: dict):
        self.doc = doc

    def execute(self, ctx: ProjectContext):
        if self.doc["type"] == "research":
            print(f"   -> Skipping extraction for research doc: {self.doc['name']}...")
            return

        if self.doc["id"] in ctx.state.get("extracted_requirements", []):
            return
            
        doc_path = ctx.get_document_path(self.doc)
        if not os.path.exists(doc_path):
            return
            
        target_path = f"docs/plan/requirements/{self.doc['id']}.md"
        expected_file = os.path.join(ctx.requirements_dir, f"{self.doc['id']}.md")
        
        doc_rel_path = f"docs/plan/{'specs' if self.doc['type'] == 'spec' else 'research'}/{self.doc['id']}.md"
        
        print(f"\n=> [Phase 4A: Extract Requirements] Extracting from {self.doc['name']}...")
        prompt_tmpl = ctx.load_prompt("extract_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            document_name=self.doc['name'],
            document_path=doc_rel_path,
            target_path=target_path
        )
        
        ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/requirements/\n!/scripts/verify_requirements.py\n!/{doc_rel_path}\n"
        allowed_files = [expected_file, doc_path]
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print(f"\n[!] Error extracting requirements from {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
        
        print(f"   -> Verifying extraction for {self.doc['name']}...")
        verify_res = subprocess.run(
            [sys.executable, "scripts/verify_requirements.py", "--verify-doc", doc_path, expected_file],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print(f"\n[!] Automated verification failed for {self.doc['name']}:")
            print(verify_res.stdout)
            sys.exit(1)
        
        ctx.stage_changes(allowed_files)
        ctx.state.setdefault("extracted_requirements", []).append(self.doc["id"])
        ctx.save_state()

class Phase4BMergeRequirements(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("requirements_merged", False):
            print("Requirements merging already completed.")
            return
            
        print("\n=> [Phase 4B: Merge and Resolve Conflicts] Consolidating all requirements...")
        prompt_tmpl = ctx.load_prompt("merge_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)
        
        # This phase can modify requirements.md AND any source doc in docs/plan/specs/
        ignore_content = "/*\n!/.sandbox/\n!/docs/plan/requirements/\n!/requirements.md\n!/docs/plan/specs/\n!/scripts/verify_requirements.py\n"
        
        # Allowed files include the final requirements.md and specs for potential conflict resolution
        allowed_files = [os.path.join(ctx.root_dir, "requirements.md")]
        allowed_files.extend([ctx.get_document_path(d) for d in DOCS if d["type"] != "research"])
        
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print("\n[!] Error merging requirements.")
            sys.exit(1)
            
        print("\n   -> Verifying merged requirements.md...")
        verify_res = subprocess.run(
            [sys.executable, "scripts/verify_requirements.py", "--verify-master"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Automated verification failed after merging requirements:")
            print(verify_res.stdout)
            sys.exit(1)
            
        ctx.stage_changes(allowed_files)
        ctx.state["requirements_merged"] = True
        ctx.save_state()

class Phase4COrderRequirements(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("requirements_ordered", False):
            print("Requirements ordering already completed.")
            return
            
        print("\n=> [Phase 4C: Order Requirements] Sequencing requirements and capturing dependencies...")
        prompt_tmpl = ctx.load_prompt("order_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)
        
        ignore_content = "/*\n!/.sandbox/\n!/requirements.md\n!/ordered_requirements.md\n!/scripts/verify_requirements.py\n"
        allowed_files = [os.path.join(ctx.root_dir, "ordered_requirements.md")]
        
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            print("\n[!] Error ordering requirements.")
            sys.exit(1)
            
        print("\n   -> Verifying ordered_requirements.md against active requirements in requirements.md...")
        verify_res = subprocess.run(
            [sys.executable, "scripts/verify_requirements.py", "--verify-ordered", "requirements.md", "ordered_requirements.md"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Automated verification failed after ordering requirements:")
            print(verify_res.stdout)
            sys.exit(1)
            
        # Overwrite master with ordered version and cleanup
        master_req_path = os.path.join(ctx.root_dir, "requirements.md")
        ordered_req_path = os.path.join(ctx.root_dir, "ordered_requirements.md")
        if os.path.exists(ordered_req_path):
            if os.path.exists(master_req_path):
                os.remove(master_req_path)
            shutil.move(ordered_req_path, master_req_path)
            
        ctx.stage_changes([master_req_path])
        ctx.state["requirements_ordered"] = True
        ctx.save_state()

class Phase5GenerateEpics(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("phases_completed", False):
            print("Phase generation already completed.")
            return
            
        print("\n=> [Phase 5: Generate Epics] Generating detailed phases/")
        phases_prompt_tmpl = ctx.load_prompt("phases.md")
        phases_prompt = ctx.format_prompt(phases_prompt_tmpl, description_ctx=ctx.description_ctx)
        ignore_content = "/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/scripts/verify_requirements.py\n"
        
        phases_dir = os.path.join(ctx.plan_dir, "phases")
        os.makedirs(phases_dir, exist_ok=True)
        # Adding trailing slash allows creating content inside it
        allowed_files = [phases_dir + os.sep]
        result = ctx.run_gemini(phases_prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print("\n[!] Error generating phases.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
            
        print("\n   -> Verifying phases/ covers all requirements...")
        verify_res = subprocess.run(
            [sys.executable, "scripts/verify_requirements.py", "--verify-phases", "requirements.md", "docs/plan/phases/"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Automated verification failed: Not all requirements mapped to phases:")
            print(verify_res.stdout)
            sys.exit(1)
            
        ctx.stage_changes(allowed_files)
        ctx.state["phases_completed"] = True
        ctx.save_state()
        print("Successfully generated project phases.")

class Phase6BreakDownTasks(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("tasks_completed", False):
            print("Task generation already completed.")
            return
            
        print("\n=> [Phase 6: Break Down Tasks] Generating detailed tasks using Sub-Epic Grouping/")
        
        phases_dir = os.path.join(ctx.plan_dir, "phases")
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        
        if not os.path.exists(phases_dir):
            print("\n[!] Error: phases directory does not exist.")
            sys.exit(1)
            
        phase_files = [f for f in os.listdir(phases_dir) if f.endswith(".md")]
        if not phase_files:
            print("\n[!] Error: No phase documents found in phases/.")
            sys.exit(1)
            
        grouping_prompt_tmpl = ctx.load_prompt("group_tasks.md")
        tasks_prompt_tmpl = ctx.load_prompt("tasks.md")
        ctx.state.setdefault("tasks_generated", [])
        ctx.state.setdefault("ordered_phases_generated", [])
        
        for phase_filename in sorted(phase_files):
            phase_id = phase_filename.replace(".md", "")
            
            group_filename = f"{phase_id}_grouping.json"
            
            # 1. Project Manager Pass: Group Requirements
            print(f"   -> Grouping requirements for {phase_filename} into Sub-Epics...")
            grouping_prompt = ctx.format_prompt(grouping_prompt_tmpl, 
                                             description_ctx=ctx.description_ctx,
                                             phase_filename=phase_filename,
                                             group_filename=group_filename)
            
            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/phases/\n!/docs/plan/tasks/\n!/scripts/verify_requirements.py\n"
            group_filepath = os.path.join(tasks_dir, group_filename)
            allowed_files = [group_filepath]
            
            if not os.path.exists(group_filepath):
                group_result = ctx.run_gemini(grouping_prompt, ignore_content, allowed_files=allowed_files, sandbox=False)
            
                if group_result.returncode != 0:
                    print(f"\n[!] Error grouping tasks for {phase_filename}.")
                    print(group_result.stdout)
                    print(group_result.stderr)
                    sys.exit(1)
                    
                if not os.path.exists(group_filepath):
                    print(f"\n[!] Error: Agent failed to generate grouping JSON file {group_filepath}.")
                    sys.exit(1)
                    
            else:
                print(f"   -> Skipping {phase_filename}: Already grouped.")

            with open(group_filepath, "r", encoding="utf-8") as f:
                try:
                    sub_epics = json.load(f)
                except json.JSONDecodeError as e:
                    print(f"\n[!] Error parsing grouping JSON file {group_filepath}: {e}")
                    sys.exit(1)
                
            print(f"   -> Found {len(sub_epics)} Sub-Epic groupings for {phase_filename}.")
            
            # 2. Lead Developer Pass: Iterative Detail Generation
            state_lock = threading.Lock()

            def process_sub_epic(sub_epic_name, reqs):
                if not isinstance(reqs, list):
                    return True
                    
                # Create a filesystem safe name for the sub-epic
                safe_name = re.sub(r'[^a-zA-Z0-9_\-]+', '_', sub_epic_name.lower())
                # E.g. tasks/phase_1/01_project_planning/
                target_dir = os.path.join(phase_id, f"{safe_name}")
                
                with state_lock:
                    if target_dir in ctx.state["tasks_generated"]:
                        print(f"      -> Skipping task generation for {target_dir} (already generated).")
                        return True
                        
                print(f"      -> Breaking down '{sub_epic_name}' ({len(reqs)} reqs) into {target_dir}/...")
                
                # Ensure the subdirectory exists
                phase_task_dir = os.path.join(tasks_dir, target_dir)
                os.makedirs(phase_task_dir, exist_ok=True)
                
                reqs_str = json.dumps(reqs)
                tasks_prompt = ctx.format_prompt(tasks_prompt_tmpl, 
                                                 description_ctx=ctx.description_ctx,
                                                 phase_filename=phase_filename,
                                                 sub_epic_name=sub_epic_name,
                                                 sub_epic_reqs=reqs_str,
                                                 target_dir=target_dir)
                
                ignore_content = f"/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/docs/plan/tasks/\n!/scripts/verify_requirements.py\n"
                
                allowed_files = [phase_task_dir + os.sep]
                result = ctx.run_gemini(tasks_prompt, ignore_content, allowed_files=allowed_files, sandbox=False)
                
                if result.returncode != 0:
                    print(f"\n[!] Error generating tasks for {target_dir}.")
                    print(result.stdout)
                    print(result.stderr)
                    return False
                    
                with state_lock:
                    ctx.state["tasks_generated"].append(target_dir)
                    ctx.save_state()
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as executor:
                futures = [
                    executor.submit(process_sub_epic, name, reqs)
                    for name, reqs in sorted(sub_epics.items())
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    if not future.result():
                        print("\n[!] Error encountered in parallel task generation. Exiting.")
                        os._exit(1)
            
        print("\n   -> Verifying tasks/ covers all requirements from phases/...")
        verify_res = subprocess.run(
            [sys.executable, "scripts/verify_requirements.py", "--verify-tasks", "docs/plan/phases/", "docs/plan/tasks/"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Automated verification failed: Not all requirements mapped to actionable tasks:")
            print(verify_res.stdout)
            sys.exit(1)
            
        ctx.stage_changes([tasks_dir])
        ctx.state["tasks_completed"] = True
        ctx.save_state()
        print("Successfully generated atomic tasks.")

class Phase7ADAGGeneration(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("dag_completed", False):
            print("DAG Generation already completed.")
            return

        print("\n=> [Phase 7A: DAG Generation] Creating dependency graphs for tasks...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        dag_prompt_tmpl = ctx.load_prompt("dag_tasks.md")
        state_lock = threading.Lock()

        def process_phase_dag(phase_id):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            dag_file_path = os.path.join(phase_dir_path, "dag.json")
            
            if os.path.exists(dag_file_path):
                 print(f"   -> Skipping DAG Generation for {phase_id} (already exists).")
                 return True
                 
            # Gather tasks
            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            if not sub_epics:
                return True
                
            tasks_content = ""
            for sub_epic in sorted(sub_epics):
                sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
                md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
                
                for md_file in sorted(md_files):
                     task_id = f"{sub_epic}/{md_file}"
                     tasks_content += f"### Task ID: {task_id}\n"
                     with open(os.path.join(sub_epic_dir, md_file), "r", encoding="utf-8") as f:
                          content = f.read()
                          # Indent content slightly so it's readable
                          tasks_content += "\n".join([f"    {line}" for line in content.split("\n")]) + "\n\n"
            
            print(f"   -> Generating DAG for {phase_id}...")
            
            prompt = ctx.format_prompt(
                dag_prompt_tmpl,
                phase_filename=phase_id,
                target_path=f"docs/plan/tasks/{phase_id}/dag.json",
                description_ctx=ctx.description_ctx,
                tasks_content=tasks_content
            )

            #print(prompt)
            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/{phase_id}/dag.json\n"
            allowed_files = [dag_file_path]
            
            for attempt in range(1, 4):
                result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)
                
                if result.returncode == 0 and os.path.exists(dag_file_path):
                    return True
                    
                print(f"\n[!] Error generating DAG for {phase_id} (Attempt {attempt}/3).")
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
                elif not os.path.exists(dag_file_path):
                    print(f"\n[!] Error: Agent failed to generate DAG JSON file {dag_file_path}.")
                    
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as executor:
            futures = [
                executor.submit(process_phase_dag, phase_id)
                for phase_id in sorted(phase_dirs)
            ]
            
            for future in concurrent.futures.as_completed(futures):
                if not future.result():
                    print("\n[!] Error encountered in parallel DAG generation. Exiting.")
                    os._exit(1)

        ctx.stage_changes([tasks_dir])
        ctx.state["dag_completed"] = True
        ctx.save_state()
        print("Successfully generated task DAGs.")


class Phase7BDAGReview(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("dag_reviewed", False):
            print("DAG Review already completed.")
            return

        print("\n=> [Phase 7B: DAG Review] Reviewing and refining dependency graphs...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            sys.exit(0)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        review_prompt_tmpl = ctx.load_prompt("dag_tasks_review.md")

        def process_phase_review(phase_id):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            dag_file_path = os.path.join(phase_dir_path, "dag.json")
            reviewed_dag_file_path = os.path.join(phase_dir_path, "dag_reviewed.json")
            
            if not os.path.exists(dag_file_path):
                return True
                
            if os.path.exists(reviewed_dag_file_path):
                 print(f"   -> Skipping DAG Review for {phase_id} (already reviewed).")
                 return True

            with open(dag_file_path, "r", encoding="utf-8") as f:
                proposed_dag = f.read()

            # Gather tasks
            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            tasks_content = ""
            for sub_epic in sorted(sub_epics):
                sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
                md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
                
                for md_file in sorted(md_files):
                     task_id = f"{sub_epic}/{md_file}"
                     tasks_content += f"### Task ID: {task_id}\n"
                     with open(os.path.join(sub_epic_dir, md_file), "r", encoding="utf-8") as f:
                          content = f.read()
                          tasks_content += "\n".join([f"    {line}" for line in content.split("\n")]) + "\n\n"
            
            print(f"   -> Reviewing DAG for {phase_id}...")
            
            prompt = ctx.format_prompt(
                review_prompt_tmpl,
                phase_filename=phase_id,
                target_path=f"docs/plan/tasks/{phase_id}/dag_reviewed.json",
                description_ctx=ctx.description_ctx,
                tasks_content=tasks_content,
                proposed_dag=proposed_dag
            )

            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/{phase_id}/dag_reviewed.json\n"
            allowed_files = [reviewed_dag_file_path]
            
            for attempt in range(1, 4):
                result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)
                
                if result.returncode == 0 and os.path.exists(reviewed_dag_file_path):
                    return True
                    
                print(f"\n[!] Error reviewing DAG for {phase_id} (Attempt {attempt}/3).")
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
                elif not os.path.exists(reviewed_dag_file_path):
                    print(f"\n[!] Error: Agent failed to generate reviewed DAG JSON file {reviewed_dag_file_path}.")
                    
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as executor:
            futures = [
                executor.submit(process_phase_review, phase_id)
                for phase_id in sorted(phase_dirs)
            ]
            
            for future in concurrent.futures.as_completed(futures):
                if not future.result():
                    print("\n[!] Error encountered in parallel DAG review. Exiting.")
                    os._exit(1)

        ctx.stage_changes([tasks_dir])
        ctx.state["dag_reviewed"] = True
        ctx.save_state()
        print("Successfully reviewed and refined task DAGs.")

class Orchestrator:
    def __init__(self, ctx: ProjectContext):
        self.ctx = ctx

    def run_phase_with_retry(self, phase, max_retries: int = 3):
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                print(f"\n   [Retry {attempt}/{max_retries}] Retrying {phase.__class__.__name__}...")
                
            try:
                phase.execute(self.ctx)
                print(f"   -> Phase {phase.__class__.__name__} completed.")
                return
            except SystemExit as e:
                # Some verify scripts might return 0 through sys.exit() on success
                if e.code == 0:
                    return
                print(f"\n[!] Phase {phase.__class__.__name__} failed on attempt {attempt}.")
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        print(f"   -> Continuing (assuming manual resolution).")
                        return
                
                self.ctx.state = self.ctx._load_state()
            except Exception as e:
                print(f"\n[!] Phase {phase.__class__.__name__} encountered an error on attempt {attempt}: {e}")
                if attempt < max_retries:
                    action = input("Press ENTER to retry, 'c' to continue (if manually resolved), or 'q' to quit: ")
                    if action.lower() == 'q':
                        sys.exit(1)
                    elif action.lower() == 'c':
                        print(f"   -> Continuing (assuming manual resolution).")
                        return
                        
                self.ctx.state = self.ctx._load_state()
                
        print(f"\n[!] {phase.__class__.__name__} failed after {max_retries} attempts.")
        sys.exit(1)

    def run(self):
        print("Beginning multi-phase document generation and lifecycle orchestration...")
        self.ctx.backup_ignore_file()
        try:
            # Phase 1 and 2 for each document
            for doc in DOCS:
                self.run_phase_with_retry(Phase1GenerateDoc(doc))
                self.run_phase_with_retry(Phase2FleshOutDoc(doc))

            self.run_phase_with_retry(Phase3FinalReview())
            
            if not self.ctx.state.get("requirements_extracted", False):
                for doc in DOCS:
                    self.run_phase_with_retry(Phase4AExtractRequirements(doc))
                self.ctx.state["requirements_extracted"] = True
                self.ctx.save_state()
                
            self.run_phase_with_retry(Phase4BMergeRequirements())
            self.run_phase_with_retry(Phase4COrderRequirements())
            self.run_phase_with_retry(Phase5GenerateEpics())
            #self.run_phase_with_retry(Phase6BreakDownTasks())
            self.run_phase_with_retry(Phase6BreakDownTasks())
            
            # DAG Generation Steps
            self.run_phase_with_retry(Phase7ADAGGeneration())
            self.run_phase_with_retry(Phase7BDAGReview())
        finally:
            self.ctx.restore_ignore_file()
        print("\nProject generation orchestration complete.")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-phase document generation orchestrator.")
    parser.add_argument(
        "--backend", choices=["gemini", "claude", "copilot"], default="gemini",
        help="AI CLI backend to use (default: gemini)"
    )
    parser.add_argument(
        "--phase", default=None,
        help="Start from a specific phase, e.g. '4-merge' (skips earlier phases)"
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Maximum number of parallel AI agents/jobs to run concurrently (default: 1)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-run of the specified phase by clearing its completed state"
    )
    args = parser.parse_args()

    runner: AIRunner
    if args.backend == "claude":
        runner = ClaudeRunner()
        print("Using Claude CLI backend.")
    elif args.backend == "copilot":
        runner = CopilotRunner()
        print("Using Copilot CLI backend.")
    else:
        runner = GeminiRunner()
        print("Using Gemini CLI backend.")

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ctx = ProjectContext(root_dir, runner=runner, jobs=args.jobs)

    if args.phase and args.force:
        phase_state_keys = {
            "4-merge": "requirements_merged",
            "4-order": "requirements_ordered",
            "5-epics": "phases_completed",
            "6-tasks": "tasks_completed",
            "7-dag": "dag_completed",
        }
        key = phase_state_keys.get(args.phase)
        if key and ctx.state.get(key, False):
            print(f"--force: Resetting state for phase '{args.phase}' ({key}).")
            ctx.state[key] = False
            ctx.save_state()
        elif not key:
            print(f"Warning: unknown phase '{args.phase}' for --force, ignoring.")

    orchestrator = Orchestrator(ctx)
    orchestrator.run()

if __name__ == "__main__":
    main()
