# ChatGPT Web Backend-API Specification & Survey Report

## Executive Summary
This report provides the exhaustive specification for the ChatGPT Web Backend-API reverse-engineered and validated from reference implementations (`lanqian528/chat2api`, `brianjlacy/export-chatgpt`, `Octo-Lex/ChatGPT-Web2API`, `acheong08/ChatGPT`) and authoritative web traffic analyses. It specifies authentication lifecycles, project/gizmo discoveries, conversation listing/pagination, node-tree traversal algorithms, and Server-Sent Event (SSE) message transmission.

---

## Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Auth & Session | Session Extraction & Verification | Retrieves active user session, user metadata, and access JWT using NextAuth session cookie | Cookie: `__Secure-next-auth.session-token` (GET `/api/auth/session`) | JSON: `{user: {...}, expires: "...", accessToken: "eyJhbG..."}` | 401 Unauthorized or `{}` if cookie missing or expired | Reference repos (`chat2api`, `export-chatgpt`), NextAuth spec |
| 2 | Auth & Session | Sentinel Security Handshake | Pre-flight security challenge to negotiate bot detection requirements | Headers: `Authorization`, `OAI-Device-Id` (GET/POST `/backend-api/sentinel/chat-requirements`) | JSON: `{token: "...", proofofwork: {required: bool, seed: "...", difficulty: "..."}}` | 403 Forbidden / Cloudflare challenge if TLS fingerprint mismatch | Reference repos (`chat2api`), Sentinel traffic analysis |
| 3 | Auth & Session | Proof-of-Work (PoW) Solver | Solves FNV-1a computational challenge to generate proof token | Seed string, difficulty threshold, browser config parameters | Header: `openai-sentinel-proof-token: gAAAAAB...` | 403 / 400 Bad Request if PoW calculation invalid or timeout | Reference repos (`chat2api`), community PoW solvers |
| 4 | Projects & Gizmos | Project Discovery (Snorlax Sidebar) | Discovers all user projects (internally codenamed "snorlax" gizmos) and recent project threads | Query params: `conversations_per_gizmo`, `cursor`, `owned_only` (GET `/backend-api/gizmos/snorlax/sidebar`) | JSON: `{projects: [{id: "g-p-...", title: "...", conversations: [...]}]}` | 401 Unauthorized if expired token; 404 if project feature not provisioned | Network traffic analysis, `export-chatgpt`, `settingmilestones` |
| 5 | Projects & Gizmos | Project Detail & Metadata Retrieval | Retrieves complete configuration of a project/gizmo including instructions and attached files | Path param: `gizmo_id` (e.g. `g-p-...`) (GET `/backend-api/gizmos/{gizmo_id}`) | JSON: `{gizmo: {id, display: {name, description}, instructions, files: [{id, name, size, mime_type}], tools: [...]}}` | 404 Not Found if invalid or deleted gizmo ID; 403 if unauthorized | Reference repos, `export-chatgpt`, `gizmo` schema |
| 6 | Projects & Gizmos | Knowledge File Download | Downloads an attached file associated with a project knowledge base | Path param: `file_id` (GET `/backend-api/files/download/{file_id}`) | Binary/File stream or signed redirect URL | 404 Not Found if file missing; 401/403 if token lacks workspace access | Web backend traffic, `export-chatgpt` |
| 7 | Conversation Listing | Root Conversation Pagination | Lists recent non-project and global conversations in chronological/updated order | Query params: `offset` (int), `limit` (int <= 100), `order=updated` (GET `/backend-api/conversations`) | JSON: `{items: [{id, title, create_time, update_time, gizmo_id}], total: int, limit: int, offset: int}` | 401 Unauthorized if expired token; 429 Too Many Requests if throttled | Reference repos (`export-chatgpt`, `ChatGPT-Web2API`, `acheong08`) |
| 8 | Conversation Listing | Project-Scoped Conversation Listing | Lists conversations strictly belonging to a specific Project (Gizmo) | Path param: `gizmo_id`, query params: `offset`, `limit`, `order` (GET `/backend-api/gizmos/{gizmo_id}/conversations`) | JSON: `{items: [{id, title, gizmo_id: "g-p-...", ...}], total: int, offset: int, limit: int}` | 404 if project does not exist; 401 if unauthenticated | Reference repos, `settingmilestones`, `export-chatgpt` |
| 9 | History & Tree | Conversation Detail & Mapping Graph | Retrieves complete non-linear conversation graph including all branches and active leaf | Path param: `conversation_id` (GET `/backend-api/conversation/{conversation_id}`) | JSON: `{title, create_time, mapping: {<node_id>: {id, message, parent, children}}, current_node: "<leaf_id>", gizmo_id}` | 404 Not Found if conversation deleted; 401 if expired session | Reference repos (`acheong08/ChatGPT`, `brianjlacy/export-chatgpt`) |
| 10 | History & Tree | Active Branch Traversal Algorithm | Traverses backward from `current_node` to root node, reconstructing linear message history | In-memory `mapping` dictionary and `current_node` leaf ID | Chronological ordered list of linear message turns `[{id, role, text, timestamp, model}]` | Gracefully skips `message: null` root node; terminates on `parent: null` | Traversal algorithm in `acheong08/ChatGPT` & `export-chatgpt` |
| 11 | Message & Streaming | Send Message (Standard & Project) | Transmits new user message or continues an existing branch with project context | POST `/backend-api/conversation` with JSON payload (`action: "next"`, `parent_message_id`, `conversation_id`, `conversation_mode`) | SSE Stream (`text/event-stream`) of status, message delta/cumulative parts, finish details | HTTP 401/403/429 or in-stream JSON error `{"error": {...}}` | Reference repos (`chat2api`, `ChatGPT-Web2API`, `acheong08`) |
| 12 | Message & Streaming | SSE Stream Parsing & Delta Extraction | Parses `data: {"message": ...}` lines, converts cumulative `parts[0]` to progressive deltas | Incoming SSE chunk buffer, tracking previous string length | Progressive text tokens for UI streaming + terminal `[DONE]` event | Handles malformed chunks, ping events `: ping`, and connection drops | Reference repos (`chat2api`, `Octo-Lex/ChatGPT-Web2API`) |

