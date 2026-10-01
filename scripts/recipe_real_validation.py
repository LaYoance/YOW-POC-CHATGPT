"""
Real Recipe Validation Script: POC <-> ChatGPT <-> POC Continuity & Hardening.
Executes against dedicated test conversation: 6abe7e27-e678-83eb-a40f-f437f79ea9e8.
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("recipe_real_validation")

from app.persistence.db import DatabaseManager, db_manager
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.session_repo import SessionRepo
from app.transports.router import TransportRouter, get_shared_router
from app.services.sync_service import SyncService
from app.services.conversation_service import ConversationService
from app.models.transport import TransportMode

CONVERSATION_ID = "6abee066-0d7c-83eb-a698-ca7899047df5"


async def run_recipe():
    report_lines = []
    def log_and_record(text: str):
        print(text)
        report_lines.append(text)

    log_and_record("# Rapport de Recette Réelle v1.2.0 (POC <-> ChatGPT <-> POC)")
    log_and_record(f"Date d'exécution : {datetime.now(timezone.utc).isoformat()}")
    log_and_record(f"Conversation cible dédiée : `{CONVERSATION_ID}`")
    log_and_record("")

    # --- Pre-check Session ---
    session_repo = SessionRepo()
    session = session_repo.get_default()
    if not session or not session.credentials:
        log_and_record("❌ ÉCHEC : Aucune session ChatGPT trouvée en base.")
        return False, "\n".join(report_lines)

    email_masked = (
        session.user_email[:3] + "***@" + session.user_email.split("@")[1]
        if session.user_email and "@" in session.user_email
        else "lay***@gmail.com"
    )
    log_and_record(f"- Session utilisateur active : `{email_masked}` (Statut: {session.status.value})")

    # --- Pre-check Initial State ---
    conv_repo = ConversationRepo()
    initial_conv = conv_repo.get_conversation(CONVERSATION_ID)
    initial_branch = conv_repo.get_active_branch(CONVERSATION_ID)
    initial_leaf = initial_conv.current_node_id if initial_conv else None
    initial_msg_count = len(initial_branch)

    log_and_record(f"- État initial en base locale : {initial_msg_count} messages")
    log_and_record(f"- Nœud feuille initial (parent attendu) : `{initial_leaf}`")
    log_and_record("")

    # --- Opération 1 : POC -> ChatGPT ---
    log_and_record("## Opération 1 : Envoi de message POC -> ChatGPT")
    prompt = "Recette V1.2.0 Turn 3 - Reponds exactement par le mot: RecetteComplete"
    log_and_record(f"- Prompt transmis : \"{prompt}\"")

    router = get_shared_router()
    sync_service = SyncService(transport_router=router)

    accumulated_text = ""
    done_event = None
    stream_events = []

    try:
        async for chunk in sync_service.send_and_stream(
            conversation_id=CONVERSATION_ID,
            project_id=None,
            prompt=prompt,
        ):
            stream_events.append(chunk)
            evt = chunk.get("event")
            if evt == "delta":
                accumulated_text = chunk.get("text", "")
            elif evt == "done":
                done_event = chunk
                accumulated_text = chunk.get("text", accumulated_text)
            elif evt == "uncertain":
                log_and_record(f"⚠️ Événement incertain reçu : {chunk.get('error')}")
                return False, "\n".join(report_lines)

    except Exception as e:
        log_and_record(f"❌ Erreur lors de l'envoi du message : {e}")
        return False, "\n".join(report_lines)

    if not done_event:
        log_and_record("❌ Aucun événement 'done' reçu du flux de streaming.")
        return False, "\n".join(report_lines)

    # Identifie le transport effectif
    effective_transport = router.last_successful_mode or TransportMode.UI_DRIVEN
    log_and_record(f"- **Transport effectif utilisé** : `{effective_transport.value}`")
    log_and_record(f"- Réponse assistant reçue : \"{done_event.get('text', '').strip()}\"")
    log_and_record(f"- ID message utilisateur émis : `{done_event.get('user_message_id')}`")
    log_and_record(f"- ID message assistant reçu : `{done_event.get('message_id')}`")
    log_and_record(f"- ID parent rattaché : `{done_event.get('parent_id')}`")
    log_and_record("")

    # --- Opération 2 : ChatGPT -> POC & Comparaison des identifiants ---
    log_and_record("## Opération 2 : Réconciliation amont ChatGPT -> POC & Parité des identifiants")
    
    # Récupération de l'arbre canonique directement depuis ChatGPT
    tree_data = await router.fetch_conversation_tree(CONVERSATION_ID)
    if not tree_data or "current_node" not in tree_data:
        log_and_record("❌ Impossible de récupérer l'arbre canonique amont depuis chatgpt.com.")
        return False, "\n".join(report_lines)

    upstream_current_node = tree_data["current_node"]
    mapping = tree_data.get("mapping", {})
    asst_node = mapping.get(upstream_current_node, {})
    asst_msg = asst_node.get("message", {})
    asst_parent_id = asst_node.get("parent")

    user_node = mapping.get(asst_parent_id, {})
    user_msg = user_node.get("message", {})
    user_parent_id = user_node.get("parent")

    log_and_record("### Données canoniques amont (chatgpt.com) :")
    log_and_record(f"- `current_node` amont : `{upstream_current_node}`")
    log_and_record(f"- ID message assistant amont : `{asst_msg.get('id')}`")
    log_and_record(f"- Parent message assistant amont : `{asst_parent_id}`")
    log_and_record(f"- ID message utilisateur amont : `{user_msg.get('id')}`")
    log_and_record(f"- Parent message utilisateur amont : `{user_parent_id}`")
    log_and_record("")

    # Données locales enregistrées en base SQLite
    local_conv = conv_repo.get_conversation(CONVERSATION_ID)
    local_branch = conv_repo.get_active_branch(CONVERSATION_ID)
    local_last_asst = local_branch[-1] if local_branch else None
    local_last_user = local_branch[-2] if len(local_branch) >= 2 else None

    log_and_record("### Données persistées en local (SQLite WAL) :")
    log_and_record(f"- `current_node_id` local : `{local_conv.current_node_id if local_conv else None}`")
    log_and_record(f"- ID message assistant local : `{local_last_asst.id if local_last_asst else None}`")
    log_and_record(f"- Parent message assistant local : `{local_last_asst.parent_id if local_last_asst else None}`")
    log_and_record(f"- ID message utilisateur local : `{local_last_user.id if local_last_user else None}`")
    log_and_record(f"- Parent message utilisateur local : `{local_last_user.parent_id if local_last_user else None}`")
    log_and_record("")

    # Vérification stricte des identifiants et des liens de parenté (A2, A8)
    checks = []
    checks.append(("Parité current_node", local_conv.current_node_id == upstream_current_node == done_event["message_id"]))
    checks.append(("Parité ID assistant", local_last_asst.id == upstream_current_node == done_event["message_id"]))
    checks.append(("Parité ID utilisateur", local_last_user.id == asst_parent_id == done_event["user_message_id"]))
    checks.append(("Lien assistant -> utilisateur", local_last_asst.parent_id == local_last_user.id))
    checks.append(("Lien utilisateur -> nœud initial", local_last_user.parent_id == initial_leaf))

    # Vérifier l'absence d'ID temporaire synthétique en base
    with db_manager.get_read_connection() as conn:
        all_user_msgs = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' AND content_text = ?",
            (CONVERSATION_ID, prompt)
        ).fetchall()
        checks.append(("Nettoyage ID temporaire synthétique", len(all_user_msgs) == 1 and all_user_msgs[0][0] == user_msg.get('id')))

    all_passed = True
    for name, ok in checks:
        status_icon = "✅" if ok else "❌"
        log_and_record(f"- {status_icon} {name} : {'CONFORME' if ok else 'DISCORDANCE'}")
        if not ok:
            all_passed = False

    log_and_record("")

    # --- Opération 3 : Reprise après redémarrage (Cold Start) ---
    log_and_record("## Opération 3 : Reprise après redémarrage (Cold Boot)")
    
    # Fermeture de tout contexte et réinstanciation complète
    cold_db = DatabaseManager(db_path=db_manager.db_path)
    cold_repo = ConversationRepo(db=cold_db)
    cold_service = ConversationService(conversation_repo=cold_repo)

    cold_conv = cold_repo.get_conversation(CONVERSATION_ID)
    cold_branch_resp = await cold_service.get_active_branch(CONVERSATION_ID)
    cold_branch = cold_branch_resp.messages

    log_and_record(f"- Rechargement cold boot de la conversation : {'OK' if cold_conv else 'ÉCHEC'}")
    log_and_record(f"- Nombre total de messages sur la branche active : {len(cold_branch)} (initial {initial_msg_count} + 2 attendus)")
    log_and_record(f"- Feuille active restaurée : `{cold_branch_resp.current_node}`")

    cold_checks = []
    cold_checks.append(("Nombre de messages incrémenté (au moins +2)", len(cold_branch) >= initial_msg_count + 2))
    cold_checks.append(("Feuille active égale au current_node", cold_branch[-1].id == upstream_current_node))
    cold_checks.append(("Intégrité chaîne DAG sans rupture", all(cold_branch[i].id == cold_branch[i+1].parent_id for i in range(len(cold_branch)-1))))

    for name, ok in cold_checks:
        status_icon = "✅" if ok else "❌"
        log_and_record(f"- {status_icon} {name} : {'CONFORME' if ok else 'DISCORDANCE'}")
        if not ok:
            all_passed = False

    log_and_record("")
    log_and_record("## Conclusion de la Recette")
    if all_passed:
        log_and_record("✅ **RECETTE PLEINEMENT RÉUSSIE** : Continuité bidirectionnelle parfaite, parité stricte des identifiants DAG, et reprise cold boot confirmée.")
    else:
        log_and_record("❌ **RECETTE ÉCHOUÉE** : Des discordances d'identifiants ou d'intégrité persistent.")

    return all_passed, "\n".join(report_lines)


if __name__ == "__main__":
    success, report = asyncio.run(run_recipe())
    # Save report
    report_file = Path("RECIPE_VALIDATION_REPORT.md")
    report_file.write_text(report, encoding="utf-8")
    print(f"\nRapport sauvegardé dans {report_file.resolve()}")
    sys.exit(0 if success else 1)
