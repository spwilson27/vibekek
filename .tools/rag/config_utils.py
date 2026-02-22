import os
import json
import glob
from dataclasses import dataclass
from typing import List, Set, Tuple


@dataclass
class Pattern:
    raw: str
    resolved: str


@dataclass
class Source:
    patterns: List[Pattern]
    extensions: frozenset
    label: str


def _normalize_extensions(exts) -> frozenset:
    norm = set()
    for e in exts:
        if not e:
            continue
        if not e.startswith('.'):
            e = '.' + e
        norm.add(e)
    return frozenset(norm)


def load_config(config_path: str) -> Tuple[List[Source], Set[str], str]:
    """
    Parse rag_config.json and return (sources, skip_dirs, config_dir).

    Each source supports either "dir" (legacy string) or "dirs" (array of
    path patterns). Patterns may contain glob tokens (e.g. "**") and are
    resolved relative to the config file directory.
    """
    with open(config_path) as f:
        cfg = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(config_path))
    skip_dirs = set(cfg.get("skip_dirs", [".git", "target", "node_modules", ".venv", "dist"]))

    sources: List[Source] = []
    for entry in cfg.get("sources", []):
        raw_dirs = []
        if "dirs" in entry and entry["dirs"] is not None:
            raw_dirs = list(entry["dirs"])
        elif "dir" in entry and entry["dir"] is not None:
            raw_dirs = [entry["dir"]]
        else:
            continue

        patterns = []
        for raw in raw_dirs:
            # Resolve relative patterns against config file directory
            resolved = raw
            if not os.path.isabs(raw):
                resolved = os.path.abspath(os.path.join(config_dir, raw))
            patterns.append(Pattern(raw=raw, resolved=resolved))

        exts = _normalize_extensions(entry.get("extensions", [".md", ".txt"]))
        label = entry.get("label", ",".join(raw_dirs))
        sources.append(Source(patterns=patterns, extensions=exts, label=label))

    return sources, skip_dirs, config_dir


def expand_source_files(source: Source, skip_dirs: Set[str]) -> List[str]:
    """
    Expand the source's patterns into a list of absolute file paths.

    Behavior:
    - If a pattern's raw string contains glob characters ('*', '?', '['),
      use glob.glob(resolved, recursive=True) to expand (supports '**').
    - If the raw pattern ends with a slash ('/') treat it as non-recursive:
      list only files directly in that directory.
    - If the pattern is a plain directory path (no glob, not ending with '/'),
      walk it recursively (current behaviour).
    - Deduplicate results, return absolute paths.
    """
    results: List[str] = []
    seen = set()

    for pat in source.patterns:
        raw = pat.raw
        resolved = pat.resolved

        has_glob = any(c in raw for c in ("*", "?", "["))
        raw_ends_slash = raw.endswith("/") or raw.endswith(os.sep)

        if has_glob:
            matches = glob.glob(resolved, recursive=True)
            for m in matches:
                if os.path.isfile(m):
                    abs_m = os.path.abspath(m)
                    if abs_m not in seen:
                        _, ext = os.path.splitext(abs_m)
                        if ext in source.extensions:
                            results.append(abs_m); seen.add(abs_m)
                elif os.path.isdir(m):
                    for root, dirs, files in os.walk(m):
                        dirs[:] = [d for d in dirs if d not in skip_dirs]
                        for name in files:
                            _, ext = os.path.splitext(name)
                            if ext in source.extensions:
                                fp = os.path.join(root, name)
                                abs_fp = os.path.abspath(fp)
                                if abs_fp not in seen:
                                    results.append(abs_fp); seen.add(abs_fp)
        else:
            if os.path.isdir(resolved):
                if raw_ends_slash:
                    # Non-recursive: list only files directly inside directory
                    try:
                        for name in os.listdir(resolved):
                            fp = os.path.join(resolved, name)
                            if os.path.isfile(fp):
                                _, ext = os.path.splitext(name)
                                if ext in source.extensions:
                                    abs_fp = os.path.abspath(fp)
                                    if abs_fp not in seen:
                                        results.append(abs_fp); seen.add(abs_fp)
                    except OSError:
                        pass
                else:
                    # Recursive walk (legacy behaviour)
                    for root, dirs, files in os.walk(resolved):
                        dirs[:] = [d for d in dirs if d not in skip_dirs]
                        for name in files:
                            _, ext = os.path.splitext(name)
                            if ext in source.extensions:
                                fp = os.path.join(root, name)
                                abs_fp = os.path.abspath(fp)
                                if abs_fp not in seen:
                                    results.append(abs_fp); seen.add(abs_fp)
            else:
                # Path doesn't exist as dir; try glob expansion as fallback
                matches = glob.glob(resolved, recursive=True)
                for m in matches:
                    if os.path.isfile(m):
                        abs_m = os.path.abspath(m)
                        _, ext = os.path.splitext(abs_m)
                        if ext in source.extensions and abs_m not in seen:
                            results.append(abs_m); seen.add(abs_m)

    return results