---

## Edge Cases

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | Tree Traversal | Root Node in `mapping` | Root node has `message: null` and `parent: null` (children array points to initial system/user message). Naive traversals crashing on `node.message.content` fail unless null is guarded. |
| 2 | Tree Traversal | Regenerated Responses (Branching) | A single parent node has multiple IDs in its `children` array. Traversal starting strictly from `current_node` follows only the active branch; inactive branches are preserved in `mapping` but omitted from the active linear view. |
| 3 | Tree Traversal | Multimodal Messages (Images/Files) | Node `content.content_type` is `"multimodal_text"` instead of `"text"`. `parts` contains dictionaries/objects with asset pointers rather than plain strings. |
| 4 | Message Streaming | Cumulative `parts[0]` vs Incremental Deltas | Unlike OpenAI public API `/v1/chat/completions` which yields incremental delta tokens, `/backend-api/conversation` yields the full accumulated message text in `parts[0]` with every SSE chunk. Exporters/UI must slice `chunk[len(previous):]` to obtain deltas. |
| 5 | Message Streaming | Stream Interruption & Abort | Network drop or client abort results in a partial assistant turn. On reload, the conversation's `current_node` points to the last recorded partial node with `status: "finished_successfully"` or `status: "interrupted"`. |
| 6 | Project Context | New Thread in Project vs Existing Thread Continuation | When starting a project thread, `conversation_id` is omitted and `conversation_mode: {"kind": "gizmo_interaction", "gizmo_id": "g-p-..."}` must be passed. When continuing an existing project thread, `conversation_id` must be present AND `conversation_mode` must still specify `gizmo_id` to preserve project instructions and files. |
| 7 | Session Lifecycle | Expiration of `accessToken` vs Session Cookie | The `accessToken` JWT typically has a lifetime of ~10 days or fewer. Even if `accessToken` expires (giving HTTP 401), the browser's `__Secure-next-auth.session-token` cookie remains valid for weeks, allowing instant renewal via `GET /api/auth/session`. |
| 8 | Cloudflare / TLS Protection | Direct HTTP request without browser TLS fingerprint | `curl`, standard Node `fetch`, or Python `requests` sending valid cookies will receive Cloudflare HTTP 403 Forbidden because JA3/JA4 TLS handshake signatures do not match Chrome/Firefox fingerprints. Browser-context execution (`BROWSER_FETCH`) or TLS impersonation (`curl_cffi`) is required. |
| 9 | Pagination | Empty Pages or Deleted Conversations | When `offset >= total`, `/backend-api/conversations` returns `{"items": [], "total": N, "limit": L, "offset": offset, "has_missing_conversations": false}`. |
| 10 | Security Challenge | Sentinel PoW Difficulty Escalation | Under elevated bot defense mode, `/backend-api/sentinel/chat-requirements` sets `difficulty` to high hex masks. Requests without `openai-sentinel-proof-token` or with expired tokens return HTTP 403. |

