# Architectural Decisions

Durable decisions that agents must respect. Split from `MEMORY.md` because decisions
are stable across many tasks while the changelog is ephemeral.

*Self-maintenance: Mark superseded decisions as `[SUPERSEDED by <decision>]` rather
than deleting them, so agents can understand why the current approach was chosen.*

---

## Interface Stability

Interfaces marked **stable** must not be refactored without explicit human approval.
Interfaces marked **in-flux** may be changed by any task that has good reason.

| Interface / Module | Status | Notes |
|--------------------|--------|-------|
| (none yet) | | |

---

## Cross-Cutting Invariants

Rules that apply globally, regardless of which task you are implementing.
Violating these will fail review even if presubmit passes.

- (none yet — add entries like: "All authentication goes through `auth::middleware`")

---

## Known Presubmit Tripwires

Patterns that reliably fail `./do presubmit`. Knowing these upfront saves retry cycles.

- (none yet — add entries like: "`clippy` denies `unwrap()` outside `#[cfg(test)]`")

---

## Concept → File Path Index

As the codebase grows, use this table to navigate without wasting tokens on `find`/`ls`.

| Domain Concept | Primary File(s) |
|----------------|-----------------|
| (none yet) | |

---

## Dependency Pins

Key dependencies and *why* they are at their current version. Do not upgrade or
replace these without understanding the reason.

| Dependency | Version | Why pinned |
|------------|---------|-----------|
| (none yet) | | |
