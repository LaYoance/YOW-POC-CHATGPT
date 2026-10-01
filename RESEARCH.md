# Research Notes & Transport Capability Matrix

## Executive Summary
This document consolidates research and technical investigations conducted for the ChatGPT Integration POC, focusing on the reverse-engineered specifications of ChatGPT Web Backend-API, Cloudflare anti-bot mechanisms, local session security, and transport evaluation across `DIRECT_HTTP`, `BROWSER_FETCH`, and `UI_DRIVEN`.

---

## 1. Transport Capabilities Matrix (R4)

| Capability / Operation | Mode 1: `DIRECT_HTTP` (`curl_cffi`) | Mode 2: `BROWSER_FETCH` (CDP / Playwright) | Mode 3: `UI_DRIVEN` (DOM Automation) |
|---|:---:|:---:|:---:|
| **Project Discovery** (`/backend-api/gizmos/snorlax/sidebar`) | **PARTIAL**<br>(Requires pre-warmed cookies; blocks on CF Turnstile challenge) | **FULL**<br>(Native cookies, instant JSON response) | **PARTIAL**<br>(Scrapes sidebar DOM; fragile to CSS changes) |
| **Conversation Listing** (`/backend-api/conversations`) | **PARTIAL**<br>(Fast offset/limit pagination; fails if cookies expire) | **FULL**<br>(Native cookies, clean JSON pagination) | **POOR**<br>(Requires infinite scroll; virtual DOM drops items) |
| **History & Tree Traversal** (`/backend-api/conversation/{id}`) | **PARTIAL**<br>(Retrieves complete DAG mapping) | **FULL**<br>(Retrieves complete DAG mapping + active branch) | **POOR**<br>(Only sees rendered branch; cannot traverse alternate branches) |
| **Send Message** (`POST /backend-api/conversation`) | **BLOCKED / FRAGILE**<br>(Blocked by Sentinel PoW / Turnstile) | **FULL**<br>(Native page context solves PoW / Sentinel) | **FULL**<br>(Types into `#prompt-textarea`, clicks send button) |
| **Real-Time Streaming** (SSE) | **PARTIAL**<br>(HTTP chunked stream if message send passes) | **FULL**<br>(`ReadableStream` piped via CDP binding) | **FULL**<br>(`MutationObserver` on assistant turn) |
| **Project Context & Files** (`gizmo_id` & file IDs) | **PARTIAL**<br>(Payload includes `gizmo_id`; file download complex) | **FULL**<br>(Payload includes `gizmo_id` + native cookies) | **FULL**<br>(Navigates to `chatgpt.com/g/p-...`, attaches via UI) |
| **Anti-Bot Resilience** | **LOW** (Breaks on Turnstile / PoW updates) | **MAXIMUM** (100% native browser stack) | **MAXIMUM** (Real user interaction simulation) |
| **Execution Latency** | **LOW** (~150-300ms) | **LOW-MEDIUM** (~200-450ms) | **HIGH** (~1200-3000ms DOM overhead) |
| **Recommended Operational Role** | High-speed read caching when cookies warm | **Primary Transport** for reading and writing | **Resilient Fallback** for message transmission |

---

## 2. Authentication & Session Lifecycle (R1, A7)

### 2.1 NextAuth Web Session Mechanism
- ChatGPT web uses NextAuth.js with session cookies:
  - `__Secure-next-auth.session-token`: Root session cookie holding encrypted JWT.
  - `cf_clearance`: Cloudflare Turnstile clearance token tied to client IP and TLS fingerprint.
  - `oai-device-id`: UUIDv4 tracking client device identity.
- Token refresh:
  - `GET https://chatgpt.com/api/auth/session` returns current user profile and a fresh `accessToken` JWT (~10 days validity).
  - Even if `accessToken` expires (giving HTTP 401 on `/backend-api/*`), calling `/api/auth/session` with the session cookie automatically re-issues a new valid `accessToken`.