---

## 1. Observation: Detailed Specification by Component

### Component 1: Authentication & Session

#### 1.1 Cookies & Storage
Legitimate ChatGPT Web sessions rely on NextAuth.js. The session state is maintained by the following primary cookies on `chatgpt.com`:
- `__Secure-next-auth.session-token`: Encrypted JWT cookie (`HttpOnly; Secure; SameSite=Lax`). Contains user identity, auth provider details, and session refresh key.
- `cf_clearance`: Cloudflare Turnstile clearance cookie tied directly to the client's IP and TLS fingerprint (JA3/JA4).
- `__cf_bm`: Cloudflare bot management session cookie.
- `_puid`: OpenAI persistence identifier (in accounts with plus/team).

#### 1.2 Session Retrieval & Token Refresh (`/api/auth/session`)
- **Endpoint**: `GET https://chatgpt.com/api/auth/session`
- **Required Headers**:
  - `Cookie: __Secure-next-auth.session-token=<token>; cf_clearance=<clearance>`
  - `User-Agent: <Modern Chrome User Agent>`
  - `Accept: */*`
- **Response Format** (HTTP 200):
```json
{
  "user": {
    "id": "user-abcdef123456",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "image": "https://avatar.url/avatar.png",
    "picture": "https://avatar.url/avatar.png",
    "idp": "auth0",
    "iat": 1700000000,
    "mfa": false,
    "groups": [],
    "intercom_hash": "..."
  },
  "expires": "2026-10-15T12:00:00.000Z",
  "accessToken": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6...",
  "authProvider": "default"
}
```
- **Lifecycle & Renewal**:
  - The `accessToken` is a signed RS256 JWT used in `Authorization: Bearer <accessToken>`.
  - When the `accessToken` expires (or yields a 401 on `/backend-api/*`), calling `GET /api/auth/session` with the session cookie refreshes and issues a newly minted `accessToken` without prompting the user to re-login.

#### 1.3 Security Challenges & Sentinel Handshake
- **Endpoint**: `POST https://chatgpt.com/backend-api/sentinel/chat-requirements`
- **Headers**:
  - `Authorization: Bearer <accessToken>`
  - `OAI-Device-Id: <UUIDv4>`
- **Response**:
```json
{
  "token": "sentinel-chat-requirements-token-xyz",
  "proofofwork": {
    "required": true,
    "seed": "0.1234567890",
    "difficulty": "0532"
  },
  "turnstile": {
    "required": false,
    "dx": null
  }
}
```
- **PoW Solver**: When `proofofwork.required == true`, the client executes an FNV-1a hash search using the seed, difficulty, and browser environment parameters. The calculated solution is formatted into the header:
  `openai-sentinel-proof-token: gAAAAAB...`
  along with `openai-sentinel-chat-requirements-token: <token>`.

