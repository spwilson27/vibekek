#!/usr/bin/env python3
"""
Generate requirements_ordered.json from requirements.json.

Strategy: Layer-based ordering
  Layer 0 - Infrastructure: workspace setup, build system, config, DB schema, core constraints
  Layer 1 - Core Services: server arch, command queue, protocol/codegen, core domain types
  Layer 2 - Business Logic: audio engine, video engine, timeline, clips, tracks, mixer
  Layer 3 - Integration: API endpoints, MCP server, WebSocket, state sync
  Layer 4 - User-Facing: UI components, views, editors, user workflows
  Layer 5 - Polish: performance, monitoring, optimization, advanced features, risk mitigation
"""

import json
import re
import sys

INPUT = "/Users/mrwilson/software/resolute/docs/plan/requirements.json"
OUTPUT = "/Users/mrwilson/software/resolute/docs/plan/requirements_ordered.json"


# ---- Layer assignment heuristics ----

LAYER_0_KEYWORDS = [
    "workspace", "cargo", "build system", "directory structure", "project structure",
    "config", "configuration", "database schema", "schema migration", "sqlite",
    "logging", "environment variable", "non-goal", "out of scope",
    "rust edition", "toolchain", "dependency", "crate layout",
    "file system", "storage layout", "project setup",
]

LAYER_1_KEYWORDS = [
    "server architecture", "local server", "command queue", "fifo", "undo/redo",
    "cap'n proto", "capnp", "codegen", "protocol", "websocket", "heartbeat",
    "axum", "static file", "health endpoint", "server-owned", "canonical state",
    "broadcast", "state-change event", "domain type", "core type",
    "serialization", "deserialization", "message format",
]

LAYER_2_KEYWORDS = [
    "audio engine", "audio thread", "audio graph", "audio buffer",
    "video engine", "render pipeline", "render frame", "gpu", "wgpu",
    "timeline", "clip", "track", "mixer", "playback", "transport",
    "h.264", "webcodecs", "encode", "decode", "frame rate",
    "sample rate", "latency", "real-time", "triple-buffer",
    "effect", "plugin", "node graph",
]

LAYER_3_KEYWORDS = [
    "api endpoint", "rpc", "mcp", "model context protocol",
    "websocket handler", "dispatch", "state sync", "synchronization",
    "import", "export", "file format", "project file",
    "undo", "redo", "command handler",
]

LAYER_4_KEYWORDS = [
    "ui component", "frontend", "solidjs", "solid.js", "browser",
    "view", "editor", "panel", "toolbar", "menu", "modal",
    "drag", "drop", "keyboard shortcut", "user interaction",
    "waveform", "thumbnail", "preview", "inspector",
    "layout", "viewport", "canvas", "timeline ui",
    "accessibility", "dark mode", "theme",
]

LAYER_5_KEYWORDS = [
    "performance", "benchmark", "optimization", "profil",
    "monitoring", "telemetry", "metric", "alert",
    "risk", "mitigation", "fallback", "recovery",
    "advanced feature", "future", "phase 8", "phase 9",
    "stress test", "load test", "memory pressure",
]

# Prefix-based layer hints
PREFIX_LAYER = {
    "1_PRD": None,        # mixed - use content
    "2_TAS": 1,           # Technical Architecture - mostly layer 1
    "3_MCP_DESIGN": 3,    # MCP integration - layer 3
    "4_USER_FEATURES": 4, # user features - layer 4
    "5_SECURITY": 1,      # security constraints - mostly layer 1
    "6_UI_UX": 4,         # UI/UX architecture - layer 4
    "7_UI_UX_DES": 4,     # UI/UX design - layer 4
    "8B_PERFORMANCE_SPEC": 5, # performance - layer 5
    "8_RISKS_MITIGATION": 5,  # risks - layer 5
    "9_PROJECT_ROADMAP": 5,   # roadmap - layer 5
}


def score_layer(req):
    text = (req.get("title", "") + " " + req.get("description", "")).lower()
    prefix = req["id"].split("-REQ-")[0]

    # Count keyword hits per layer
    scores = [0, 0, 0, 0, 0, 0]
    for kw in LAYER_0_KEYWORDS:
        if kw in text:
            scores[0] += 1
    for kw in LAYER_1_KEYWORDS:
        if kw in text:
            scores[1] += 1
    for kw in LAYER_2_KEYWORDS:
        if kw in text:
            scores[2] += 1
    for kw in LAYER_3_KEYWORDS:
        if kw in text:
            scores[3] += 1
    for kw in LAYER_4_KEYWORDS:
        if kw in text:
            scores[4] += 1
    for kw in LAYER_5_KEYWORDS:
        if kw in text:
            scores[5] += 1

    # Find best content layer
    max_score = max(scores)
    if max_score > 0:
        content_layer = scores.index(max_score)
    else:
        content_layer = None

    # Use prefix hint
    prefix_hint = PREFIX_LAYER.get(prefix)

    if prefix_hint is not None:
        # Blend: if content strongly points to a lower layer, trust content
        if content_layer is not None and content_layer < prefix_hint and max_score >= 2:
            return content_layer
        return prefix_hint
    else:
        # 1_PRD - use content, default layer 1
        if content_layer is not None:
            return content_layer
        # Fallback based on category
        cat = req.get("category", "")
        if cat == "constraint":
            return 1
        return 2


