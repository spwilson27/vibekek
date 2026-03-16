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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
    # Strip // line comments (only when // appears at start of line or after whitespace)
    # and trailing commas before parsing. Be careful not to strip // inside strings (e.g., URLs).
    stripped = re.sub(r"(?m)^\s*//[^\n]*", "", raw)  # Only strip // at start of lines
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


@dataclass
class SCCacheConfig:
    """sccache server configuration for shared Rust build cache.

    When enabled, all workflow agents (containerized or native) connect to
    the sccache server running on the host for accelerated compilation.

    :param enabled: Whether sccache integration is enabled (default: false).
    :param host: Hostname or IP address for agents to reach the sccache server.
        For Docker containers, typically "host.docker.internal".
    :param port: TCP port the sccache server listens on (default: 6301).
    :param cache_dir: Path to the cache directory on the host filesystem.
    """

    enabled: bool = False
    host: str = "host.docker.internal"
    port: int = 6301
    cache_dir: str = "/home/mrwilson/.cache/sccache"


def get_sccache_config() -> Optional[SCCacheConfig]:
    """Return :class:`SCCacheConfig` from ``.workflow.jsonc``.

    Reads the top-level ``"sccache"`` block.  When present and enabled, all
    workflow steps (implementation, review, presubmit, commit, merge) use
    the sccache server for Rust compilation caching.

    :raises ValueError: If the ``sccache`` block is present but missing
        required fields or contains invalid values.
    :returns: A :class:`SCCacheConfig` instance, or ``None`` when no
        ``"sccache"`` block is configured.
    """
    cfg = load_config()
    if "sccache" not in cfg:
        return None

    scc = cfg["sccache"]
    return SCCacheConfig(
        enabled=bool(scc.get("enabled", False)),
        host=str(scc.get("host", "host.docker.internal")),
        port=int(scc.get("port", 6301)),
        cache_dir=str(scc.get("cache_dir", "/home/mrwilson/.cache/sccache")),
    )


def get_sccache_enabled() -> bool:
    """Return whether sccache integration is enabled.

    Reads the ``"sccache.enabled"`` key from ``.workflow.jsonc``.  Defaults to
    ``False`` when the key is absent or the config file cannot be loaded.

    :returns: ``True`` if sccache is enabled, ``False`` otherwise.
    :rtype: bool
    """
    cfg = get_sccache_config()
    return cfg is not None and cfg.enabled


@dataclass
class SCCacheDistConfig:
    """sccache-dist configuration for distributed compilation.

    When enabled, workflow agents connect to the sccache-dist scheduler
    for remote build execution across multiple machines.

    :param enabled: Whether sccache-dist integration is enabled (default: false).
    :param scheduler_url: URL of the sccache-dist scheduler endpoint.
        For Docker containers, use "http://host.docker.internal:10600".
    :param auth_token: Authentication token for scheduler access.
        Used for client authentication with the scheduler.
    :param config_file: Path to the scheduler config file on the host.
    """

    enabled: bool = False
    scheduler_url: str = "http://host.docker.internal:10600"
    auth_token: str = "gooey-dist-token-2024"
    config_file: str = "/home/mrwilson/.tools/sccache-dist.toml"


@dataclass
class SCCacheServicesConfig:
    """sccache services control configuration.

    Unified configuration for sccache service auto-start and container integration.

    :param auto_start: Whether to auto-start sccache services when workflow runs (default: false).
        When true, checks if sccache server and/or sccache-dist scheduler are running
        (based on their enabled flags) and starts them if not.
    :param configure_containers: Whether to configure containers with sccache environment
        variables (default: true). When true, containers are started with RUSTC_WRAPPER,
        SCCACHE_SERVER, and/or SCCACHE_DIST_SCHEDULER_URL environment variables.
        When false, containers run without sccache configuration.
    """

    auto_start: bool = False
    configure_containers: bool = True