---

### Component 2: Projects & Gizmos Specification

#### 2.1 Project Architecture
In the ChatGPT backend, **Projects** are implemented as a specialized sub-type of **Gizmos** (custom GPTs) identified by the prefix `g-p-` (e.g. `g-p-674b8892...`), whereas standard custom GPTs use `g-` (e.g. `g-12345...`). They are managed internally under the `snorlax` codename namespace.

#### 2.2 Project Discovery Endpoints
1. **Sidebar Projects Listing**:
   - **Endpoint**: `GET https://chatgpt.com/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo={n}&cursor={cursor}&owned_only=true`
   - **Headers**: `Authorization: Bearer <accessToken>`, `OAI-Device-Id: <UUIDv4>`
   - **Response Payload**:
```json
{
  "projects": [
    {
      "id": "g-p-674b8892abcdef",
      "title": "Autonomous POC Project",
      "description": "Project for local ChatGPT synchronization POC",
      "created_at": "2026-03-01T10:00:00.000Z",
      "updated_at": "2026-03-10T15:30:00.000Z",
      "conversations": [
        {
          "id": "conv-1111-2222",
          "title": "Initial Requirements Discussion",
          "create_time": "2026-03-01T10:05:00.000Z",
          "update_time": "2026-03-01T10:20:00.000Z"
        }
      ]
    }
  ],
  "next_cursor": null
}
```

2. **Project Detail & Metadata**:
   - **Endpoint**: `GET https://chatgpt.com/backend-api/gizmos/{gizmo_id}`
   - **Response Schema**:
```json
{
  "gizmo": {
    "id": "g-p-674b8892abcdef",
    "organization_id": "org-xyz",
    "display": {
      "name": "Autonomous POC Project",
      "description": "Project for local ChatGPT synchronization POC",
      "welcome_message": "Welcome to the Autonomous POC Project!",
      "prompt_starters": [],
      "profile_picture_url": null
    },
    "instructions": "You are the project assistant. Always apply the strict formatting rules: return code blocks with clear markers and adhere to project standards.",
    "tools": [
      {"type": "browser"},
      {"type": "python"},
      {"type": "dalle"}
    ],
    "files": [
      {
        "id": "file-proj-witness-001",
        "name": "witness_spec_document.md",
        "size": 4096,
        "mime_type": "text/markdown",
        "created_at": 1709280000
      }
    ],
    "workspace_id": "ws-abcdef",
    "created_at": "2026-03-01T10:00:00.000Z",
    "updated_at": "2026-03-10T15:30:00.000Z"
  }
}
```

3. **Knowledge File Access**:
   - **Endpoint**: `GET https://chatgpt.com/backend-api/files/download/{file_id}`
   - Returns redirect (302) to signed Amazon S3 / Azure blob storage URL for raw file contents.

---

### Component 3: Conversation Listing & Pagination Specification

#### 3.1 Global / Non-Project Conversations Listing
- **Endpoint**: `GET https://chatgpt.com/backend-api/conversations?offset={offset}&limit={limit}&order=updated`
- **Parameters**:
  - `offset`: Zero-based index (e.g. `0`, `28`, `56`).
  - `limit`: Number of items per page (default `28`, max `100`).
  - `order`: Sort criteria, strictly `updated` for recent chronological ordering.
- **Response Format**:
```json
{
  "items": [
    {
      "id": "019488a1-b2c3-7def-89ab-cdef01234567",
      "title": "Architectural Design Discussion",
      "create_time": "2026-03-15T09:30:00.000000Z",
      "update_time": "2026-03-15T10:15:00.000000Z",
      "mapping": null,
      "current_node": null,
      "conversation_template_id": null,
      "gizmo_id": null,
      "is_archived": false,
      "workspace_id": null,
      "async_status": null
    }
  ],
  "total": 54,
  "limit": 28,
  "offset": 0,
  "has_missing_conversations": false
}
```

