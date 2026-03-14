"""Symbol database for semantic search using SQLite."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Symbol:
    """A code symbol (function, class, variable, etc.)."""
    name: str
    symbol_type: str  # function, class, method, variable, etc.
    file_path: str
    start_line: int
    end_line: int
    content: str
    language: str
    # For references
    referenced_symbol: Optional[str] = None
    # For call hierarchy
    calls: list[str] = None  # Symbols this symbol calls
    # For relationships
    parent: Optional[str] = None  # Parent class or containing scope
    decorators: list[str] = None  # Decorators applied to this symbol
    parameters: list[str] = None  # Function/method parameters
    extends: list[str] = None  # Classes this extends/inherits from
    
    def __post_init__(self):
        if self.calls is None:
            self.calls = []
        if self.decorators is None:
            self.decorators = []
        if self.parameters is None:
            self.parameters = []
        if self.extends is None:
            self.extends = []


class SymbolDatabase:
    """SQLite-based symbol database for semantic search."""

    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = index_dir / "symbols.db"
        self._db_initialized = False

        # Status tracking
        self._is_indexing = False
        self._total_files = 0
        self._indexed_files = 0

    def _ensure_initialized(self):
        """Lazy initialization of database schema."""
        if not self._db_initialized:
            self._init_db()
            self._db_initialized = True

    def _init_db(self):
        """Initialize the database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                symbol_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                parent TEXT,
                decorators TEXT,
                parameters TEXT,
                extends TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symbol_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symbol_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_symbol TEXT NOT NULL,
                caller_file TEXT NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                call_content TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS indexed_files (
                file_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL
            )
        """)

        # Create indexes for faster lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbols_type ON symbols(symbol_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_references ON symbol_references(symbol_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_calls ON symbol_calls(caller_symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_calls_callee ON symbol_calls(callee_name)")

        conn.commit()
        conn.close()
    
    @property
    def is_indexing(self) -> bool:
        return self._is_indexing
    
    @is_indexing.setter
    def is_indexing(self, value: bool):
        self._is_indexing = value
    
    @property
    def indexing_status(self) -> dict:
        self._ensure_initialized()
        return {
            "is_indexing": self._is_indexing,
            "indexed_files": self._indexed_files,
            "total_files": self._total_files,
            "total_symbols": self.get_symbol_count(),
        }
    
    def set_file_count(self, count: int):
        self._total_files = count
    
    def increment_indexed(self):
        self._indexed_files += 1
    
    def reset_counters(self):
        self._indexed_files = 0
        self._total_files = 0
    
    def get_symbol_count(self) -> int:
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM symbols")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def add_symbol(self, symbol: Symbol, content_hash: str):
        """Add a symbol to the database."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT OR REPLACE INTO symbols
                (name, symbol_type, file_path, start_line, end_line, content, language, 
                 content_hash, parent, decorators, parameters, extends)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.name, symbol.symbol_type, symbol.file_path,
                symbol.start_line, symbol.end_line, symbol.content,
                symbol.language, content_hash,
                symbol.parent,
                json.dumps(symbol.decorators) if symbol.decorators else None,
                json.dumps(symbol.parameters) if symbol.parameters else None,
                json.dumps(symbol.extends) if symbol.extends else None,
            ))
            conn.commit()
        finally:
            conn.close()

    def add_call(self, caller_symbol: Symbol, callee_name: str, call_line: int, 
                 call_content: str, call_hash: str):
        """Add a call relationship between symbols."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT OR REPLACE INTO symbol_calls
                (caller_symbol, caller_file, callee_name, call_line, call_content, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (caller_symbol.name, caller_symbol.file_path, callee_name, 
                  call_line, call_content, call_hash))
            conn.commit()
        finally:
            conn.close()
    
    def add_reference(self, symbol_name: str, file_path: str, start_line: int,
                      end_line: int, content: str, language: str, content_hash: str):
        """Add a reference to a symbol."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT OR REPLACE INTO symbol_references
                (symbol_name, file_path, start_line, end_line, content, language, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (symbol_name, file_path, start_line, end_line, content, language, content_hash))
            conn.commit()
        finally:
            conn.close()
    
    def mark_file_indexed(self, file_path: str, content_hash: str):
        """Mark a file as indexed."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO indexed_files (file_path, content_hash)
                VALUES (?, ?)
            """, (file_path, content_hash))
            conn.commit()
        finally:
            conn.close()
    
    def get_indexed_files(self) -> dict[str, str]:
        """Get all indexed files with their content hashes."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT file_path, content_hash FROM indexed_files")
            return {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()
    
    def remove_file_symbols(self, file_path: str):
        """Remove all symbols and references for a file."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
            cursor.execute("DELETE FROM symbol_references WHERE file_path = ?", (file_path,))
            cursor.execute("DELETE FROM indexed_files WHERE file_path = ?", (file_path,))
            conn.commit()
        finally:
            conn.close()
    
    def find_definitions(self, name: str, symbol_type: Optional[str] = None) -> list[dict]:
        """Find symbol definitions by name."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if symbol_type:
                cursor.execute("""
                    SELECT name, symbol_type, file_path, start_line, end_line, content, language
                    FROM symbols
                    WHERE name = ? AND symbol_type = ?
                """, (name, symbol_type))
            else:
                cursor.execute("""
                    SELECT name, symbol_type, file_path, start_line, end_line, content, language
                    FROM symbols
                    WHERE name = ?
                """, (name,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "name": row[0],
                    "symbol_type": row[1],
                    "file_path": row[2],
                    "start_line": row[3],
                    "end_line": row[4],
                    "content": row[5],
                    "language": row[6],
                })
            return results
        finally:
            conn.close()
    
    def find_references(self, symbol_name: str) -> list[dict]:
        """Find all references to a symbol."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT symbol_name, file_path, start_line, end_line, content, language
                FROM symbol_references
                WHERE symbol_name = ?
            """, (symbol_name,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "symbol_name": row[0],
                    "file_path": row[1],
                    "start_line": row[2],
                    "end_line": row[3],
                    "content": row[4],
                    "language": row[5],
                })
            return results
        finally:
            conn.close()
    
    def search_symbols(self, query: str, n_results: int = 20) -> list[dict]:
        """Search for symbols by name (partial match)."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT name, symbol_type, file_path, start_line, end_line, content, language,
                       parent, decorators, parameters, extends
                FROM symbols
                WHERE name LIKE ?
                LIMIT ?
            """, (f"%{query}%", n_results))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "name": row[0],
                    "symbol_type": row[1],
                    "file_path": row[2],
                    "start_line": row[3],
                    "end_line": row[4],
                    "content": row[5],
                    "language": row[6],
                    "parent": row[7],
                    "decorators": json.loads(row[8]) if row[8] else [],
                    "parameters": json.loads(row[9]) if row[9] else [],
                    "extends": json.loads(row[10]) if row[10] else [],
                })
            return results
        finally:
            conn.close()

    def list_symbols_by_type(self, symbol_type: str, file_path: Optional[str] = None,
                             n_results: int = 100) -> list[dict]:
        """List all symbols of a given type, optionally filtered by file."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            if file_path:
                cursor.execute("""
                    SELECT name, symbol_type, file_path, start_line, end_line, content, language,
                           parent, decorators, parameters, extends
                    FROM symbols
                    WHERE symbol_type = ? AND file_path = ?
                    ORDER BY start_line
                    LIMIT ?
                """, (symbol_type, file_path, n_results))
            else:
                cursor.execute("""
                    SELECT name, symbol_type, file_path, start_line, end_line, content, language,
                           parent, decorators, parameters, extends
                    FROM symbols
                    WHERE symbol_type = ?
                    ORDER BY file_path, start_line
                    LIMIT ?
                """, (symbol_type, n_results))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "name": row[0],
                    "symbol_type": row[1],
                    "file_path": row[2],
                    "start_line": row[3],
                    "end_line": row[4],
                    "content": row[5],
                    "language": row[6],
                    "parent": row[7],
                    "decorators": json.loads(row[8]) if row[8] else [],
                    "parameters": json.loads(row[9]) if row[9] else [],
                    "extends": json.loads(row[10]) if row[10] else [],
                })
            return results
        finally:
            conn.close()

    def get_file_symbols(self, file_path: str) -> list[dict]:
        """Get all symbols in a file (for document outline)."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT name, symbol_type, file_path, start_line, end_line, content, language,
                       parent, decorators, parameters, extends
                FROM symbols
                WHERE file_path = ?
                ORDER BY start_line
            """, (file_path,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "name": row[0],
                    "symbol_type": row[1],
                    "file_path": row[2],
                    "start_line": row[3],
                    "end_line": row[4],
                    "content": row[5],
                    "language": row[6],
                    "parent": row[7],
                    "decorators": json.loads(row[8]) if row[8] else [],
                    "parameters": json.loads(row[9]) if row[9] else [],
                    "extends": json.loads(row[10]) if row[10] else [],
                })
            return results
        finally:
            conn.close()

    def get_callers(self, callee_name: str) -> list[dict]:
        """Find all symbols that call a given function/method."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT caller_symbol, caller_file, callee_name, call_line, call_content
                FROM symbol_calls
                WHERE callee_name = ?
            """, (callee_name,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "caller_symbol": row[0],
                    "caller_file": row[1],
                    "callee_name": row[2],
                    "call_line": row[3],
                    "call_content": row[4],
                })
            return results
        finally:
            conn.close()

    def get_callees(self, caller_symbol: str) -> list[dict]:
        """Find all functions/methods called by a symbol."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT caller_symbol, caller_file, callee_name, call_line, call_content
                FROM symbol_calls
                WHERE caller_symbol = ?
            """, (caller_symbol,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "caller_symbol": row[0],
                    "caller_file": row[1],
                    "callee_name": row[2],
                    "call_line": row[3],
                    "call_content": row[4],
                })
            return results
        finally:
            conn.close()

    def get_class_members(self, class_name: str) -> list[dict]:
        """Get all methods and members of a class."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT name, symbol_type, file_path, start_line, end_line, content, language,
                       parent, decorators, parameters, extends
                FROM symbols
                WHERE parent = ?
                ORDER BY start_line
            """, (class_name,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "name": row[0],
                    "symbol_type": row[1],
                    "file_path": row[2],
                    "start_line": row[3],
                    "end_line": row[4],
                    "content": row[5],
                    "language": row[6],
                    "parent": row[7],
                    "decorators": json.loads(row[8]) if row[8] else [],
                    "parameters": json.loads(row[9]) if row[9] else [],
                    "extends": json.loads(row[10]) if row[10] else [],
                })
            return results
        finally:
            conn.close()

    def get_class_hierarchy(self, class_name: str) -> dict:
        """Get class inheritance hierarchy."""
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # Find the class
            cursor.execute("""
                SELECT name, file_path, extends
                FROM symbols
                WHERE name = ? AND symbol_type = 'class'
            """, (class_name,))
            row = cursor.fetchone()
            
            if not row:
                return {"class": class_name, "parents": [], "children": []}

            parents = json.loads(row[2]) if row[2] else []

            # Find child classes that extend this class
            cursor.execute("""
                SELECT name, file_path
                FROM symbols
                WHERE symbol_type = 'class' AND extends LIKE ?
            """, (f'%"{class_name}"%',))
            
            children = [{"name": r[0], "file_path": r[1]} for r in cursor.fetchall()]

            return {
                "class": class_name,
                "file_path": row[1],
                "parents": parents,
                "children": children,
            }
        finally:
            conn.close()
