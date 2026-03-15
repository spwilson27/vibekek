"""Configuration loader for ``.workflow.jsonc``.

This module reads the project-level ``.workflow.jsonc`` file and exposes
typed accessors for individual feature flags.  The file uses JSONC format
(JSON with ``//`` line comments), which is stripped before parsing so that
standard :mod:`json` can be used.

Example ``.workflow.jsonc``::

    {
      // Enable Serena MCP server integration for code intelligence.
      "serena": true
    }
"""

import os
import re
import json
from typing import Any, Dict, List

from .constants import TOOLS_DIR, ROOT_DIR

# Prefer root-level config (copied from templates); fall back to .tools/.
_CONFIG_FILE_ROOT = os.path.join(ROOT_DIR, ".workflow.jsonc")
_CONFIG_FILE_TOOLS = os.path.join(TOOLS_DIR, ".workflow.jsonc")


def _config_file() -> str:
    """Return the path to the config file, preferring root over .tools/."""
    if os.path.exists(_CONFIG_FILE_ROOT):
        return _CONFIG_FILE_ROOT
    return _CONFIG_FILE_TOOLS


def load_config() -> Dict[str, Any]:
    """Read and parse ``.workflow.jsonc``, returning its contents as a dict.

    Line comments (``// …``) are stripped before JSON parsing so that the
    JSONC superset is handled without an external dependency.  Returns ``{}``
    when the file does not exist.  Raises :exc:`json.JSONDecodeError` on
    malformed JSON so that configuration errors are never silently ignored.

    :returns: Parsed configuration mapping, or ``{}`` when the file does not
        exist.
    :rtype: dict
    :raises json.JSONDecodeError: If the file exists but contains invalid JSON
        after comment/trailing-comma stripping.
    """
    cfg = _config_file()
    if not os.path.exists(cfg):
        return {}
    with open(cfg, "r", encoding="utf-8") as f:
        raw = f.read()
    # Strip // line comments and trailing commas before parsing
    stripped = re.sub(r"//[^\n]*", "", raw)
    stripped = re.sub(r",\s*([}\]])", r"\1", stripped)
    return json.loads(stripped)


def get_serena_enabled() -> bool:
    """Return whether the Serena MCP server integration is enabled.

    Reads the ``"serena"`` key from ``.workflow.jsonc``.  Defaults to
    ``False`` when the key is absent or the config file cannot be loaded.

    :returns: ``True`` if Serena is opted in, ``False`` otherwise.
    :rtype: bool
    """
    return bool(load_config().get("serena", False))


def get_rag_enabled() -> bool:
    """Return whether the RAG MCP server integration is enabled.

    Reads the ``"rag"`` key from ``.workflow.jsonc``.  Defaults to
    ``True`` when the key is absent (RAG is enabled by default for
    backward compatibility) or the config file cannot be loaded.

    :returns: ``True`` if RAG is enabled, ``False`` otherwise.
    :rtype: bool
    """
    # Default to True for backward compatibility
    return bool(load_config().get("rag", True))


def get_dev_branch() -> str:
    """Return the configured dev branch name.

    Reads the ``"dev_branch"`` key from ``.workflow.jsonc``.  Defaults to
    ``"dev"`` when absent.

    :returns: Branch name to use as the integration branch.
    :rtype: str
    """
    return str(load_config().get("dev_branch", "dev"))


def get_pivot_remote() -> str:
    """Return the configured pivot remote name.

    Reads the ``"pivot_remote"`` key from ``.workflow.jsonc``.  This is the
    git remote used as the single source of truth for clones and pushes during
    the workflow run — task branches are cloned from it and merged results are
    pushed back to it.  Defaults to ``"origin"`` when absent.

    :returns: Remote name, e.g. ``"origin"``, ``"github"``, or ``"upstream"``.
    :rtype: str
    """
    return str(load_config().get("pivot_remote", "origin"))