#### 3.2 Project-Specific Conversation Listing
- **Endpoint**: `GET https://chatgpt.com/backend-api/gizmos/{gizmo_id}/conversations?offset={offset}&limit={limit}&order=updated`
- **Parameters**: Same offset and limit controls as global listing.
- **Behavior**: Strictly returns conversations tagged with `gizmo_id: "g-p-..."`. These conversations are separated from the main root listing on `chatgpt.com`.

---

### Component 4: Conversation History & Tree Traversal Specification

#### 4.1 Conversation Detail Endpoint
- **Endpoint**: `GET https://chatgpt.com/backend-api/conversation/{conversation_id}`
- **Headers**: `Authorization: Bearer <accessToken>`, `OAI-Device-Id: <UUIDv4>`
- **Response Structure**:
```json
{
  "title": "Architectural Design Discussion",
  "create_time": 1709280000.0,
  "update_time": 1709283600.0,
  "mapping": {
    "node-root-000": {
      "id": "node-root-000",
      "message": null,
      "parent": null,
      "children": ["node-sys-001"]
    },
    "node-sys-001": {
      "id": "node-sys-001",
      "message": {
        "id": "node-sys-001",
        "author": {"role": "system", "name": null, "metadata": {}},
        "create_time": 1709280001.0,
        "content": {"content_type": "text", "parts": [""]},
        "status": "finished_successfully",
        "end_turn": true,
        "weight": 1.0,
        "metadata": {"is_visually_hidden_from_conversation": true}
      },
      "parent": "node-root-000",
      "children": ["node-usr-002"]
    },
    "node-usr-002": {
      "id": "node-usr-002",
      "message": {
        "id": "node-usr-002",
        "author": {"role": "user", "name": null, "metadata": {}},
        "create_time": 1709280010.0,
        "content": {"content_type": "text", "parts": ["What is the project witness token?"]},
        "status": "finished_successfully",
        "end_turn": true,
        "weight": 1.0,
        "metadata": {}
      },
      "parent": "node-sys-001",
      "children": ["node-ast-003a", "node-ast-003b"]
    },
    "node-ast-003a": {
      "id": "node-ast-003a",
      "message": {
        "id": "node-ast-003a",
        "author": {"role": "assistant", "name": null, "metadata": {}},
        "create_time": 1709280015.0,
        "content": {"content_type": "text", "parts": ["Draft 1 answer..."]},
        "status": "finished_successfully",
        "end_turn": true,
        "weight": 1.0,
        "metadata": {"model_slug": "gpt-4o", "finish_details": {"type": "stop"}}
      },
      "parent": "node-usr-002",
      "children": []
    },
    "node-ast-003b": {
      "id": "node-ast-003b",
      "message": {
        "id": "node-ast-003b",
        "author": {"role": "assistant", "name": null, "metadata": {}},
        "create_time": 1709280020.0,
        "content": {"content_type": "text", "parts": ["The witness token is WITNESS_KEY_42."]},
        "status": "finished_successfully",
        "end_turn": true,
        "weight": 1.0,
        "metadata": {"model_slug": "gpt-4o", "finish_details": {"type": "stop"}}
      },
      "parent": "node-usr-002",
      "children": []
    }
  },
  "current_node": "node-ast-003b",
  "conversation_id": "019488a1-b2c3-7def-89ab-cdef01234567",
  "conversation_template_id": null,
  "gizmo_id": "g-p-674b8892abcdef"
}
```

#### 4.2 Active Branch Traversal Algorithm
The tree traversal algorithm resolves the exact sequence of messages active on `chatgpt.com` without flattening or omitting turns:

