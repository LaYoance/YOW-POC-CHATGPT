# Guide de Revue & Spécification de Livraison (Reviewer Guide)

Ce guide est destiné au reviewer technique pour tester, auditer et valider le **POC YOW - Intégration ChatGPT**.

---

## 1. Synthèse du Projet

Ce composant fournit un **bridge autonome local** entre un environnement de développement local (YOW) et un compte ChatGPT web légitime :
- **Continuité bidirectionnelle** : conservation exacte des identifiants upstream (`g-p-*`, `conv-*`, `msg-*`), rattachement aux projets et intégrité de l'arborescence DAG.
- **Routage multi-transports intelligent** :
  - `BROWSER_FETCH` (défaut/recommandé) : bypass natif Cloudflare Turnstile et Sentinel PoW au sein d'une instance Chromium Playwright locale.
  - `DIRECT_HTTP` : appels API rapides via `curl_cffi` (impersonation TLS `chrome124`).
  - `UI_DRIVEN` : secours par manipulation DOM Playwright.
- **Isolation sécurisée** : aucun secret ni cookie dans le dépôt Git ; confinement POSIX `0700` / `0600` dans `.session_data/` et caviardage des journaux.
- **Interface web riche** :
  - Reconstitution fidèle de la branche active par CTE récursive SQLite (élimine les branches orphelines/mortes).
  - Rendu Markdown GFM complet, coloration syntaxique multi-langages (Highlight.js) et bouton de copie de code.
  - URLs d'accès direct avec slug projet et ID de conversation (`/g/{project_slug}/c/{conversation_id}`).
  - Deep linking au niveau du message (`#msg-{id}`) avec surlignage visuel et copie de l'ancre en un clic.
  - Proxy de téléchargement pour les fichiers générés par le Code Interpreter (`sandbox:/mnt/data/...`).

---

## 2. Structure du Code

```text
.
├── app/
│   ├── api/                     # Endpoints FastAPI
│   │   ├── routes_chat.py       # Envoi de message & streaming SSE
│   │   ├── routes_conversations.py # Arborescence, branche active & proxy de téléchargement sandbox
│   │   ├── routes_projects.py   # Projets (gizmos)
│   │   ├── routes_session.py    # Statut session, import & assistant de connexion
│   │   └── routes_transport.py  # Diagnostic & bascule des modes de transport
│   ├── core/                    # Filtres de journalisation & sécurité
│   ├── models/                  # Schémas de données Pydantic v2
│   ├── persistence/             # Persistance SQLite (WAL, CTE récursives, transactions isolées)
│   ├── services/                # Logique métier (ConversationService, SessionService, etc.)
│   ├── static/                  # Frontend Vanilla JS / CSS (Marked.js, Highlight.js)
│   └── transports/              # Implémentations ChatGPTTransport (DirectHttp, BrowserFetch, UiDriven, Router)
├── tests/                       # Suite de tests automatisée (61 tests)
│   ├── mocks/                   # FakeChatGPTServer haute fidélité
│   ├── test_acceptance_criteria.py  # Validation formelle des critères d'acceptation A1 à A8
│   ├── test_interpreter_download.py # Tests unitaires du téléchargement Code Interpreter
│   ├── test_tier1_features/     # Tests de résilience, routage et session
│   └── test_tier2_boundaries/   # Tests de sécurité adversariale et stress M1
├── run.sh                       # Script de démarrage tout-en-un
├── requirements.txt             # Dépendances Python verrouillées
├── README.md                    # Documentation complète d'architecture
└── REVIEW_GUIDE.md              # Ce guide
```

---

## 3. Procédure de Vérification Rapide

### Étape 1 : Prérequis et Dépendances
```bash
# Vérifier Python 3.10+
python3 --version

# Créer l'environnement virtuel et installer les dépendances
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Étape 2 : Exécution de la Suite de Tests (61 tests validés)
Tous les tests sont conçus pour s'exécuter hors-ligne grâce au mock haute-fidélité `FakeChatGPTServer` :
```bash
source venv/bin/activate

# 1. Validation des critères d'acceptation fondamentaux A1 à A8
pytest tests/test_acceptance_criteria.py -v

# 2. Validation du téléchargement sandbox Code Interpreter
pytest tests/test_interpreter_download.py -v

# 3. Exécution globale (61 tests)
pytest tests/ -v
```
*Résultat attendu : 61 passed.*

### Étape 3 : Lancement de l'Application
```bash
./run.sh
```
L'interface est accessible sur `http://127.0.0.1:8000`.

---

## 4. Points Clés à Valider lors de la Revue

| Fonctionnalité | Point de Contrôle | Fichier Référent |
|---|---|---|
| **Critères A1 - A8** | Validation des 8 critères du cahier des charges | [`tests/test_acceptance_criteria.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/tests/test_acceptance_criteria.py) |
| **Branche Active DAG** | Résolution par CTE récursive SQLite sans doublon | [`app/persistence/conversation_repo.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/persistence/conversation_repo.py) |
| **Bypass Turnstile** | Exécution d'appels `fetch()` sous contexte Chromium | [`app/transports/browser_fetch.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/transports/browser_fetch.py) |
| **Routage d'URL** | URLs canoniques `/g/{project}/c/{conv}` et auto-sync | [`app/static/js/app.js`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/static/js/app.js) |
| **Ciblage de Message** | Ancre `#msg-{id}` avec bouton de copie d'URL | [`app/static/js/app.js`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/static/js/app.js) & [`app/static/css/style.css`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/static/css/style.css) |
| **Téléchargement Sandbox** | Bouton de téléchargement pour `sandbox:/mnt/data/...` | [`app/services/conversation_service.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/services/conversation_service.py) & [`app/api/routes_conversations.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/api/routes_conversations.py) |
| **Sécurité & Confinement** | Droits `0700` / `0600` et caviardage des secrets | [`app/config.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/config.py) & [`app/core/logging_filter.py`](file:///var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/app/core/logging_filter.py) |

---

## 5. Changelog / Release Notes (v1.0.0)

- **v1.0.0** (2026-10-01) :
  - **Core Bridge** : Architecture multi-transports avec bascule automatique (`BROWSER_FETCH`, `DIRECT_HTTP`, `UI_DRIVEN`).
  - **Session Isolation** : Assistant de connexion Chromium intégré, auto-capture de session et confinement `.session_data/`.
  - **DAG & Active Branch** : Ingestion d'arbre complet et extraction de branche active linéaire via CTE récursive.
  - **UI / UX** : Interface Web complète avec streaming SSE, sélecteur de projets, recherche et synchronisation amont.
  - **Rich Formatting** : Rendu Markdown GFM et coloration syntaxique multi-langages avec Highlight.js.
  - **Deep Linking** : Gestion d'URL avec identifiant de conversation, slug de projet et ciblage précis de message (`#msg-{id}`).
  - **Sandbox Downloads** : Proxy et cache local pour les livrables générés par Code Interpreter (`sandbox:/mnt/data/...`).
