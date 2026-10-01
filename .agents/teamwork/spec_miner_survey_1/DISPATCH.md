# Spec Miner Survey Dispatch
Target: Examine authoritative specifications and reference implementations for ChatGPT Backend-API endpoints, session extraction, conversation trees, and project endpoints.
Working directory: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/spec_miner_survey_1
Original Request: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md
Report output: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/spec_miner_survey_1/handoff.md

## 2026-10-01T11:07:07Z
From: cc1b08b2-2f82-49cf-9e45-b8c933af6ebf
Content:
You are the ChatGPT API Spec Miner for the Survey Phase of the ChatGPT Integration POC.
Your working directory: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/spec_miner_survey_1
Original Request Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md

You MUST read /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md first.

Your mission:
Investigate authoritative sources and reference implementations cited in ORIGINAL_REQUEST.md:
- https://github.com/lanqian528/chat2api
- https://github.com/brianjlacy/export-chatgpt
- https://github.com/Octo-Lex/ChatGPT-Web2API
- https://github.com/acheong08/ChatGPT

Probe and document the complete specification for ChatGPT Web Backend-API:
1. Authentication & Session:
   - How web sessions work: cookies (__Secure-next-auth.session-token, cf_clearance, etc.), /api/auth/session endpoint, accessToken extraction, expiry, token refresh.
   - Header requirements: Authorization: Bearer <accessToken>, OAI-Device-Id, User-Agent, accept, etc.
   - Security challenges: Arkose token, Proof-of-Work (PoW / sentinel / challenge algorithm if used by OpenAI web), how they are requested or bypassed.
2. Projects & Gizmos:
   - Endpoints for discovering user projects: /backend-api/gizmos/bootstrap, /backend-api/gizmos/discovery, /backend-api/projects or workspace projects.
   - Project schema: project ID, gizmo ID, title, description, custom instructions/system prompts, attached files/knowledge files list and structure.
3. Conversation Listing & Pagination:
   - Listing endpoint: /backend-api/conversations?offset={offset}&limit={limit}&order=updated.
   - How project-specific conversations are filtered or tagged (e.g., gizmo_id or workspace_id filter).
4. Conversation History & Tree Traversal:
   - Detail endpoint: /backend-api/conversation/{conversation_id}.
   - Response structure: mapping (dictionary of node IDs to node objects), current_node, parent-child relationships.
   - Exact algorithm to walk the active branch from current_node backwards to root (or root to current_node), extracting author roles (user, assistant, system, tool), message parts/content, timestamps, models, metadata.
5. Message Transmission & Streaming:
   - Post endpoint: /backend-api/conversation.
   - Request payload: action: "next", messages: [...] (id, author, content), parent_message_id, conversation_id, model, gizmo_id / project context.
   - Response SSE stream: format of data: {"message": ...}, delta text, finish_details, error formats.

Write your complete, structured analysis report to:
/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/spec_miner_survey_1/handoff.md
Update your progress.md in your working directory as you work.
When finished, send a completion message to the parent orchestrator.
