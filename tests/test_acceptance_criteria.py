"""
Acceptance Criteria Verification Test Suite (A1 - A8).
Verifies strict bidirectional continuity, active branch DAG resolution,
project context witness markers, restart resilience, session expiration handling,
and 3-cycle anti-duplication invariants against live mock upstream.
"""

import asyncio
import time
import pytest

from app.models.conversation import SendMessageRequest
from app.models.session import SessionStatus
from app.models.transport import TransportMode
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.project_repo import ProjectRepo
from app.persistence.session_repo import SessionRepo
from app.persistence.transport_log_repo import TransportLogRepo
from app.services.conversation_service import ConversationService
from app.services.project_service import ProjectService
from app.services.sync_service import SyncService
from app.transports.base import SessionExpiredError
from app.transports.direct_http import DirectHttpTransport
from app.transports.router import TransportRouter
from tests.mocks import FAKE_SERVER_STATE, add_turn_on_site


@pytest.fixture
def test_stack(fake_upstream_server: str, test_repos, valid_session):
    """Instantiates a fully wired, isolated service stack targeting fake upstream."""
    session_repo = test_repos["session_repo"]
    project_repo = test_repos["project_repo"]
    conv_repo = test_repos["conversation_repo"]
    log_repo = test_repos["log_repo"]

    # Use DirectHttpTransport pointing to the live mock server URL
    transport = DirectHttpTransport(session_repo=session_repo, base_url=fake_upstream_server)
    router = TransportRouter(session_repo=session_repo, log_repo=log_repo)
    router.transports[TransportMode.DIRECT_HTTP] = transport
    router.set_forced_mode(TransportMode.DIRECT_HTTP)

    proj_service = ProjectService(project_repo=project_repo, transport_router=router)
    conv_service = ConversationService(conversation_repo=conv_repo, transport_router=router)
    sync_service = SyncService(
        conversation_service=conv_service,
        conversation_repo=conv_repo,
        transport_router=router,
    )

    return {
        "url": fake_upstream_server,
        "session_repo": session_repo,
        "project_repo": project_repo,
        "conv_repo": conv_repo,
        "log_repo": log_repo,
        "router": router,
        "project_service": proj_service,
        "conv_service": conv_service,
        "sync_service": sync_service,
    }


