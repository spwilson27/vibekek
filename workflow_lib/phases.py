"""Planning workflow phase implementations.

Each class in this module represents one discrete step in the multi-phase
planning pipeline.  All phases inherit from :class:`BasePhase` and implement
a single :meth:`~BasePhase.execute` method that receives the shared
:class:`~workflow_lib.context.ProjectContext` instance.

Phase catalogue (sequential numbering)
---------------------------------------

Phase 1:  GenerateDoc — Sequential generation of research + spec documents.
Phase 2:  FleshOutDoc — Section-by-section expansion (parallel).
Phase 3:  SummarizeDoc — Compact summaries for context carriage (parallel, specs only).
Phase 4:  FinalReview — Holistic consistency review.
Phase 5:  ConflictResolution — Resolve contradictions between specs.
Phase 6:  AdversarialReview — Scope-creep / gaps review.
Phase 7:  ExtractRequirements — Per-doc JSON requirement extraction (parallel, specs only).
Phase 8:  FilterMetaRequirements — Remove process/meta requirements (parallel).
Phase 9:  MergeRequirements — Consolidate into requirements.json.
Phase 10: DeduplicateRequirements — Remove duplicates.
Phase 11: OrderRequirements — E2E-first ordering.
Phase 12: GenerateEpics — JSON epic/requirement mappings.
Phase 13: E2EInterfaces — Public interface definitions per phase.
Phase 14: FeatureGates — File-based feature gate definitions.
Phase 15: HolisticTasks — Holistic task breakdown with JSON sidecars (parallel).
Phase 16: ReviewHolisticTasks — Per-phase task review (parallel).
Phase 17: CrossPhaseReview — Global duplicate/coverage review.
Phase 18: PreInitTask — Bootstrap task (Dockerfile, gates).
Phase 19: DAGGeneration — Per-phase dependency DAGs.
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

# ANSI color codes for terminal output
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

from .constants import TOOLS_DIR, DOCS
from .context import ProjectContext, _count_tokens

# Canonical set of non-task markdown files to exclude from DAG generation,
# task counting, dependency validation, etc.
_NON_TASK_FILES = {
    "README.md",
    "SUB_EPIC_SUMMARY.md",
    "REQUIREMENTS_TRACEABILITY.md",
    "REQUIREMENTS_COVERAGE_MAP.md",
    "REQUIREMENTS_COVERAGE.md",
    "REQUIREMENTS_MATRIX.md",
    "IMPLEMENTATION_SUMMARY.md",
    "review_summary.md",
    "cross_phase_review_summary.md",
    "cross_phase_review_summary_pass_1.md",
    "cross_phase_review_summary_pass_2.md",
    "reorder_tasks_summary.md",
    "reorder_tasks_summary_pass_1.md",
    "reorder_tasks_summary_pass_2.md",
    "00_index.md",
}


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

        base_prompt_template = ctx.load_prompt(self.doc["prompt_file"])
        extra = _count_tokens(base_prompt_template) + _count_tokens(ctx.description_ctx) + 40
        accumulated_context = ctx.get_accumulated_context(self.doc, extra_tokens=extra)

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
            f"4. CRITICAL: You MUST use the `write_file` tool to save the document to '{target_path}'. Do NOT just output the content as text.\n"
            f"5. After calling `write_file`, end your turn immediately. Do not add any additional commentary.\n"
        )
        
        print(f"\n=> [Phase 1: Generate] {self.doc['name']} into docs/plan/specs/{self.doc['id']}.md ...")
        
        allowed_files = [expected_file]
        result = ctx.run_gemini(full_prompt, allowed_files=allowed_files)
        
        if result.returncode != 0 or not os.path.exists(expected_file):
            print(f"\n[!] Error generating {self.doc['name']}.")
            print(result.stdout)
            print(result.stderr)
            sys.exit(1)
            
        # Save canonical headers before any flesh-out passes can modify the doc
        ctx.save_headers(self.doc, expected_file)
        allowed_files.append(ctx.get_headers_path(self.doc))

        ctx.stage_changes(allowed_files)
        ctx.state.setdefault("generated", []).append(self.doc["id"])
        ctx.save_state()

class Phase2FleshOutDoc(BasePhase):
    """Expand each section of a spec document with additional AI passes.

    Each markdown
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
        return f"Phase2: Flesh Out {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        """Iterate over document sections and expand each with an AI pass.

        :param ctx: Shared project context.
        :type ctx: ProjectContext
        :raises SystemExit: On AI runner failure for any section.
        """
        if self.doc["id"] in ctx.state.get("fleshed_out", []):
            print(f"Skipping fleshing out for {self.doc['name']} (already fleshed out).")
            return

        expected_file = ctx.get_document_path(self.doc)
        target_path = ctx.get_target_path(self.doc)
        headers = ctx.parse_markdown_headers(expected_file, doc=self.doc)
        flesh_prompt_tmpl = ctx.load_prompt("flesh_out.md")
        extra = _count_tokens(flesh_prompt_tmpl) + _count_tokens(ctx.description_ctx) + 40
        accumulated_context = ctx.get_accumulated_context(self.doc, extra_tokens=extra, include_all=True)
        
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
            with ctx._state_lock:
                ctx.state["fleshed_out_headers"][self.doc["id"]].append(header_clean)
            ctx.save_state()

        with ctx._state_lock:
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
        return f"Phase3: Summarize {self.doc.get('name', self.doc.get('id', '?'))}"

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
        with ctx._state_lock:
            ctx.state.setdefault("summarized", []).append(self.doc["id"])
        ctx.save_state()


class Phase3FinalReview(BasePhase):
    """Holistic alignment review of all planning documents.

    Checks all generated spec documents for consistency with
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
            
        print("\n=> [Phase 4: Final Alignment Review] Reviewing all documents for consistency...")
        final_prompt_tmpl = ctx.load_prompt("final_review.md")
        extra = _count_tokens(final_prompt_tmpl) + _count_tokens(ctx.description_ctx) + 40
        accumulated_context = ctx.get_accumulated_context(extra_tokens=extra)
        final_prompt = ctx.format_prompt(final_prompt_tmpl, description_ctx=ctx.description_ctx, accumulated_context=accumulated_context)
        
        # Final review can modify all existing spec files
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

    Automatically removes scope creep from spec documents and
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

        print("\n=> [Phase 6: Adversarial Review] Comparing specs against original description for scope creep...")
        target_path = "docs/plan/adversarial_review.md"
        expected_file = os.path.join(ctx.plan_dir, "adversarial_review.md")

        prompt_tmpl = ctx.load_prompt("adversarial_review.md")
        extra = _count_tokens(prompt_tmpl) + _count_tokens(ctx.description_ctx) + 40
        accumulated_context = ctx.get_accumulated_context(extra_tokens=extra)
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path,
            accumulated_context=accumulated_context
        )

        specs_dir = os.path.join(ctx.plan_dir, "specs") + os.sep
        allowed_files = [expected_file, specs_dir]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

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


