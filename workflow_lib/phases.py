"""Planning workflow phase implementations.

Each class in this module represents one discrete step in the multi-phase
planning pipeline.  All phases inherit from :class:`BasePhase` and implement
a single :meth:`~BasePhase.execute` method that receives the shared
:class:`~workflow_lib.context.ProjectContext` instance.

Phase catalogue
---------------

+-------------------+------------------------------------------------------+
| Class             | Purpose                                              |
+===================+======================================================+
| Phase1GenerateDoc | Initial AI generation of a planning document.        |
+-------------------+------------------------------------------------------+
| Phase2FleshOutDoc | Section-by-section expansion of a generated spec.   |
+-------------------+------------------------------------------------------+
| Phase3FinalReview | Holistic consistency review of all documents.        |
+-------------------+------------------------------------------------------+
| Phase3BAdversarialReview | Scope-creep / gaps review against the original |
|                          | project description.                            |
+-------------------+------------------------------------------------------+
| Phase4AExtractRequirements | Extract structured requirements from a doc.  |
+-------------------+------------------------------------------------------+
| Phase4BMergeRequirements  | Consolidate per-doc requirements into master. |
+-------------------+------------------------------------------------------+
| Phase4BScopeGate  | Human checkpoint to review requirements scope.       |
+-------------------+------------------------------------------------------+
| Phase4COrderRequirements  | Sequence and prioritise requirements.         |
+-------------------+------------------------------------------------------+
| Phase5GenerateEpics       | Generate implementation epics/phases.         |
+-------------------+------------------------------------------------------+
| Phase5BSharedComponents   | Identify shared modules and ownership.        |
+-------------------+------------------------------------------------------+
| Phase6BreakDownTasks      | Decompose epics into atomic task files.       |
+-------------------+------------------------------------------------------+
| Phase6BReviewTasks        | Review tasks within each phase for coverage. |
+-------------------+------------------------------------------------------+
| Phase6CCrossPhaseReview   | Global duplicate/coverage review (2 passes). |
+-------------------+------------------------------------------------------+
| Phase6DReorderTasks       | Reorder tasks for logical progression.        |
+-------------------+------------------------------------------------------+
| Phase7ADAGGeneration      | Build per-phase dependency DAGs.              |
+-------------------+------------------------------------------------------+
"""

import concurrent.futures
import os
import shutil
import subprocess
import sys
import json
import re
import threading
from typing import List, Dict, Any, Optional

from .constants import TOOLS_DIR, DOCS, parse_requirements
from .context import ProjectContext


class BasePhase:
    """Abstract base class for all planning phases.

    Subclasses must override :meth:`execute`.  The
    :class:`~workflow_lib.orchestrator.Orchestrator` calls ``execute`` and
    wraps it with retry logic so that individual phases do not need to handle
    transient failures themselves.
    """

    @property
    def operation(self) -> str:
        """Short operation label shown in the dashboard Command column.

        Derives a readable label from the class name by stripping the leading
        ``Phase\\d+[A-Z]?`` prefix and splitting CamelCase into words.
        Override to provide a custom label.
        """
        import re
        name = self.__class__.__name__
        # Strip "PhaseNX" prefix where X is a sub-letter (e.g. "Phase6C" in
        # "Phase6CCrossPhaseReview") — only strip the letter when it's followed
        # by another uppercase (meaning it's an abbreviation, not a word start).
        name = re.sub(r"^Phase\d+(?:[A-Z](?=[A-Z]))?", "", name) or name
        # Split CamelCase into words (e.g. "FinalReview" → "Final Review")
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
        return name

    @property
    def display_name(self) -> str:
        """Human-readable name used in dashboard status rows.

        Defaults to the class name.  Subclasses that operate on a specific
        document or phase should override this to include the target name.
        """
        return self.__class__.__name__

    def execute(self, ctx: ProjectContext) -> None:
        """Execute the phase using the shared project context.

        :param ctx: The :class:`~workflow_lib.context.ProjectContext` instance
            providing filesystem paths, AI runner access, and persisted state.
        :type ctx: ProjectContext
        :raises NotImplementedError: Always, unless overridden by a subclass.
        """
        raise NotImplementedError()