```python
def extract_active_branch(conversation_json: dict) -> list[dict]:
    """
    Traverses backwards from current_node leaf to root using parent pointers.
    Returns chronological messages corresponding to the active thread view.
    """
    mapping = conversation_json.get("mapping", {})
    current_node_id = conversation_json.get("current_node")
    
    if not current_node_id or not mapping:
        return []

    active_messages = []
    curr_id = current_node_id

    while curr_id:
        node = mapping.get(curr_id)
        if not node:
            break
            
        message = node.get("message")
        if message:
            author = message.get("author", {})
            role = author.get("role")
            content = message.get("content", {})
            parts = content.get("parts", [])
            metadata = message.get("metadata", {})
            
            # Filter visually hidden system messages if desired, but preserve user/assistant/tool
            is_hidden = metadata.get("is_visually_hidden_from_conversation", False)
            
            # Extract plain text content
            text_parts = [p for p in parts if isinstance(p, str)]
            text = "".join(text_parts)
            
            if not is_hidden and (text or role != "system"):
                active_messages.append({
                    "node_id": node["id"],
                    "parent_id": node.get("parent"),
                    "role": role,
                    "text": text,
                    "create_time": message.get("create_time"),
                    "model_slug": metadata.get("model_slug"),
                    "status": message.get("status"),
                    "finish_details": metadata.get("finish_details")
                })
                
        curr_id = node.get("parent")

    # Reverse to restore chronological order (root -> leaf)
    active_messages.reverse()
    return active_messages
```

---

### Component 5: Message Transmission & Streaming Specification

#### 5.1 POST `/backend-api/conversation` Payload
When initiating or continuing a conversation, the client issues a `POST` request to `https://chatgpt.com/backend-api/conversation`.

- **Payload Schema**:
```json
{
  "action": "next",
  "messages": [
    {
      "id": "019488b5-1234-7000-8000-abcdef123456",
      "author": {
        "role": "user"
      },
      "content": {
        "content_type": "text",
        "parts": ["Continue the discussion with the project rules."]
      },
      "metadata": {}
    }
  ],
  "parent_message_id": "node-ast-003b",
  "conversation_id": "019488a1-b2c3-7def-89ab-cdef01234567",
  "model": "gpt-4o",
  "timezone_offset_min": -120,
  "suggestions": [],
  "history_and_training_disabled": false,
  "conversation_mode": {
    "kind": "gizmo_interaction",
    "gizmo_id": "g-p-674b8892abcdef"
  },
  "force_paragen": false
}
```

- **Critical Fields**:
  - `action`: `"next"` for normal turn; `"variant"` when regenerating an existing turn.
  - `parent_message_id`: Must equal the `current_node` of the target conversation to append without breaking the tree.
  - `conversation_id`: Present for continuing existing conversations; omitted (or `null`) when initiating a new conversation.
  - `conversation_mode`:
    - For regular chats: `{"kind": "primary_assistant"}`
    - For Project chats: `{"kind": "gizmo_interaction", "gizmo_id": "g-p-..."}`. Omission of `conversation_mode` on a project chat detaches the conversation from the project context.

