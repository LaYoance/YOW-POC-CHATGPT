# Original User Request

## Initial Request — 2026-10-01T11:04:45Z

POC local autonome permettant de retrouver et poursuivre les projets et conversations d'un compte ChatGPT depuis une interface indépendante, puis de continuer exactement les mêmes conversations sur chatgpt.com avec synchronisation bidirectionnelle stricte.

Working directory: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity
Integrity mode: development

## Reference Material
- https://github.com/lanqian528/chat2api
- https://github.com/brianjlacy/export-chatgpt
- https://github.com/Octo-Lex/ChatGPT-Web2API
- https://github.com/acheong08/ChatGPT

## Requirements

### R1. Authentification et session
- Authentification par réutilisation d'une session web ChatGPT légitime (sans clé API OpenAI ni OAuth Codex).
- Isolation locale stricte des secrets de session (aucun partage externe, aucun proxy tiers, pas de commit de tokens).
- Gestion claire des états de session : valide, expirée, absente, et procédure de reconnexion documentée.

### R2. Exploration et lecture des projets et conversations
- Récupération et affichage de la liste des projets ChatGPT associés au compte.
- Récupération et pagination des conversations au sein d'un projet et hors projet.
- Chargement de l'historique complet d'une conversation et identification correcte de la branche active.
- Actualisation des nouveaux échanges ajoutés directement sur chatgpt.com.

### R3. Continuation bidirectionnelle et persistance
- Envoi d'un message depuis l'interface locale vers une conversation existante rattachée à un projet sans altérer les identifiants ni dupliquer le fil.
- Réception et affichage progressif (streaming) de la réponse lorsque le transport le supporte.
- Persistance vérifiable sur chatgpt.com : les messages envoyés depuis le POC apparaissent dans la même conversation sur le site officiel, et vice-versa.
- Conservation de la continuité après redémarrage complet du service local.
- Prise en compte du contexte projet (instructions spécifiques, fichiers associés au projet).

### R4. Évaluation méthodique des transports
- Tester et catégoriser les capacités selon 3 modes de transport par ordre de priorité :
  1. `DIRECT_HTTP` : appels réseau directs du backend local vers `/backend-api/` avec cookies/headers de session.
  2. `BROWSER_FETCH` : appels exécutés dans le contexte du navigateur authentifié.
  3. `UI_DRIVEN` : automatisation de l'interface officielle par navigateur piloté (repli explicite).
- Consigner précisément pour chaque opération (liste, lecture, écriture, streaming) le transport fonctionnel validé.

### R5. Interface et exploitation
- Interface utilisateur simple et autonome (projets, conversations, historique, zone de saisie, statuts d'exécution et de transport).
- Procédure d'installation et de lancement reproductible.
- Dépendances verrouillées et architecture modulaire séparant adaptateur ChatGPT et interface.

## Acceptance Criteria

### A1. Découverte projets et conversations
- [ ] Le POC retrouve un projet existant et ses conversations avec les identifiants identiques à ceux affichés sur chatgpt.com.

### A2. Historique et arborescence
- [ ] Le POC affiche l'historique d'une conversation et suit fidèlement la branche active sans aplatissement ni omission.

### A3. Envoi POC -> Site
- [ ] Un message envoyé depuis le POC produit une réponse et l'ensemble de l'échange apparaît dans la même conversation sur chatgpt.com.

### A4. Envoi Site -> POC
- [ ] Un message ajouté directement sur chatgpt.com est récupéré par le POC et la conversation peut être poursuivie depuis le POC.

### A5. Contexte projet et fichiers
- [ ] Démonstration de la prise en compte d'une instruction de projet et d'un fichier témoin avec des marqueurs distinctifs absents du message de test.

### A6. Résilience au redémarrage
- [ ] Après arrêt et redémarrage complet du service local, la continuité de la conversation et du projet est immédiatement opérationnelle sans duplication.

### A7. Gestion de session invalide
- [ ] En cas de session manquante ou expirée, l'application présente un message d'erreur intelligible et guide vers la procédure de reconnexion sans crash ni boucle infinie.

### A8. Robustesse et non-duplication
- [ ] Réalisation de trois cycles successifs de continuité sans créer de conversation dupliquée ni doubler l'envoi d'un message.

## Livrables
- Code source avec dépendances verrouillées.
- `README.md` documentant installation, authentification, lancement et reconnexion.
- Commandes de tests reproductibles.
- Notes de recherche (`RESEARCH.md`) et matrice des capacités par transport.
- Journal d'avancement (`PROGRESS.md`).
- Rapport de vérification avec résultats et preuves pour A1 à A8 (`VERIFICATION.md`).
- Statut final documenté : `POC_VALIDATED`, `POC_PARTIAL` ou `POC_BLOCKED`.
