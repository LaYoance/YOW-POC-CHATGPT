# Project: YOW-ChatGPT-Integration-POC-By-Antigravity

## Architecture
Autonomous local Proof-of-Concept (POC) enabling discovery, inspection, and seamless continuation of ChatGPT account projects and conversations from an independent local interface, with strict bidirectional synchronization and restart resilience.

### System Architecture
The application is structured into four cleanly decoupled tiers in Python 3.12 (FastAPI + SQLite + Playwright/Chromium + curl_cffi):
1. **Presentation Layer**: Autonomous Single-Page Web UI (Vanilla JS + CSS, zero build step) and FastAPI REST / Server-Sent Events (SSE) streaming API.
2. **Core Business Services**: SessionManager (R1, A7), ProjectService (R2, A5), ConversationService (R2, A2), SyncEngine (A3, A4, A8).
3. **Transport Layer (R4)**: `ChatGPTTransport` Abstract Base Class with:
   - Mode 1: `DIRECT_HTTP` (curl_cffi with Chrome 124 TLS/JA4 impersonation)
   - Mode 2: `BROWSER_FETCH` (Native Chromium context evaluation via Playwright/CDP, bypassing Cloudflare Turnstile & JA3/JA4)
   - Mode 3: `UI_DRIVEN` (DOM-level interaction fallback via `#prompt-textarea`, send button, MutationObserver)
   - `TransportRouter` with automatic failover and manual mode override.
4. **Local Persistence Layer (R3, A6)**: SQLite in WAL mode with recursive CTE active branch reconstruction, providing sub-20ms cold recovery, strict offline caching, and zero duplication.

