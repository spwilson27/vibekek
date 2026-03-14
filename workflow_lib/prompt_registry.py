"""Canonical registry of prompt templates and their required placeholders.

Used for:
1. Startup validation — verify all prompt files exist before running phases.
2. Substitution validation — ensure all required placeholders are provided.
3. Test coverage — automated tests can verify prompts match this registry.
"""

# Maps prompt filename -> set of required placeholder names (without braces).
# Placeholders that appear as examples in prose (e.g. {year}, {tech}) are NOT listed.
PROMPT_PLACEHOLDERS = {
    # Phase 1: Document generation
    "spec_prd.md": {"document_name", "document_description", "target_path"},
    "spec_tas.md": {"document_name", "document_description", "target_path"},
    "spec_mcp_design.md": {"document_name", "document_description", "target_path"},
    "spec_user_features.md": {"document_name", "document_description", "target_path"},
    "spec_security_design.md": {"document_name", "document_description", "target_path"},
    "spec_ui_ux_architecture.md": {"document_name", "document_description", "target_path"},
    "spec_ui_ux_design.md": {"document_name", "document_description", "target_path"},
    "spec_risks_mitigation.md": {"document_name", "document_description", "target_path"},
    "spec_performance_spec.md": {"document_name", "document_description", "target_path"},
    "spec_project_roadmap.md": {"document_name", "document_description", "target_path"},
    "research_market.md": {"document_name", "document_description", "target_path"},
    "research_competitive_analysis.md": {"document_name", "document_description", "target_path"},
    "research_technical_analysis.md": {"document_name", "document_description", "target_path"},
    "research_user_research.md": {"document_name", "document_description", "target_path"},

    # Phase 2: Flesh out
    "flesh_out.md": {"description_ctx", "target_path", "header", "accumulated_context"},

    # Phase 2B: Summarize
    "summarize_doc.md": {"document_name", "document_content", "summary_path"},

    # Phase 3: Reviews
    "final_review.md": {"description_ctx", "source_doc"},
    "conflict_resolution_review.md": {"description_ctx", "target_path"},
    "adversarial_review.md": {"description_ctx", "target_path"},

    # Phase 4: Requirements
    "extract_requirements.md": {"description_ctx", "document_name", "document_path", "target_path"},
    "merge_requirements.md": {"description_ctx"},
    "order_requirements.md": {"description_ctx"},

    # Phase 5: Epics & components
    "phases.md": {"description_ctx"},
    "shared_components.md": {"description_ctx", "target_path"},
    "interface_contracts.md": {"description_ctx", "target_path"},

    # Phase 6: Tasks
    "group_tasks.md": {"description_ctx", "phase_filename", "group_filename"},
    "tasks.md": {"description_ctx", "phase_filename", "sub_epic_name", "sub_epic_reqs", "target_dir", "shared_components_ctx"},
    "review_tasks_in_phase.md": {"description_ctx", "phase_filename", "phase_id", "tasks_content"},
    "cross_phase_review.md": {"description_ctx", "tasks_content", "summary_filename"},
    "reorder_tasks.md": {"description_ctx", "tasks_content"},
    "integration_test_plan.md": {"description_ctx", "target_path"},

    # Phase 7: DAG
    "dag_tasks.md": {"description_ctx", "phase_filename", "target_path", "tasks_content"},
    "dag_tasks_review.md": {"description_ctx", "phase_filename", "proposed_dag", "target_path", "tasks_content"},

    # Implementation (run phase)
    "implement_task.md": {"description_ctx", "memory_ctx", "phase_filename", "target_dir", "task_details", "task_name", "spec_ctx", "shared_components_ctx"},
    "review_task.md": {"description_ctx", "memory_ctx", "phase_filename", "target_dir", "task_details", "task_name", "spec_ctx", "shared_components_ctx"},
    "add_task.md": {"description_ctx", "existing_tasks_content", "phase_filename", "shared_components_ctx", "sub_epic_name", "target_dir", "task_filename", "user_description"},
    "fix_requirements.md": {"description_ctx", "existing_tasks_content", "next_task_num", "phase_filename", "shared_components_ctx", "sub_epic_name", "target_dir", "unmapped_reqs_list"},
    "merge_task.md": {"description_ctx", "branches_list"},
    "requirements.md": {"description_ctx"},
    
    # Feature addition
    "feature_discuss.md": {"description_ctx", "discussion_history", "feature_brief", "phases_ctx", "requirements_ctx", "shared_components_ctx"},
    "feature_spec.md": {"description_ctx", "discussion_history", "feature_brief", "requirements_ctx", "shared_components_ctx", "spec_output_path"},
    "feature_execute.md": {"description_ctx", "feature_spec", "phases_ctx", "requirements_ctx", "shared_components_ctx", "phase_id", "sub_epic", "next_task_num"},

    # Fixup operations
    "fix_description_length.md": {"description_ctx", "requirements_context", "short_reqs_list"},
}


def validate_all_prompts_exist(prompts_dir: str) -> list:
    """Check that every registered prompt file exists on disk.

    Returns a list of missing prompt filenames (empty on success).
    """
    import os
    missing = []
    for filename in sorted(PROMPT_PLACEHOLDERS):
        path = os.path.join(prompts_dir, filename)
        if not os.path.exists(path):
            missing.append(filename)
    return missing


def get_required_placeholders(prompt_filename: str) -> set:
    """Return the set of required placeholder names for a prompt, or empty set if unknown."""
    return PROMPT_PLACEHOLDERS.get(prompt_filename, set())