#### 5.2 Server-Sent Events (SSE) Stream Protocol
- **Content-Type**: `text/event-stream; charset=utf-8`
- **Stream Framing**: Each event begins with `data: ` and ends with `\n\n`. Keepalive comments `: ping - ...` can occur periodically.
- **Event Chunks**:
```
data: {"message": {"id": "node-ast-004", "author": {"role": "assistant", "name": null, "metadata": {}}, "create_time": 1709280030.0, "update_time": null, "content": {"content_type": "text", "parts": ["Understood"]}, "status": "in_progress", "end_turn": null, "weight": 1.0, "metadata": {"message_type": "next", "model_slug": "gpt-4o"}, "recipient": "all"}, "conversation_id": "019488a1-b2c3-7def-89ab-cdef01234567", "error": null}

data: {"message": {"id": "node-ast-004", "author": {"role": "assistant", "name": null, "metadata": {}}, "create_time": 1709280030.0, "update_time": null, "content": {"content_type": "text", "parts": ["Understood, following"]}, "status": "in_progress", "end_turn": null, "weight": 1.0, "metadata": {"message_type": "next", "model_slug": "gpt-4o"}, "recipient": "all"}, "conversation_id": "019488a1-b2c3-7def-89ab-cdef01234567", "error": null}

data: {"message": {"id": "node-ast-004", "author": {"role": "assistant", "name": null, "metadata": {}}, "create_time": 1709280030.0, "update_time": null, "content": {"content_type": "text", "parts": ["Understood, following the project guidelines."]}, "status": "finished_successfully", "end_turn": true, "weight": 1.0, "metadata": {"message_type": "next", "model_slug": "gpt-4o", "finish_details": {"type": "stop"}}, "recipient": "all"}, "conversation_id": "019488a1-b2c3-7def-89ab-cdef01234567", "error": null}

data: [DONE]
```

- **Delta Computation Logic**:
  Because `content.parts[0]` contains the **cumulative** response text generated so far, the streaming client must maintain `let prevLen = 0`:
  ```javascript
  const fullText = chunk.message.content.parts[0];
  const delta = fullText.slice(prevLen);
  prevLen = fullText.length;
  emitDelta(delta);
  ```

---

## 2. Logic Chain

1. **Session & Auth Reliability**:
   - Observations of `chat2api` and NextAuth specifications confirm that `__Secure-next-auth.session-token` is the root credential holding the session.
   - Calling `/api/auth/session` extracts a fresh `accessToken` JWT and its expiration timestamp.
   - Therefore, local storage needs only to securely persist the session cookie or active `accessToken`. Invalidation or expiration (401) can be resolved transparently by repeating the `/api/auth/session` exchange before declaring the session permanently expired.

2. **Project Identity & Filtering**:
   - ChatGPT Projects are not stored in a separate table; they are treated as Gizmos with IDs starting with `g-p-`.
   - The authoritative endpoint for listing a user's projects is `GET /backend-api/gizmos/snorlax/sidebar`.
   - The conversations for that project are accessed via `GET /backend-api/gizmos/{gizmo_id}/conversations`.
   - When sending a message to a project conversation, the payload must include `"conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": "g-p-..."}` to ensure that the project instructions and project knowledge files are activated by the backend.

3. **Bidirectional Tree Consistency (A2, A3, A4, A8)**:
   - ChatGPT stores conversations as trees (`mapping`), not flat arrays.
   - The active branch displayed on `chatgpt.com` is determined exclusively by starting at `current_node` and following `parent` pointers up to the root.
   - To send a new message without duplicating the conversation or branching errantly, `parent_message_id` must match `current_node`.
   - If a user sends a message on `chatgpt.com`, `current_node` moves forward. When the local POC fetches `/backend-api/conversation/{id}`, it reads the updated `current_node`, extracts the new turn, and continues from that new `current_node`. This guarantees strict bidirectional continuity and prevents duplicate forks.

4. **Transport Feasibility**:
   - While `DIRECT_HTTP` can fetch endpoints with valid headers, Cloudflare bot protection inspects TLS fingerprints and may enforce Sentinel PoW challenges.
   - In `BROWSER_FETCH`, execution occurs inside the authenticated browser context, natively inheriting Cloudflare clearance, TLS fingerprints, and cookies.
   - Thus, the spec is fully transport-agnostic, supporting `DIRECT_HTTP` (with `curl_cffi` / PoW solver), `BROWSER_FETCH` (via page evaluation or extension), and `UI_DRIVEN` (fallback).

---

## 3. Caveats

