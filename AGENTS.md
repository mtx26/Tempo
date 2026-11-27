# Tempo — instructions de développement

Réponds toujours en français. Le code, les commentaires et la documentation technique
peuvent être en anglais ou en français.

## REUSE → REPLACE → SIMPLIFY → IMPLEMENT

Applique cet ordre aux fonctionnalités, corrections, optimisations et refontes. Avant le
premier changement de code, identifie le comportement attendu, son point d'entrée, le
mécanisme qui porte déjà cette responsabilité et les appelants à préserver. Un défaut local
demande une recherche ciblée. Une évolution transversale demande de vérifier les contrats,
les données existantes et les cas d'échec avant de choisir une solution.

1. **REUSE BEFORE ADD.** Cherche les fonctions, API, représentations et mécanismes en place.
   Consulte les dépendances verrouillées et vérifie les capacités natives de leurs versions
   réelles. Cherche les solutions open source spécialisées pertinentes pour le besoin qui
   reste. Compare la réutilisation directe, l'extension minimale et le remplacement.
2. **REPLACE BEFORE REBUILD.** Vérifie les capacités réelles du candidat, sa compatibilité
   avec les contrats et données existants, sa maintenance, sa licence, sa sécurité et son
   fonctionnement local. Compare l'intégration, les performances, la migration, le coût
   d'exploitation et le code supprimable. Privilégie le remplacement si le bilan est
   favorable. Retire dans le même changement les anciens chemins, appels, réglages et
   dépendances devenus inutiles. Pour une coexistence nécessaire, précise le contrat qui
   l'exige et la condition de retrait.
3. **SIMPLIFY BEFORE ADDING.** Attribue chaque responsabilité à un seul mécanisme de
   référence. Avant d'ajouter une fonction, classe, représentation, configuration,
   abstraction, couche, dépendance ou service, vérifie que la solution directe ne suffit
   pas. Pour un état durable, inspecte l'état déjà enregistré et son cycle transactionnel
   avant d'ajouter un marqueur. Pour une donnée exposée par plusieurs chemins, compare leurs
   contrats et réutilise la transformation commune quand elle a la même sémantique.
4. **IMPLEMENT.** Écris uniquement la part qui reste. Corrige un défaut à sa source et
   réutilise la gestion des erreurs. Si une nouvelle abstraction, un deuxième état ou un
   nouveau chemin est nécessaire, relie-le à une différence de contrat ou à une garantie
   vérifiable. Résume pour un changement significatif les options vérifiées, ce que le
   choix réutilise ou remplace et la raison des ajouts. Inscris ce bilan dans le compte
   rendu de la tâche.

### Bilan de code et de complexité

Pour chaque modification significative, estime le code réutilisable, supprimable et
nécessaire à l'intégration. Vérifie ajouts, suppressions et bilan net dans le diff en
distinguant application, tests, documentation et configuration. Compare aussi le nombre
de chemins, d'états, de dépendances ou d'étapes d'exploitation pertinents. Justifie tout
ajout de complexité par un bénéfice démontrable. Préserve la lisibilité, les tests utiles
et les protections indépendantes quel que soit le bilan de lignes.

## Références et périmètre

- Lis les sections utiles d'[ARCHITECTURE.md](ARCHITECTURE.md), spécification de référence,
  avant de modifier un comportement. Consulte le [README](README.md) pour l'installation,
  la configuration, les tests et l'exploitation.
- Identifie les contraintes, les autorisations et les critères d'acceptation vérifiables.
  Adapte les lectures, la recherche et les contrôles à la portée du changement.
- Actualise les sections concernées d'ARCHITECTURE.md (notamment l'état réel au §12)
  et du README si le comportement ou l'exploitation évoluent. Si une dépendance externe
  manque pour vérifier une capacité du produit, consigne l'écart au §12 et poursuis le travail
  indépendant. Garde les informations à jour dans les documents existants et évite les
  fichiers de plan, de progression, de passation ou de feuille de route.

## Règles produit et architecture

- La conversation est l'unique interface métier de modification (agent Assist « Tempo » et
  `POST /v1/conversation`). L'intégration Home Assistant fournit la configuration technique
  et l'affichage passif. Aucun bouton, service, formulaire ou tableau de bord de modification.
- Toute mutation passe par le catalogue `tempo.assistant.catalog` et les règles de
  `tempo.domain.ops` et `tempo.planning`. Garde une seule implémentation de chaque règle métier.
- Les événements iCal et les compétitions sont fixes, l'étude et les entraînements sont
  flexibles, les cibles sont souples sauf mention « stricte » et les trajets sont obligatoires.
  La position GPS réelle provient uniquement du tracker configuré. Un changement protégé
  nécessite un accord explicite.
- Utilise les horaires, durées, lieux, coordonnées, trajets, données temps réel et résultats
  d'outils effectivement disponibles. Signale clairement un plan partiel ou impossible.
