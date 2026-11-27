# Tempo

Planificateur personnel auto-hébergé, géré **uniquement par la conversation** (texte ou voix)
dans Home Assistant. Le backend (API, worker, PostgreSQL, OR-Tools, MOTIS) tourne dans Docker
Swarm sur un serveur Debian ; l'intégration Home Assistant est dans le dépôt séparé
`tempo-homeassistant` (HACS).

- Produit, règles, architecture, état réel et écarts : [`ARCHITECTURE.md`](ARCHITECTURE.md).
- Règles de développement : [`CLAUDE.md`](CLAUDE.md).

**État** : fonctionnel et testé localement (PostgreSQL réel, MOTIS réel, client HA réel contre
le backend) ; **rien n'est publié ni déployé**. Voir `ARCHITECTURE.md` §12 pour ce qui n'a pas
encore pu être vérifié (LLM réel, Home Assistant réel, HACS, Swarm, sources réelles).

## Contenu du dépôt

| Chemin | Contenu |
|---|---|
| `backend/` | Paquet Python `tempo` (API FastAPI, worker, planificateur, transport, synchronisations), tests, `Dockerfile`. |
| `routing/` | Image MOTIS (`Dockerfile`), configurations `config/brussels.yml` (test) et `config/belgium.yml` (national), `scripts/supervisor.sh` (reconstruction atomique des graphes quand les données ou la configuration changent ; le graphe en service répond pendant l'import). Le géocodage local (`geocoding: true`) sert à la recherche d'adresse. |
| `deploy/` | `stack.yml` (Swarm), `compose.dev.yml` (pile locale), rôles PostgreSQL, sauvegarde et exercice de restauration. |
| `../tempo-homeassistant` | Intégration `custom_components/tempo` (dépôt séparé). |

## Utilisation

Dans Home Assistant, choisir l'agent de conversation **Tempo** dans Assist et parler
normalement. Exemples :

- « Prévois 15 heures de physique avant vendredi » / « … avant vendredi, c'est strict ».
- « Dentiste jeudi à 14 h au centre-ville » ; « Cinéma samedi soir, 2 h 30, quand ça m'arrange ».
- « Solfège tous les mardis à 19 h jusqu'à fin janvier » ; « Annule celui de la semaine
  prochaine » ; « À partir de février, c'est à 20 h ».
- « J'ai fait 45 minutes de physique » ; « En fait c'était 30 » ; « Session terminée ».
- « Demain je dors chez papa » ; « Je ne suis pas disponible samedi matin » (toutes les
  activités) ; « Pas d'entraînement jeudi » (cette catégorie seulement) ; « Cette semaine,
  plutôt le soir pour le sport » (préférence temporaire, elle expire seule).
- « J'alterne entre chez maman et chez papa chaque vendredi : ce vendredi je vais chez
  papa après les cours » (changement après le dernier cours ; sans cours ce jour-là, Tempo
  demande l'heure) ; « Cette semaine je reste chez maman » (exception) ; « Où je dors la semaine
  prochaine ? ».
- « Ajoute le lieu Chez papa : rue de l'Exemple 12, 7000 Mons » (adresse cherchée dans le géocodeur
  local, à confirmer) ; « Depuis chez maman, on me dépose à l'arrêt de bus du village en
  10 minutes » (point d'accès aux transports) ; « Ne me propose pas de trajet de plus de 2 h 30 ».
- « Comment j'arrive au campus pour 8 h ? » ; « Rappelle-moi 30 min avant le médecin » ;
  « Préviens-moi 60 et 15 minutes avant chaque départ ».
- « Qu'est-ce qui a changé cette nuit ? » ; « Pourquoi la chimie n'est pas planifiée ? » ;
  « Accepte la proposition » ; « Réorganise ma journée » ; « Réorganise toute ma semaine » ;
  « J'ai fini mon rapport » (séances restantes retirées) ; « Verrouille mon entraînement ».

Home Assistant affiche passivement : calendrier `calendar.tempo`, travail restant
`todo.tempo_tasks`, capteurs (prochain événement, prochain départ, état du plan, propositions,
…) et notifications. Il n'y a ni bouton ni formulaire métier.

La conversation passe par Gemini : s'il est indisponible, Tempo le dit et ne modifie rien.
Synchronisations, planification, trajets, rappels et contrôle de départ ne dépendent jamais du
LLM et restent automatiques. Les éléments déjà prévus aujourd'hui ne bougent qu'avec votre
accord (propositions visibles dans le capteur « Approbations en attente »).

Pour examiner une demande composée, demandez par exemple « Simule l'annulation de mon
rendez-vous de sport et le déplacement de ma séance de physique ». Tempo enregistre le lot
après la simulation et donne un code. Dites « confirme CODE » ou « refuse CODE » dans les
15 minutes dans le même fil Assist. Si le planning ou une de ses entrées a changé, demandez
un nouvel aperçu.

Si Tempo demande une précision, répondez dans le même fil. Les opérations préparées et leurs
contraintes restent en brouillon pendant 24 heures après la dernière précision. L'aperçu
regroupe les étapes une fois les réponses obtenues. « Annule CODE » abandonne le brouillon
identifié dans la réponse. Une autre demande peut être traitée pendant ce temps.
Home Assistant envoie une clé `Idempotency-Key` par tour et réutilise la même clé après une
coupure de la connexion. Les autres clients HTTP peuvent également fournir cette clé.
Mettez à jour le backend avant l'intégration Home Assistant pour que l'API accepte le champ
`user_id` envoyé par Assist.

## Développement local

Prérequis : Python 3.13 et [`uv`](https://docs.astral.sh/uv/) ; Docker Desktop pour MOTIS et la
pile de développement (sous Windows, `docker.exe` est dans
`$LOCALAPPDATA/Programs/DockerDesktop/resources/bin`).

```bash
cd backend
uv sync
# PostgreSQL 16 jetable pour les tests (laisser tourner ; affiche TEMPO_TEST_DATABASE_URL)
uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py .pgdata
# Qualité (dans un autre terminal)
uv run ruff format --check . && uv run ruff check . && uv run mypy src tests
TEMPO_TEST_DATABASE_URL=... uv run pytest -q --cov=tempo
```

Variables de test facultatives (sinon les tests concernés sont **ignorés avec leur raison**,
jamais réussis en silence) : `TEMPO_TEST_MOTIS_URL` (MOTIS Bruxelles),
`TEMPO_TEST_MOTIS_BE_URL` (MOTIS national), `TEMPO_TEST_HA_REPO=../../tempo-homeassistant`
(contrat avec le vrai client HA), `TEMPO_TEST_IMAP=127.0.0.1` (serveur IMAP GreenMail),
`TEMPO_TEST_BMC_KEY` (passerelle Belgian Mobility réelle, 3 requêtes).
Le test de redémarrage PostgreSQL utilise `TEMPO_TEST_POSTGRES_CONTAINER` avec un conteneur
Docker jetable nommé `tempo-audit-…`, publié sur un port local fixe. Il redémarre ce conteneur.

Si `dev_postgres.py` expire après un arrêt brutal, PostgreSQL démarre souvent quand même sur le
port affiché (voir `.pgdata/log`). Serveur IMAP de test :
`docker run -d --name tempo-greenmail -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled" -p 127.0.0.1:3025:3025 -p 127.0.0.1:3993:3993 greenmail/standalone:2.1.2`.

Intégration Home Assistant (Linux ou WSL uniquement) : voir le README de `tempo-homeassistant`
(`uv sync && uv run pytest && uv run mypy && uv run ruff check .`).

### MOTIS local

```bash
export PATH="$LOCALAPPDATA/Programs/DockerDesktop/resources/bin:$PATH"
export TEMPO_BMC_PARTNER_KEY="$(cat deploy/secrets/tempo_bmc_partner_key)"  # clé obligatoire
(cd backend && uv run python -m tempo.transport.gtfs_update ../routing/data/brussels brussels)  # ou belgium
docker build -t tempo-routing:v2.11.4 routing
W="$(cygpath -w "$PWD/routing")"
V="$(cat routing/data/brussels/READY)"
MSYS_NO_PATHCONV=1 docker run --rm --memory 6g \
  -v "$W\data\brussels\snapshots\$V:/input:ro" -v "$W\config\brussels.yml:/config/config.yml:ro" \
  -v tempo-routing-brussels:/data tempo-routing:v2.11.4 import -c /config/config.yml -d /data/brussels
MSYS_NO_PATHCONV=1 docker run -d --name tempo-motis-brussels --memory 4g \
  -p 127.0.0.1:18080:8080 -v tempo-routing-brussels:/data tempo-routing:v2.11.4 server -d /data/brussels
```

Les données expirent (le GTFS STIB du 23/11/2026 est valable jusqu'au 20/12/2026) : relancer
`gtfs_update` (le même outil que la mise à jour nocturne de production : ETag, validation,
remplacement atomique) puis l'import ; hors fenêtre, MOTIS répond 400 et Tempo signale « routage
indisponible ». Pour le national, `config/belgium.yml` lit le temps réel via le relais
(`python -m tempo.transport.rt_relay`, port 8090, à ne jamais publier : il consomme le quota du
portail).

Le relais rejette les messages TripUpdates âgés de plus de 90 s et les alertes âgées de plus
de 10 min. Il retire les prévisions individuelles dépourvues d'horodatage ou expirées avant
ingestion. Les horaires et quais précédemment enregistrés restent visibles dans iCal et
Home Assistant avec la qualification des prévisions. Une recherche d'itinéraire relance
MOTIS. Les lieux et alias
enregistrés sont recherchés avant le géocodeur local, qui permet aussi de retrouver des
adresses, établissements et arrêts. Un homonyme demande une clarification.

Le benchmark CP-SAT s'exécute depuis `backend/` avec
`uv run python -m benchmarks.scheduler_bench 2 --json` (budget de 2 s par scénario).
Ajoutez `--compare-hints` pour comparer les deux modes avec un ancien planning et des
contraintes nouvelles.

### Pile de développement

```bash
python deploy/make_dev_secrets.py              # secrets dans deploy/secrets/ (ignoré par Git)
docker compose -f deploy/compose.dev.yml up -d # postgres -> migrate -> api (127.0.0.1:18000), relais, MOTIS (127.0.0.1:18081)
```

### Recette Home Assistant réelle

Vrai Home Assistant jetable (jamais votre instance) relié à la pile de développement : flux de
configuration, entités, conversation Assist, GPS du téléphone, liste de tâches.

```bash
export PATH="$LOCALAPPDATA/Programs/DockerDesktop/resources/bin:$PATH"
docker build -t tempo-backend:dev backend
docker compose -f deploy/compose.dev.yml up -d postgres migrate api
CFG=/tmp/tempo-ha && mkdir -p $CFG/custom_components/tempo
cp -r ../tempo-homeassistant/custom_components/tempo/. $CFG/custom_components/tempo/
printf 'default_config:\n' > $CFG/configuration.yaml
MSYS_NO_PATHCONV=1 docker run -d --name tempo-ha-e2e --network tempo-dev_front \
  -p 127.0.0.1:18123:8123 -v "$(cygpath -w $CFG):/config" ghcr.io/home-assistant/home-assistant:2026.11.2
cd backend && uv run python scripts/ha_e2e.py ../deploy/secrets/tempo_api_token  # 15 contrôles
```

Scénario de recette sur la pile complète (`docker compose ... up -d`, MOTIS national) :
stabilité, déplacement minimal, trajets réels, proposition approuvée en conversation, plan
partiel expliqué, alternatives, trajet direct entre activités, localisation (15 contrôles ;
données de développement uniquement) :
`cd backend && uv run python scripts/live_scenario.py ../deploy/secrets/tempo_api_token`.

## Configuration

Toute la configuration passe par des secrets Docker (`/run/secrets/<nom>`) ou des variables
`TEMPO_*` ; aucun secret dans Git ni dans les journaux.

| Paramètre | Service | Rôle |
|---|---|---|
| `tempo_api_token` (secret) | api | Jeton de service saisi dans Home Assistant : `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `tempo_calendar_token` (secret) | api | Jeton du flux iCalendar en lecture seule `/calendar/<jeton>.ics` (agenda du téléphone) ; distinct de `tempo_api_token`. Sans lui, pas de flux. |
| `tempo_api_token_previous` (secret, temporaire) | api | Ancien jeton accepté pendant une rotation. |
| `tempo_database_url` (secret) | api, worker | `postgresql+psycopg://tempo_app:<mdp>@postgres:5432/tempo`. |
| `TEMPO_VERTEX_PROJECT_ID`, secret `tempo_gcp_credentials`, `TEMPO_LLM_MODEL` (`gemini-3.5-flash-lite` par défaut, remplaçable depuis Home Assistant : réglages de l'intégration), `TEMPO_VERTEX_REGION` (`global` par défaut) | api, worker | LLM facultatif (assistant conversationnel et e-mails du coach) : Gemini sur Google Cloud Vertex AI. Projet Google Cloud (API Vertex AI / Agent Platform activée) et clé JSON d'un compte de service ayant le rôle Vertex AI User ; le modèle doit être accessible au projet. Sans projet ou sans clé : mode déterministe. Chaque appel journalise une ligne `llm usage` (usage, modèle, latence, jetons d'entrée dont servis par le cache implicite, de sortie, de réflexion), jamais le contenu. |
| `TEMPO_MOTIS_URL` | api, worker | `http://routing:8080`. |
| `TEMPO_RT_RELAY_URL` | api | URL interne facultative du relais pour les diagnostics, `http://tempo-rt-relay:8090` dans les piles fournies. |
| `TEMPO_TIMEZONE` | api, worker | `Europe/Brussels` par défaut. |
| `tempo_ics_url` (secret) | worker | URL iCal de l'université (contient souvent un jeton personnel). |
| `TEMPO_TRAINING_IMAP_HOST`, `TEMPO_TRAINING_IMAP_USER`, `TEMPO_TRAINING_ALLOWED_SENDER`, `TEMPO_TRAINING_AUTHSERV_ID`, secret `tempo_training_imap_password` | worker | Import du programme du coach : seule l'adresse exacte autorisée est lue, et seulement si le serveur de réception désigné par `TEMPO_TRAINING_AUTHSERV_ID` (premier mot de son en-tête `Authentication-Results`, ex. `mx.google.com`) a validé DMARC ou DKIM pour le domaine de l'expéditeur. Désactivé tant que tout n'est pas défini. |
| `tempo_bmc_partner_key` (secret, **obligatoire**) | relais / mise à jour des données | Clé Belgian Mobility Standard (data.belgianmobility.io), envoyée uniquement à `api-management-opendata-production.azure-api.net` dans l'en-tête `bmc-partner-key` : horaires statiques et temps réel SNCB, TEC et De Lijn (trajets chaque minute, alertes toutes les 5 min, 43 % du quota de 12 000/jour). Sans clé, le relais refuse de démarrer et la mise à jour s'arrête avec un message explicite ; une clé refusée (401/403) est signalée comme telle. État et pause éventuelle après un 429 : `GET /health` du relais (`paused_until`, `requests_last_24h`, `last_error`). |

Côté Home Assistant (intégration) : URL interne du backend, jeton, `device_tracker` du
téléphone, service `notify` facultatif, modèle d'IA, adresse publique de l'agenda. Le reste
(lieux, résidences, disponibilités, préférences, rappels) se dit à l'assistant ; les
coordonnées d'un lieu sont fournies par l'utilisateur, jamais devinées.

### Observabilité

- Uptime Kuma peut vérifier `GET /health`. `GET /v1/diagnostics`, protégé par le jeton API,
  rassemble les connexions, la taille et les attentes de verrous PostgreSQL, les derniers
  jobs et leur dernier succès, la replanification en attente et le dernier signal du worker.
  MOTIS et le relais exposent séparément leur joignabilité et leur état opérationnel.
  Le relais publie par flux les compteurs d'entités fraîches, périmées ou sans horodatage.
- Les journaux Docker indiquent la latence et le résultat de l'API, de MOTIS et de Gemini,
  ainsi que les imports GTFS, les basculements de graphe et les rapprochements GTFS-RT.
  Glances et Docker fournissent le CPU, la mémoire et l'espace libre. Tempo utilise le cache
  MOTIS limité à un calcul de planning pour éviter la réutilisation entre deux relevés.

## Déploiement Swarm (Portainer)

Déploiement manuel par l'utilisateur via Portainer sur le serveur Debian (Swarm). La pile
`deploy/stack.yml` est autonome : images GHCR, aucun fichier du dépôt à monter.

1. **Images** (CI `.github/workflows/ci.yml`) : tests à chaque push et pull request ; si ils
   passent, publication automatique sur GHCR de `ghcr.io/mtx26/tempo-backend`,
   `tempo-routing` (MOTIS v2.11.4 + `routing/config/belgium.yml`) et `tempo-postgres`
   (postgres 16 + rôles et scripts de sauvegarde) : `latest` et `sha-<court>` à chaque push
   sur `main`, `X.Y.Z` pour une étiquette `vX.Y.Z` (version à figer dans `TEMPO_VERSION`).
   Images publiques : aucun registre à déclarer dans Portainer.
2. **Serveur** : MOTIS et la mise à jour des données tournent sur le nœud manager (volume
   local partagé) ; prévoir ~8 Go de RAM libres (MOTIS : 6 Go max).
3. **Accès de Home Assistant** : Home Assistant tourne en conteneur avec le réseau `host` ;
   `api` publie donc un seul port sur le nœud, `TEMPO_API_PORT` (défaut `18000`, mode `host`).
   L'intégration utilise `http://<IP du serveur>:18000` ; jeton obligatoire, tentatives
   limitées. Ne pas ouvrir ce port sur Internet (box/pare-feu).
4. **Secrets** : `python deploy/make_swarm_secrets.py` écrit les 10 fichiers dans
   `deploy/secrets/swarm/` (ignoré par git ; mots de passe, URL de base et jetons générés une
   fois, clés validées recopiées), puis `sh deploy/push_secrets.sh <hôte ssh>` les crée sur le
   serveur par l'entrée standard de SSH (aucune valeur affichée ; secrets existants gardés) :
   `tempo_owner_db_password`, `tempo_app_db_password`, `tempo_owner_database_url`,
   `tempo_database_url`, `tempo_api_token`, `tempo_calendar_token`, `tempo_ics_url`,
   `tempo_bmc_partner_key`, `tempo_gcp_credentials`, `tempo_training_imap_password`.
5. **Pile** : *Stacks > Add stack*, nom `tempo`, *Web editor* = contenu de `deploy/stack.yml`,
   *Environment variables* (valeurs dans `deploy/.env`, ignoré par git) : `TEMPO_VERSION`
   (`0.1.0`), `TEMPO_VERTEX_PROJECT_ID`, `TEMPO_TRAINING_IMAP_HOST`, `TEMPO_TRAINING_IMAP_USER`,
   `TEMPO_TRAINING_ALLOWED_SENDER`, `TEMPO_TRAINING_AUTHSERV_ID` ; facultatif
   `TEMPO_REGISTRY` (défaut `ghcr.io/mtx26`), `TEMPO_API_PORT` (défaut `18000`). Une valeur manquante bloque
   le déploiement avec son nom. *Deploy the stack*.
6. **Premier démarrage** : `migrate` crée le schéma et redonne les droits à `tempo_app` ;
   `routing-update` télécharge les GTFS et OSM (~1 Go), puis `routing` importe le graphe
   (~4 min ; santé « starting » jusque-là). Vérifier : `tempo_api` sain, `tempo_rt-relay`
   `/health` sans `last_error`.
7. **Home Assistant** : HACS, dépôt personnalisé `https://github.com/mtx26/tempo-homeassistant`
   (catégorie Intégration), télécharger « Tempo », redémarrer, ajouter l'intégration Tempo : URL `http://<IP du serveur>:18000`, jeton =
   `deploy/secrets/swarm/tempo_api_token`, « Vérifier le SSL » décoché (réseau interne),
   puis le `device_tracker` du téléphone.
8. **Mettre à jour** : sauvegarde, nouvelle étiquette `vX.Y.Z`, puis *Update the stack* avec
   `TEMPO_VERSION` (stop-first, retour arrière automatique en cas d'échec du démarrage).
   **Retour arrière vers une version au schéma plus ancien** : d'abord descendre le schéma avec
   l'image **récente** (seule à connaître ses migrations), puis remettre l'ancienne version :
   `docker run --rm --network <réseau backend> -e TEMPO_DATABASE_URL=<URL propriétaire>
   ghcr.io/mtx26/tempo-backend:<récente> python -m tempo.db downgrade <révision>` (0.6.x :
   `0007`). La descente garde les données que l'ancien schéma peut contenir (0008 : fenêtres
   d'étude rattachées à la première résidence, heure de changement remise à 12:00 si aucune
   n'était donnée ; perdus : indisponibilités propres à une autre catégorie que l'étude, séances
   `indoor` / `rest_days`) ; sinon, restaurer la sauvegarde prise avant la mise à jour.
9. **Agenda du téléphone** (facultatif) : publier uniquement le chemin `/calendar/` par un
   tunnel Cloudflare (*Zero Trust > Networks > Tunnels > Public Hostname* : chemin
   `^/calendar/`, service `http://localhost:18000`, sans application Cloudflare Access sur
   ce nom d'hôte car un agenda ne peut pas s'y authentifier), puis s'abonner sur l'iPhone à
   `https://<hôte>/calendar/<tempo_calendar_token>.ics` (*Réglages > Calendrier > Comptes >
   Ajouter un compte > Autre > Ajouter un calendrier avec abonnement*). L'URL est le secret :
   la régénérer = nouveau secret et nouvel abonnement.
10. **Rotation du jeton** : nouveau secret déployé comme `tempo_api_token`, l'ancien en
   `tempo_api_token_previous`, mettre à jour Home Assistant, retirer l'ancien.

Exploitation :
- Santé du relais GTFS-RT : `GET /health` rapporte par flux son état, la validation
  du dernier message analysé, le dernier succès, l'âge des données, la latence et les
  erreurs consécutives. Les compteurs de trajets et d'alertes décrivent le dernier message
  accepté par le relais. Une copie périmée reste visible dans le diagnostic et n'est plus servie.
- Mise à jour routage : `READY` désigne un lot sous `snapshots/`. Le superviseur vérifie le
  démarrage du candidat sur un port de boucle locale, le jeu d'arrêts attendu et un trajet de
  référence. Le graphe précédent reste disponible dans le volume `routing-data` pour une
  restauration. Le journal `import-<version>.log` contient la cause d'un échec.
- Santé : health checks `api` et `rt-relay` ; table `job_run` (exposée par `get_sources_status`
  et les capteurs HA).
- Sauvegarde quotidienne (14 jours) dans le volume `backups` ; la copier hors du nœud.
  Exercice mensuel : `docker exec <tâche backup> sh /scripts/restore_drill.sh` (restaure le
  dernier dump dans une base temporaire, compare les comptes, la supprime).
- La rétention automatique conserve les notifications lues 90 j, les propositions décidées
  et les activités 180 j, les plans remplacés 30 j et les conversations 7 j. Les brouillons de
  conversation sont purgés 7 j après expiration. Seul le rôle propriétaire purge `audit_entry`.
- Les purges de rétention et de conversation reprennent indépendamment après un échec.
  Le dernier succès est enregistré avec chaque purge dans `app_setting`.
- Une seule réplique pour `api` (limiteur d'authentification en mémoire), `worker` et
  `rt-relay`.

## Licences des données

© contributeurs OpenStreetMap (ODbL) ; STIB, SNCB/NMBS, TEC, De Lijn — Open Data via Belgian
Mobility (CC BY 4.0, « Source : <opérateur> – Open Data – <date> »).
