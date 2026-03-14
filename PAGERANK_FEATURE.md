# PageRank-Style Importance Scoring for Semantic Search

## Overview

The semantic search tool now includes a PageRank-style algorithm that ranks code symbols (functions, classes, methods) based on their importance in the codebase. This helps prioritize search results by identifying frequently-called or central functions.

## How It Works

The algorithm treats function calls as "links" between symbols:
- When function A calls function B, it's like A is "voting" for B's importance
- Functions called by many other functions are more important
- Functions called by important functions are themselves more important
- The algorithm iteratively computes scores until convergence

## Configuration

Add the following to your `config.jsonc` under the `semantic` tool:

```jsonc
{
  "tools": {
    "semantic": {
      "pagerank": {
        // Enable PageRank scoring (default: true)
        "enabled": true,
        
        // Damping factor - probability of following a call link (standard: 0.85)
        // Higher values give more weight to the call graph structure
        "damping_factor": 0.85,
        
        // Number of iterations for convergence (default: 20)
        // More iterations = more accurate but slower
        "iterations": 20,
        
        // Weight of PageRank score in final ranking (0-1, default: 0.5)
        // Higher values give more importance to PageRank vs other factors
        "weight": 0.5
      }
    }
  }
}
```

## Features

### 1. Automatic Result Ranking

Search results are automatically sorted by PageRank score when enabled. More important symbols appear first.

**Example:**
```
semantic_search(query="utility_function", search_type="definition")
```

Results will show the most important/called `utility_function` definitions first.

### 2. PageRank Scores in Output

Each search result includes its PageRank score:

```
### utility_function (function)
File: /path/to/utils.py
Lines: 10-25
Language: python
PageRank Score: 0.156789
```

### 3. Line Limit for Results

To prevent overwhelming output, you can limit the number of lines displayed per result:

```jsonc
{
  "tools": {
    "semantic": {
      "limits": {
        // Maximum lines to display per search result (0 = no limit, default: 25)
        "line_limit": 25
      }
    }
  }
}
```

When results are truncated, a warning is shown:
```
⚠️ **Note**: Content truncated to first 25 lines
```

## Use Cases

### 1. Finding Core Functions

PageRank helps identify core/utility functions that are called frequently:

```
semantic_search(query="Logger")
```

The most central logging functions (used by many parts of the codebase) will rank highest.

### 2. Understanding Code Structure

Browse symbols by importance to understand the codebase architecture:

```
semantic_list(list_type="by_type", symbol_type="function")
```

Functions are sorted by PageRank score, showing the most important ones first.

### 3. Prioritizing Search Results

When multiple symbols have the same name, PageRank helps identify the most relevant one:

```
semantic_search(query="process", search_type="definition")
```

The `process` function that's called most often will appear first.

## Algorithm Details

### PageRank Formula

The implementation uses the standard PageRank formula:

```
PR(A) = (1-d)/N + d * Σ(PR(Ti)/C(Ti))
```

Where:
- `PR(A)` = PageRank of symbol A
- `d` = damping factor (typically 0.85)
- `N` = total number of symbols
- `Ti` = symbols that call A
- `C(Ti)` = number of callees of Ti

### Call Graph Construction

The call graph is built during indexing:
1. Tree-sitter parses each file
2. Function/method calls are extracted
3. Call relationships are registered with PageRank
4. After indexing completes, PageRank scores are computed

### Integration with Search

When you search for symbols:
1. Database finds matching symbols
2. PageRank scores are retrieved for each match
3. Results are sorted by score (highest first)
4. Scores are displayed in the output

## Performance

- **Indexing**: Minimal overhead - call relationships are extracted during normal parsing
- **Computation**: Fast - typically <1 second for 1000s of symbols
- **Search**: Negligible impact - scores are pre-computed

## Disabling PageRank

To disable PageRank scoring:

```jsonc
{
  "tools": {
    "semantic": {
      "pagerank": {
        "enabled": false
      }
    }
  }
}
```

Results will be returned in database order without importance ranking.

## Examples

### Example 1: Finding Hub Functions

```python
# A hub function called by many places
def log_message(msg):
    print(f"[LOG] {msg}")

# Many functions call log_message
def process_data():
    log_message("Processing...")
    
def save_data():
    log_message("Saving...")
    
def validate_data():
    log_message("Validating...")
```

With PageRank, `log_message` will have a high score because it's called frequently.

### Example 2: Identifying Core Classes

```python
class DatabaseConnection:
    """Core class used everywhere"""
    pass

class RarelyUsedClass:
    """Used in one place"""
    pass

def main():
    db = DatabaseConnection()  # Calls DatabaseConnection
    
def helper():
    db = DatabaseConnection()  # Calls DatabaseConnection
```

`DatabaseConnection` will rank higher than `RarelyUsedClass`.

## Testing

Run the PageRank tests:

```bash
pytest tests/test_pagerank.py -v
```

Test coverage includes:
- PageRank algorithm correctness
- Symbol database integration
- Configuration options
- Line limit functionality
- Various call graph scenarios (hubs, chains, recursive calls)

## Troubleshooting

### PageRank scores are all zero
- Check that `pagerank.enabled` is `true` in config
- Wait for indexing to complete
- Verify the codebase has function calls

### Results not sorted by importance
- Ensure PageRank is enabled
- Check that indexing has completed
- Verify call relationships are being extracted (check for supported languages)

### High memory usage
- Reduce `max_files` in limits config
- Decrease PageRank `iterations` (default 20 is usually sufficient)