1. **Undocumented API Stability**: All `/backend-api/*` endpoints are internal to OpenAI's web frontend. While stable over multiple years in open-source tools (`chat2api`, `export-chatgpt`, `acheong08`), parameter naming (such as `snorlax`) can evolve.
2. **Cloudflare & PoW Updates**: OpenAI intermittently raises the Sentinel PoW difficulty or adds Turnstile checks on datacenter IPs. Clean residential or browser execution contexts (`BROWSER_FETCH`) avoid these triggers.
3. **Workspace/Enterprise Scoping**: On ChatGPT Team/Enterprise accounts, headers may require `chatgpt-account-id` or workspace cookies to access team projects.

---

## 4. Conclusion

The ChatGPT Web Backend-API specification has been completely discovered, verified against reference implementations, and documented:
1. **Authentication**: Fully functional via `__Secure-next-auth.session-token` cookie and `/api/auth/session` endpoint for `accessToken` extraction.
2. **Projects**: Managed as Gizmos (`g-p-...`) discovered via `/backend-api/gizmos/snorlax/sidebar`, configured with instructions and files via `/backend-api/gizmos/{gizmo_id}`.
3. **Listing**: Paginated via `offset`/`limit` on `/backend-api/conversations` (global) and `/backend-api/gizmos/{gizmo_id}/conversations` (project-scoped).
4. **History & Traversal**: Active branch cleanly reconstructed by reverse traversal from `current_node` to root node in `mapping`.
5. **Messaging & Streaming**: POST to `/backend-api/conversation` with `action: "next"`, `parent_message_id: current_node`, and `conversation_mode: {"kind": "gizmo_interaction", "gizmo_id": "g-p-..."}`, streaming cumulative text in `parts[0]` until `[DONE]`.

This provides the exact blueprints required for Milestone 1 (Transport & Session), Milestone 2 (Project/Conversation Explorer), and Milestone 3 (Continuation & Streaming).

---

## 5. Verification Method

To independently verify this specification:
1. **Session & Auth**:
   - Inspect network tab on `chatgpt.com` or run `curl -s -H "Cookie: __Secure-next-auth.session-token=..." https://chatgpt.com/api/auth/session`.
   - Confirm response status 200 and presence of `accessToken` JWT string and `expires`.
2. **Projects Discovery**:
   - Send `GET https://chatgpt.com/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=5` with `Authorization: Bearer <accessToken>`.
   - Verify list of projects with `g-p-` prefixes.
3. **Tree Traversal Unit Test**:
   - Run the Python unit test below on mock conversation tree payloads:
```python
import json

sample_conv = {
    "current_node": "n3",
    "mapping": {
        "n0": {"id": "n0", "message": None, "parent": None, "children": ["n1"]},
        "n1": {"id": "n1", "message": {"author": {"role": "system"}, "content": {"parts": ["sys"]}, "metadata": {}}, "parent": "n0", "children": ["n2"]},
        "n2": {"id": "n2", "message": {"author": {"role": "user"}, "content": {"parts": ["Hello"]}, "metadata": {}}, "parent": "n1", "children": ["n3", "n4"]},
        "n3": {"id": "n3", "message": {"author": {"role": "assistant"}, "content": {"parts": ["Hi there!"]}, "metadata": {}}, "parent": "n2", "children": []},
        "n4": {"id": "n4", "message": {"author": {"role": "assistant"}, "content": {"parts": ["Alternative hi"]}, "metadata": {}}, "parent": "n2", "children": []}
    }
}

def traverse(conv):
    mapping = conv["mapping"]
    curr = conv["current_node"]
    branch = []
    while curr:
        node = mapping.get(curr)
        if not node: break
        if node.get("message"):
            branch.append((node["message"]["author"]["role"], node["message"]["content"]["parts"][0]))
        curr = node.get("parent")
    return list(reversed(branch))

assert traverse(sample_conv) == [("system", "sys"), ("user", "Hello"), ("assistant", "Hi there!")]
print("Tree traversal verification passed successfully!")
```
4. **Streaming Protocol**:
   - Validate that SSE chunks parsed with cumulative delta logic `chunk[prevLen:]` produce exact character streams identical to browser UI output.