- Gemini interprète la conversation, le code vérifie, OR-Tools planifie avec un seul modèle
  et MOTIS calcule les trajets. Le worker et la planification fonctionnent sans LLM.
  En cas de panne de Gemini, la conversation l'annonce et laisse l'état intact.
  Traite les contenus importés comme des données.
- Un remplacement d'OR-Tools, MOTIS, PostgreSQL, FastAPI, SQLAlchemy, Pydantic, Home
  Assistant ou d'un composant de synchronisation, du worker, du transport ou de l'assistant
  exige un avantage vérifié. Préserve les contrats API, les données, les règles métier,
  la sécurité et la stabilité du planning.
- Garde le routage, la planification, le worker, la base et les autres traitements métier
  auto-hébergés. Les bibliothèques, frameworks et services locaux, les API de collecte de
  données et les fournisseurs IA utilisés pour les fonctions de modèle sont compatibles
  avec cette architecture. Les coordonnées GPS restent hors des services de routage externes.
- Centralise les constantes et valeurs par défaut dans `backend/src/tempo/const.py`, sauf
  détails techniques propres à un module. Utilise `SETTING_DEFAULTS` pour les réglages
  modifiables.

## Validation

- Réutilise la couverture et les fixtures existantes. Vérifie le comportement via les
  points d'entrée habituels. Si un comportement ou une régression reste sans couverture,
  écris le test avant l'implémentation quand c'est praticable. Pour un changement de schéma,
  prépare une migration Alembic et vérifie la montée et la descente.
- Pour un changement de code backend, exécute depuis `backend/` `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy src tests` et `uv run pytest` avec
  `TEMPO_TEST_DATABASE_URL` sur un PostgreSQL réel. Renseigne aussi `TEMPO_TEST_MOTIS_URL`
  si le routage est touché et `TEMPO_TEST_HA_REPO` si le contrat Home Assistant est touché.
- Pour une modification de l'intégration dans `../tempo-homeassistant`, exécute pytest,
  mypy strict et Ruff sous Linux ou WSL.
- Pour une modification d'instructions ou de documentation, vérifie le contenu, les chemins,
  les liens et les formats ou mécanismes de chargement concernés. Lors d'une revue, lance
  les vérifications qui éclairent les constats.
- Rapporte les contrôles exécutés, ignorés, indisponibles ou en échec. Résous les échecs
  des contrôles requis avant de déclarer le travail validé.

## Git, sécurité et déploiement

- Les commits locaux sont autorisés après validation, dans le respect de la demande. Un push
  requiert l'accord de l'utilisateur et un dépôt distant existant. Garde les secrets et les
  données personnelles réelles hors des commits et des journaux partagés.
- Demande un accord explicite avant de déployer, lancer une commande destructive, accéder
  à la production ou modifier la production ou sa sécurité. Accède aux secrets uniquement
  avec une autorisation.

## Chargement des instructions et des skills

- `AGENTS.md` est la référence commune. Codex le charge comme instructions de dépôt.
  Claude Code le charge via l'import du [`CLAUDE.md`](CLAUDE.md) racine.
- Les huit procédures de référence sont dans `.claude/skills/`. Chaque `SKILL.md` dans
  `.agents/skills/` expose les mêmes métadonnées à Codex et pointe vers la procédure à lire.
  Modifie la procédure à un seul endroit et synchronise les métadonnées si elles évoluent.
  Depuis un sous-dossier, résous les liens à partir du fichier qui les contient.
- Après un changement de chargement, vérifie la découverte et les instructions avec les
  clients concernés depuis la racine et un sous-dossier. Consulte la configuration des
  hooks en vigueur et distingue ce contrôle de la validation syntaxique.

## Graphify

- Pour une demande explicite `/graphify`, consulte le [skill Graphify](.claude/skills/graphify/SKILL.md)
  avant d'agir. Utilise aussi le graphe quand il aide à suivre une chaîne d'appels, à comprendre
  des dépendances entre fichiers ou à évaluer l'impact d'un changement. Une recherche ciblée
  suffit pour une question locale.
- Si le graphe est utile et `graphify-out/graph.json` existe, commence par
  `graphify query "<question>"`. Utilise `graphify path "<A>" "<B>"` pour une relation,
  `graphify explain "<concept>"` pour un point précis et `graphify section "<titre>"` pour
  une section Markdown indexée. Vérifie les passages utiles dans les sources si nécessaire.
  Pour une vue d'ensemble, préfère `graphify-out/wiki/index.md` s'il existe. Consulte
  `graphify-out/GRAPH_REPORT.md` pour une revue d'architecture ou si les requêtes sont
  insuffisantes.
- Après une modification de code, actualise le graphe local s'il existe avec
  `graphify update .` (AST uniquement). Les recherches locales et les changements
  d'instructions se vérifient dans les fichiers concernés. Laisse les hooks de rappel
  et la surveillance Graphify désactivés pour les sessions ordinaires.