class Phase7ExtractRequirements(BasePhase):
    """Extract structured requirements from a single spec document as JSON.

    Produces ``docs/plan/requirements/<doc_id>.json``.  Skipped for docs already
    recorded in ``ctx.state["extracted_requirements"]``.

    :param doc: Document descriptor dict.
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Extract Reqs"

    @property
    def display_name(self) -> str:
        return f"Phase7: Extract {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        expected_file = os.path.join(ctx.requirements_dir, f"{self.doc['id']}.json")

        # Skip only if the state says extracted AND the .json file exists on disk.
        # Stale state from old .md-based runs must not prevent re-extraction.
        if self.doc["id"] in ctx.state.get("extracted_requirements", []):
            if os.path.exists(expected_file):
                return
            print(f"   -> State says {self.doc['id']} extracted but {expected_file} missing, re-extracting.")

        doc_path = ctx.get_document_path(self.doc)
        if not os.path.exists(doc_path):
            return

        target_path = f"docs/plan/requirements/{self.doc['id']}.json"
        doc_rel_path = f"docs/plan/specs/{self.doc['id']}.md"

        print(f"\n=> [Phase 7: Extract Requirements] Extracting from {self.doc['name']}...")
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
            sys.exit(1)

        # Validate JSON schema
        print(f"   -> Validating extraction for {self.doc['name']}...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "7"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print(f"\n[!] Validation failed for {self.doc['name']}:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        with ctx._state_lock:
            ctx.state.setdefault("extracted_requirements", []).append(self.doc["id"])
        ctx.save_state()


class Phase8FilterMetaRequirements(BasePhase):
    """Filter out process/meta requirements from extracted requirement JSONs.

    Runs in parallel across all per-document extracted requirement files.
    Removes requirements that refer to process, methodology, or tooling
    rather than actual product/technical requirements.

    :param doc: Document descriptor dict.
    :type doc: dict
    """

    def __init__(self, doc: dict) -> None:
        self.doc = doc

    @property
    def operation(self) -> str:
        return "Filter Meta Reqs"

    @property
    def display_name(self) -> str:
        return f"Phase8: Filter {self.doc.get('name', self.doc.get('id', '?'))}"

    def execute(self, ctx: ProjectContext) -> None:
        req_file = os.path.join(ctx.requirements_dir, f"{self.doc['id']}.json")
        if not os.path.exists(req_file):
            print(f"\n[!] Cannot filter {self.doc['id']}: {req_file} does not exist.")
            print(f"    Phase 7 (Extract Requirements) must produce this file first.")
            sys.exit(1)

        print(f"\n=> [Phase 8: Filter Meta Requirements] Filtering {self.doc['name']}...")

        with open(req_file, "r", encoding="utf-8") as f:
            requirements_json = f.read()

        target_path = f"docs/plan/requirements/{self.doc['id']}.json"

        prompt_tmpl = ctx.load_prompt("filter_meta_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            requirements_json=requirements_json,
            target_path=target_path
        )

        allowed_files = [req_file]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print(f"\n[!] Error filtering meta requirements for {self.doc['name']}.")
            sys.exit(1)

        ctx.stage_changes(allowed_files)


class Phase9MergeRequirements(BasePhase):
    """Consolidate all per-document requirement JSON files into ``requirements.json``.

    Idempotent via ``ctx.state["requirements_merged"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("requirements_merged", False):
            print("Requirements merging already completed.")
            return

        print("\n=> [Phase 9: Merge Requirements] Consolidating all requirements into JSON...")
        prompt_tmpl = ctx.load_prompt("merge_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)

        requirements_json = os.path.join(ctx.plan_dir, "requirements.json")
        allowed_files = [requirements_json]

        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print("\n[!] Error merging requirements.")
            sys.exit(1)

        # Validate merged JSON
        print("\n   -> Validating merged requirements.json...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "9"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Validation failed after merging requirements:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["requirements_merged"] = True
        ctx.save_state()


class Phase10DeduplicateRequirements(BasePhase):
    """Deduplicate requirements in ``requirements.json``.

    Updates ``requirements.json`` in-place (removing duplicates) and writes
    a deduplication record to ``requirements_deduped.json``.
    Idempotent via ``ctx.state["requirements_deduplicated"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("requirements_deduplicated", False):
            print("Requirements deduplication already completed.")
            return

        print("\n=> [Phase 10: Deduplicate Requirements] Removing duplicates...")
        requirements_json_path = os.path.join(ctx.plan_dir, "requirements.json")
        deduped_path = os.path.join(ctx.plan_dir, "requirements_deduped.json")

        if not os.path.exists(requirements_json_path):
            print("\n[!] requirements.json not found.")
            sys.exit(1)

        with open(requirements_json_path, "r", encoding="utf-8") as f:
            req_content = f.read()

        prompt_tmpl = ctx.load_prompt("deduplicate_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            requirements_json_path="docs/plan/requirements.json",
            deduped_target_path="docs/plan/requirements_deduped.json"
        )

        allowed_files = [requirements_json_path, deduped_path]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print("\n[!] Error deduplicating requirements.")
            sys.exit(1)

        # Validate
        print("\n   -> Validating deduplication...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "10"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Validation failed after deduplication:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["requirements_deduplicated"] = True
        ctx.save_state()


class Phase12OrderRequirements(BasePhase):
    """Order requirements for implementation, prioritizing E2E testability.

    Reads ``requirements.json`` and produces ``requirements_ordered.json``.
    Idempotent via ``ctx.state["requirements_ordered"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("requirements_ordered", False):
            print("Requirements ordering already completed.")
            return

        print("\n=> [Phase 12: Order Requirements] Sequencing for E2E-first implementation...")
        prompt_tmpl = ctx.load_prompt("order_requirements.md")
        prompt = ctx.format_prompt(prompt_tmpl, description_ctx=ctx.description_ctx)

        ordered_path = os.path.join(ctx.plan_dir, "requirements_ordered.json")
        allowed_files = [ordered_path]

        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print("\n[!] Error ordering requirements.")
            sys.exit(1)

        # Validate
        print("\n   -> Validating ordered requirements...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "12"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Validation failed after ordering requirements:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["requirements_ordered"] = True
        ctx.save_state()


class Phase13GenerateEpics(BasePhase):
    """Generate JSON epic/requirement mappings.

    Produces ``docs/plan/epic_mappings.json`` with epics, their features,
    and requirement mappings.  Validates with ``validate.py``.
    Idempotent via ``ctx.state["epics_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("epics_completed", False):
            print("Epic generation already completed.")
            return

        print("\n=> [Phase 13: Generate Epics] Generating JSON epic/requirement mappings...")

        # Load all spec summaries to inject as context
        summaries_parts = []
        for fname in sorted(os.listdir(ctx.summaries_dir)):
            fpath = os.path.join(ctx.summaries_dir, fname)
            if os.path.isfile(fpath) and fname.endswith(".md"):
                with open(fpath, "r", encoding="utf-8") as f:
                    summaries_parts.append(
                        f"<summary name=\"{fname}\">\n{f.read()}\n</summary>"
                    )
        summaries_ctx = "\n\n".join(summaries_parts) if summaries_parts else "(no summaries found)"

        prompt_tmpl = ctx.load_prompt("phases.md")
        prompt = ctx.format_prompt(
            prompt_tmpl,
            description_ctx=ctx.description_ctx,
            summaries_ctx=summaries_ctx,
        )

        epic_mappings_path = os.path.join(ctx.plan_dir, "epic_mappings.json")
        phases_dir = os.path.join(ctx.plan_dir, "phases") + os.sep
        allowed_files = [epic_mappings_path, phases_dir]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print("\n[!] Error generating epic mappings.")
            sys.exit(1)

        # Validate
        print("\n   -> Validating epic mappings...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "13"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Validation failed:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["epics_completed"] = True
        ctx.save_state()
        print("Successfully generated epic mappings.")


class Phase14E2EInterfaces(BasePhase):
    """Generate E2E interface definitions for each implementation phase.

    Produces ``docs/plan/e2e_interfaces.md`` with public API and data structure
    definitions that E2E tests will validate.  Single agent.
    Idempotent via ``ctx.state["e2e_interfaces_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("e2e_interfaces_completed", False):
            print("E2E interfaces already generated.")
            return

        print("\n=> [Phase 14: E2E Interfaces] Defining public interfaces per phase...")

        epic_mappings_path = os.path.join(ctx.plan_dir, "epic_mappings.json")
        requirements_path = os.path.join(ctx.plan_dir, "requirements_ordered.json")
        e2e_interfaces_path = os.path.join(ctx.plan_dir, "e2e_interfaces.md")

        if not os.path.exists(epic_mappings_path):
            print("\n[!] epic_mappings.json not found.")
            sys.exit(1)

        with open(epic_mappings_path, "r", encoding="utf-8") as f:
            epic_mappings_json = f.read()
        with open(requirements_path, "r", encoding="utf-8") as f:
            requirements_json = f.read()

        prompt_tmpl = ctx.load_prompt("e2e_interfaces.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            epic_mappings_json=epic_mappings_json,
            requirements_json=requirements_json
        )

        allowed_files = [e2e_interfaces_path]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(e2e_interfaces_path):
            print("\n[!] Error generating E2E interfaces.")
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["e2e_interfaces_completed"] = True
        ctx.save_state()
        print("Successfully generated E2E interface definitions.")


class Phase15FeatureGates(BasePhase):
    """Break down E2E interface support into file-based feature gates.

    Produces ``docs/plan/feature_gates.md`` defining feature gate files and
    their mapping to E2E test scenarios.  Single agent.
    Idempotent via ``ctx.state["feature_gates_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("feature_gates_completed", False):
            print("Feature gates already generated.")
            return

        print("\n=> [Phase 15: Feature Gates] Defining feature gate files...")

        e2e_interfaces_path = os.path.join(ctx.plan_dir, "e2e_interfaces.md")
        feature_gates_path = os.path.join(ctx.plan_dir, "feature_gates.md")

        if not os.path.exists(e2e_interfaces_path):
            print("\n[!] e2e_interfaces.md not found.")
            sys.exit(1)

        with open(e2e_interfaces_path, "r", encoding="utf-8") as f:
            e2e_interfaces_content = f.read()

        prompt_tmpl = ctx.load_prompt("feature_gates.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            e2e_interfaces_content=e2e_interfaces_content
        )

        allowed_files = [feature_gates_path]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0 or not os.path.exists(feature_gates_path):
            print("\n[!] Error generating feature gates.")
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["feature_gates_completed"] = True
        ctx.save_state()
        print("Successfully generated feature gate definitions.")


class Phase15HolisticTasks(BasePhase):
    """Break down epics into holistic task sets for each implementation phase.

    Each task covers both tests and implementation as a single unit of work.
    Each task gets a .md file and a .json sidecar.

    Runs in parallel across phases.  Validates with ``validate.py``.
    Idempotent via ``ctx.state["tasks_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("tasks_completed", False):
            print("Holistic task generation already completed.")
            return

        print("\n=> [Phase 15: Holistic Tasks] Breaking down epics into task sets...")

        epic_mappings_path = os.path.join(ctx.plan_dir, "epic_mappings.json")
        e2e_interfaces_path = os.path.join(ctx.plan_dir, "e2e_interfaces.md")
        feature_gates_path = os.path.join(ctx.plan_dir, "feature_gates.md")
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")

        if not os.path.exists(epic_mappings_path):
            print("\n[!] epic_mappings.json not found.")
            sys.exit(1)

        with open(epic_mappings_path, "r", encoding="utf-8") as f:
            epic_data = json.load(f)
        with open(e2e_interfaces_path, "r", encoding="utf-8") as f:
            e2e_interfaces = f.read()
        with open(feature_gates_path, "r", encoding="utf-8") as f:
            feature_gates = f.read()

        # Group epics by phase
        phases: Dict[int, list] = {}
        for epic in epic_data.get("epics", []):
            phase_num = epic.get("phase_number", 0)
            phases.setdefault(phase_num, []).append(epic)

        os.makedirs(tasks_dir, exist_ok=True)
        allowed_files = [tasks_dir + os.sep]

        def generate_phase_tasks(phase_num: int, phase_epics: list) -> bool:
            phase_id = f"phase_{phase_num}"
            phase_dir = os.path.join(tasks_dir, phase_id)
            os.makedirs(phase_dir, exist_ok=True)

            epic_json = json.dumps(phase_epics, indent=2)
            target_dir = phase_id

            prompt_tmpl = ctx.load_prompt("holistic_tasks.md")
            prompt = ctx.format_prompt(prompt_tmpl,
                description_ctx=ctx.description_ctx,
                phase_filename=phase_id,
                epic_json=epic_json,
                e2e_interfaces=e2e_interfaces,
                feature_gates=feature_gates,
                target_dir=target_dir
            )

            result = ctx.run_gemini(prompt, allowed_files=[phase_dir + os.sep])
            if result.returncode != 0:
                print(f"\n[!] Error generating tasks for {phase_id}.")
                return False
            return True

        # Generate tasks in parallel
        if ctx.jobs > 1 and len(phases) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=ctx.jobs) as executor:
                futures = {
                    executor.submit(generate_phase_tasks, pn, epics): pn
                    for pn, epics in sorted(phases.items())
                }
                for future in concurrent.futures.as_completed(futures):
                    pn = futures[future]
                    if not future.result():
                        sys.exit(1)
        else:
            for pn, epics in sorted(phases.items()):
                if not generate_phase_tasks(pn, epics):
                    sys.exit(1)

        # Validate
        print("\n   -> Validating task sidecars...")
        verify_res = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "15"],
            capture_output=True, text=True, cwd=ctx.root_dir
        )
        if verify_res.returncode != 0:
            print("\n[!] Task validation failed:")
            print(verify_res.stdout)
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["tasks_completed"] = True
        ctx.save_state()
        print("Successfully generated holistic tasks.")


class Phase16ReviewHolisticTasks(BasePhase):
    """Review holistic tasks for a single implementation phase.

    Checks completeness, no duplication, feature gate coverage, and correct
    depends_on relationships.  Runs in parallel across phases.

    :param phase_id: Phase directory name (e.g. ``"phase_1"``).
    :type phase_id: str
    """

    def __init__(self, phase_id: str) -> None:
        self.phase_id = phase_id

    @property
    def operation(self) -> str:
        return "Review Tasks"

    @property
    def display_name(self) -> str:
        return f"Phase16: Review {self.phase_id}"

    def execute(self, ctx: ProjectContext) -> None:
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        phase_dir = os.path.join(tasks_dir, self.phase_id)
        if not os.path.isdir(phase_dir):
            print(f"   -> Skipping review for {self.phase_id} (no directory).")
            return

        print(f"\n=> [Phase 16: Review] Reviewing tasks for {self.phase_id}...")

        # Collect all task content
        tasks_content_parts = []
        for root, dirs, files in os.walk(phase_dir):
            for fname in sorted(files):
                if fname.endswith(".json") and not fname.startswith("dag"):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        sidecar = json.load(f)
                    md_path = fpath.replace(".json", ".md")
                    md_content = ""
                    if os.path.exists(md_path):
                        with open(md_path, "r", encoding="utf-8") as f:
                            md_content = f.read()
                    entry = f"### {fname}\n```json\n{json.dumps(sidecar, indent=2)}\n```\n{md_content}\n"
                    tasks_content_parts.append(entry)

        feature_gates_path = os.path.join(ctx.plan_dir, "feature_gates.md")
        feature_gates = ""
        if os.path.exists(feature_gates_path):
            with open(feature_gates_path, "r", encoding="utf-8") as f:
                feature_gates = f.read()

        prompt_tmpl = ctx.load_prompt("review_holistic_tasks.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            phase_id=self.phase_id,
            tasks_content="\n".join(tasks_content_parts) or "(no tasks)",
            feature_gates=feature_gates
        )

        allowed_files = [phase_dir + os.sep]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print(f"\n[!] Error reviewing tasks for {self.phase_id}.")
            sys.exit(1)

        ctx.stage_changes(allowed_files)


class Phase19PreInitTask(BasePhase):
    """Generate the Pre-Init task definition for project bootstrapping.

    Creates a task .md and .json sidecar responsible for Dockerfile verification,
    harness.py creation, feature gates directory setup, and E2E infrastructure.
    Idempotent via ``ctx.state["pre_init_task_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("pre_init_task_completed", False):
            print("Pre-Init task already generated.")
            return

        print("\n=> [Phase 19: Pre-Init Task] Generating bootstrap task definition...")

        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        pre_init_dir = os.path.join(tasks_dir, "phase_0")
        os.makedirs(pre_init_dir, exist_ok=True)

        requirements_path = os.path.join(ctx.plan_dir, "requirements_ordered.json")
        requirements_json = ""
        if os.path.exists(requirements_path):
            with open(requirements_path, "r", encoding="utf-8") as f:
                requirements_json = f.read()

        target_path = "docs/plan/tasks/phase_0/00_pre_init"

        prompt_tmpl = ctx.load_prompt("pre_init_task.md")
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            requirements_json=requirements_json,
            target_path=target_path
        )

        allowed_files = [pre_init_dir + os.sep]
        result = ctx.run_gemini(prompt, allowed_files=allowed_files)

        if result.returncode != 0:
            print("\n[!] Error generating pre-init task.")
            sys.exit(1)

        ctx.stage_changes(allowed_files)
        ctx.state["pre_init_task_completed"] = True
        ctx.save_state()
        print("Successfully generated pre-init task.")


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

        print(f"\n=> [Phase 18: Cross-Phase Review (Pass {self.pass_num})] Reviewing tasks across all phases for global duplication and coverage...")
        
        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        review_summary_path = os.path.join(tasks_dir, f"cross_phase_review_summary_pass_{self.pass_num}.md")

        if os.path.exists(review_summary_path):
             print(f"   -> Skipping Cross-Phase Task Review (already reviewed).")
             ctx.state[state_key] = True
             ctx.save_state()
             return

        print(f"   -> Performing global cross-phase review...")

        before_count = ctx.count_task_files(tasks_dir)

        summary_filename = f"cross_phase_review_summary_pass_{self.pass_num}.md"
        allowed_files = [tasks_dir + os.sep]

        for attempt in range(1, 4):
            result = ctx.run_gemini(
                "cross_phase_review.md",
                allowed_files=allowed_files,
                context_files={
                    "description_ctx": ctx.input_dir,
                    "tasks_content": [os.path.join(tasks_dir, p) for p in phase_dirs],
                },
                params={"summary_filename": summary_filename},
            )

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
    def _read_sidecar_deps(phase_dir_path: str, task_id: str) -> Optional[List[str]]:
        """Read depends_on from a task's JSON sidecar file.

        :param phase_dir_path: Absolute path to the phase task directory.
        :param task_id: Task ID in ``sub_epic/filename.md`` format.
        :returns: List of dependency task IDs, or ``None`` if no sidecar exists.
        """
        sidecar_path = os.path.join(phase_dir_path, task_id.replace(".md", ".json"))
        if not os.path.exists(sidecar_path):
            return None
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("depends_on", [])
        except (json.JSONDecodeError, KeyError):
            return None

    @staticmethod
    def _build_programmatic_dag(phase_dir_path: str) -> Optional[Dict[str, List[str]]]:
        """Build a DAG from task JSON sidecars or markdown metadata.

        Prefers JSON sidecar ``depends_on`` fields; falls back to parsing
        markdown front-matter.  Returns ``None`` when any task is missing
        dependency metadata entirely (triggering AI fallback).

        :param phase_dir_path: Absolute path to a phase task directory.
        :returns: ``{task_id: [prerequisite_task_ids]}`` mapping, or ``None``.
        """
        dag = {}
        task_files = {}  # filename -> sub_epic/filename mapping
        all_have_metadata = True

        sub_epics = [d for d in os.listdir(phase_dir_path)
                     if os.path.isdir(os.path.join(phase_dir_path, d))]

        for sub_epic in sorted(sub_epics):
            sub_epic_dir = os.path.join(phase_dir_path, sub_epic)
            md_files = [f for f in os.listdir(sub_epic_dir)
                       if f.endswith(".md") and f not in _NON_TASK_FILES]

            for md_file in sorted(md_files):
                task_id = f"{sub_epic}/{md_file}"
                task_files[md_file] = task_id

                # Prefer JSON sidecar, fall back to markdown parsing
                deps = Phase7ADAGGeneration._read_sidecar_deps(phase_dir_path, task_id)
                if deps is None:
                    filepath = os.path.join(sub_epic_dir, md_file)
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                    deps = Phase7ADAGGeneration._parse_depends_on(content)

                if deps is None:
                    all_have_metadata = False
                    continue

                # Resolve dependency filenames to full task_ids
                resolved_deps = []
                for dep in deps:
                    if '/' in dep:
                        resolved_deps.append(dep)
                    elif dep in task_files:
                        resolved_deps.append(task_files[dep])
                    else:
                        for se in sorted(sub_epics):
                            candidate = f"{se}/{dep}"
                            candidate_path = os.path.join(phase_dir_path, se, dep)
                            if os.path.exists(candidate_path):
                                resolved_deps.append(candidate)
                                break

                dag[task_id] = [d for d in resolved_deps if d != task_id]

        if not all_have_metadata:
            return None

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
        # Collect all .md files on disk (excluding non-task files)
        on_disk = set()
        for sub_epic in sorted(os.listdir(phase_dir_path)):
            se_path = os.path.join(phase_dir_path, sub_epic)
            if not os.path.isdir(se_path):
                continue
            for md in sorted(os.listdir(se_path)):
                if md.endswith(".md") and md not in _NON_TASK_FILES:
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

        print("\n=> [Phase 20: DAG Generation] Creating dependency graphs for tasks...")

        tasks_dir = os.path.join(ctx.plan_dir, "tasks")
        if not os.path.exists(tasks_dir):
            print("\n[!] Error: tasks directory does not exist. Run Phase 6 first.")
            sys.exit(1)

        phase_dirs = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d)) and d.startswith("phase_")]
        if not phase_dirs:
            print("\n[!] Error: No phase directories found in tasks/.")
            sys.exit(1)

        def process_phase_dag(phase_id):
            phase_dir_path = os.path.join(tasks_dir, phase_id)
            dag_file_path = os.path.join(phase_dir_path, "dag.json")

            if os.path.exists(dag_file_path):
                try:
                    with open(dag_file_path, "r", encoding="utf-8") as f:
                        existing_dag = json.load(f)
                    errors = self._validate_dag(phase_dir_path, existing_dag)
                    if not errors:
                        print(f"   -> Skipping DAG Generation for {phase_id} (already exists and is valid).")
                        return True
                    print(f"\n[!] WARNING: Existing DAG for {phase_id} is stale ({len(errors)} issue(s)). Regenerating...")
                    for e in errors[:5]:
                        print(f"      - {e}")
                    if len(errors) > 5:
                        print(f"      ... and {len(errors) - 5} more")
                except (json.JSONDecodeError, OSError) as exc:
                    print(f"\n[!] WARNING: Existing DAG for {phase_id} is corrupt ({exc}). Regenerating...")
                try:
                    os.remove(dag_file_path)
                except OSError:
                    pass

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

            sub_epics = [d for d in os.listdir(phase_dir_path) if os.path.isdir(os.path.join(phase_dir_path, d))]
            if not sub_epics:
                return True

            for attempt in range(1, 4):
                result = ctx.run_gemini(
                    "dag_tasks.md",
                    allowed_files=[dag_file_path],
                    context_files={
                        "description_ctx": ctx.input_dir,
                        "tasks_content": phase_dir_path,
                    },
                    params={
                        "phase_filename": phase_id,
                        "target_path": f"docs/plan/tasks/{phase_id}/dag.json",
                    },
                )

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

        # Post-generation validation: run validate.py --phase 20
        if os.path.exists(tasks_dir):
            print("\n=> [Phase 20: Post-Validation] Validating DAGs...")

            import subprocess
            result = subprocess.run(
                [sys.executable, os.path.join(TOOLS_DIR, "validate.py"), "--phase", "20"],
                capture_output=True, text=True, cwd=ctx.root_dir
            )

            if result.returncode != 0:
                print(result.stdout)
                if result.stderr:
                    print(result.stderr)
                print("\n[!] DAG validation failed.")
                print("After fixing, re-run Phase 7A to regenerate DAGs.\n")
                sys.exit(1)
            else:
                print(result.stdout)

        ctx.stage_changes([tasks_dir])
        ctx.state["dag_completed"] = True
        ctx.save_state()
        print("Successfully generated task DAGs.")


class Phase3AConflictResolution(BasePhase):
    """Systematic conflict resolution between planning documents.

    Compares all spec documents for contradictions and resolves
    them using a defined priority hierarchy.  Produces
    ``docs/plan/conflict_resolution.md``.  Idempotent via
    ``ctx.state["conflict_resolution_completed"]``.
    """

    def execute(self, ctx: ProjectContext) -> None:
        if ctx.state.get("conflict_resolution_completed", False):
            print("Conflict resolution review already completed.")
            return

        print("\n=> [Phase 5: Conflict Resolution] Resolving contradictions between documents...")
        target_path = "docs/plan/conflict_resolution.md"
        expected_file = os.path.join(ctx.plan_dir, "conflict_resolution.md")

        prompt_tmpl = ctx.load_prompt("conflict_resolution_review.md")
        extra = _count_tokens(prompt_tmpl) + _count_tokens(ctx.description_ctx) + 40
        accumulated_context = ctx.get_accumulated_context(extra_tokens=extra)
        prompt = ctx.format_prompt(prompt_tmpl,
            description_ctx=ctx.description_ctx,
            target_path=target_path,
            accumulated_context=accumulated_context
        )

        allowed_files = [expected_file]
        allowed_files.extend([ctx.get_document_path(d) for d in DOCS])
        result = ctx.run_ai(prompt, allowed_files=allowed_files)

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