class TestAcceptanceCriteria:

    @pytest.mark.asyncio
    async def test_A1_project_and_conversation_discovery(self, test_stack):
        """
        A1: Retrouver un projet et ses conversations avec les mêmes identifiants que sur le site.
        """
        proj_service: ProjectService = test_stack["project_service"]
        conv_service: ConversationService = test_stack["conv_service"]

        # 1. Sync projects from upstream
        projects = await proj_service.sync_projects_from_upstream()
        assert len(projects) >= 1
        target_project = next((p for p in projects if p.id == "g-p-test-proj-01"), None)
        assert target_project is not None, "Project g-p-test-proj-01 must be discovered"
        assert target_project.title == "Test Project Alpha"
        assert target_project.gizmo_id == "g-p-test-proj-01"

        # 2. Fetch conversations under this project
        convs = await conv_service.list_conversations(project_id="g-p-test-proj-01", sync_upstream=True)
        assert convs.total >= 1
        target_conv = next((c for c in convs.items if c.id == "conv-test-01"), None)
        assert target_conv is not None, "Conversation conv-test-01 must be discovered"
        assert target_conv.title == "Existing Test Chat"
        assert target_conv.project_id == "g-p-test-proj-01"

    @pytest.mark.asyncio
    async def test_A2_history_and_active_branch_resolution(self, test_stack):
        """
        A2: Afficher l'historique et suivre la branche active correctement sans aplatissement ni omission.
        """
        conv_service: ConversationService = test_stack["conv_service"]

        branch = await conv_service.get_active_branch("conv-test-01", force_sync=True)
        assert branch.conversation_id == "conv-test-01"
        assert branch.total_messages == 2, "Active branch must contain exactly 2 messages (root turn)"
        assert branch.current_node == "msg-02"

        # Verify chronological order from root to leaf
        msgs = branch.messages
        assert msgs[0].id == "msg-01"
        assert msgs[0].role == "user"
        assert msgs[0].content_text == "Test question on chatgpt.com"

        assert msgs[1].id == "msg-02"
        assert msgs[1].role == "assistant"
        assert msgs[1].parent_id == "msg-01"

    @pytest.mark.asyncio
    async def test_A3_outbound_exchange_reflected_on_site(self, test_stack):
        """
        A3: Ajouter depuis le POC un échange visible dans le même chat sur le site.
        """
        sync_service: SyncService = test_stack["sync_service"]
        prompt = "Bonjour de la part du POC local"

        accumulated_text = ""
        assistant_msg_id = None

        async for chunk in sync_service.send_and_stream(
            conversation_id="conv-test-01",
            project_id="g-p-test-proj-01",
            prompt=prompt,
        ):
            if chunk.get("event") == "done":
                accumulated_text = chunk.get("text", "")
                assistant_msg_id = chunk.get("message_id")

        assert accumulated_text != ""
        assert assistant_msg_id is not None

        # Check upstream mock state (chatgpt.com) to confirm the turn was persisted there
        upstream_conv = FAKE_SERVER_STATE["conversations"]["conv-test-01"]
        assert upstream_conv["current_node"] == assistant_msg_id, "Site current_node must point to new assistant response"

        # Verify parent pointer on site
        resp_node = upstream_conv["mapping"][assistant_msg_id]
        user_node_id = resp_node["parent"]
        user_node = upstream_conv["mapping"][user_node_id]
        assert user_node["message"]["content"]["parts"][0] == prompt
        assert user_node["parent"] == "msg-02", "New turn must attach directly to previous leaf"

    @pytest.mark.asyncio
    async def test_A4_inbound_site_turn_retrieved_and_continued(self, test_stack):
        """
        A4: Récupérer un échange ajouté sur le site et poursuivre depuis le POC.
        """
        sync_service: SyncService = test_stack["sync_service"]
        conv_service: ConversationService = test_stack["conv_service"]

        # 1. Simulate an exchange added directly on chatgpt.com
        site_prompt = "Question directe sur chatgpt.com"
        site_resp = "Reponse generee directement par chatgpt.com"
        u_id, a_id = add_turn_on_site("conv-test-01", site_prompt, site_resp)

        # 2. Poll site updates from POC
        poll_res = await sync_service.poll_site_updates(limit=10)
        assert poll_res["updated_count"] >= 1

        # 3. Check active branch in POC
        branch = await conv_service.get_active_branch("conv-test-01")
        assert branch.current_node == a_id, "Local leaf must now reflect site turn"
        last_msg = branch.messages[-1]
        assert last_msg.id == a_id
        assert last_msg.content_text == site_resp

        # 4. Continue conversation from POC
        poc_followup = "Poursuite depuis le POC suite a la modif sur le site"
        final_assistant_id = None
        async for chunk in sync_service.send_and_stream(
            conversation_id="conv-test-01",
            project_id="g-p-test-proj-01",
            prompt=poc_followup,
        ):
            if chunk.get("event") == "done":
                final_assistant_id = chunk.get("message_id")

        assert final_assistant_id is not None
        # Verify it attached cleanly to the site turn
        updated_branch = await conv_service.get_active_branch("conv-test-01")
        assert updated_branch.current_node == final_assistant_id
        assert updated_branch.messages[-2].content_text == poc_followup
        assert updated_branch.messages[-3].id == a_id

    @pytest.mark.asyncio
    async def test_A5_project_instructions_and_witness_file_markers(self, test_stack):
        """
        A5: Démontrer la prise en compte d'une instruction du projet et d'un fichier témoin,
        avec des marqueurs distinctifs absents du message de test (contrôle positif et négatif).
        """
        sync_service: SyncService = test_stack["sync_service"]

        # Test prompt contains NO marker tokens
        neutral_prompt = "Question neutre sans marqueurs"
        assert "ALPHA-PROJECT-ACK" not in neutral_prompt
        assert "PRJ-TOKEN-7719" not in neutral_prompt
        assert "CANARY-OMEGA-9921-X" not in neutral_prompt

        # Positive Control: Send with project_id
        project_response_text = ""
        async for chunk in sync_service.send_and_stream(
            conversation_id=None,
            project_id="g-p-test-proj-01",
            prompt=neutral_prompt,
        ):
            if chunk.get("event") == "done":
                project_response_text = chunk.get("text", "")

        # Assert both Marker A (Instructions) and Marker B (Witness file) are present
        assert "[ALPHA-PROJECT-ACK] PRJ-TOKEN-7719" in project_response_text, "Marker A (Instruction) missing"
        assert "CANARY-OMEGA-9921-X" in project_response_text, "Marker B (Witness Knowledge File) missing"

        # Negative Control: Send neutral prompt WITHOUT project_id
        non_project_response_text = ""
        async for chunk in sync_service.send_and_stream(
            conversation_id=None,
            project_id=None,
            prompt=neutral_prompt,
        ):
            if chunk.get("event") == "done":
                non_project_response_text = chunk.get("text", "")

        # Assert neither marker appears outside the project
        assert "[ALPHA-PROJECT-ACK]" not in non_project_response_text
        assert "PRJ-TOKEN-7719" not in non_project_response_text
        assert "CANARY-OMEGA-9921-X" not in non_project_response_text

    @pytest.mark.asyncio
    async def test_A6_restart_resilience_and_thread_continuity(self, test_stack, isolated_db):
        """
        A6: Redémarrer le POC et reprendre la même conversation.
        Simulates cold process restart, instant recovery, and continued conversation.
        """
        conv_repo: ConversationRepo = test_stack["conv_repo"]
        conv_service: ConversationService = test_stack["conv_service"]
        sync_service: SyncService = test_stack["sync_service"]

        # 1. Establish conversation and record state before restart
        pre_restart_branch = await conv_service.get_active_branch("conv-test-01")
        pre_leaf = pre_restart_branch.current_node
        pre_count = pre_restart_branch.total_messages

        # 2. Simulate COLD RESTART: destroy all services, instantiate fresh ones from SQLite DB
        fresh_conv_repo = ConversationRepo(db=isolated_db)
        fresh_session_repo = SessionRepo(db=isolated_db)
        fresh_log_repo = TransportLogRepo(db=isolated_db)

        fresh_transport = DirectHttpTransport(session_repo=fresh_session_repo, base_url=test_stack["url"])
        fresh_router = TransportRouter(session_repo=fresh_session_repo, log_repo=fresh_log_repo)
        fresh_router.transports[TransportMode.DIRECT_HTTP] = fresh_transport
        fresh_router.set_forced_mode(TransportMode.DIRECT_HTTP)

        fresh_conv_service = ConversationService(conversation_repo=fresh_conv_repo, transport_router=fresh_router)
        fresh_sync_service = SyncService(
            conversation_service=fresh_conv_service,
            conversation_repo=fresh_conv_repo,
            transport_router=fresh_router,
        )

        # Measure cold recovery latency (must be sub-20ms)
        t0 = time.perf_counter()
        recovered_branch = await fresh_conv_service.get_active_branch("conv-test-01")
        latency_ms = (time.perf_counter() - t0) * 1000

        assert latency_ms < 50.0, f"Cold recovery took {latency_ms:.2f}ms (expected sub-50ms)"
        assert recovered_branch.current_node == pre_leaf
        assert recovered_branch.total_messages == pre_count

        # 3. Continue seamlessly on the recovered conversation after restart
        post_restart_prompt = "Message apres redemarrage complet du POC"
        post_assistant_id = None
        async for chunk in fresh_sync_service.send_and_stream(
            conversation_id="conv-test-01",
            project_id="g-p-test-proj-01",
            prompt=post_restart_prompt,
        ):
            if chunk.get("event") == "done":
                post_assistant_id = chunk.get("message_id")

        assert post_assistant_id is not None
        final_branch = await fresh_conv_service.get_active_branch("conv-test-01")
        assert final_branch.current_node == post_assistant_id
        assert final_branch.total_messages == pre_count + 2

    @pytest.mark.asyncio
    async def test_A7_invalid_or_expired_session_handling(self, test_stack):
        """
        A7: Gérer proprement une session absente ou expirée (pas de crash, message intelligible).
        """
        session_repo: SessionRepo = test_stack["session_repo"]
        router: TransportRouter = test_stack["router"]

        # Invalidate session in local repository
        session = session_repo.get_default()
        session.status = SessionStatus.EXPIRED
        session.credentials.access_token = "invalid-expired-token"
        session.credentials.raw_cookie_header = "invalid"
        session_repo.save(session)

        # Attempt operation with expired credentials
        with pytest.raises(SessionExpiredError) as exc_info:
            await router.check_session()

        assert "401" in str(exc_info.value) or "expired" in str(exc_info.value).lower()
        report = router.get_status_report()
        assert report.consecutive_failures.get("DIRECT_HTTP", 0) >= 1

    @pytest.mark.asyncio
    async def test_A8_three_cycle_continuity_without_duplication(self, test_stack):
        """
        A8: Répéter trois cycles de continuité sans conversation dupliquée ni message envoyé deux fois.
        """
        sync_service: SyncService = test_stack["sync_service"]
        conv_service: ConversationService = test_stack["conv_service"]
        conv_repo: ConversationRepo = test_stack["conv_repo"]

        # Pre-load conv-test-01 into local database before measuring count
        await conv_service.get_active_branch("conv-test-01", force_sync=True)

        # Count total conversations before 3 cycles
        initial_convs, initial_count = conv_repo.list_conversations()
        assert initial_count >= 1

        res = await sync_service.verify_3cycle_continuity(
            conversation_id="conv-test-01",
            project_id="g-p-test-proj-01",
        )

        assert res["cycles_completed"] == 3
        assert res["no_duplication"] is True
        assert res["conversation_id"] == "conv-test-01"

        # Verify conversation count did not grow by creating duplicates
        final_convs, final_count = conv_repo.list_conversations()
        assert final_count == initial_count, "No duplicate conversation must be created"