def get_sccache_services_config() -> Optional[SCCacheServicesConfig]:
    """Return :class:`SCCacheServicesConfig` from ``.workflow.jsonc``.

    Reads the top-level ``"sccache_services"`` block. Controls unified
    auto-start behavior and container configuration for sccache services.

    :returns: A :class:`SCCacheServicesConfig` instance, or ``None`` when no
        ``"sccache_services"`` block is configured.
    """
    cfg = load_config()
    if "sccache_services" not in cfg:
        return None

    scs = cfg["sccache_services"]
    return SCCacheServicesConfig(
        auto_start=bool(scs.get("auto_start", False)),
        configure_containers=bool(scs.get("configure_containers", True)),
    )


def get_sccache_dist_config() -> Optional[SCCacheDistConfig]:
    """Return :class:`SCCacheDistConfig` from ``.workflow.jsonc``.

    Reads the top-level ``"sccache_dist"`` block.  When present and enabled,
    workflow agents connect to the sccache-dist scheduler for distributed
    compilation.

    :raises ValueError: If the ``sccache_dist`` block is present but missing
        required fields or contains invalid values.
    :returns: A :class:`SCCacheDistConfig` instance, or ``None`` when no
        ``"sccache_dist"`` block is configured.
    """
    cfg = load_config()
    if "sccache_dist" not in cfg:
        return None

    scd = cfg["sccache_dist"]
    return SCCacheDistConfig(
        enabled=bool(scd.get("enabled", False)),
        scheduler_url=str(scd.get("scheduler_url", "http://host.docker.internal:10600")),
        auth_token=str(scd.get("auth_token", "gooey-dist-token-2024")),
        config_file=str(scd.get("config_file", "/home/mrwilson/.tools/sccache-dist.toml")),
    )


def get_sccache_dist_enabled() -> bool:
    """Return whether sccache-dist integration is enabled.

    Reads the ``"sccache_dist.enabled"`` key from ``.workflow.jsonc``.  Defaults to
    ``False`` when the key is absent or the config file cannot be loaded.

    :returns: ``True`` if sccache-dist is enabled, ``False`` otherwise.
    :rtype: bool
    """
    cfg = get_sccache_dist_config()
    return cfg is not None and cfg.enabled


def ensure_sccache_services():
    """Auto-start sccache services if configured with auto_start=True.

    Checks if sccache server and/or sccache-dist scheduler are running
    based on config settings, and starts them if not.

    :returns: Tuple of (sccache_ok, sccache_dist_ok) indicating service status.
    :rtype: tuple
    """
    import subprocess
    from .constants import ROOT_DIR

    services_cfg = get_sccache_services_config()
    if not services_cfg or not services_cfg.auto_start:
        return True, True  # Auto-start disabled, assume OK

    sccache_ok = True
    sccache_dist_ok = True

    # Check and start sccache server if enabled
    sccache_cfg = get_sccache_config()
    if sccache_cfg and sccache_cfg.enabled:
        # Check if running
        result = subprocess.run(["pgrep", "-f", "sccache"], capture_output=True, text=True)
        if result.returncode != 0:
            # Not running, start it
            sccache_script = os.path.join(ROOT_DIR, ".tools", "start-sccache.sh")
            if os.path.exists(sccache_script):
                subprocess.run([sccache_script, "start"], capture_output=True)
                # Wait for it to be ready
                import time
                time.sleep(2)
                # Verify
                result = subprocess.run(["pgrep", "-f", "sccache"], capture_output=True, text=True)
                sccache_ok = result.returncode == 0

    # Check and start sccache-dist scheduler if enabled
    dist_cfg = get_sccache_dist_config()
    if dist_cfg and dist_cfg.enabled:
        # Check if running
        result = subprocess.run(["pgrep", "-f", "sccache-dist"], capture_output=True, text=True)
        if result.returncode != 0:
            # Not running, start it
            dist_script = os.path.join(ROOT_DIR, ".tools", "start-sccache-dist.sh")
            if os.path.exists(dist_script):
                subprocess.run([dist_script, "start"], capture_output=True)
                # Wait for it to be ready
                import time
                time.sleep(2)
                # Verify
                result = subprocess.run(["pgrep", "-f", "sccache-dist"], capture_output=True, text=True)
                sccache_dist_ok = result.returncode == 0

    return sccache_ok, sccache_dist_ok


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
            env=entry.get("env", {}),
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
