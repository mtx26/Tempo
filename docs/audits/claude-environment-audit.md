# Audit de l'environnement Claude Code — Tempo

- Date : 2026-11-23
- Portée : lecture seule. Aucune installation, désinstallation ou reconfiguration. Aucun code applicatif modifié.
  Seul ce fichier a été créé.
- Méthode : inspection directe des fichiers de configuration, commandes CLI non destructives
  (`claude --version`, `claude plugin list`, `claude mcp list`), exécution des contrôles qualité du backend,
  analyse statistique des transcripts de session (`~/.claude/projects/...Tempo/*.jsonl`, comptage des appels
  d'outils uniquement, sans contenu personnel).

Légende : **VERIFIED** (preuve directe de fonctionnement) · **PARTIALLY VERIFIED** (chargé/présent, fonctionnement
non démontré entièrement) · **NOT WORKING** · **NOT INSTALLED** · **UNKNOWN** (impossible à vérifier sans risque).

---

## 1. Inventaire de l'environnement

### 1.1 Claude Code
| Élément | Valeur | Statut |
|---|---|---|
| Version | `2.1.330 (Claude Code)` | VERIFIED |
| Modèles utilisés (dernière session) | `claude-opus-5-5` (principal, 20,10 $), `claude-haiku-5-5` (sous-tâches, 0,12 $) — source `~/.claude.json` `lastModelUsage` | VERIFIED |
| Niveau d'effort global | `effortLevel: "xhigh"` (`~/.claude/settings.json`) | VERIFIED |
| Mode de permissions de cette session | bypass permissions ; `skipDangerousModePermissionPrompt: true` | VERIFIED |
| Mise à jour automatique | canal `latest` | VERIFIED |
| `claude doctor` | non exécuté (commande interactive) | UNKNOWN |

### 1.2 Hiérarchie d'instructions
| Fichier | Présent | Remarque |
|---|---|---|
| `~/.claude/CLAUDE.md` (utilisateur) | non | — |
| `Tempo/CLAUDE.md` (projet, versionné) | oui, 3 995 o | Contrat principal, chargé à chaque session (VERIFIED : présent dans le contexte) |
| `.claude/settings.json` (projet) | oui, 154 o | Uniquement 4 règles `deny` |
| `.claude/settings.local.json` | non | — |
| `~/.claude/settings.json` (utilisateur) | oui, 6 923 o | ~55 règles `allow` héritées d'un autre projet, 2 plugins activés, 2 marketplaces |
| `docs/BOOTSTRAP_PROMPT.md` | oui | Prompt initial, désormais partiellement contradictoire (voir §3) |
| Mémoire auto (`~/.claude/projects/C--Users-mtx-2-Documents-Code-Tempo/memory/`) | répertoire vide | Aucune mémoire persistante |

### 1.3 Plugins (`claude plugin list`)
| Plugin | Version | Portée | Statut CLI |
|---|---|---|---|
| superpowers@superpowers-marketplace | 7.0.2 | user | enabled |
| context7@context7-marketplace | 1.0.4 | user | enabled |
| ponytail@ponytail | 5.3.3 | project | **disabled** |
| agent-skills@addy-agent-skills | 0.6.17 | project | **disabled** |
| cowork-plugin-management@synced | 0.2.2 | synchronisé claude.ai | loaded |
| mcpmarket-me@synced | 0.1.0 | synchronisé claude.ai | loaded (unique skill : `kicad`) |
| Marketplace `claude-plugins-official` | — | — | connue, aucun plugin installé depuis |

### 1.4 Serveurs MCP (`claude mcp list`)
| Serveur | État |
|---|---|
| claude.ai Claude Docs | ✔ Connected |
| plugin:context7:context7 (HTTP) | **! Needs authentication** — seuls les outils `authenticate`/`complete_authentication` sont exposés |
| Serveurs MCP utilisateur/projet (`~/.claude.json`, `.mcp.json`) | aucun |
| IDE (`mcp__ide__*`) | exposé par l'extension VS Code |

### 1.5 Sous-agents projet (`.claude/agents/`)
`architect`, `integrations`, `scheduler`, `security`, `tester`, `transport` — tous `tools: Read, Grep, Glob, Bash`,
`model: inherit`, corps de 1 paragraphe généré depuis un gabarit commun.

### 1.6 Skills projet (`.claude/skills/`)
`debug`, `implement-feature`, `review`, `tempo-deploy`, `tempo-integrations`, `tempo-schedule`, `tempo-transport` —
tous construits sur le même gabarit en 6 étapes ; l'étape 2 répète simplement la description.

### 1.7 Hooks actifs
| Source | Événement | Commande | Timeout |
|---|---|---|---|
| superpowers | SessionStart (`startup\|clear\|compact`) | `hooks/run-hook.cmd session-start` → injecte `using-superpowers` (3 192 o) | non défini, `async: false` |
| mcpmarket-me (synced) | SessionStart (`startup\|resume\|clear\|compact`) | `bash hook-shim.sh` (synchro MCPmarket) | 30 s |
| mcpmarket-me (synced) | PostToolUse / PostToolUseFailure sur `Skill` | `bash shared/skill-invocation.sh` | 10 s |
| projet / utilisateur | — | aucun hook défini | — |
| Git (`.git/hooks`) | — | aucun hook actif (seulement `*.sample`) | — |

### 1.8 Outils de développement
| Outil | Résultat | Statut |
|---|---|---|
| uv | `uv` disponible, `uv.lock` présent | VERIFIED |
| Ruff | `uv run --frozen ruff check .` → `All checks passed!` ; `ruff format --check` → 44 fichiers OK | VERIFIED |
| mypy strict | `uv run --frozen mypy src tests` → `Success: no issues found in 43 source files` (absent du PATH global, présent via uv) | VERIFIED |
| pytest | `157 passed, 41 skipped in 14.08s` — les 41 tests d'intégration (PostgreSQL/MOTIS) sont SKIPPED sans `TEMPO_TEST_DATABASE_URL` | PARTIALLY VERIFIED |
| Couverture | non mesurée dans cet audit (dernière valeur déclarée : 98 %) | UNKNOWN |
| pip-audit | non exécuté (accès réseau, hors périmètre) | UNKNOWN |
| Docker | Docker Desktop **en cours d'exécution** (processus + distro WSL `docker-desktop`), mais `docker` **absent du PATH** des shells Bash et PowerShell de Claude | NOT WORKING (depuis Claude) |
| gh | installé | non testé (aucun remote) |
| Git | dépôt local, branche `main`, 7 commits, **aucun remote** | VERIFIED |
| CI GitHub Actions | `.github/workflows/backend.yml` présent, **jamais exécuté** (pas de remote) | NOT WORKING (non exécutable) |

### 1.9 Variables d'environnement (noms seulement, pertinentes)
`CLAUDECODE`, `CLAUDE_EFFORT`, `CLAUDE_CODE_*` (session, entrypoint, messaging), `VSCODE_*`. Aucune variable
`ANTHROPIC_BASE_URL` / `ANTHROPIC_*` (ni dans la session, ni au niveau User/Machine Windows) → **aucun routage de
modèle personnalisé**. Aucune variable `TEMPO_*` dans la session.

### 1.10 Documentation projet
`CLAUDE.md`, `PROGRESS.md`, `README.md`, `docs/{REQUIREMENTS, ARCHITECTURE, ROADMAP, DECISIONS (D001–D018+),
SCHEDULER, TRANSPORT, FEASIBILITY, DEPLOYMENT, INSTALLATION, BOOTSTRAP_PROMPT}.md` — 35,8 Ko au total.
Dépôt annexe `../tempo-homeassistant` : 1 commit (`c485e2c`, scaffold).

---

## 2. Matrice de vérification outil par outil

| Outil | Installé | Version | Configuré | Accessible | Test sûr | Activation | Bénéfice Tempo | Recouvrement | Surcoût |
|---|---|---|---|---|---|---|---|---|---|
| **Superpowers** | oui | 7.0.2 | oui | oui (15 skills listés) | **VERIFIED** : hook SessionStart a injecté `using-superpowers` dans cette session ; historiquement 1 appel `superpowers:test-driven-development` | auto (hook) + manuel (Skill) | Élevé (TDD, debugging, plans, vérification) | Forte avec skills projet `debug`/`implement-feature`/`review` et avec agent-skills | ~800 tokens/session + incitation forte à invoquer des skills |
| **Context7** | oui | 1.0.4 | partiellement | non : outils doc absents | **NOT WORKING** : `! Needs authentication` | — | Moyen (docs à jour OR-Tools, FastAPI, HA) | WebSearch/web-fetch | Faible |
| **Ponytail** | oui (cache) | 5.3.3 | désactivé | non (aucun skill chargé) | **NOT WORKING (désactivé)** ; 36 usages historiques (22/11) ; fichier `~/.claude/.ponytail-active` = `full` et script statusline orphelins | — | Faible/inconnu (compression de réponses) | — | Nul actuellement |
| **Agent Skills** (addyosmani) | oui (cache) | 0.6.17 | désactivé | non | **NOT WORKING (désactivé)** ; son agent `code-reviewer` a été utilisé 6 fois les 22–23/11 avant désactivation | — | Faible (doublon Superpowers) | Très forte avec Superpowers (TDD, debugging, review, plans) | Élevé si réactivé (26 skills) |
| **Graphify** | oui (CLI hors Claude) | 0.9.115 | non (ni skill, ni MCP enregistré ; `graphify-mcp.exe` présent mais non déclaré) | non depuis Claude | **PARTIALLY VERIFIED** : `graphify --version` OK ; aucun `graphify-out/` dans le dépôt | — | Faible à ce stade (dépôt ~45 fichiers source) | Grep/Glob/Explore | Nul actuellement |
| **OmniRoute** | oui (`%LOCALAPPDATA%\omniroute`, Node embarqué) | v3.8.54 (`serve.log`) | **non relié à Claude Code** | non | **NOT WORKING** : port 20128 fermé (connexion refusée) ; dernier démarrage 22/11 22:38 ; aucune `ANTHROPIC_BASE_URL` | — | Aucun actuellement | — | Nul ; si activé, risque d'acheminer prompts/code via un proxy tiers |
| **Serena** | non | — | — | — | **NOT INSTALLED** | — | — | — | — |
| **mcpmarket-me** (synced) | oui | 0.1.0 | oui | oui (skill `kicad`) | **VERIFIED** : hook affiché en tête de session (`MCPmarket sync: cached`) | auto (hooks) | **Aucun** (KiCad sans rapport) | — | Hook SessionStart ≤30 s + hook sur chaque appel Skill |
| **cowork-plugin-management** (synced) | oui | 0.2.2 | oui | oui | PARTIALLY VERIFIED (listé) | manuel | Aucun | — | Entrées dans la liste de skills |
| **Claude Docs MCP** | oui | — | oui | oui | VERIFIED (`✔ Connected`) | manuel | Faible | Fichiers `docs/` du dépôt | Description d'outils dans le contexte |
| **Skills anthropic synchronisés** (docx, pptx, xlsx, pdf, morning, computer-use…) | oui | — | oui | oui | PARTIALLY VERIFIED (listés) | manuel | Quasi nul | — | ~2–3 k tokens de descriptions par session |

---

## 3. Conflits et redondances de configuration

| # | Constat | Preuve | Gravité |
|---|---|---|---|
| C1 | **Liste `allow` utilisateur obsolète** : ~55 règles héritées d'un projet Django/Expo (`localhost:8009`, `pnpm`, `expo`, `react-native-google-signin`), dont `grep … GOOGLE_OAUTH_CLIENT_ID … .env`, et des jokers larges (`Bash(python -c ' *)`, `Bash(node -e ' *)`, `Bash(uv run *)`) appliqués aussi à Tempo | `~/.claude/settings.json` | Moyenne (sécurité/hygiène) |
| C2 | **Garde-fous `deny` contournables** alors que le mode bypass est actif : `Bash(git push *)` n'empêche ni `git -C . push`, ni l'outil PowerShell ; `Bash(rm -rf *)` n'empêche ni `rm -r -f` ni `Remove-Item -Recurse`. Aucun deny pour `docker service`, `docker stack rm`, `git push` via PowerShell | `.claude/settings.json` | Moyenne |
| C3 | `BOOTSTRAP_PROMPT.md` interdit de committer sans approbation ; `CLAUDE.md` (politique autonome) autorise les commits locaux automatiques ; `CLAUDE.md` contient lui-même « no unapproved commits » puis « local commits are authorized » | `docs/BOOTSTRAP_PROMPT.md`, `CLAUDE.md` | Faible (résolu en pratique par la règle la plus récente, mais ambigu) |
| C4 | **`PROGRESS.md` non à jour et auto-contradictoire** : M6a (commit `d89ba6e`) absent ; « CI … not a git repo » et « `git init` + first commit only with your approval » obsolètes ; « PostgreSQL / MOTIS integration tests: none yet (no Docker locally) » contredit M4b « 7 live integration tests pass » ; nombre de tests 179 vs 198 annoncés dans le commit M6a | `PROGRESS.md`, `git log` | **Élevée** (reprise de session) |
| C5 | `docs/ROADMAP.md` ne mentionne pas M6a ; la règle « Update PROGRESS.md and the roadmap after each milestone » n'a pas été respectée au dernier jalon | `git show --stat d89ba6e` (aucun fichier doc) | Élevée |
| C6 | Étape « Dependency vulnerability audit » **dupliquée** dans la CI | `.github/workflows/backend.yml` | Faible |
| C7 | Skills projet `debug` / `implement-feature` / `review` redondants avec `superpowers:systematic-debugging` / `test-driven-development`+`executing-plans` / `requesting-code-review` et avec les commandes intégrées `/code-review`, `/review` | listes de skills | Moyenne (ambiguïté de sélection) |
| C8 | Skill `review` demande de « mettre à jour PROGRESS.md » : inadapté à une revue (effet de bord) | `.claude/skills/review/SKILL.md` | Faible |
| C9 | Agents décrits comme devant « implémenter… mettre à jour la documentation » mais sans `Edit`/`Write` : toute écriture passerait par des heredocs Bash fragiles. Dans les faits, ils ne servent qu'à des revues | `.claude/agents/*.md`, transcripts | Moyenne |
| C10 | Ponytail désactivé mais traces actives : `~/.claude/.ponytail-active`, `ponytail-modes/`, `ponytail-statusline.ps1` non branché | `~/.claude/` | Faible |
| C11 | `docs/INSTALLATION.md` indique `C:\Projects\Tempo` ; le dépôt est dans `C:\Users\mtx_2\Documents\Code\Tempo` | doc | Faible |
| C12 | Doubles entrées projet dans `~/.claude.json` (`c:/…/Tempo` et `C:/…/Tempo`, casse différente) | `~/.claude.json` | Faible |
| C13 | `temp.txt` (ce prompt d'audit) non suivi à la racine du dépôt | `git status` | Faible |

---

## 4. Outils inutilisés ou non fonctionnels

| Outil | État | Preuve d'usage |
|---|---|---|
| Context7 | NOT WORKING (authentification manquante) | 0 appel |
| OmniRoute | NOT WORKING (arrêté, non relié) | aucun routage |
| Graphify | installé, non intégré | 0 appel, aucun graphe |
| Ponytail | désactivé | 36 usages avant désactivation (22/11) |
| Agent Skills | désactivé | 6 appels `code-reviewer` avant désactivation |
| Serena | NOT INSTALLED | — |
| mcpmarket-me (`kicad`) | actif mais sans rapport avec Tempo | 0 appel utile ; hooks exécutés à chaque session |
| Skills projet (7) | chargés | **0 invocation** dans les 4 transcripts Tempo |
| Agents `architect`, `integrations` | chargés | **0 invocation** |
| Agents `security` (3), `scheduler` (2), `transport` (2), `tester` (1) | VERIFIED (exécutés en session `fd57a8f6`) | revues adverses ayant produit des correctifs documentés dans `PROGRESS.md` |
| Superpowers | actif | 1 skill invoqué en 4 sessions (`test-driven-development`) ; ni `writing-plans`, ni `verification-before-completion`, ni `systematic-debugging` |
| Mémoire auto | vide | 0 fichier |

Répartition des appels d'outils, dernière session complète (`fd57a8f6`) : Bash 288, Write 44, Edit 22, Agent 13,
Read 5, Skill 1. Le travail repose massivement sur Bash ; les outils dédiés (Read/Grep) sont peu utilisés.

---

## 5. Efficacité du contexte et des tokens

- **Contexte de base raisonnable** : `CLAUDE.md` ≈ 1 k tokens ; lecture obligatoire des 5 documents de démarrage
  ≈ 4–5 k tokens. Pas d'injection volumineuse côté projet.
- **Surcharge évitable** : injection Superpowers (~800 tokens, à chaque `startup|clear|compact`) ; liste de ~45
  skills dont une majorité hors sujet (KiCad, cowork, docx/pptx/xlsx, morning, computer-use, Chrome…) ≈ 2–3 k
  tokens ; description des outils Claude Docs MCP.
- **Répétitions** : « Always respond in French » répété dans `CLAUDE.md` (2×), chacun des 6 agents et 7 skills ;
  le gabarit commun des skills répète les mêmes étapes génériques sans information spécifique (commandes uv,
  `TEMPO_TEST_DATABASE_URL`, `backend/scripts/dev_postgres.py`, conteneur `tempo-motis-brussels`).
- **Coût réel** : dernière session 20,22 $, dont 20,10 $ sur Opus avec 50,6 M tokens lus en cache sur une seule
  session de ~8 h. L'effort global `xhigh` et les sessions très longues dominent le coût, bien plus que la
  configuration des plugins.
- **Isolation des agents** : correcte (agents en contexte séparé, outils restreints, `model: inherit` → Opus pour
  chaque revue, coûteux pour des tâches de lecture).

---

## 6. Autonomie de développement actuelle

| Capacité | Évaluation |
|---|---|
| Comprendre la tâche / lire le contrat | VERIFIED (procédure « First actions » claire) |
| Planification | PARTIALLY VERIFIED — aucun plan écrit persistant (pas de `docs/plans/`, `writing-plans` jamais utilisé) |
| TDD et qualité | VERIFIED — Ruff, mypy strict et pytest verts ; tests de régression documentés |
| Revues indépendantes par agents | VERIFIED (security/scheduler/transport/tester) |
| Points de contrôle Git | VERIFIED (1 commit par jalon) |
| Tests d'intégration reproductibles | NOT WORKING depuis le shell de Claude (Docker hors PATH, pas de `TEMPO_TEST_DATABASE_URL`) → 41 tests SKIPPED |
| CI | NOT WORKING (jamais exécutée) |
| Reprise après interruption | **Fragile** : `PROGRESS.md` est la seule source de reprise et il est en retard d'un jalon et contradictoire ; mémoire auto vide ; pas de plan en cours persisté ; dernier commit à 10:02 UTC alors que la session `fd57a8f6` a duré jusqu'à 16:36 UTC (activité intermédiaire non tracée — UNKNOWN) |
| Garde-fous automatiques (hooks qualité) | Absents : rien n'empêche de terminer un jalon sans lint/tests, ni de committer sans mise à jour de `PROGRESS.md` |

---

## 7. Validation de bout en bout (Phase 7)

| Étape | Test effectué | Résultat |
|---|---|---|
| Compréhension de la tâche | Lecture de `temp.txt` + `CLAUDE.md` chargé automatiquement | VERIFIED |
| Planification | — | Pas de test indépendant possible sans créer de fichier |
| Sélection de skill | Hook Superpowers exécuté ; skills projet visibles dans la liste | PARTIALLY VERIFIED (chargement prouvé ; invocation des skills projet jamais observée) |
| Délégation à un agent | Preuve historique : 8 invocations réussies d'agents projet | VERIFIED pour 4/6 agents ; architect/integrations PARTIALLY VERIFIED |
| Consultation de documentation | Context7 : NOT WORKING ; web-fetch/WebSearch : utilisés historiquement (VERIFIED) | PARTIALLY VERIFIED |
| Vérification sûre | Ruff, format, mypy, pytest exécutés | VERIFIED (avec 41 SKIPPED) |
| Rapport de progression | `PROGRESS.md` non mis à jour au dernier jalon | NOT WORKING (procédure non respectée) |

Non effectué volontairement : nouvel appel d'agent (coût Opus, preuve historique suffisante) ; authentification
Context7 et démarrage d'OmniRoute (reconfiguration interdite) ; tests d'intégration live (nécessitent de démarrer
PostgreSQL/MOTIS).

Procédure de vérification manuelle proposée :
1. Context7 : `/mcp` → `context7` → Authenticate, puis demander « résous la doc OR-Tools CP-SAT via Context7 ».
2. Intégration : ajouter le dossier `docker` de Docker Desktop au PATH, puis
   `uv run python scripts/dev_postgres.py` et `TEMPO_TEST_DATABASE_URL=… uv run pytest -q` dans `backend/` ;
   attendu : 0 SKIPPED hors MOTIS si le conteneur MOTIS n'est pas lancé.
3. Agents non testés : `Agent(subagent_type="architect", prompt="Liste les ADR de docs/DECISIONS.md sans rien modifier")`.

---

## 8. Plan d'optimisation priorisé

| Prio | Problème actuel | Preuve | Amélioration proposée | Bénéfice | Risques / compromis |
|---|---|---|---|---|---|
| **P1** | Reprise de session peu fiable | C4, C5 | Mettre `PROGRESS.md` et `ROADMAP.md` à jour (M6a, contradictions, prochaines étapes exactes) ; ajouter une section « État courant / tâche en cours » réécrite à chaque jalon | Reprise correcte après interruption | Aucun |
| **P1** | Garde-fous contournables en mode bypass | C2 | Compléter `deny` : `Bash(git -C * push*)`, `PowerShell(git push*)`, `Bash(rm -r *)`, `PowerShell(Remove-Item * -Recurse*)`, `Bash(docker stack *)`, `Bash(docker service *)` ; envisager de ne pas utiliser bypass pour les sessions autonomes longues | Réduit le risque d'action irréversible | Quelques confirmations supplémentaires |
| **P1** | Allowlist utilisateur obsolète | C1 | Purger les ~55 règles du projet Django/Expo de `~/.claude/settings.json` (les garder éventuellement dans les settings de ce projet-là) | Hygiène, moins de surface | Prompts de permission à reconstituer dans l'autre projet |
| **P2** | Jalon terminé sans mise à jour doc ni contrôle automatique | C5 | Hook `Stop` ou script `backend/scripts/check.sh` (ruff + format + mypy + pytest) appelé par la skill `implement-feature` ; vérification que `PROGRESS.md` est modifié avant un commit de jalon | Qualité systématique | Hook Stop trop lent si tests longs → limiter aux contrôles rapides |
| **P2** | Tests d'intégration non reproductibles | §1.8 | Documenter dans `CLAUDE.md`/skill la commande exacte (PATH Docker Desktop, `dev_postgres.py`, `TEMPO_TEST_DATABASE_URL`) | 41 tests réellement exécutés | Aucun |
| **P2** | Skills projet génériques jamais invoqués, redondants avec Superpowers | C7, §4 | Supprimer `debug`/`review` (utiliser `superpowers:systematic-debugging`, `/code-review`) ; réécrire `implement-feature` et les `tempo-*` avec des commandes et pièges spécifiques à Tempo ; décrire explicitement dans `CLAUDE.md` quel skill utiliser à quelle étape | Moins d'ambiguïté, skills réellement utiles | Travail de rédaction |
| **P2** | Agents en Opus pour des revues en lecture seule ; rôle « implémenter » sans outils d'écriture | C9, §5 | Redéfinir les agents comme réviseurs (supprimer « implement/update documentation ») ; tester `model: sonnet` pour `tester`/`transport`, garder `inherit` pour `security`/`scheduler` | Coût réduit, rôle clair | Revue potentiellement moins profonde |
| **P3** | Context7 inutilisable | §2 | Authentifier Context7 (`/mcp`) **ou** le désactiver | Docs à jour OR-Tools/HA/FastAPI | Compte tiers ; requêtes envoyées à Upstash (pas de données privées à y mettre) |
| **P3** | Plugins/skills hors sujet dans le contexte | §5 | Désactiver pour ce projet les synchronisations sans rapport (mcpmarket-me/kicad, cowork) si l'interface claude.ai le permet | ~2–3 k tokens/session, un hook en moins | Ces outils resteraient utiles dans d'autres projets |
| **P3** | Coût de session élevé | §5 | Sessions plus courtes par jalon, `/clear` entre jalons (la reprise dépend alors de P1) ; envisager `effortLevel: high` pour le travail courant | Coût et latence réduits | Raisonnement moins poussé sur les tâches difficiles |
| **P3** | Pas de plans persistés | §6 | Utiliser `superpowers:writing-plans` pour chaque jalon, sauvegardé dans `docs/plans/` | Reprise et traçabilité | Quelques tokens de plus |
| **P4** | OmniRoute installé mais inactif | §2 | Ne pas l'activer pour Tempo : aucun besoin démontré, ajouterait un proxy tiers sur l'accès modèle | Simplicité, sécurité | — |
| **P4** | Graphify, Ponytail, Agent Skills inactifs | §4 | Laisser désactivés ; nettoyer les fichiers orphelins Ponytail (C10). Réévaluer Graphify si le dépôt dépasse quelques centaines de fichiers | Configuration lisible | — |
| **P4** | Divers | C6, C11, C12, C13 | Retirer l'étape pip-audit dupliquée ; corriger le chemin d'installation ; déplacer/supprimer `temp.txt` | Propreté | — |

Aucune nouvelle installation n'est recommandée.

---

## 9. Verdict final

**READY WITH ISSUES**

Le socle de développement est fonctionnel et vérifié (Ruff, mypy strict, 157 tests verts, Superpowers actif,
agents de revue opérationnels, commits par jalon). Les problèmes bloquants pour une autonomie longue sont la
**fiabilité de la reprise** (PROGRESS/ROADMAP en retard et contradictoires), des **garde-fous de permission
contournables en mode bypass**, une **allowlist utilisateur héritée d'un autre projet** et l'**impossibilité
d'exécuter les tests d'intégration depuis le shell de Claude**. Context7, OmniRoute, Graphify, Ponytail et Agent
Skills n'apportent actuellement aucune valeur active.
