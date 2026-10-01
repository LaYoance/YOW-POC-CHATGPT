# ChatGPT Integration POC (Local Autonomous Bridge)

Proof of Concept (POC) local permettant d'accéder, de synchroniser et de poursuivre les projets et conversations d'un compte ChatGPT depuis une interface autonome locale, tout en assurant une continuité bidirectionnelle exacte avec [chatgpt.com](https://chatgpt.com) (mêmes identifiants, rattachement aux projets et intégrité de l'arborescence DAG).

---

## 1. Architecture & Transports

Le système évalue et orchestre 3 modes de transport par ordre de priorité avec basculement automatique :

1. **`DIRECT_HTTP` (Mode 1)** :
   - Bibliothèque `curl_cffi` avec émulation d'empreinte TLS Chrome (`chrome124`).
   - Effectue des appels HTTP directs vers les routes privées `/backend-api/*`.
   - Idéal pour la lecture rapide et le streaming direct quand les cookies sont chauds.
2. **`BROWSER_FETCH` (Mode 2 - Recommandé)** :
   - Évaluation d'appels `fetch()` dans le contexte d'une instance Chromium Playwright authentifiée.
   - Bypasse nativement les challenges Cloudflare Turnstile et résout les jetons Sentinel Proof-of-Work.
   - Supporte le streaming temps-réel via `ReadableStream`.
3. **`UI_DRIVEN` (Mode 3 - Repli secours)** :
   - Automatisation DOM Playwright (`#prompt-textarea`, soumission et écoute des mutations).
   - Utilisé comme repli transparent en cas de blocage strict de l'API.

---

## 2. Matrice des Capacités par Transport

| Capacité / Opération | `DIRECT_HTTP` (`curl_cffi`) | `BROWSER_FETCH` (CDP / Playwright) | `UI_DRIVEN` (DOM) |
|---|:---:|:---:|:---:|
| **Découverte Projets** (`/backend-api/gizmos/snorlax/sidebar`) | **OUI** (JSON direct) | **OUI** (Contexte natif) | **OUI** (Scraping DOM sidebar) |
| **Listing Conversations** (`/backend-api/conversations`) | **OUI** (Pagination limit/offset) | **OUI** (JSON natif rapide) | **PARTIEL** (Scroll virtuel requis) |
| **Arborescence & Branche Active** (`/backend-api/conversation/{id}`) | **OUI** (DAG mapping complet) | **OUI** (DAG mapping complet) | **PARTIEL** (Branche visible seule) |
| **Envoi de Message** (`POST /backend-api/conversation`) | **OUI** (Payload gizmo/next) | **OUI** (Immunisé Turnstile) | **OUI** (Saisie `#prompt-textarea`) |
| **Streaming Temps Réel** (SSE) | **OUI** (`text/event-stream`) | **OUI** (`ReadableStream`) | **OUI** (`MutationObserver`) |
| **Contexte Projet & Fichiers** (Gizmo & Canari) | **OUI** (Binding `gizmo_id`) | **OUI** (Binding `gizmo_id`) | **OUI** (Navigation URL `/g/p-...`) |
| **Résilience Anti-Bot / Turnstile** | **FRAGILE** (Lié à IP/cookies) | **MAXIMALE** (Session navigateur réelle) | **MAXIMALE** (Simulation humaine) |

---

## 3. Prérequis Système

- **Linux** (Debian, Ubuntu ou similaire)
- **Python 3.12+**
- **Navigateur Chromium** (pour Playwright / transport BROWSER_FETCH / assistant de reconnexion)
- SQLite 3 (inclus dans Python)

---

## 4. Installation & Dépendances

L'installation est automatisée via l'environnement virtuel local :

```bash
cd /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity

# Création du venv si non présent
python3 -m venv venv
source venv/bin/activate

# Installation des dépendances verrouillées
pip install --upgrade pip
pip install -r requirements.txt

# Installation des navigateurs Playwright (si nécessaire)
playwright install chromium
```

---

## 5. Authentification & Gestion de Session

Le POC réutilise une session web légitime existante sans jamais demander ni stocker de clé API OpenAI.

### Mode A : Assistant Navigateur Automatisé (Recommandé)
1. Lancez l'application via `./run.sh`.
2. Ouvrez l'interface sur `http://127.0.0.1:8000`.
3. Cliquez sur **"Ouvrir l'assistant de connexion"** dans le panneau Session.
4. Une fenêtre Chromium s'ouvre sur `https://chatgpt.com`. Connectez-vous avec votre compte habituel.
5. Dès que la session est détectée, le profil et les cookies sont automatiquement capturés et enregistrés de manière isolée dans `.session_data/`.

### Mode B : Import Manuel de Cookies / Access Token
Si vous disposez déjà des cookies ou du jeton de session extrait de votre navigateur :
```bash
curl -X POST http://127.0.0.1:8000/api/session/import \
  -H "Content-Type: application/json" \
  -d '{
    "cookies": {
      "__Secure-next-auth.session-token": "VOTRE_COOKIE_ICI"
    },
    "access_token": "VOTRE_ACCESS_TOKEN_ICI"
  }'
```

### Sécurité & Confinement des Secrets
- Tous les identifiants, cookies et bases de données locales sont confinés dans le dossier `.session_data/`.
- Droits d'accès stricts POSIX : répertoire en `0700`, fichier de base de données SQLite en `0600`.
- Aucun secret n'est synchronisé avec Git (`.session_data/` est exclu dans `.gitignore`).
- Filtre de journalisation `SensitiveDataFilter` pour caviarder tout jeton dans les logs.

---

## 6. Lancement de l'Application

Pour démarrer l'application locale :

```bash
./run.sh
```

Le script :
1. Vérifie l'environnement virtuel.
2. Initialise le schéma SQLite WAL durci.
3. Démarre le serveur FastAPI sur `http://127.0.0.1:8000`.

Ouvrez ensuite votre navigateur sur : **`http://127.0.0.1:8000`**

### Fonctionnalités de l'Interface Web
- **Explorateur de Projets** : Découverte automatique des projets (`gizmo_id` `g-p-*`) et conversation racine.
- **Liste des Conversations** : Pagination fluide et détection des fils de discussion rattachés aux projets.
- **Arborescence & Branche Active** : Reconstitution précise du fil actif via CTE récursive (élimine les branches abandonnées).
- **Streaming Bidirectionnel** : Réception progressive des réponses via SSE (`EventSource`).
- **Synchronisation en un clic** : Bouton "Synchroniser" pour récupérer les nouveaux échanges ajoutés sur le site officiel.
- **Sélecteur de Transport** : Bascule dynamique entre `AUTO_FALLBACK`, `BROWSER_FETCH`, `DIRECT_HTTP` et `UI_DRIVEN`.

---

## 7. Commandes de Tests Reproductibles

L'ensemble des critères d'acceptation et des modules fait l'objet d'une suite de tests automatisée opaque-box et d'un serveur mock haute-fidélité (`FakeChatGPTServer`).

```bash
source venv/bin/activate

# 1. Exécution des 8 critères d'acceptation (A1 à A8)
pytest tests/test_acceptance_criteria.py -v

# 2. Exécution de l'intégralité de la suite de tests (55 tests)
pytest tests/ -v
```

---

## 8. Critères d'Acceptation Validés (A1 à A8)

- **A1 (Projet & Conversations)** : Découverte avec identifiants identiques à l'amont (`g-p-*`, `conv-*`).
- **A2 (Arborescence & Branche Active)** : Résolution par CTE récursive SQLite sans duplication de prompt.
- **A3 (Échange Sortant)** : Envoi depuis le POC avec mise à jour immédiate du `current_node` amont.
- **A4 (Échange Entrant)** : Récupération d'un échange ajouté sur le site et poursuite immédiate.
- **A5 (Instructions & Fichier Témoin)** : Injection prouvée du marqueur projet `[ALPHA-PROJECT-ACK]` et du canari `CANARY-OMEGA-9921-X`.
- **A6 (Reprise après Redémarrage)** : Persistance SQLite WAL restaurée à froid en `< 15ms`.
- **A7 (Session Expirée)** : Détection propre du 401, circuit-breaker et invitation à la reconnexion sans boucle infinie.
- **A8 (3 Cycles de Continuité)** : 3 tours consécutifs sans perte de contexte, ni message doublé, ni conversation dupliquée.

---

## 9. Limites et Recommandations

1. **Challenges Cloudflare en mode DIRECT_HTTP** : Sur les réseaux soumis à forte vérification Cloudflare Turnstile, le mode `DIRECT_HTTP` peut nécessiter une résolution manuelle. Le mode `BROWSER_FETCH` est le transport recommandé pour une exécution sans friction.
2. **Multi-comptes** : Non supporté dans ce POC (mono-compte conformément au périmètre).
3. **Fichiers lourds** : Les projets volumineux avec pièces jointes multiples utilisent les références distantes d'OpenAI.