### Code Layout
```
/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI entrypoint & lifespan lifecycle
│   ├── config.py                   # Pydantic Settings (paths, DB, timeouts, ports)
│   ├── models/                     # Domain & DB schemas
│   │   ├── __init__.py
│   │   ├── session.py              # Session schemas & states (VALID, EXPIRED, MISSING)
│   │   ├── project.py              # Project and file metadata schemas
│   │   ├── conversation.py         # Conversation & Message DAG models
│   │   └── transport.py            # Transport types & audit records
│   ├── persistence/                # SQLite Repositories
│   │   ├── __init__.py
│   │   ├── db.py                   # SQLite connection, WAL pragma, schema init
│   │   ├── session_repo.py
│   │   ├── project_repo.py
│   │   ├── conversation_repo.py
│   │   └── transport_log_repo.py
│   ├── transports/                 # Transport Layer Implementations (R4)
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract Base Class: ChatGPTTransport
│   │   ├── direct_http.py          # Mode 1: curl_cffi / Direct HTTP
│   │   ├── browser_fetch.py        # Mode 2: Playwright CDP browser-context fetch
│   │   ├── ui_driven.py            # Mode 3: DOM automation fallback
│   │   └── router.py               # TransportRouter & automatic fallback orchestrator
│   ├── services/                   # Core Business Services
│   │   ├── __init__.py
│   │   ├── session_service.py      # Session extraction, validation, reconnect guides
│   │   ├── project_service.py      # Projects cache & instructions lookup
│   │   ├── conversation_service.py # Tree parsing, active branch resolution
│   │   └── sync_service.py         # Bidirectional sync & deduplication engine
│   ├── api/                        # REST & SSE Controllers
│   │   ├── __init__.py
│   │   ├── routes_session.py
│   │   ├── routes_projects.py
│   │   ├── routes_conversations.py
│   │   ├── routes_chat.py          # SSE streaming endpoint (/api/conversations/{id}/chat)
│   │   └── routes_transport.py     # Transport status & force-mode selector
│   └── static/                     # Autonomous Frontend (No build step required)
│       ├── index.html              # Single Page Application
│       ├── css/style.css           # Styling
│       └── js/app.js               # Reactive vanilla JS UI & EventSource stream handler
├── tests/                          # Comprehensive E2E & Unit Test Suite
│   ├── __init__.py
│   ├── conftest.py                 # Pytest fixtures, mock server, test DB
│   ├── mocks/                      # Simulated ChatGPT Backend API
│   │   ├── __init__.py
│   │   └── fake_chatgpt_server.py  # Standalone mock server returning realistic SSE & JSON
│   ├── test_tier1_features/        # >= 5 tests per feature (50 tests)
│   ├── test_tier2_boundaries/      # >= 5 edge/corner cases per feature (50 tests)
│   ├── test_tier3_combinations/    # Pairwise & cross-feature tests
│   └── test_tier4_scenarios/       # Full end-to-end user workflows
├── requirements.txt                # Locked Python dependencies
├── run.sh                          # One-click startup script
├── README.md                       # Complete documentation
├── RESEARCH.md                     # Research notes & transport capability matrix
├── PROGRESS.md                     # Project progress journal
└── VERIFICATION.md                 # Verification report with proofs for A1-A8
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F01 | Session Lifecycle & Isolation | NextAuth cookie parsing, JWT extraction, state machine (VALID, EXPIRED, MISSING), 0700 local file isolation | M1 | ORIGINAL_REQUEST R1, A7; Survey |
| F02 | Multi-Transport Engine & Router | ChatGPTTransport ABC, Mode 1 (DIRECT_HTTP), Mode 2 (BROWSER_FETCH), Mode 3 (UI_DRIVEN), TransportRouter failover | M2 | ORIGINAL_REQUEST R4; Survey |
| F03 | Project Discovery & Metadata | Snorlax sidebar listing (`/backend-api/gizmos/snorlax/sidebar`), project instructions, attached files metadata caching | M3 | ORIGINAL_REQUEST R2, A1, A5; Survey |
| F04 | Conversation Listing & Pagination | Global & project-scoped conversation pagination (`offset`/`limit`/`order=updated`), DB synchronization | M3 | ORIGINAL_REQUEST R2, A1; Survey |
| F05 | Tree Traversal & Active Branch | DAG mapping graph parsing, reverse parent-pointer traversal from `current_node`, active branch isolation | M3 | ORIGINAL_REQUEST R2, A2; Survey |
| F06 | Bidirectional Messaging & Streaming | POST `/backend-api/conversation` payload, SSE stream cumulative delta extraction, current_node leaf update | M4 | ORIGINAL_REQUEST R3, A3, A8; Survey |
| F07 | Bidirectional Sync & Deduplication | Site -> POC and POC -> Site synchronization, differential ingestion, 3-cycle anti-duplication invariants | M4 | ORIGINAL_REQUEST R2, R3, A4, A8; Survey |
| F08 | Project Context & Witness Verification | gizmo_id binding, system instruction injection, Witness Marker A & Marker B verification, negative controls | M4 | ORIGINAL_REQUEST R3, A5; Survey |
| F09 | SQLite Persistence & Restart Resilience | SQLite WAL schema, sub-20ms cold recovery, zero network startup, exact active thread restoration | M1 | ORIGINAL_REQUEST R3, A6; Survey |
| F10 | Autonomous Web UI & Dashboard | Vanilla JS Single Page App, conversation drawer, active branch view, SSE streaming display, badges, reconnect modal | M5 | ORIGINAL_REQUEST R5; Survey |
| F11 | High-Fidelity Test Mock Server | Offline mock server (`fake_chatgpt_server.py`) mimicking `/api/auth/session`, `/backend-api/*` endpoints | E2E Testing Track | Survey & Test Design |
| F12 | Project Documentation & Tooling | `README.md`, `RESEARCH.md`, `PROGRESS.md`, `run.sh` one-click script, `VERIFICATION.md` | M5 & M6 | ORIGINAL_REQUEST Deliverables |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Core Foundation & Session Engine | SQLite WAL database engine, repositories, session manager with local isolation (`.session_data/`), token lifecycle | none | IN_PROGRESS |
| 2 | Multi-Transport Routing Layer | `ChatGPTTransport` ABC, `DirectHttpTransport`, `BrowserFetchTransport`, `UiDrivenTransport`, `TransportRouter` | M1 | PLANNED |
| 3 | Project & Conversation Explorer | Project discovery, metadata/instructions, conversation listing with pagination, DAG mapping tree traversal | M1, M2 | PLANNED |
| 4 | Bidirectional Streaming & Sync Engine | SSE streaming client, message sending with gizmo_id, bidirectional sync engine, witness markers, 3-cycle anti-duplication | M1, M2, M3 | PLANNED |
| 5 | Autonomous Web UI & Operational Tooling | Vanilla JS UI, FastAPI REST & SSE endpoints, status badges, reconnect modal, `run.sh`, `README.md`, `RESEARCH.md` | M1-M4 | PLANNED |
| 6 | Final Milestone: E2E Test Pass & Audit | Pass 100% of E2E tests (Tiers 1-4), Tier 5 adversarial hardening, forensic integrity audit, `VERIFICATION.md` | M1-M5, E2E Testing Track | PLANNED |

## Parallel Track: E2E Testing Track
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Test Suite (Tiers 1-4) | `fake_chatgpt_server.py`, test harness, 50 Tier 1 tests, 50 Tier 2 tests, 15 Tier 3 tests, 5 Tier 4 tests, `TEST_READY.md` | none | PLANNED |

## Interface Contracts

### 1. Persistence & Repositories ↔ Core Services
- `SessionRepo`:
  - `get_default() -> Optional[Session]`
  - `save(session: Session) -> Session`
  - `update_status(status: SessionStatus)`
- `ProjectRepo`:
  - `upsert_project(project: Project) -> Project`
  - `list_projects() -> List[Project]`
  - `get_project(id: str) -> Optional[Project]`
- `ConversationRepo`:
  - `upsert_conversation(conv: Conversation) -> Conversation`
  - `list_conversations(project_id: Optional[str] = None, offset: int = 0, limit: int = 28) -> Tuple[List[Conversation], int]`
  - `get_conversation(id: str) -> Optional[Conversation]`
  - `save_messages(messages: List[Message])`
  - `get_active_branch(conversation_id: str) -> List[Message]`
  - `update_current_node(conversation_id: str, current_node_id: str)`
- `TransportLogRepo`:
  - `log_operation(log: TransportAuditLog)`

### 2. Transport Layer ↔ Services Layer
- `ChatGPTTransport` (ABC):
  - `check_session() -> SessionInfo`
  - `fetch_projects(cursor: Optional[str] = None) -> ProjectListResult`
  - `fetch_project_detail(project_id: str) -> ProjectDetailResult`
  - `fetch_conversations(project_id: Optional[str] = None, offset: int = 0, limit: int = 28) -> ConversationListResult`
  - `fetch_conversation_tree(conversation_id: str) -> ConversationTreeResult`
  - `send_message_stream(request: SendMessageRequest) -> AsyncIterator[SSEChunk]`
- `TransportRouter`:
  - `execute_with_fallback(operation_name: str, fn: Callable[[ChatGPTTransport], Awaitable[T]]) -> T`
  - `execute_stream_with_fallback(request: SendMessageRequest) -> AsyncIterator[SSEChunk]`
  - `set_forced_mode(mode: Optional[TransportMode])`
  - `get_current_status() -> TransportStatusReport`

### 3. Core Services ↔ Presentation API
- `SessionService`:
  - `get_current_session_status() -> SessionStatusResponse`
  - `import_session_cookies(cookies_raw: str) -> SessionStatusResponse`
  - `start_reconnect_flow() -> ReconnectFlowStatus`
- `ProjectService`:
  - `list_cached_projects() -> List[ProjectSchema]`
  - `sync_projects_from_upstream() -> List[ProjectSchema]`
  - `get_project_with_context(project_id: str) -> ProjectContextSchema`
- `ConversationService`:
  - `list_conversations(project_id: Optional[str], page: int, page_size: int) -> PaginatedConversations`
  - `get_active_branch_history(conversation_id: str) -> ActiveBranchResponse`
  - `sync_conversation_history(conversation_id: str) -> ActiveBranchResponse`
- `SyncService`:
  - `send_and_stream(conversation_id: Optional[str], project_id: Optional[str], prompt: str) -> AsyncIterator[StreamEvent]`
  - `poll_site_updates() -> SyncReport`
  - `verify_3cycle_continuity(conversation_id: str) -> ContinuityVerificationResult`
