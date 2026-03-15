"""
Enforce that workflow_lib has ≥88% test coverage.
Run with: pytest tests/test_coverage.py
"""
import subprocess
import sys
import os
import json

TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
THRESHOLD = 88


def test_workflow_lib_coverage():
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/",
            "--ignore=tests/test_coverage.py",
            "--cov=workflow_lib",
            "--cov-report=json:.coverage_report.json",
            "-q",
            "--tb=no",
        ],
        cwd=TOOLS_DIR,
        capture_output=True,
        text=True,
    )

    report_path = os.path.join(TOOLS_DIR, ".coverage_report.json")
    assert os.path.exists(report_path), (
        f"Coverage report not generated. pytest output:\n{result.stdout}\n{result.stderr}"
    )

    with open(report_path) as f:
        report = json.load(f)

    total_pct = report["totals"]["percent_covered"]

    # Print per-file breakdown for visibility on failure
    lines = [f"workflow_lib coverage: {total_pct:.1f}% (threshold: {THRESHOLD}%)"]
    for filename, data in sorted(report["files"].items()):
        pct = data["summary"]["percent_covered"]
        lines.append(f"  {os.path.relpath(filename, TOOLS_DIR)}: {pct:.1f}%")

    summary = "\n".join(lines)
    assert total_pct >= THRESHOLD, f"Coverage below {THRESHOLD}%:\n{summary}"
