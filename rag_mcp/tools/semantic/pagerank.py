"""PageRank-style importance scoring for code symbols."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class PageRankResult:
    """PageRank score for a symbol."""
    symbol_name: str
    file_path: str
    score: float
    rank: int


class PageRankCalculator:
    """
    Implements PageRank algorithm for code symbols based on call relationships.
    
    The algorithm treats function calls as 'links' between symbols:
    - When function A calls function B, it's like A is 'voting' for B's importance
    - Functions called by many other functions are more important
    - Functions called by important functions are themselves more important
    """

    def __init__(self, damping_factor: float = 0.85, iterations: int = 20):
        """
        Initialize PageRank calculator.
        
        Args:
            damping_factor: Probability of following a call link (0.85 is standard)
            iterations: Number of iterations for convergence
        """
        self.damping_factor = damping_factor
        self.iterations = iterations
        self._scores: dict[str, float] = {}
        self._call_graph: dict[str, set[str]] = defaultdict(set)  # caller -> callees
        self._reverse_graph: dict[str, set[str]] = defaultdict(set)  # callee -> callers
        self._symbol_files: dict[str, str] = {}  # symbol_name -> file_path

    def reset(self):
        """Reset all internal state."""
        self._scores.clear()
        self._call_graph.clear()
        self._reverse_graph.clear()
        self._symbol_files.clear()

    def add_symbol(self, symbol_name: str, file_path: str):
        """Register a symbol for PageRank calculation."""
        # Use file_path:name as unique key to handle duplicate names
        key = f"{file_path}:{symbol_name}"
        self._symbol_files[key] = file_path
        self._scores[key] = 0.0
        # Ensure all symbols are in the graphs even if they have no calls
        if key not in self._call_graph:
            self._call_graph[key] = set()
        if key not in self._reverse_graph:
            self._reverse_graph[key] = set()

    def add_call(self, caller_symbol: str, caller_file: str, callee_name: str):
        """
        Add a call relationship.
        
        Args:
            caller_symbol: Name of the calling symbol
            caller_file: File path of the caller
            callee_name: Name of the called symbol
        """
        caller_key = f"{caller_file}:{caller_symbol}"
        
        # Find the callee - it could be in any file
        # For now, we'll create a link to all symbols with this name
        # In a more sophisticated version, we'd resolve to the specific symbol
        callee_found = False
        for key, file_path in self._symbol_files.items():
            if key.endswith(f":{callee_name}"):
                self._call_graph[caller_key].add(key)
                self._reverse_graph[key].add(caller_key)
                callee_found = True
        
        # If callee not found yet (forward reference), still track it
        if not callee_found:
            # Create a placeholder for the callee
            # This handles cases where the callee is in a file not yet indexed
            callee_key = f"unknown:{callee_name}"
            self._call_graph[caller_key].add(callee_key)
            self._reverse_graph[callee_key].add(caller_key)

    def compute(self) -> dict[str, float]:
        """
        Compute PageRank scores for all symbols.
        
        Returns:
            Dictionary mapping symbol keys to PageRank scores
        """
        symbols = list(self._scores.keys())
        n = len(symbols)
        
        if n == 0:
            return {}
        
        # Initialize all scores equally
        initial_score = 1.0 / n
        scores = {s: initial_score for s in symbols}
        
        # Iterative PageRank computation
        for iteration in range(self.iterations):
            new_scores = {}
            
            for symbol in symbols:
                # Base score from random jump (teleportation)
                rank = (1.0 - self.damping_factor) / n
                
                # Add contributions from incoming links
                callers = self._reverse_graph.get(symbol, set())
                for caller in callers:
                    if caller in scores:
                        # Get outgoing links from caller
                        callees = self._call_graph.get(caller, set())
                        out_degree = len(callees)
                        
                        if out_degree > 0:
                            # Distribute caller's score among all its callees
                            rank += self.damping_factor * (scores[caller] / out_degree)
                
                new_scores[symbol] = rank
            
            scores = new_scores
        
        self._scores = scores
        return scores

    def get_ranked_symbols(self, symbol_type: Optional[str] = None, 
                           limit: int = 100) -> list[PageRankResult]:
        """
        Get symbols ranked by PageRank score.
        
        Args:
            symbol_type: Optional filter by symbol type (not used in base implementation)
            limit: Maximum number of results to return
            
        Returns:
            List of PageRankResult objects sorted by score (highest first)
        """
        if not self._scores:
            self.compute()
        
        # Sort by score descending
        sorted_symbols = sorted(
            self._scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        results = []
        for rank, (symbol_key, score) in enumerate(sorted_symbols[:limit], 1):
            file_path = self._symbol_files.get(symbol_key, "unknown")
            # Extract symbol name from key (format: file_path:symbol_name)
            symbol_name = symbol_key.split(":", 1)[1] if ":" in symbol_key else symbol_key
            
            results.append(PageRankResult(
                symbol_name=symbol_name,
                file_path=file_path,
                score=score,
                rank=rank
            ))
        
        return results

    def get_symbol_score(self, symbol_name: str, file_path: str) -> float:
        """Get PageRank score for a specific symbol."""
        key = f"{file_path}:{symbol_name}"
        return self._scores.get(key, 0.0)

    def get_scores_for_file(self, file_path: str) -> dict[str, float]:
        """Get all PageRank scores for symbols in a file."""
        return {
            key.split(":", 1)[1]: score
            for key, score in self._scores.items()
            if self._symbol_files.get(key) == file_path
        }
