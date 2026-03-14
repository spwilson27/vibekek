"""Git-aware file scanner that respects .gitignore."""

import hashlib
from pathlib import Path
from typing import Generator, Optional

import git


# Code file extensions to index
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".lua", ".r", ".R", ".jl", ".ex", ".exs",
    ".erl", ".hs", ".clj", ".cljs", ".vue", ".svelte", ".css", ".scss",
    ".sass", ".less", ".html", ".htm", ".xml", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".md", ".rst", ".txt", ".sql",
    ".graphql", ".proto", ".thrift", ".avsc", ".tf", ".tfvars", ".hcl",
    ".dockerfile", ".makefile", ".cmake", ".gradle", ".maven", ".pom",
}


def get_repo_path_md5(repo_path: str) -> str:
    """Get MD5 hash of repo path for temp directory naming."""
    return hashlib.md5(repo_path.encode()).hexdigest()


def get_temp_dir(repo_path: str) -> Path:
    """Get temp directory path for storing index."""
    md5hash = get_repo_path_md5(repo_path)
    return Path("/tmp") / f"rag-{md5hash}"


def is_code_file(path: Path, extensions: Optional[set[str]] = None) -> bool:
    """Check if a file is a code file based on extension."""
    if extensions:
        return path.suffix.lower() in extensions or path.name in ("Dockerfile", "Makefile")
    return path.suffix.lower() in CODE_EXTENSIONS or path.name in ("Dockerfile", "Makefile")


def should_exclude_path(rel_path: str, exclude_dirs: list[str]) -> bool:
    """Check if a path should be excluded based on directory names."""
    parts = rel_path.split("/")
    for part in parts:
        if part in exclude_dirs:
            return True
    return False


def get_file_priority(rel_path: str, priority_dirs: list[str]) -> int:
    """
    Get priority score for a file (lower = higher priority).
    Files in priority_dirs get lower scores.
    """
    for i, priority_dir in enumerate(priority_dirs):
        if rel_path.startswith(priority_dir):
            return i
    return len(priority_dirs)  # Default priority


def scan_git_repo(
    repo_path: str,
    exclude_dirs: Optional[list[str]] = None,
    extensions: Optional[list[str]] = None,
    max_file_size_kb: int = 0,
    priority_dirs: Optional[list[str]] = None,
) -> Generator[tuple[Path, str, int], None, None]:
    """
    Scan git repository for code files, respecting .gitignore.

    Args:
        repo_path: Path to git repository
        exclude_dirs: Directory names to exclude (e.g., ["node_modules", "vendor"])
        extensions: Only include these extensions (None = all code extensions)
        max_file_size_kb: Skip files larger than this (0 = no limit)
        priority_dirs: Directories to prioritize (indexed first)

    Yields tuples of (file_path, file_content_hash, priority).
    """
    repo = git.Repo(repo_path)
    repo_path = Path(repo.working_tree_dir or repo_path)
    exclude_dirs = exclude_dirs or []
    extensions_set = set(extensions) if extensions else None
    priority_dirs = priority_dirs or []

    # Get all tracked files from git
    try:
        tracked_files = repo.git.ls_files().splitlines()
    except git.GitCommandError:
        tracked_files = []

    # Also get untracked files (not ignored)
    try:
        untracked_files = repo.untracked_files
    except git.GitCommandError:
        untracked_files = []

    all_files = set(tracked_files) | set(untracked_files)

    # Collect files with priorities
    files_with_priority = []
    for rel_path in all_files:
        # Check exclusions
        if should_exclude_path(rel_path, exclude_dirs):
            continue

        file_path = repo_path / rel_path
        if not file_path.exists():
            continue

        # Check extension filter
        if not is_code_file(file_path, extensions_set):
            continue

        # Check file size limit
        if max_file_size_kb > 0:
            try:
                file_size_kb = file_path.stat().st_size / 1024
                if file_size_kb > max_file_size_kb:
                    continue
            except OSError:
                continue

        # Calculate priority
        priority = get_file_priority(rel_path, priority_dirs)

        # Calculate file content hash
        try:
            content_hash = hashlib.md5(file_path.read_bytes()).hexdigest()
            files_with_priority.append((file_path, content_hash, priority, rel_path))
        except (OSError, IOError):
            continue

    # Sort by priority (lower = higher priority)
    files_with_priority.sort(key=lambda x: (x[2], x[3]))

    # Yield sorted files
    for file_path, content_hash, priority, _ in files_with_priority:
        yield (file_path, content_hash, priority)


def get_all_indexed_files(index_dir: Path) -> set[str]:
    """Get set of relative file paths currently in the index."""
    indexed = set()
    if index_dir.exists():
        for meta_file in index_dir.glob("*.meta"):
            # Meta files store the relative path
            try:
                rel_path = meta_file.read_text().strip()
                indexed.add(rel_path)
            except Exception:
                continue
    return indexed


def get_directory_size_mb(path: Path) -> float:
    """
    Calculate total size of a directory in megabytes.
    
    Args:
        path: Path to directory
        
    Returns:
        Total size in MB
    """
    if not path.exists():
        return 0.0
    
    total_size = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total_size += item.stat().st_size
            except OSError:
                continue
    
    return total_size / (1024 * 1024)
