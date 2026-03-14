"""Tree-sitter based code parser for semantic analysis."""

import hashlib
import re
from pathlib import Path
from typing import Optional

from tree_sitter import Parser

from .store import Symbol


# Tree-sitter language mappings
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# Node type mappings for different languages
SYMBOL_TYPES = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "assignment": "variable",
    },
    "javascript": {
        "function_declaration": "function",
        "arrow_function": "function",
        "class_declaration": "class",
        "variable_declarator": "variable",
        "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function",
        "arrow_function": "function",
        "class_declaration": "class",
        "variable_declarator": "variable",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
}


class CodeParser:
    """Parse code files using tree-sitter."""

    def __init__(self):
        self._parsers: dict[str, Parser] = {}
        self._load_languages()

    def _load_languages(self):
        """Load tree-sitter languages."""
        pass

    def _get_parser(self, language: str) -> Optional[Parser]:
        """Get or create a parser for a language."""
        if language not in self._parsers:
            try:
                import tree_sitter_python
                import tree_sitter_javascript
                import tree_sitter_typescript
                from tree_sitter import Language

                lang_map = {
                    "python": tree_sitter_python.language(),
                    "javascript": tree_sitter_javascript.language(),
                    "typescript": tree_sitter_typescript.language_typescript(),
                }

                if language in lang_map:
                    lang_obj = Language(lang_map[language])
                    parser = Parser(lang_obj)
                    self._parsers[language] = parser
            except ImportError:
                return None

        return self._parsers.get(language)

    def parse_file(self, file_path: Path, content: str) -> list[Symbol]:
        """Parse a file and extract symbols."""
        ext = file_path.suffix.lower()
        language = LANGUAGE_EXTENSIONS.get(ext)

        if not language:
            return []

        parser = self._get_parser(language)
        if not parser:
            return []

        try:
            tree = parser.parse(bytes(content, "utf8"))
            symbols = []
            symbol_types = SYMBOL_TYPES.get(language, {})

            # Use iterative approach to walk the tree
            self._walk_tree(tree.root_node, file_path, content, language, 
                           symbol_types, symbols, parent=None)

            return symbols
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")
            return []

    def _walk_tree(self, node, file_path: Path, content: str, language: str,
                   symbol_types: dict, symbols: list, parent: Optional[str] = None):
        """Walk tree and extract symbols iteratively."""
        # Check if this node is a symbol we care about
        if node.type in symbol_types:
            symbol = self._extract_symbol(node, file_path, content, language,
                                         symbol_types, parent)
            if symbol:
                symbols.append(symbol)
                # For classes, extract methods as children
                if symbol.symbol_type == "class":
                    # Process children with this class as parent
                    for child in node.children:
                        self._walk_tree(child, file_path, content, language,
                                       symbol_types, symbols, parent=symbol.name)
                    return  # Children already processed

        # Process all children
        for child in node.children:
            self._walk_tree(child, file_path, content, language,
                           symbol_types, symbols, parent)

    def _extract_symbol(self, node, file_path: Path, content: str,
                        language: str, symbol_types: dict, 
                        parent: Optional[str] = None) -> Optional[Symbol]:
        """Extract a symbol from a tree node."""
        symbol_type = symbol_types.get(node.type)
        if not symbol_type:
            return None

        name = self._get_symbol_name(node, node.type)
        if not name:
            return None

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        start_byte = node.start_byte
        end_byte = node.end_byte
        symbol_content = content[start_byte:end_byte]

        content_hash = hashlib.md5(
            f"{file_path}:{start_line}:{end_line}:{symbol_content}".encode()
        ).hexdigest()

        # Extract additional metadata based on language
        decorators = self._extract_decorators(node, language)
        parameters = self._extract_parameters(node, language)
        extends = self._extract_extends(node, language)

        return Symbol(
            name=name,
            symbol_type=symbol_type,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            content=symbol_content,
            language=language,
            parent=parent,
            decorators=decorators,
            parameters=parameters,
            extends=extends,
        )

    def _extract_decorators(self, node, language: str) -> list[str]:
        """Extract decorators from a symbol node."""
        decorators = []
        
        if language == "python":
            # Look for decorator nodes before the definition
            cursor = node.walk()
            # Go to previous sibling to find decorators
            if cursor.goto_previous_sibling():
                while cursor.node.type == "decorated_definition":
                    # Found decorated definition, extract decorators
                    if cursor.goto_first_child():
                        while cursor.node.type == "decorator":
                            decorator_name = cursor.node.text.decode("utf8")
                            # Remove @ prefix
                            if decorator_name.startswith("@"):
                                decorator_name = decorator_name[1:]
                            decorators.append(decorator_name)
                            if not cursor.goto_next_sibling():
                                break
                    break
            # Also check for decorators as children
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "decorator":
                        decorator_text = cursor.node.text.decode("utf8")
                        if decorator_text.startswith("@"):
                            decorators.append(decorator_text[1:])
                    if not cursor.goto_next_sibling():
                        break
        
        elif language in ("javascript", "typescript"):
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type in ("decorator", "decorator_expression"):
                        decorator_text = cursor.node.text.decode("utf8")
                        if decorator_text.startswith("@"):
                            decorators.append(decorator_text[1:])
                        else:
                            decorators.append(decorator_text)
                    if not cursor.goto_next_sibling():
                        break
        
        return decorators

    def _extract_parameters(self, node, language: str) -> list[str]:
        """Extract parameter names from a function/method."""
        parameters = []
        
        if language == "python":
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "parameters":
                        # Extract parameter names
                        param_cursor = cursor.node.walk()
                        if param_cursor.goto_first_child():
                            while True:
                                if param_cursor.node.type == "identifier":
                                    parameters.append(param_cursor.node.text.decode("utf8"))
                                elif param_cursor.node.type == "default_parameter":
                                    # Has default value, get name from left side
                                    if param_cursor.goto_first_child():
                                        if param_cursor.node.type == "identifier":
                                            parameters.append(param_cursor.node.text.decode("utf8"))
                                        param_cursor.goto_parent()
                                if not param_cursor.goto_next_sibling():
                                    break
                        break
                    if not cursor.goto_next_sibling():
                        break
        
        elif language in ("javascript", "typescript"):
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "formal_parameters":
                        param_cursor = cursor.node.walk()
                        if param_cursor.goto_first_child():
                            while True:
                                if param_cursor.node.type == "identifier":
                                    parameters.append(param_cursor.node.text.decode("utf8"))
                                elif param_cursor.node.type in ("required_parameter", "optional_parameter"):
                                    # Extract identifier from parameter
                                    if param_cursor.goto_first_child():
                                        if param_cursor.node.type == "identifier":
                                            parameters.append(param_cursor.node.text.decode("utf8"))
                                        param_cursor.goto_parent()
                                if not param_cursor.goto_next_sibling():
                                    break
                        break
                    if not cursor.goto_next_sibling():
                        break
        
        return parameters

    def _extract_extends(self, node, language: str) -> list[str]:
        """Extract parent classes/interfaces that this symbol extends."""
        extends = []
        
        if language == "python":
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "supertype":
                        extends.append(cursor.node.text.decode("utf8"))
                    if not cursor.goto_next_sibling():
                        break
        
        elif language in ("javascript", "typescript"):
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "class_heritage":
                        # Look for extends or implements
                        heritage_cursor = cursor.node.walk()
                        if heritage_cursor.goto_first_child():
                            while True:
                                if heritage_cursor.node.type == "identifier":
                                    extends.append(heritage_cursor.node.text.decode("utf8"))
                                if not heritage_cursor.goto_next_sibling():
                                    break
                    if not cursor.goto_next_sibling():
                        break
        
        return extends

    def _get_symbol_name(self, node, node_type: str) -> Optional[str]:
        """Extract the name from a symbol node."""
        # Python uses 'identifier' for names, JavaScript/TypeScript use 'name'
        python_name_nodes = {
            "function_definition": "identifier",
            "class_definition": "identifier",
            "assignment": "identifier",
        }
        
        js_name_nodes = {
            "function_declaration": "name",
            "class_declaration": "name",
            "variable_declarator": "name",
            "assignment": "left",
            "method_definition": "name",
            "interface_declaration": "name",
            "type_alias_declaration": "name",
        }

        # Try Python first
        child_type = python_name_nodes.get(node_type)
        if child_type:
            for child in node.children:
                if child.type == child_type:
                    return child.text.decode("utf8")
        
        # Try JavaScript/TypeScript
        child_type = js_name_nodes.get(node_type)
        if child_type:
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == child_type:
                        return cursor.node.text.decode("utf8")
                    if not cursor.goto_next_sibling():
                        break

        return None

    def extract_calls(self, file_path: Path, content: str, symbol: Symbol) -> list[tuple]:
        """Extract function calls from a symbol's body.
        
        Returns list of (callee_name, call_line, call_content, call_hash)
        """
        calls = []
        ext = file_path.suffix.lower()
        language = LANGUAGE_EXTENSIONS.get(ext)
        
        if not language:
            return calls
        
        parser = self._get_parser(language)
        if not parser:
            return calls
        
        try:
            tree = parser.parse(bytes(content, "utf8"))
            cursor = tree.walk()
            
            # Navigate to the symbol's node
            self._navigate_to_node(cursor, symbol.start_line - 1, symbol.end_line - 1)
            
            # Find call expressions within this symbol
            self._find_calls(cursor, content, symbol, calls)
            
        except Exception:
            pass
        
        return calls

    def _navigate_to_node(self, cursor, start_line: int, end_line: int):
        """Navigate cursor to a node within the given line range."""
        # Simple navigation - go to first child and search
        if cursor.goto_first_child():
            while True:
                node_start = cursor.node.start_point[0]
                node_end = cursor.node.end_point[0]
                
                if node_start >= start_line and node_end <= end_line:
                    return
                
                if cursor.goto_next_sibling():
                    continue
                break

    def _find_calls(self, cursor, content: str, symbol: Symbol, calls: list):
        """Recursively find call expressions in the tree."""
        node = cursor.node
        
        # Look for call expressions
        if node.type in ("call", "call_expression"):
            call_info = self._extract_call_info(node, content, symbol)
            if call_info:
                calls.append(call_info)
        
        # Recurse into children
        if cursor.goto_first_child():
            self._find_calls(cursor, content, symbol, calls)
            cursor.goto_parent()
        
        # Check siblings
        if cursor.goto_next_sibling():
            self._find_calls(cursor, content, symbol, calls)
            cursor.goto_parent()

    def _extract_call_info(self, node, content: str, symbol: Symbol) -> Optional[tuple]:
        """Extract information about a function call."""
        # Get the called function name
        cursor = node.walk()
        if cursor.goto_first_child():
            # First child is usually the function being called
            if cursor.node.type in ("identifier", "member_expression", "attribute"):
                call_name = self._get_call_name(cursor.node)
                if call_name:
                    call_line = node.start_point[0] + 1
                    call_content = content[node.start_byte:node.end_byte]
                    call_hash = hashlib.md5(
                        f"{symbol.file_path}:{call_line}:{call_content}".encode()
                    ).hexdigest()
                    return (call_name, call_line, call_content, call_hash)
        
        return None

    def _get_call_name(self, node) -> Optional[str]:
        """Get the name of the function being called."""
        if node.type == "identifier":
            return node.text.decode("utf8")
        elif node.type in ("member_expression", "attribute"):
            # For method calls like obj.method(), get the method name
            cursor = node.walk()
            if cursor.goto_first_child():
                while True:
                    if cursor.node.type == "identifier":
                        return cursor.node.text.decode("utf8")
                    if not cursor.goto_next_sibling():
                        break
        return None