def get_config_defaults() -> Dict[str, Any]:
    """Return workflow defaults from ``.workflow.jsonc``.

    Supported keys (all optional):

    * ``backend`` (str) — AI CLI backend (``"gemini"``, ``"claude"``,
      ``"opencode"``, ``"copilot"``, ``"cline"``, ``"aider"``, ``"codex"``, ``"qwen"``).
    * ``model`` (str) — Model name passed through to the AI CLI.
    * ``ignore_sandbox`` (bool) — Disable sandbox violation checks.
    * ``timeout`` (int) — Timeout in seconds per AI agent invocation.
    * ``retries`` (int) — Max retry attempts per phase on failure.
    * ``soft_timeout`` (int) — Soft timeout in seconds for Qwen sessions.
      When reached, the session is interrupted and resumed with a
      "finish up" prompt.  Defaults to 480 (8 minutes).
    * ``context_limit`` (int) — Maximum prompt size in words for phases
      that aggregate task content.  Defaults to 126 000.

    :returns: Dict of config values (only keys present in the file).
    :rtype: dict
    """
    cfg = load_config()
    defaults: Dict[str, Any] = {}
    for key in ("backend", "model", "ignore_sandbox", "timeout", "retries", "soft_timeout", "context_limit"):
        if key in cfg:
            defaults[key] = cfg[key]
    return defaults


def _parse_docker_dict(d: dict, label: str) -> Any:
    """Parse a raw docker config dict into a :class:`~workflow_lib.agent_pool.DockerConfig`.

    :param d: Raw dict from JSON (the contents of a ``"docker"`` block).
    :param label: Human-readable location string used in error messages.
    :raises ValueError: If ``"image"`` is absent or any ``copy_files`` entry is malformed.
    :returns: :class:`~workflow_lib.agent_pool.DockerConfig` instance.
    """
    from .agent_pool import DockerConfig, DockerCopyFile  # local import to avoid circular deps

    if "image" not in d:
        raise ValueError(f".workflow.jsonc: {label} docker block missing required 'image' field")
    copy_files = []
    for j, cf in enumerate(d.get("copy_files", [])):
        for cf_field in ("src", "dest"):
            if cf_field not in cf:
                raise ValueError(
                    f".workflow.jsonc: {label} docker.copy_files[{j}] missing required field {cf_field!r}"
                )
        copy_files.append(DockerCopyFile(src=cf["src"], dest=cf["dest"]))
    return DockerConfig(
        image=d["image"],
        pivot_remote=d.get("pivot_remote", "origin"),
        volumes=list(d.get("volumes", [])),
        copy_files=copy_files,
    )


def merge_docker_configs(base: Any, override: Any) -> Any:
    """Merge *override* on top of *base*, returning an effective :class:`~workflow_lib.agent_pool.DockerConfig`.

    Fields present in *override* replace the corresponding fields in *base*.
    ``copy_files`` and ``volumes`` are replaced entirely (not appended) when
    the override specifies them; otherwise *base* values are kept.

    :param base: Base :class:`~workflow_lib.agent_pool.DockerConfig`, or ``None``.
    :param override: Override :class:`~workflow_lib.agent_pool.DockerConfig`, or ``None``.
    :returns: Merged :class:`~workflow_lib.agent_pool.DockerConfig`, or ``None`` if both are ``None``.
    """
    if base is None:
        return override
    if override is None:
        return base
    from .agent_pool import DockerConfig  # local import to avoid circular deps

    return DockerConfig(
        image=override.image if override.image else base.image,
        pivot_remote=override.pivot_remote if override.pivot_remote != "origin" else base.pivot_remote,
        volumes=override.volumes if override.volumes else base.volumes,
        copy_files=override.copy_files if override.copy_files else base.copy_files,
    )


def get_docker_config() -> Any:
    """Return global :class:`~workflow_lib.agent_pool.DockerConfig` from ``.workflow.jsonc``.

    Reads the top-level ``"docker"`` block.  When present, all workflow steps
    (implementation, review, presubmit, commit, merge) run inside a Docker
    container that git-clones from the configured pivot remote.

    :raises ValueError: If the ``docker`` block is present but missing the
        required ``"image"`` field, or if any ``copy_files`` entry is missing
        ``"src"`` or ``"dest"``.
    :returns: A :class:`~workflow_lib.agent_pool.DockerConfig` instance, or
        ``None`` when no ``"docker"`` block is configured.
    """
    cfg = load_config()
    if "docker" not in cfg:
        return None
    return _parse_docker_dict(cfg["docker"], "global")


