import os
import subprocess
import sys
import json
import re
from typing import List, Dict, Any, Optional

from .constants import TOOLS_DIR, DOCS, parse_requirements
from .context import ProjectContext
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
        
        # Exclude research docs from spec context to prevent hallucinated
        # market/competitive data from influencing architectural decisions
        include_research = self.doc["type"] == "research"
        accumulated_context = ctx.get_accumulated_context(self.doc, include_research=include_research)
        base_prompt_template = ctx.load_prompt(self.doc["prompt_file"])

        base_prompt = base_prompt_template.replace("{target_path}", target_path)
        base_prompt = base_prompt.replace("{document_name}", self.doc["name"])
        base_prompt = base_prompt.replace("{document_description}", self.doc["desc"])

        full_prompt = (
            f"{base_prompt}\n\n"
            f"# ORIGINAL PROJECT DESCRIPTION (This is the primary source of truth)\n"
            f"{ctx.description_ctx}\n\n"
            f"# DERIVED CONTEXT (For reference — defer to the original description above when conflicts arise)\n"
            f"{accumulated_context}\n\n"
            f"# FINAL INSTRUCTIONS\n"
            f"1. Read the Original Project Description above as your primary source of truth.\n"
            f"2. Execute the Task as described in the Persona section.\n"
            f"3. Do NOT add scope, features, or complexity beyond what the original description requests.\n"
            f"4. Ensure the document is written to '{target_path}'.\n"
            f"5. You MUST end your turn immediately after writing the file.\n"
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
        accumulated_context = ctx.get_accumulated_context(self.doc, include_research=False)

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


class Phase3BAdversarialReview(BasePhase):
    """Devil's advocate review comparing specs against original description."""
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("adversarial_review_completed", False):
            print("Adversarial review already completed.")
            return

        print("\n=> [Phase 3B: Adversarial Review] Comparing specs against original description for scope creep...")
        target_path = "docs/plan/adversarial_review.md"
        expected_file = os.path.join(ctx.plan_dir, "adversarial_review.md")

        prompt_tmpl = ctx.load_prompt("adversarial_review.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path
        )

        ignore_content = "/*\n!/.sandbox/\n!/docs/plan/specs/\n!/docs/plan/research/\n!/docs/plan/adversarial_review.md\n"
        allowed_files = [expected_file]
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(expected_file):
            print("\n[!] Error during adversarial review.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["adversarial_review_completed"] = True
        ctx.save_state()

        # Display summary for human review
        with open(expected_file, "r", encoding="utf-8") as f:
            content = f.read()
        scope_creep_count = content.lower().count("scope creep")
        needs_clarification_count = content.lower().count("needs clarification")

        print(f"\n   Adversarial Review Results:")
        print(f"   - SCOPE CREEP findings: {scope_creep_count}")
        print(f"   - NEEDS CLARIFICATION findings: {needs_clarification_count}")
        print(f"   Review saved to: {target_path}")

        if scope_creep_count > 0 or needs_clarification_count > 0:
            print(f"\n   Review the adversarial review report before continuing.")
            action = input("   [c]ontinue / [e]dit specs / [q]uit: ").strip().lower()
            if action == 'q':
                sys.exit(0)
            elif action == 'e':
                editor = os.environ.get("EDITOR", "vim")
                subprocess.run([editor, expected_file])

        print("Adversarial review complete.")


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
        
        ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/requirements/\n!/.tools/verify_requirements.py\n!/{doc_rel_path}\n"
        allowed_files = [expected_file, doc_path]
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print(f"\n[!] Error extracting requirements from {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
        
        print(f"   -> Verifying extraction for {self.doc['name']}...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "verify_requirements.py"), "--verify-doc", doc_path, expected_file],
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
        ignore_content = "/*\n!/.sandbox/\n!/docs/plan/requirements/\n!/requirements.md\n!/docs/plan/specs/\n!/.tools/verify_requirements.py\n"
        
        # Allowed files include the final requirements.md and specs for potential conflict resolution
        allowed_files = [os.path.join(ctx.root_dir, "requirements.md")]
        allowed_files.extend([ctx.get_document_path(d) for d in DOCS if d["type"] != "research"])
        
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print("\n[!] Error merging requirements.")
            sys.exit(1)
            
        print("\n   -> Verifying merged requirements.md...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "verify_requirements.py"), "--verify-master"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Automated verification failed after merging requirements:")
            print(verify_res.stdout)
            sys.exit(1)
            
        ctx.stage_changes(allowed_files)
        ctx.state["requirements_merged"] = True
        ctx.save_state()

class Phase4BScopeGate(BasePhase):
    """Human checkpoint to review requirements scope before proceeding."""
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("scope_gate_passed", False):
            print("Scope gate already passed.")
            return

        req_path = os.path.join(ctx.root_dir, "requirements.md")
        if not os.path.exists(req_path):
            print("\n[!] requirements.md not found. Cannot perform scope gate.")
            sys.exit(1)

        with open(req_path, "r", encoding="utf-8") as f:
            req_content = f.read()

        # Count requirements
        req_ids = re.findall(r'\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]', req_content)
        unique_reqs = set(req_ids)
        line_count = len(req_content.splitlines())

        print("\n" + "=" * 70)
        print("  SCOPE GATE — Human Review Required")
        print("=" * 70)
        print(f"\n  Total unique requirements: {len(unique_reqs)}")
        print(f"  Requirements document: {line_count} lines")
        print(f"\n  Original description: {len(ctx.description_ctx.splitlines())} lines")
        print(f"\n  Review 'requirements.md' to check for scope inflation.")
        print(f"  You may edit the file to remove or defer requirements.\n")

        while True:
            action = input("  [c]ontinue / [e]dit (opens $EDITOR) / [q]uit: ").strip().lower()
            if action == 'q':
                print("  Aborting.")
                sys.exit(0)
            elif action == 'e':
                editor = os.environ.get("EDITOR", "vim")
                subprocess.run([editor, req_path])
                # Recount after edit
                with open(req_path, "r", encoding="utf-8") as f:
                    req_content = f.read()
                req_ids = re.findall(r'\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]', req_content)
                unique_reqs = set(req_ids)
                print(f"\n  Updated requirement count: {len(unique_reqs)}")
            elif action == 'c':
                break

        ctx.stage_changes([req_path])
        ctx.state["scope_gate_passed"] = True
        ctx.save_state()
        print("  Scope gate passed. Continuing...\n")


class Phase4COrderRequirements(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("requirements_ordered", False):
            print("Requirements ordering already completed.")
            return
            
        print("\n=> [Phase 4C: Order Requirements] Sequencing requirements and capturing dependencies...")
        prompt_tmpl = ctx.load_prompt("order_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)
        
        ignore_content = "/*\n!/.sandbox/\n!/requirements.md\n!/ordered_requirements.md\n!/.tools/verify_requirements.py\n"
        allowed_files = [os.path.join(ctx.root_dir, "ordered_requirements.md")]
        
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)
        
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            print("\n[!] Error ordering requirements.")
            sys.exit(1)
            
        print("\n   -> Verifying ordered_requirements.md against active requirements in requirements.md...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "verify_requirements.py"), "--verify-ordered", "requirements.md", "ordered_requirements.md"],
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
        ignore_content = "/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/.tools/verify_requirements.py\n"
        
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
            [sys.executable, os.path.join(TOOLS_DIR, "verify_requirements.py"), "--verify-phases", "requirements.md", "docs/plan/phases/"],
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


class Phase5BSharedComponents(BasePhase):
    """Generate a shared components manifest to coordinate parallel agents."""
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("shared_components_completed", False):
            print("Shared components manifest already generated.")
            return

        print("\n=> [Phase 5B: Shared Components] Identifying shared modules and ownership...")
        target_path = "docs/plan/shared_components.md"
        expected_file = os.path.join(ctx.plan_dir, "shared_components.md")

        prompt_tmpl = ctx.load_prompt("shared_components.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path
        )

        ignore_content = "/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/docs/plan/shared_components.md\n"
        allowed_files = [expected_file]
        result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(expected_file):
            print("\n[!] Error generating shared components manifest.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["shared_components_completed"] = True
        ctx.save_state()
        print("Successfully generated shared components manifest.")


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
            
            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/phases/\n!/docs/plan/tasks/\n!/.tools/verify_requirements.py\n"
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
                shared_components_ctx = ctx.load_shared_components()
                tasks_prompt = ctx.format_prompt(tasks_prompt_tmpl,
                                                 description_ctx=ctx.description_ctx,
                                                 phase_filename=phase_filename,
                                                 sub_epic_name=sub_epic_name,
                                                 sub_epic_reqs=reqs_str,
                                                 target_dir=target_dir,
                                                 shared_components_ctx=shared_components_ctx)
                
                ignore_content = f"/*\n!/.sandbox/\n!/requirements.md\n!/docs/plan/phases/\n!/docs/plan/tasks/\n!/.tools/verify_requirements.py\n"
                
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
            [sys.executable, os.path.join(TOOLS_DIR, "verify_requirements.py"), "--verify-tasks", "docs/plan/phases/", "docs/plan/tasks/"],
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

class Phase6BReviewTasks(BasePhase):
    def execute(self, ctx: ProjectContext):
        if ctx.state.get("tasks_reviewed", False):
            print("Task review already completed.")
            return

        print("\n=> [Phase 6B: Review Tasks] Reviewing tasks within each phase for duplicates and coverage...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        review_prompt_tmpl = ctx.load_prompt("review_tasks_in_phase.md")

        def process_phase_review(phase_id):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            review_summary_path = os.path.join(phase_dir_path, "review_summary.md")
            
            if os.path.exists(review_summary_path):
                 print(f"   -> Skipping Task Review for {phase_id} (already reviewed).")
                 return True
                 
            # Gather tasks
            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            if not sub_epics:
                return True
                
            tasks_content = ""
            for sub_epic in sorted(sub_epics):
                sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
                if not os.path.isdir(sub_epic_dir):
                    continue
                md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
                
                for md_file in sorted(md_files):
                     task_id = f"{sub_epic}/{md_file}"
                     tasks_content += f"### Task ID: {task_id}\n"
                     with open(os.path.join(sub_epic_dir, md_file), "r", encoding="utf-8") as f:
                          content = f.read()
                          # Indent content slightly so it's readable
                          tasks_content += "\n".join([f"    {line}" for line in content.split("\n")]) + "\n\n"
            
            if not tasks_content:
                return True

            print(f"   -> Reviewing tasks for {phase_id}...")

            before_count = ctx.count_task_files(phase_dir_path)

            prompt = ctx.format_prompt(
                review_prompt_tmpl,
                phase_id=phase_id,
                phase_filename=f"{phase_id}.md",
                description_ctx=ctx.description_ctx,
                tasks_content=tasks_content
            )

            ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/{phase_id}/\n!/docs/plan/phases/{phase_id}.md\n"
            allowed_files = [phase_dir_path + os.sep]

            for attempt in range(1, 4):
                result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)

                if result.returncode == 0 and os.path.exists(review_summary_path):
                    after_count = ctx.count_task_files(phase_dir_path)
                    if after_count > before_count:
                        print(f"\n[!] WARNING: Review of {phase_id} increased task count from {before_count} to {after_count}. Review should be subtractive.")
                    return True
                    
                print(f"\n[!] Error reviewing tasks for {phase_id} (Attempt {attempt}/3).")
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
                elif not os.path.exists(review_summary_path):
                    print(f"\n[!] Error: Agent failed to generate review summary {review_summary_path}.")
                    
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as executor:
            futures = [
                executor.submit(process_phase_review, phase_id)
                for phase_id in sorted(phase_dirs)
            ]
            
            for future in concurrent.futures.as_completed(futures):
                if not future.result():
                    print("\n[!] Error encountered in parallel task review. Exiting.")
                    os._exit(1)

        ctx.stage_changes([tasks_dir])
        ctx.state["tasks_reviewed"] = True
        ctx.save_state()
        print("Successfully reviewed tasks.")

class Phase6CCrossPhaseReview(BasePhase):
    def __init__(self, pass_num: int = 1):
        self.pass_num = pass_num

    def execute(self, ctx: ProjectContext):
        state_key = f"cross_phase_reviewed_pass_{self.pass_num}"
        if ctx.state.get(state_key, False):
            print(f"Cross-phase task review (Pass {self.pass_num}) already completed.")
            return

        print(f"\n=> [Phase 6C: Cross-Phase Review (Pass {self.pass_num})] Reviewing tasks across all phases for global duplication and coverage...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        review_prompt_tmpl = ctx.load_prompt("cross_phase_review.md")
        review_summary_path = os.path.join(tasks_dir, f"cross_phase_review_summary_pass_{self.pass_num}.md")
        
        if os.path.exists(review_summary_path):
             print(f"   -> Skipping Cross-Phase Task Review (already reviewed).")
             ctx.state[state_key] = True
             ctx.save_state()
             return

        # Gather ALL tasks
        tasks_content = ""
        for phase_id in sorted(phase_dirs):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            for sub_epic in sorted(sub_epics):
                sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
                if not os.path.isdir(sub_epic_dir):
                    continue
                md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
                
                for md_file in sorted(md_files):
                     task_id = f"{phase_id}/{sub_epic}/{md_file}"
                     tasks_content += f"### Task ID: {task_id}\n"
                     with open(os.path.join(sub_epic_dir, md_file), "r", encoding="utf-8") as f:
                          content = f.read()
                          tasks_content += "\n".join([f"    {line}" for line in content.split("\n")]) + "\n\n"

        if not tasks_content:
            return

        print(f"   -> Performing global cross-phase review...")

        before_count = ctx.count_task_files(tasks_dir)

        prompt = ctx.format_prompt(
            review_prompt_tmpl,
            description_ctx=ctx.description_ctx,
            tasks_content=tasks_content
        )

        ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/\n"
        allowed_files = [tasks_dir + os.sep]

        for attempt in range(1, 4):
            result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)

            if result.returncode == 0 and os.path.exists(review_summary_path):
                after_count = ctx.count_task_files(tasks_dir)
                if after_count > before_count:
                    print(f"\n[!] WARNING: Cross-phase review increased task count from {before_count} to {after_count}. Review should be subtractive.")
                ctx.stage_changes([tasks_dir])
                ctx.state[state_key] = True
                ctx.save_state()
                print("Successfully completed cross-phase review.")
                return
                
            print(f"\n[!] Error reviewing tasks across phases (Attempt {attempt}/3).")
            if result.returncode != 0:
                print(result.stdout)
                print(result.stderr)
            elif not os.path.exists(review_summary_path):
                print(f"\n[!] Error: Agent failed to generate review summary {review_summary_path}.")
                
        sys.exit(1)


class Phase6DReorderTasks(BasePhase):
    def __init__(self, pass_num: int = 1):
        self.pass_num = pass_num

    def execute(self, ctx: ProjectContext):
        state_key = f"tasks_reordered_pass_{self.pass_num}"
        if ctx.state.get(state_key, False):
            print(f"Task reordering across phases (Pass {self.pass_num}) already completed.")
            return

        print(f"\n=> [Phase 6D: Task Reordering (Pass {self.pass_num})] Reordering tasks across all phases for logical implementation progression...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        reorder_prompt_tmpl = ctx.load_prompt("reorder_tasks.md")
        reorder_summary_path = os.path.join(tasks_dir, f"reorder_tasks_summary_pass_{self.pass_num}.md")
        
        if os.path.exists(reorder_summary_path):
             print(f"   -> Skipping Task Reordering (already reordered).")
             ctx.state[state_key] = True
             ctx.save_state()
             return

        # Gather ALL tasks
        tasks_content = ""
        for phase_id in sorted(phase_dirs):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            for sub_epic in sorted(sub_epics):
                sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
                if not os.path.isdir(sub_epic_dir):
                    continue
                md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
                
                for md_file in sorted(md_files):
                     task_id = f"{phase_id}/{sub_epic}/{md_file}"
                     tasks_content += f"### Task ID: {task_id}\n"
                     with open(os.path.join(sub_epic_dir, md_file), "r", encoding="utf-8") as f:
                          f_content = f.read()
                          tasks_content += "\n".join([f"    {line}" for line in f_content.split("\n")]) + "\n\n"

        if not tasks_content:
            return

        print(f"   -> Performing global task reordering...")
        
        prompt = ctx.format_prompt(
            reorder_prompt_tmpl,
            description_ctx=ctx.description_ctx,
            tasks_content=tasks_content
        )

        ignore_content = f"/*\n!/.sandbox/\n!/docs/plan/tasks/\n"
        allowed_files = [tasks_dir + os.sep]
        
        for attempt in range(1, 4):
            result = ctx.run_gemini(prompt, ignore_content, allowed_files=allowed_files, sandbox=False)
            
            if result.returncode == 0 and os.path.exists(reorder_summary_path):
                ctx.stage_changes([tasks_dir])
                ctx.state[state_key] = True
                ctx.save_state()
                print("Successfully completed task reordering.")
                return
                
            print(f"\n[!] Error reordering tasks (Attempt {attempt}/3).")
            if result.returncode != 0:
                print(result.stdout)
                print(result.stderr)
            elif not os.path.exists(reorder_summary_path):
                print(f"\n[!] Error: Agent failed to generate reorder summary {reorder_summary_path}.")
                
        sys.exit(1)

class Phase7ADAGGeneration(BasePhase):
    """Hybrid DAG generation: programmatic from task metadata + AI fallback."""

    @staticmethod
    def _parse_depends_on(content: str) -> Optional[List[str]]:
        """Extract depends_on list from task markdown content.

        Returns list of dependency task filenames, or None if no depends_on field found.
        """
        match = re.search(r'- depends_on:\s*\[([^\]]*)\]', content, re.IGNORECASE)
        if not match:
            return None
        raw = match.group(1).strip()
        if not raw or raw.lower() == '"none"' or raw.lower() == 'none':
            return []
        # Parse comma-separated values, stripping quotes and whitespace
        deps = [d.strip().strip('"').strip("'") for d in raw.split(',')]
        return [d for d in deps if d and d.lower() != 'none']

    @staticmethod
    def _parse_shared_components(content: str) -> List[str]:
        """Extract shared_components list from task markdown content."""
        match = re.search(r'- shared_components:\s*\[([^\]]*)\]', content, re.IGNORECASE)
        if not match:
            return []
        raw = match.group(1).strip()
        if not raw or raw.lower() == 'none':
            return []
        components = [c.strip().strip('"').strip("'") for c in raw.split(',')]
        return [c for c in components if c and c.lower() != 'none']

    @staticmethod
    def _build_programmatic_dag(phase_dir_path: str) -> Optional[Dict[str, List[str]]]:
        """Build DAG from depends_on metadata in task files.

        Returns the DAG dict if all tasks have depends_on metadata,
        or None if any task is missing it (requiring AI fallback).
        """
        dag = {}
        task_files = {}  # filename -> sub_epic/filename mapping
        all_have_metadata = True

        sub_epics = [d for d in os.listdir(phase_dir_path)
                     if os.path.isdir(os.path.join(phase_dir_path, d))]

        for sub_epic in sorted(sub_epics):
            sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
            md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]

            for md_file in sorted(md_files):
                task_id = f"{sub_epic}/{md_file}"
                filepath = os.path.join(sub_epic_dir, md_file)
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()

                task_files[md_file] = task_id

                deps = Phase7ADAGGeneration._parse_depends_on(content)
                if deps is None:
                    all_have_metadata = False
                    continue

                # Resolve dependency filenames to full task_ids
                resolved_deps = []
                for dep in deps:
                    # dep might be just a filename or sub_epic/filename
                    if '/' in dep:
                        resolved_deps.append(dep)
                    elif dep in task_files:
                        resolved_deps.append(task_files[dep])
                    else:
                        # Try to find it in any sub_epic
                        for se in sorted(sub_epics):
                            candidate = f"{se}/{dep}"
                            candidate_path = os.path.join(phase_dir_path, se, dep)
                            if os.path.exists(candidate_path):
                                resolved_deps.append(candidate)
                                break

                dag[task_id] = resolved_deps

        if not all_have_metadata:
            return None

        # Add shared component dependencies: if task A creates component X
        # and task B consumes component X, B depends on A
        component_creators = {}  # component_name -> task_id
        component_consumers = {}  # component_name -> [task_ids]

        for sub_epic in sorted(sub_epics):
            sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
            md_files = [f for f in os.listdir(sub_epic_dir) if f.endswith(".md")]
            for md_file in sorted(md_files):
                task_id = f"{sub_epic}/{md_file}"
                filepath = os.path.join(sub_epic_dir, md_file)
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                components = Phase7ADAGGeneration._parse_shared_components(content)
                for comp in components:
                    # Heuristic: first task to reference a component is the creator
                    if comp not in component_creators:
                        component_creators[comp] = task_id
                    else:
                        component_consumers.setdefault(comp, []).append(task_id)

        # Add implicit dependencies from shared components
        for comp, consumers in component_consumers.items():
            creator = component_creators.get(comp)
            if creator:
                for consumer in consumers:
                    if consumer in dag and creator not in dag[consumer]:
                        dag[consumer].append(creator)

        return dag

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

        def process_phase_dag(phase_id):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            dag_file_path = os.path.join(phase_dir_path, "dag.json")

            if os.path.exists(dag_file_path):
                 print(f"   -> Skipping DAG Generation for {phase_id} (already exists).")
                 return True

            # Try programmatic DAG first
            programmatic_dag = self._build_programmatic_dag(phase_dir_path)
            if programmatic_dag is not None:
                print(f"   -> Built DAG programmatically for {phase_id} from task metadata ({len(programmatic_dag)} tasks).")
                with open(dag_file_path, "w", encoding="utf-8") as f:
                    json.dump(programmatic_dag, f, indent=2)
                return True

            # Fall back to AI inference
            print(f"   -> Some tasks in {phase_id} lack depends_on metadata. Falling back to AI DAG inference...")

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
                          tasks_content += "\n".join([f"    {line}" for line in content.split("\n")]) + "\n\n"

            prompt = ctx.format_prompt(
                dag_prompt_tmpl,
                phase_filename=phase_id,
                target_path=f"docs/plan/tasks/{phase_id}/dag.json",
                description_ctx=ctx.description_ctx,
                tasks_content=tasks_content
            )

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

