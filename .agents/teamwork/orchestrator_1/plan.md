# Orchestrator Plan — Autonomous ChatGPT Integration POC

## Mission
Deliver a complete, robust, verified local autonomous POC for ChatGPT integration (R1-R5, A1-A8, and all deliverables specified in ORIGINAL_REQUEST.md).

## Phase 0: Survey & Specification Mining
- Spawn 3 parallel investigators:
  1. `teamwork_preview_spec_miner` (Survey 1): ChatGPT Backend-API endpoints, session extraction, conversation trees, project/gizmo endpoints.
  2. `teamwork_preview_explorer` (Survey 2): Transport evaluation (DIRECT_HTTP vs BROWSER_FETCH vs UI_DRIVEN), Cloudflare/TLS protections, session handling.
  3. `teamwork_preview_explorer` (Survey 3): POC architecture, project context/files (A5), local persistence, UI design, restart resilience, testing strategy.
- Merge findings into `PROJECT.md` (Feature Inventory, Architecture, Interfaces, Code Layout) and initialize `RESEARCH.md`.

## Phase 1: Dual Track Decomposition
- Implementation Track:
  - Milestone 1: Session Management & Multi-Transport Engine (DIRECT_HTTP, BROWSER_FETCH, UI_DRIVEN)
  - Milestone 2: Project & Conversation Explorer (Discovery, pagination, tree navigation, active branch)
  - Milestone 3: Bidirectional Streaming & Continuation Engine (Send, stream SSE, project instructions/files, state sync)
  - Milestone 4: Autonomous UI & Local Storage (Web UI, transport status indicators, restart persistence)
  - Final Milestone: Pass 100% E2E Tests (Tiers 1-4) & Adversarial Hardening (Tier 5)
- E2E Testing Track:
  - Design opaque-box test suite for A1-A8 and R1-R5 (Tiers 1-4)
  - Publish `TEST_READY.md`

## Phase 2: Execution & Verification
- Dispatch Sub-Orchestrators for milestones
- Iteration loops: Explorer -> Worker -> Reviewer -> Challenger -> Forensic Auditor
- Continuous documentation updates (`README.md`, `RESEARCH.md`, `PROGRESS.md`, `VERIFICATION.md`)

## Phase 3: Victory Audit & Final Handover
- Execute forensic victory verification against A1-A8
- Produce `VERIFICATION.md` with concrete evidence
- Deliver victory claim to Sentinel
