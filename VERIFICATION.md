# Rapport de Vérification & Recette d'Acceptation (A1 à A8) — v1.3.0

**Statut Global** : `POC_QUALIFIED` (Durcissement v1.3.0 — Critères A1, A2, A3, A4, A6, A8 démontrés en réel in-situ ; A7 et régressions démontrés par tests de sécurité et durcissement ; A5 démontré en simulation contrôlée).  
**Date d'exécution** : 2026-10-04  
**Environnement** : Linux (x86_64), Python 3.12, SQLite 3 (WAL), Playwright Chromium headless, curl_cffi  
**Rapport de Recette Réelle Associé** : [`RECIPE_VALIDATION_REPORT.md`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/RECIPE_VALIDATION_REPORT.md)

---

## 1. Distinction Méthodologique des Validations

Conformément aux exigences de rigueur technique, le présent rapport sépare strictement :
1. **Les vérifications réelles in-situ** exécutées directement contre l'infrastructure de production [chatgpt.com](https://chatgpt.com) avec une session active authentifiée.
2. **Les tests de régression et de sécurité** exécutés hermétiquement via la suite automatisée (70 tests pytest).
3. **Les tests simulés** exécutés contre un mock haute-fidélité (`FakeChatGPTServer`).
4. **Les résultats non vérifiés in-situ** nécessitant une intervention ou configuration humaine préalable.

---

## 2. Synthèse d'Évaluation des Critères d'Acceptation (A1 à A8)

| Critère | Intitulé | Type de Preuve | Statut | Résultat / Observation |
|---|---|:---:|:---:|---|
| **A1** | Projets et conversations avec mêmes identifiants | **Réel In-Situ** | **DÉMONTRÉ** | Découverte réelle de 8 projets (`g-p-*`) et conversations sur le compte upstream ; parité stricte des identifiants avec SQLite. |
| **A2** | Historique et branche active résolus par DAG | **Réel In-Situ** | **DÉMONTRÉ** | Ingestion de l'arborescence DAG chatgpt.com et parcours chronologique par CTE récursive SQLite sans rupture ni orphelins. |
| **A3** | Échange sortant depuis POC visible sur le site | **Réel In-Situ** | **DÉMONTRÉ** | Message émis via POC, traité et répondu par l'IA upstream, rattaché au nœud feuille officiel sur `chatgpt.com`. |
| **A4** | Récupération d'un échange site et poursuite locale | **Réel In-Situ** | **DÉMONTRÉ** | Poursuite fluide d'une conversation créée sur le site sans désynchronisation. |
| **A5** | Prise en compte instruction projet et fichier témoin | **Simulation** | **SIMULÉ** | Validé en simulation contrôlée avec marqueurs `[ALPHA-PROJECT-ACK]` et `CANARY-OMEGA-9921-X`. Non vérifié in-situ (aucun fichier attaché au projet upstream). |
| **A6** | Reprise après redémarrage (Cold Boot) | **Réel In-Situ** | **DÉMONTRÉ** | Redémarrage complet à froid des composants ; restauration instantanée de la branche active en `< 15ms` depuis la base WAL. |
| **A7** | Gestion propre session absente/expirée & circuit-breaker | **Test Sécurité** | **DÉMONTRÉ** | Interception 401, circuit-breaker télémétrique, absence de bouclage infini et bascule sur l'assistant de reconnexion. |
| **A8** | Continuité multi-tours sans duplication | **Réel In-Situ** | **DÉMONTRÉ** | 3 tours consécutifs réalisés sur fil dédié (`6abee066-0d7c-83eb-a698-ca7899047df5`) : 0 doublon, 0 UUID synthétique résiduel, 100% parité. |

---

## 3. Détail des Corrections de Revue (v1.2.0 Hardening)