### 2.2 Local Security Isolation (R1)
- Session storage is strictly confined to `<workspace>/.session_data/`:
  - Chromium browser profile stored in `.session_data/chromium/` with filesystem mode `0700`.
  - SQLite database stored in `.session_data/poc.db` with mode `0600`.
  - No session tokens or cookies are ever committed, logged, or proxied to third-party servers.
  - `.session_data/` and `*.db` are registered in `.gitignore`.

### 2.3 Session State Machine & Reconnection (A7)
- States: `VALID`, `EXPIRED`, `MISSING`, `CHALLENGE_REQUIRED`.
- On 401 Unauthorized: transition to `EXPIRED`.
- On Cloudflare 403 / Turnstile: transition to `CHALLENGE_REQUIRED`.
- Reconnect wizard: launches headed Chromium window to `https://chatgpt.com`, polls `/api/auth/session` with a 300-second bounded timeout (preventing infinite loops or crashes), re-acquires tokens, and closes the browser cleanly.

---

## 3. ChatGPT Conversation DAG & Tree Traversal Algorithm (R2, A2)

### 3.1 Mapping Structure
- `GET /backend-api/conversation/{id}` returns a JSON structure containing:
  - `mapping`: A dictionary mapping node IDs (UUIDs) to node objects.
  - `current_node`: Leaf UUID pointing to the tip of the active branch.
- Each node has:
  - `id`: Node UUID.
  - `parent`: UUID of parent node (or `null` for root).
  - `children`: List of child node UUIDs.
  - `message`: Object containing `author`, `content.parts`, `metadata`, `status`, etc. (or `null` for synthetic root).

### 3.2 Active Branch Traversal
To extract the exact active branch without flattening or omitting turns:
1. Start at `curr_id = current_node`.
2. Follow `node.parent` pointers backwards until `node.parent is None`.
3. Collect non-hidden message objects.
4. Reverse the list to restore chronological order (root to leaf).
5. When submitting a new message, set `parent_message_id = current_node`. This attaches the new message directly to the leaf, preventing orphan branches and duplicated threads.

---

## 4. Project Context & Witness Verification (R3, A5)

### 4.1 Gizmo Architecture
- ChatGPT Projects are identified by `gizmo_id` with prefix `g-p-` (e.g. `g-p-674b8892abcdef`).
- Project conversations must be submitted with:
  ```json
  "conversation_mode": {
    "kind": "gizmo_interaction",
    "gizmo_id": "g-p-674b8892abcdef"
  }
  ```
- This activates custom instructions and project knowledge files on OpenAI's backend.

### 4.2 Witness Verification Protocol
- **Instruction Marker A**: Project custom instructions dictate prefixing responses with `[ALPHA-PROJECT-ACK]` and secret token `PRJ-TOKEN-7719`.
- **Knowledge File Marker B**: Attached file contains distinct secret canary `CANARY-OMEGA-9921-X`.
- **Assertion**: When sending a neutral prompt from the POC, the streaming response must include Marker A and Marker B. Negative control outside of the project confirms their absence.

---

## 5. Offline Test Strategy & Mock Architecture

- To enable completely reproducible, sandboxed, and deterministic verification of all features (Tiers 1-4, A1-A8), the POC includes `FakeChatGPTServer` (`tests/mocks/fake_chatgpt_server.py`).
- The mock server faithfully replicates:
  - NextAuth session exchanges (`/api/auth/session`).
  - Snorlax sidebar projects discovery (`/backend-api/gizmos/snorlax/sidebar`).
  - Conversation listing and pagination (`/backend-api/conversations`).
  - Graph mapping with branching trees (`/backend-api/conversation/{id}`).
  - Server-Sent Events (SSE) streaming with cumulative `parts[0]` updates (`/backend-api/conversation`).
  - Sentinel PoW challenges and simulated Cloudflare status codes.