def is_e2e_testable(req):
    """Determine if this requirement can be validated by an E2E test."""
    cat = req.get("category", "")
    text = (req.get("title", "") + " " + req.get("description", "")).lower()

    # Non-functional constraints about code structure are usually not e2e testable
    if cat == "constraint":
        # Some constraints ARE testable (e.g., FIFO order, heartbeat)
        testable_constraint_kws = [
            "fifo", "heartbeat", "ping", "keyframe", "sample rate",
            "frame rate", "must not", "must be", "maximum", "minimum",
            "latency", "timeout", "retry", "limit",
        ]
        for kw in testable_constraint_kws:
            if kw in text:
                return True
        # Non-goal constraints are not testable
        if "non-goal" in text or "out of scope" in text or "not permitted" in text:
            return False
        # Code structure constraints
        structure_kws = [
            "must not use", "hand-written", "directory structure",
            "crate", "workspace", "cargo", "rust edition",
        ]
        for kw in structure_kws:
            if kw in text:
                return False
        return True

    if cat == "non-functional":
        # Performance, monitoring requirements are testable
        nf_testable = [
            "latency", "throughput", "frame rate", "memory", "cpu",
            "response time", "benchmark", "performance", "metric",
        ]
        for kw in nf_testable:
            if kw in text:
                return True
        return False

    if cat in ("functional", "interface"):
        return True

    return True


def get_prefix_deps(prefix, all_ids_by_prefix):
    """Get IDs of requirements that this prefix depends on."""
    # Dependency chain: 1_PRD -> nothing
    # 2_TAS depends on 1_PRD constraints
    # 3_MCP_DESIGN depends on 2_TAS, 1_PRD
    # 5_SECURITY depends on 1_PRD, 2_TAS
    # 6_UI_UX depends on 1_PRD, 2_TAS, 4_USER_FEATURES
    # 7_UI_UX_DES depends on 6_UI_UX
    # 4_USER_FEATURES depends on 1_PRD
    # 8B_PERFORMANCE_SPEC depends on 2_TAS
    # 8_RISKS_MITIGATION depends on 1_PRD
    # 9_PROJECT_ROADMAP depends on everything

    # We just return empty deps - order is handled by layer assignment
    # Individual deps would be too complex to compute accurately
    return []


def main():
    print("Loading requirements.json...")
    with open(INPUT) as f:
        data = json.load(f)

    reqs = data["requirements"]
    print(f"Loaded {len(reqs)} requirements")

    # Assign layers
    layered = []
    for req in reqs:
        layer = score_layer(req)
        e2e = is_e2e_testable(req)
        layered.append((layer, req, e2e))

    # Sort: by layer, then by prefix (to group related reqs), then by ID number
    def sort_key(item):
        layer, req, _ = item
        req_id = req["id"]
        prefix = req_id.split("-REQ-")[0]
        num_str = req_id.split("-REQ-")[1]
        num = int(num_str)
        return (layer, prefix, num)

    layered.sort(key=sort_key)

    # Build output requirements
    output_reqs = []
    for order_idx, (layer, req, e2e) in enumerate(layered, start=1):
        out = {
            "id": req["id"],
            "title": req["title"],
            "description": req["description"],
            "category": req.get("category", "functional"),
            "priority": req.get("priority", "must"),
            "source_documents": req.get("source_documents", []),
            "order": order_idx,
            "depends_on_requirements": [],
            "e2e_testable": e2e,
        }
        output_reqs.append(out)

    output = {
        "version": 1,
        "ordering_strategy": "Layered topological ordering: Layer 0 (Infrastructure) -> Layer 1 (Core Services) -> Layer 2 (Business Logic) -> Layer 3 (Integration) -> Layer 4 (User-Facing) -> Layer 5 (Polish/Performance). Within each layer, requirements are grouped by source document prefix and ordered by requirement number.",
        "requirements": output_reqs,
    }

    print(f"Writing {len(output_reqs)} ordered requirements...")
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Done. Written to {OUTPUT}")

    # Print layer distribution
    from collections import Counter
    layer_counts = Counter(layer for layer, _, _ in layered)
    for l in sorted(layer_counts):
        print(f"  Layer {l}: {layer_counts[l]} requirements")
    e2e_count = sum(1 for _, _, e2e in layered if e2e)
    print(f"  E2E testable: {e2e_count}/{len(layered)}")


if __name__ == "__main__":
    main()