Cinq défauts majeurs identifiés lors de la revue v1.1.0 ont été corrigés et vérifiés par des tests de régression dédiés ([`tests/test_tier1_features/test_defect_regressions.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/tests/test_tier1_features/test_defect_regressions.py)) :

### 1. Protection Anti-Doublon lors d'une Interruption de Flux
- **Problème v1.1.0** : En cas de coupure réseau ou d'erreur de flux après soumission, le routeur tentait un repli vers un autre transport, risquant de poster un deuxième message identique en amont.
- **Correction v1.2.0** :
  - Ajout d'un verrou par conversation (`_submission_locks`).
  - Détection `submission_potentially_accepted` dès le premier octet émis.
  - Si une défaillance survient, interdiction absolue de bascule vers d'autres transports.
  - Tentative de réconciliation de l'arbre amont (`fetch_conversation_tree`) ; si non confirmé, émission d'un événement typé `{"event": "uncertain", "status": "uncertain"}` sans réémission.

### 2. Validation Stricte du Transport UI-Driven
- **Problème v1.1.0** : Risque de retourner un succès sur timeout ou réponse incomplète.
- **Correction v1.2.0** :
  - Contrôle strict de fin de génération (`generation_completed == True`).
  - Rejet formel des réponses vides (`accumulated_text.strip()`).
  - Rejet formel si l'identifiant de conversation ou les identifiants canoniques de messages utilisateur et assistant ne sont pas confirmés depuis l'arbre amont.

### 3. Préservation des Identifiants Officiels et Réconciliation DAG dans SyncService
- **Problème v1.1.0** : Le message utilisateur initial utilisait un UUID synthétique local qui pouvait diverger de l'ID officiel attribué par chatgpt.com.
- **Correction v1.2.0** :
  - Création initiale d'un UUID temporaire de suivi local.
  - Réconciliation systématique post-envoi de l'arborescence DAG complète via `ingest_conversation_tree`.
  - Suppression automatique du nœud temporaire (`DELETE FROM messages WHERE id = temp_user_message_id`) dès confirmation de l'ID utilisateur officiel amont.
  - Enregistrement des liens de parenté canoniques `parent_id`.

### 4. Partage Global du Routeur de Transport & Respect des Modes Forcés
- **Problème v1.1.0** : Des instances indépendantes de `TransportRouter` étaient créées dans les routes FastAPI et les services, ignorant parfois le mode forcé sélectionné.
- **Correction v1.2.0** :
  - Centralisation via `get_shared_router()`.
  - Branchement unifié dans `routes_transport.py`, `conversation_service.py`, `project_service.py` et `sync_service.py`.
  - Respect strict des modes forcés (`DIRECT_HTTP`, `BROWSER_FETCH`, `UI_DRIVEN`) sans repli non autorisé.

### 5. Assainissement HTML Markdown (Sécurité XSS)
- **Problème v1.1.0** : Injection directe du rendu HTML Markdown dans le DOM (`innerHTML = renderMarkdown(...)`).
- **Correction v1.2.0** :
  - Intégration de DOMPurify 3.1.6 (`/static/js/dompurify.min.js`).
  - Assainissement strict avant insertion : `DOMPurify.sanitize(html, { ADD_ATTR: [...], ADD_TAGS: [...] })`.
  - Neutralisation des balises `<script>` et attributs malveillants tout en préservant les boutons de copie et liens sandbox avec délégation d'événements.

### 6. Robustesse UI_DRIVEN & Court-Circuit Typé 404 (v1.3.0)
- **Problème v1.2.0** : 
  - Lorsque la page Playwright n'avait pas encore navigué sur le domaine cible, l'évaluation de chemins relatifs (`fetch('/backend-api/...')`) échouait avec `TypeError: Failed to parse URL`.
  - Lors de la suppression d'une conversation amont (`conversation_deleted` 404), le routeur parcourait inutilement la chaîne de transports, puis levait une erreur 500 générique sur `/branch?sync=true`.
- **Correction v1.3.0** :
  - Navigation proactive vers `base_url` dans `UiDrivenTransport._ensure_browser()` et passage systématique d'URLs absolues dans les contextes d'évaluation DOM.
  - Création de `ConversationNotFoundError` (`status_code=404`) levée par l'ensemble des transports sur réponse 404 amont.
  - Court-circuit immédiat de la cascade de fallbacks dans `TransportRouter._execute_with_failover`.
  - Retour d'un statut HTTP 404 propre avec message explicatif sur l'endpoint `/api/conversations/{id}/branch`.

---

## 4. Preuve de Recette Réelle In-Situ (Fil Dédié)

Extrait du rapport réel d'exécution ([`RECIPE_VALIDATION_REPORT.md`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/RECIPE_VALIDATION_REPORT.md)) sur la conversation `6abee066-0d7c-83eb-a698-ca7899047df5` :

- **Opération 1 (POC -> ChatGPT)** :
  - Transport effectif : `DIRECT_HTTP`
  - Prompt : `"Recette V1.2.0 Turn 3 - Reponds exactement par le mot: RecetteComplete"`
  - Réponse reçue : `"RecetteComplete"`
  - ID utilisateur émis : `6f2f47cd-aa60-4847-84a5-805a7f6444ac`
  - ID assistant reçu : `8761f26e-8513-42f6-bcc6-11271b2f1f56`
  - Parent rattaché : `988ee727-8f58-4bc4-9906-8326bb28b84d`

- **Opération 2 (ChatGPT -> POC & Parité des Identifiants)** :
  - `current_node` amont (`8761f26e-...`) == local (`8761f26e-...`) : **CONFORME**
  - ID utilisateur amont (`6f2f47cd-...`) == local (`6f2f47cd-...`) : **CONFORME**
  - Lien assistant -> utilisateur amont == local : **CONFORME**
  - Lien utilisateur -> nœud précédent amont == local : **CONFORME**
  - Nettoyage de l'ID synthétique temporaire : **CONFORME** (0 UUID résiduel)

- **Opération 3 (Reprise Cold Boot)** :
  - Réinstanciation à froid des gestionnaires SQLite et services.
  - Reconstitution de la branche active : 7 messages valides, continuité stricte de la chaîne DAG, feuille active restaurée.

---

## 5. Matrice des Capacités et Transports Observés

| Transport | Rôle dans le Routeur | Comportement Observé en Réel |
|---|---|---|
| **`DIRECT_HTTP`** | Mode 1 (Rapide) | Fonctionne pour le streaming et l'arborescence quand les cookies de session sont actifs. |
| **`BROWSER_FETCH`** | Mode 2 (Navigateur) | Utilisé pour les appels contextuels dans Chromium. |
| **`UI_DRIVEN`** | Mode 3 (Bypass Turnstile) | Contourne intégralement Cloudflare Sentinel Turnstile par saisie native DOM clavier/souris. Détecte les redirections d'URL. |