def get_agent_pool_configs() -> List[Any]:
    """Return the list of agent pool configurations from ``.workflow.jsonc``.

    Each entry in the ``"agents"`` array is parsed into an
    :class:`~workflow_lib.agent_pool.AgentConfig`.  Missing optional fields
    receive sensible defaults:

    * ``model`` — ``None``
    * ``priority`` — ``1``
    * ``parallel`` — ``1``
    * ``quota-time`` — ``60``
    * ``steps`` — ``["all"]``
    * ``user`` — current OS user (``os.getenv("USER", "")``)
    * ``context_limit`` (or ``context-limit``) — ``None`` (inherits global setting)

    Returns an empty list when the ``"agents"`` key is absent, allowing
    callers to fall back to single-backend behaviour.

    :raises ValueError: If any agent entry is missing a required field
        (``name``, ``backend``), specifies an unknown ``backend`` value, or
        contains an unsupported ``steps`` value.
    :returns: List of :class:`~workflow_lib.agent_pool.AgentConfig` objects,
        or ``[]`` when no agents are configured.
    """
    from .agent_pool import AgentConfig, VALID_STEPS  # local import to avoid circular deps
    from .runners import VALID_BACKENDS
    global_docker = get_docker_config()

    raw = load_config().get("agents", [])
    configs: List[AgentConfig] = []
    for i, entry in enumerate(raw):
        label = f"agents[{i}]" + (f" (name={entry['name']!r})" if "name" in entry else "")

        # Required fields
        for field in ("name", "backend"):
            if field not in entry:
                raise ValueError(f".workflow.jsonc {label}: missing required field {field!r}")

        # Backend validation
        backend = entry["backend"]
        if backend not in VALID_BACKENDS:
            raise ValueError(
                f".workflow.jsonc {label}: unsupported backend {backend!r}. "
                f"Valid backends: {sorted(VALID_BACKENDS)}"
            )

        # Steps validation
        raw_steps = entry.get("steps", ["all"])
        steps = list(raw_steps) if isinstance(raw_steps, (list, tuple)) else [raw_steps]
        bad_steps = [s for s in steps if s not in VALID_STEPS]
        if bad_steps:
            raise ValueError(
                f".workflow.jsonc {label}: unsupported step(s) {bad_steps}. "
                f"Valid steps: {sorted(VALID_STEPS)}"
            )

        # Per-agent docker override: merge with global (agent fields win)
        agent_docker = global_docker
        if "docker" in entry:
            agent_docker_override = _parse_docker_dict(entry["docker"], label)
            agent_docker = merge_docker_configs(global_docker, agent_docker_override)

        raw_ctx_limit = entry.get("context_limit") or entry.get("context-limit")
        agent_context_limit = int(raw_ctx_limit) if raw_ctx_limit is not None else None

        configs.append(AgentConfig(
            name=entry["name"],
            backend=backend,
            user=entry.get("user") or os.getenv("USER", ""),
            parallel=int(entry.get("parallel", 1)),
            priority=int(entry.get("priority", 1)),
            quota_time=int(entry.get("quota-time", 60)),
            spawn_rate=float(entry.get("spawn_rate", 0.0)),
            model=entry.get("model") or None,
            steps=steps,
            cargo_target_dir=entry.get("cargo-target-dir") or None,
            docker_config=agent_docker,
            context_limit=agent_context_limit,
        ))
    return configs


_DEFAULT_CONTEXT_LIMIT = 126_000
_context_limit_override: int | None = None
_agent_context_limit: int | None = None


def set_context_limit_override(value: int) -> None:
    """Override the context limit for the current process.

    Takes precedence over all other context limit settings.  Intended to be
    called once at startup when the user passes ``--context-limit`` on the
    command line.

    :param value: Maximum prompt size in words to enforce.
    :type value: int
    """
    global _context_limit_override
    _context_limit_override = value


def set_agent_context_limit(value: int | None) -> None:
    """Set the active agent's context limit.

    Takes precedence over the global ``"context_limit"`` in ``.workflow.jsonc``
    and the built-in default, but is overridden by the CLI
    ``--context-limit`` flag.  Intended to be called when an agent with a
    per-agent ``"context_limit"`` setting is selected for execution.

    Pass ``None`` to clear any previously set agent-level limit.

    :param value: Maximum prompt size in words for the active agent, or
        ``None`` to clear.
    :type value: int or None
    """
    global _agent_context_limit
    _agent_context_limit = value


def get_context_limit() -> int:
    """Return the configured context limit in words.

    Resolution order (first match wins):

    1. CLI ``--context-limit`` override set via :func:`set_context_limit_override`.
    2. Per-agent limit set via :func:`set_agent_context_limit` (from the
       active agent's ``"context_limit"`` pool definition).
    3. ``"context_limit"`` key in ``.workflow.jsonc``.
    4. Built-in default of 126 000.

    :returns: Maximum prompt size in words.
    :rtype: int
    """
    if _context_limit_override is not None:
        return _context_limit_override
    if _agent_context_limit is not None:
        return _agent_context_limit
    return int(load_config().get("context_limit", _DEFAULT_CONTEXT_LIMIT))