class Phase1GenerateDoc(BasePhase):
    """Generate the initial draft of a single planning document.

    Idempotent: skipped when the document's ID already appears in
    ``ctx.state["generated"]``.

    :param doc: Document descriptor dict from :data:`~workflow_lib.constants.DOCS`
        with keys ``id``, ``type``, ``name``, ``desc``, and ``prompt_file``.
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        """Initialise the phase for a specific document.

        :param doc: Document descriptor.
        :type doc: dict
        """
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Generate"

    @property
    def display_name(self) -> str:
        return f"Phase1: {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        """Generate the document using the document-specific prompt template.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: When the AI runner fails or the expected output
            file is not produced.
        """
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
        
        print(f"\n=> [Phase 1: Generate] {self.doc['name']} into docs/plan/{out_folder}/{self.doc['id']}.md ...")
        
        allowed_files = [expected_file]
        result = ctx.run_gemini(full_prompt, allowed_files=allowed_files)
        
        if result.returncode != 0 or not os.path.exists(expected_file):
            print(f"\n[!] Error generating {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
            
        # Save canonical headers before any flesh-out passes can modify the doc
        if self.doc["type"] == "spec":
            ctx.save_headers(self.doc, expected_file)
            allowed_files.append(ctx.get_headers_path(self.doc))

        ctx.stage_changes(allowed_files)
        ctx.state.setdefault("generated", []).append(self.doc["id"])
        ctx.save_state()

class Phase2FleshOutDoc(BasePhase):
    """Expand each section of a spec document with additional AI passes.

    Only runs for spec-type documents (skipped for research).  Each markdown
    header in the document gets its own focused AI pass.  Sections that have
    already been expanded are skipped via ``ctx.state["fleshed_out_headers"]``.

    :param doc: Document descriptor dict (same structure as
        :class:`Phase1GenerateDoc`).
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        """Initialise the phase for a specific document.

        :param doc: Document descriptor.
        :type doc: dict
        """
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Flesh Out"

    @property
    def display_name(self) -> str:
        return f"Phase2: {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        """Iterate over document sections and expand each with an AI pass.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure for any section.
        """
        if self.doc["type"] != "spec":
            return
            
        if self.doc["id"] in ctx.state.get("fleshed_out", []):
            print(f"Skipping fleshing out for {self.doc['name']} (already fleshed out).")
            return
            
        expected_file = ctx.get_document_path(self.doc)
        target_path = ctx.get_target_path(self.doc)
        out_folder = "specs"
        accumulated_context = ctx.get_accumulated_context(self.doc, include_research=False)

        headers = ctx.parse_markdown_headers(expected_file, doc=self.doc)
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
            allowed_files = [expected_file]
            result = ctx.run_gemini(flesh_prompt, allowed_files=allowed_files)
            
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


class Phase2BSummarizeDoc(BasePhase):
    """Generate a condensed summary of a planning document for use as context.

    Runs after Phase 2 (flesh out) for each document.  The summary preserves
    key decisions, identifiers, and architectural details while reducing size
    to ~15-25% of the original, keeping subsequent prompts within model input
    limits.

    :param doc: Document descriptor dict.
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Summarize"

    @property
    def display_name(self) -> str:
        return f"Phase2B: Summarize {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        """Generate a summary of the document for accumulated context use.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure.
        """
        if self.doc["id"] in ctx.state.get("summarized", []):
            print(f"Skipping summarization for {self.doc['name']} (already summarized).")
            return

        source_file = ctx.get_document_path(self.doc)
        if not os.path.exists(source_file):
            print(f"Skipping summarization for {self.doc['name']} (source not found).")
            return

        with open(source_file, "r", encoding="utf-8") as f:
            document_content = f.read()

        summary_path = ctx.get_summary_target_path(self.doc)
        summary_abs = ctx.get_summary_path(self.doc)

        print(f"   -> [Phase 2B: Summarize] {self.doc['name']} into {summary_path} ...")
        prompt_tmpl = ctx.load_prompt("summarize_doc.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            document_name=self.doc["name"],
            document_content=document_content,
            summary_path=summary_path,
        )

        allowed_files = [summary_abs]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print(f"\n[!] Error summarizing {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state.setdefault("summarized", []).append(self.doc["id"])
        ctx.save_state()


class Phase3FinalReview(BasePhase):
    """Holistic alignment review of all planning documents.

    Checks all generated specs and research documents for consistency with
    the original project description.  Idempotent via
    ``ctx.state["final_review_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Run the final alignment review across all documents.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure.
        """
        if ctx.state.get("final_review_completed", False):
            print("Final alignment review already completed.")
            return
            
        print("\n=> [Phase 3: Final Alignment Review] Reviewing all documents for consistency...")
        final_prompt_tmpl = ctx.load_prompt("final_review.md")
        final_prompt = ctx.format_prompt(final_prompt_tmpl, description_ctx=ctx.description_ctx)
        
        # Final review can modify all existing specs and research files
        allowed_files = [ctx.get_document_path(d) for d in DOCS]
        result = ctx.run_gemini(final_prompt, allowed_files=allowed_files)
        
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
    """Devil's advocate review comparing specs against the original description.

    Automatically removes scope creep from spec/research documents and
    produces ``docs/plan/adversarial_review.md`` logging all changes.
    Prompts the user to review NEEDS CLARIFICATION items before continuing.
    Idempotent via ``ctx.state["adversarial_review_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Run the adversarial review and optionally open the editor on findings.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure or when the user chooses to
            quit (``'q'``).
        """
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

        specs_dir = os.path.join(ctx.plan_dir, "specs") + os.sep
        research_dir = os.path.join(ctx.plan_dir, "research") + os.sep
        allowed_files = [expected_file, specs_dir, research_dir]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files, sandbox=False)

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
            action = ctx.prompt_input(
                f"Adversarial review found {scope_creep_count} SCOPE CREEP (auto-removed) and "
                f"{needs_clarification_count} NEEDS CLARIFICATION issues.\n"
                f"  Review: {target_path}\n\n"
                f"  SCOPE CREEP items have been automatically removed from spec documents.\n"
                f"  NEEDS CLARIFICATION items require your attention:\n"
                f"    - Update input/project-description.md to clarify intent, or\n"
                f"    - Edit the specs (docs/plan/specs/*.md) to resolve ambiguity.\n\n"
                f"  [c]ontinue / [e]dit review / [q]uit"
            ).strip().lower()
            if action == 'q':
                sys.exit(0)
            elif action == 'e':
                editor = os.environ.get("EDITOR", "vim")
                subprocess.run([editor, expected_file])

        print("Adversarial review complete.")


class Phase4AExtractRequirements(BasePhase):
    """Extract structured requirements from a single planning document.

    Produces ``docs/plan/requirements/<doc_id>.md``.  Skipped for research
    documents and for docs already recorded in
    ``ctx.state["extracted_requirements"]``.  Runs automated verification
    via ``verify_requirements.py --verify-doc`` after each extraction.

    :param doc: Document descriptor dict.
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        """Initialise the phase for a specific document.

        :param doc: Document descriptor.
        :type doc: dict
        """
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Extract Reqs"

    @property
    def display_name(self) -> str:
        return f"Phase4A: {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        """Extract requirements and verify coverage against the source document.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure or verification failure.
        """
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
        
        allowed_files = [expected_file, doc_path]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)
        
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
    """Consolidate all per-document requirement files into ``requirements.md``.

    Runs ``verify_requirements.py --verify-master`` after merging.  Idempotent
    via ``ctx.state["requirements_merged"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Merge per-doc requirement files and verify the master list.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner or verification failure.
        """
        if ctx.state.get("requirements_merged", False):
            print("Requirements merging already completed.")
            return
            
        print("\n=> [Phase 4B: Merge and Resolve Conflicts] Consolidating all requirements...")
        prompt_tmpl = ctx.load_prompt("merge_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)
        
        # This phase can modify requirements.md AND any source doc in docs/plan/specs/
        
        # Allowed files include the final requirements.md and specs for potential conflict resolution
        allowed_files = [os.path.join(ctx.root_dir, "requirements.md")]
        allowed_files.extend([ctx.get_document_path(d) for d in DOCS if d["type"] != "research"])
        
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)
        
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
    """Human checkpoint to review the requirements scope before proceeding.

    Displays a summary of unique requirement count and line count, then
    prompts the user to continue (``c``), edit ``requirements.md`` (``e``),
    or abort (``q``).  Idempotent via ``ctx.state["scope_gate_passed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Present the scope gate prompt and wait for user confirmation.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: When the user chooses to quit.
        """
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

        summary = (
            f"SCOPE GATE — Human Review Required\n"
            f"  Total unique requirements: {len(unique_reqs)}\n"
            f"  Requirements document: {line_count} lines\n"
            f"  Original description: {len(ctx.description_ctx.splitlines())} lines\n"
            f"  Review 'requirements.md' to check for scope inflation.\n"
            f"  You may edit the file to remove or defer requirements.\n"
            f"  [c]ontinue / [e]dit (opens $EDITOR) / [q]uit"
        )

        while True:
            action = ctx.prompt_input(summary).strip().lower()
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
    """Sequence and prioritise requirements by dependency and implementation order.

    Produces ``ordered_requirements.md``, verifies it with
    ``verify_requirements.py --verify-ordered``, then renames it to
    ``requirements.md``.  Idempotent via ``ctx.state["requirements_ordered"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Generate and verify the ordered requirements document.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner or verification failure.
        """
        if ctx.state.get("requirements_ordered", False):
            print("Requirements ordering already completed.")
            return
            
        print("\n=> [Phase 4C: Order Requirements] Sequencing requirements and capturing dependencies...")
        prompt_tmpl = ctx.load_prompt("order_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)
        
        allowed_files = [os.path.join(ctx.root_dir, "ordered_requirements.md")]
        
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)
        
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
    """Generate the ``docs/plan/phases/`` epic/phase documents.

    Each phase file groups related requirements into an implementation epic.
    Runs ``verify_requirements.py --verify-phases`` after generation.
    Idempotent via ``ctx.state["phases_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Generate phase documents and verify requirement coverage.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner or verification failure.
        """
        if ctx.state.get("phases_completed", False):
            print("Phase generation already completed.")
            return
            
        print("\n=> [Phase 5: Generate Epics] Generating detailed phases/")
        phases_prompt_tmpl = ctx.load_prompt("phases.md")
        phases_prompt = ctx.format_prompt(phases_prompt_tmpl, description_ctx=ctx.description_ctx)
        
        phases_dir = os.path.join(ctx.plan_dir, "phases")
        os.makedirs(phases_dir, exist_ok=True)
        # Adding trailing slash allows creating content inside it
        allowed_files = [phases_dir + os.sep]
        result = ctx.run_gemini(phases_prompt, allowed_files=allowed_files)
        
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
    """Generate a shared components manifest to coordinate parallel agents.

    Produces ``docs/plan/shared_components.md`` listing modules that are owned
    or consumed by multiple implementation tasks.  Idempotent via
    ``ctx.state["shared_components_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Generate the shared components manifest.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure or when the output file is
            not produced.
        """
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

        allowed_files = [expected_file]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

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
    """Decompose each phase epic into atomic task markdown files.

    Uses two AI passes per phase: a grouping pass (Project Manager) that
    clusters requirements into sub-epics, and a task-generation pass (Lead
    Developer) that produces the individual ``<NN>_<name>.md`` task files.
    The task-generation pass runs in parallel across sub-epics up to
    ``ctx.jobs`` workers.

    Runs ``verify_requirements.py --verify-tasks`` at the end.  Idempotent
    via ``ctx.state["tasks_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Generate sub-epic groupings and atomic task files for all phases.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner or verification failure, or when
            required directories do not exist.
        """
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
            group_filepath = os.path.join(tasks_dir, group_filename)
            allowed_files = [group_filepath]
            
            if not os.path.exists(group_filepath):
                group_result = ctx.run_gemini(grouping_prompt, allowed_files=allowed_files, sandbox=False)
            
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
                
                        
                allowed_files = [phase_task_dir + os.sep]
                result = ctx.run_gemini(tasks_prompt, allowed_files=allowed_files, sandbox=False)
                
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

class Phase6AFixupValidation(BasePhase):
    """Run validation checks and automatically fix phase/task mapping gaps.

    After task breakdown (Phase 6), runs ``_run_all_checks()`` to detect
    requirements that are not assigned to any phase or not covered by any
    task.  Fixes each category using AI, then re-validates.  Idempotent via
    ``ctx.state["fixup_validation_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Run fixup validation and fix any failures.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: When fixes fail or validation still fails after fixing.
        """
        if ctx.state.get("fixup_validation_completed", False):
            print("Fixup validation already completed.")
            return

        print("\n=> [Phase 6A-Fixup: Validation Fixup] Running validation and fixing gaps...")

        from .replan import _run_all_checks, _fix_phase_mappings, _fix_task_mappings

        results = _run_all_checks()

        if results["all_pass"]:
            print("All validation checks passed. No fixup needed.")
            ctx.state["fixup_validation_completed"] = True
            ctx.save_state()
            return

        fixed_anything = False

        # Fix verify-phases failures first
        phases_check = results["checks"].get("verify-phases", {})
        if not phases_check.get("passed", True) and phases_check.get("missing_reqs"):
            if _fix_phase_mappings(phases_check["missing_reqs"], ctx):
                fixed_anything = True

        # Fix verify-tasks failures
        tasks_check = results["checks"].get("verify-tasks", {})
        if not tasks_check.get("passed", True) and tasks_check.get("missing_reqs"):
            if _fix_task_mappings(tasks_check["missing_reqs"], ctx):
                fixed_anything = True

        if not fixed_anything:
            print("[!] Validation failures detected but no automatic fixes available.")
            sys.exit(1)

        # Re-verify
        print("\n=> Re-running validation after fixup...")
        final = _run_all_checks()

        if not final["all_pass"]:
            print("[!] Some checks still failing after fixup.")
            sys.exit(1)

        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        ctx.stage_changes([tasks_dir, os.path.join(ctx.plan_dir, "phases")])
        ctx.state["fixup_validation_completed"] = True
        ctx.save_state()
        print("Fixup validation complete — all checks passing.")


class Phase6BReviewTasks(BasePhase):
    """Review tasks within each phase for duplicates and coverage gaps.

    For each phase directory, produces a ``review_summary.md`` file.  The
    review is expected to be *subtractive* — a warning is emitted if the task
    count increases.  Runs phases in parallel up to ``ctx.jobs`` workers.
    Idempotent via ``ctx.state["tasks_reviewed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        """Run per-phase task review in parallel.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure after 3 attempts for any
            phase.
        """
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
            allowed_files = [phase_dir_path + os.sep]

            for attempt in range(1, 4):
                result = ctx.run_gemini(prompt, allowed_files=allowed_files, sandbox=False)

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
    """Global cross-phase duplicate and coverage review (configurable pass number).

    Gathers all tasks across all phases and submits them in a single prompt
    to find global duplication or missing coverage.  Produces
    ``cross_phase_review_summary_pass_<N>.md`` in the tasks directory.
    Idempotent via ``ctx.state["cross_phase_reviewed_pass_<N>"]``.

    :param pass_num: Pass number (1 or 2).  Two passes are run by the
        :class:`~workflow_lib.orchestrator.Orchestrator`.  Defaults to ``1``.
    :type pass_num: int
    """

    def __init__(self, pass_num: int = 1) -> None:
        """Initialise for a specific pass number.

        :param pass_num: Pass number (1-based).
        :type pass_num: int
        """
        self.pass_num = pass_num

    def execute(self, ctx: ProjectContext) -> None:
        """Execute the cross-phase review for this pass.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure after 3 attempts.
        """
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

        summary_filename = f"cross_phase_review_summary_pass_{self.pass_num}.md"
        prompt = ctx.format_prompt(
            review_prompt_tmpl,
            description_ctx=ctx.description_ctx,
            tasks_content=tasks_content,
            summary_filename=summary_filename
        )

        allowed_files = [tasks_dir + os.sep]

        for attempt in range(1, 4):
            result = ctx.run_gemini(prompt, allowed_files=allowed_files, sandbox=False)

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
    """Validate and fix task ordering across all phases by moving misplaced files.

    Gathers all tasks and prompts the AI to validate logical ordering,
    then move misplaced tasks to their correct phase directories.
    Produces ``reorder_tasks_summary_pass_<N>.md``.
    Idempotent via ``ctx.state["tasks_reordered_pass_<N>"]``.

    :param pass_num: Pass number (1 or 2).  Defaults to ``1``.
    :type pass_num: int
    """

    def __init__(self, pass_num: int = 1) -> None:
        """Initialise for a specific pass number.

        :param pass_num: Pass number (1-based).
        :type pass_num: int
        """
        self.pass_num = pass_num

    def execute(self, ctx: ProjectContext) -> None:
        """Execute the task reordering pass.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure after 3 attempts.
        """
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
            tasks_content=tasks_content,
            pass_num=self.pass_num
        )

        allowed_files = [tasks_dir + os.sep]
        
        for attempt in range(1, 4):
            result = ctx.run_gemini(prompt, allowed_files=allowed_files, sandbox=False)
            
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
    """Hybrid DAG generation: programmatic from task metadata with AI fallback.

    For each phase directory, attempts to build the dependency DAG by parsing
    ``depends_on`` and ``shared_components`` metadata fields directly from task
    files (no AI needed).  Falls back to an AI inference pass for phases where
    any task is missing the metadata.

    Produces ``dag.json`` in each phase directory.  Idempotent via
    ``ctx.state["dag_completed"]``.
    """

    @staticmethod
    def _parse_depends_on(content: str) -> Optional[List[str]]:
        """Extract the ``depends_on`` list from task markdown front-matter.

        Looks for a line matching ``- depends_on: [...]`` in *content*.

        :param content: Full text of a task markdown file.
        :type content: str
        :returns: List of dependency filenames, an empty list when the field
            is present but empty/``none``, or ``None`` when the field is absent
            entirely (indicating AI fallback is required).
        :rtype: Optional[List[str]]
        """
        # Match both bracketed `- depends_on: [...]` and bare `- depends_on: none`
        match = re.search(r'- depends_on:\s*\[([^\]]*)\]', content, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()
            if not raw or raw.lower() == '"none"' or raw.lower() == 'none':
                return []
            # Parse comma-separated values, stripping quotes and whitespace
            deps = [d.strip().strip('"').strip("'") for d in raw.split(',')]
            return [d for d in deps if d and d.lower() != 'none']
        # Handle bare value without brackets (e.g. `- depends_on: none`)
        bare_match = re.search(r'- depends_on:\s*(\S+)', content, re.IGNORECASE)
        if bare_match:
            raw = bare_match.group(1).strip().strip('"').strip("'")
            if raw.lower() == 'none':
                return []
            return [raw]
        return None

    @staticmethod
    def _parse_shared_components(content: str) -> List[str]:
        """Extract the ``shared_components`` list from task markdown front-matter.

        :param content: Full text of a task markdown file.
        :type content: str
        :returns: List of shared component name strings, or an empty list when
            the field is absent or empty.
        :rtype: List[str]
        """
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
        """Build a DAG from ``depends_on`` and ``shared_components`` task metadata.

        Scans every ``.md`` file under *phase_dir_path*, parses their
        ``depends_on`` fields, and augments the result with implicit
        shared-component dependencies (the first task to reference a component
        is treated as its creator; all later tasks that reference the same
        component implicitly depend on it).

        :param phase_dir_path: Absolute path to a phase task directory, e.g.
            ``docs/plan/tasks/phase_1/``.
        :type phase_dir_path: str
        :returns: ``{task_id: [prerequisite_task_ids]}`` mapping when all
            tasks have ``depends_on`` metadata, or ``None`` when any task is
            missing the field (triggering AI fallback).
        :rtype: Optional[Dict[str, List[str]]]
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

                # Filter out self-references
                dag[task_id] = [d for d in resolved_deps if d != task_id]

        if not all_have_metadata:
            return None

        # Add shared component dependencies: if task A creates component X
        # and task B consumes component X, B depends on A
        component_creators = {}  # component_name -> task_id
        component_consumers: Dict[str, List[str]] = {}  # component_name -> [task_ids]

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

    @staticmethod
    def _validate_dag(phase_dir_path: str, dag: Dict[str, List[str]]) -> List[str]:
        """Validate that a DAG matches the files on disk.

        Returns a list of error messages (empty if valid).  Checks that:

        1. Every DAG key corresponds to a ``.md`` file on disk.
        2. Every ``.md`` task file on disk is a key in the DAG.

        :param phase_dir_path: Absolute path to the phase task directory.
        :param dag: The DAG mapping ``{task_id: [deps]}``.
        :returns: List of error strings (empty means valid).
        """
        errors = []
        # Collect all .md files on disk (excluding review/summary files)
        on_disk = set()
        for sub_epic in sorted(os.listdir(phase_dir_path)):
            se_path = os.path.join(phase_dir_path, sub_epic)
            if not os.path.isdir(se_path):
                continue
            for md in sorted(os.listdir(se_path)):
                if md.endswith(".md"):
                    on_disk.add(f"{sub_epic}/{md}")

        dag_keys = set(dag.keys())

        # DAG references files that don't exist
        phantom = dag_keys - on_disk
        for p in sorted(phantom):
            errors.append(f"DAG references non-existent file: {p}")

        # Files on disk not in DAG
        orphans = on_disk - dag_keys
        for o in sorted(orphans):
            errors.append(f"File on disk not in DAG: {o}")

        return errors

    def execute(self, ctx: ProjectContext) -> None:
        """Generate per-phase DAGs, using programmatic build with AI fallback.

        Phase DAGs are generated in parallel (up to ``ctx.jobs`` workers).

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure after 3 attempts for any
            phase, or when required directories are missing.
        """
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
                errors = self._validate_dag(phase_dir_path, programmatic_dag)
                if errors:
                    print(f"\n[!] WARNING: Programmatic DAG for {phase_id} has {len(errors)} consistency issues:")
                    for e in errors:
                        print(f"      - {e}")
                    print(f"   -> Falling back to AI DAG inference for {phase_id}...")
                else:
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
            allowed_files = [dag_file_path]

            for attempt in range(1, 4):
                result = ctx.run_gemini(prompt, allowed_files=allowed_files, sandbox=False)

                if result.returncode == 0 and os.path.exists(dag_file_path):
                    # Validate AI-generated DAG against disk
                    try:
                        with open(dag_file_path, "r", encoding="utf-8") as f:
                            ai_dag = json.load(f)
                        errors = self._validate_dag(phase_dir_path, ai_dag)
                        if errors:
                            print(f"\n[!] WARNING: AI-generated DAG for {phase_id} has {len(errors)} consistency issues (attempt {attempt}/3):")
                            for e in errors[:10]:
                                print(f"      - {e}")
                            if len(errors) > 10:
                                print(f"      ... and {len(errors) - 10} more")
                            os.remove(dag_file_path)
                            continue
                    except (json.JSONDecodeError, OSError) as exc:
                        print(f"\n[!] Invalid DAG JSON for {phase_id} (attempt {attempt}/3): {exc}")
                        os.remove(dag_file_path)
                        continue
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


class Phase3AConflictResolution(BasePhase):
    """Systematic conflict resolution between planning documents.

    Compares all spec and research documents for contradictions and resolves
    them using a defined priority hierarchy.  Produces
    ``docs/plan/conflict_resolution.md``.  Idempotent via
    ``ctx.state["conflict_resolution_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("conflict_resolution_completed", False):
            print("Conflict resolution review already completed.")
            return

        print("\n=> [Phase 3A: Conflict Resolution] Resolving contradictions between documents...")
        target_path = "docs/plan/conflict_resolution.md"
        expected_file = os.path.join(ctx.plan_dir, "conflict_resolution.md")

        prompt_tmpl = ctx.load_prompt("conflict_resolution_review.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path
        )

        allowed_files = [expected_file]
        allowed_files.extend([ctx.get_document_path(d) for d in DOCS])
        result = ctx.run_ai(prompt, allowed_files=allowed_files, sandbox=False)

        if result.returncode != 0 or not os.path.exists(expected_file):
            print("\n[!] Error during conflict resolution review.")
            if result.returncode != 0:
                print(result.stdout)
                print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["conflict_resolution_completed"] = True
        ctx.save_state()
        print("Successfully completed conflict resolution review.")


class Phase5CInterfaceContracts(BasePhase):
    """Generate interface contracts for shared components and cross-phase boundaries.

    Produces ``docs/plan/interface_contracts.md``.  Idempotent via
    ``ctx.state["interface_contracts_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("interface_contracts_completed", False):
            print("Interface contracts already generated.")
            return

        print("\n=> [Phase 5C: Interface Contracts] Defining API contracts for shared components...")
        target_path = "docs/plan/interface_contracts.md"
        expected_file = os.path.join(ctx.plan_dir, "interface_contracts.md")

        prompt_tmpl = ctx.load_prompt("interface_contracts.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path
        )

        allowed_files = [expected_file]
        result = ctx.run_ai(prompt, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(expected_file):
            print("\n[!] Error generating interface contracts.")
            if result.returncode != 0:
                print(result.stdout)
                print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["interface_contracts_completed"] = True
        ctx.save_state()
        print("Successfully generated interface contracts.")


class Phase6EIntegrationTestPlan(BasePhase):
    """Generate integration test plan for cross-task boundaries.

    Produces ``docs/plan/integration_test_plan.md``.  Idempotent via
    ``ctx.state["integration_test_plan_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("integration_test_plan_completed", False):
            print("Integration test plan already generated.")
            return

        print("\n=> [Phase 6E: Integration Test Plan] Defining cross-task integration tests...")
        target_path = "docs/plan/integration_test_plan.md"
        expected_file = os.path.join(ctx.plan_dir, "integration_test_plan.md")

        prompt_tmpl = ctx.load_prompt("integration_test_plan.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path
        )

        allowed_files = [expected_file]
        result = ctx.run_ai(prompt, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(expected_file):
            print("\n[!] Error generating integration test plan.")
            if result.returncode != 0:
                print(result.stdout)
                print(result.stderr)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["integration_test_plan_completed"] = True
        ctx.save_state()
        print("Successfully generated integration test plan.")

