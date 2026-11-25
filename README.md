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
| `routing/` | Image MOTIS (`Dockerfile`), configurations `config/brussels.yml` (test) et `config/belgium.yml` (national), `scripts/supervisor.sh` (reconstruction atomique des graphes). |
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
- « Demain je dors chez papa » ; « Je ne suis pas disponible samedi matin ».
- « Comment j'arrive au campus pour 8 h ? » ; « Rappelle-moi 30 min avant le médecin » ;
  « Préviens-moi 60 et 15 minutes avant chaque départ ».
- « Qu'est-ce qui a changé cette nuit ? » ; « Pourquoi la chimie n'est pas planifiée ? » ;
  « Accepte la proposition » ; « Réorganise toute ma semaine ».

Home Assistant affiche passivement : calendrier `calendar.tempo`, travail restant
`todo.tempo_tasks`, capteurs (prochain événement, prochain départ, état du plan, propositions,
…) et notifications. Il n'y a ni bouton ni formulaire métier.

Sans clé LLM, la conversation fonctionne en mode réduit (analyseur déterministe : ajout de
volumes d'étude, d'entraînements, de courses, questions courantes, « replanifie », « accepte »,
« c'est fait ») ; synchronisations, planification, trajets et rappels restent automatiques.

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
`TEMPO_TEST_PORTAL_LIVE=1` et `TEMPO_TEST_BMC_KEY` (portail Belgian Mobility réel).

Si `dev_postgres.py` expire après un arrêt brutal, PostgreSQL démarre souvent quand même sur le
port affiché (voir `.pgdata/log`). Serveur IMAP de test :
`docker run -d --name tempo-greenmail -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled" -p 127.0.0.1:3025:3025 -p 127.0.0.1:3993:3993 greenmail/standalone:2.1.2`.

Intégration Home Assistant (Linux ou WSL uniquement) : voir le README de `tempo-homeassistant`
(`uv sync && uv run pytest && uv run mypy && uv run ruff check .`).

### MOTIS local

```bash
export PATH="$LOCALAPPDATA/Programs/DockerDesktop/resources/bin:$PATH"
(cd backend && uv run python -m tempo.transport.gtfs_update ../routing/data/brussels brussels)  # ou belgium
docker build -t tempo-routing:v2.11.4 routing
W="$(cygpath -w "$PWD/routing")"
MSYS_NO_PATHCONV=1 docker run --rm --memory 6g \
  -v "$W\data\brussels:/input:ro" -v "$W\config\brussels.yml:/config/config.yml:ro" \
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
| `tempo_api_token_previous` (secret, temporaire) | api | Ancien jeton accepté pendant une rotation. |
| `tempo_database_url` (secret) | api, worker | `postgresql+psycopg://tempo_app:<mdp>@postgres:5432/tempo`. |
| `TEMPO_LLM_PROVIDER` (`anthropic` par défaut, ou `vertex`), `TEMPO_LLM_MODEL` (`claude-sonnet-5-5` par défaut) | api, worker | Fournisseur et modèle de l'assistant conversationnel et de l'interprète des e-mails du coach. Sans fournisseur complet : mode déterministe. |
| `tempo_anthropic_api_key` (secret, facultatif) | api, worker | Fournisseur `anthropic` : clé de l'API Claude (`platform.claude.com`). |
| `TEMPO_VERTEX_PROJECT_ID`, `TEMPO_VERTEX_REGION` (`global` par défaut), secret `tempo_gcp_credentials` | api, worker | Fournisseur `vertex` : projet Google Cloud où le modèle est activé (Model Garden) et clé JSON d'un compte de service ayant le rôle Vertex AI User. |
| `TEMPO_MOTIS_URL` | api, worker | `http://routing:8080`. |
| `TEMPO_TIMEZONE` | api, worker | `Europe/Brussels` par défaut. |
| `tempo_ics_url` (secret) | worker | URL iCal de l'université (contient souvent un jeton personnel). |
| `TEMPO_TRAINING_IMAP_HOST`, `TEMPO_TRAINING_IMAP_USER`, `TEMPO_TRAINING_ALLOWED_SENDER`, `TEMPO_TRAINING_AUTHSERV_ID`, secret `tempo_training_imap_password` | worker | Import du programme du coach : seule l'adresse exacte autorisée est lue, et seulement si le serveur de réception désigné par `TEMPO_TRAINING_AUTHSERV_ID` (premier mot de son en-tête `Authentication-Results`, ex. `mx.google.com`) a validé DMARC ou DKIM pour le domaine de l'expéditeur. Désactivé tant que tout n'est pas défini. |
| `tempo_relay_bmc_partner_key` / `tempo_bmc_partner_key` (secrets, facultatifs) | relais / mise à jour des données | Clé Belgian Mobility Standard (temps réel TEC et De Lijn, quota du portail). Sans clé : SNCB seulement, toutes les 30 min. |

Côté Home Assistant (intégration) : URL interne du backend, jeton, `device_tracker` du
téléphone, service `notify` facultatif, puis option *Utiliser la zone domicile comme domicile
Tempo*. Le reste (lieux, disponibilités, préférences, rappels) se dit à l'assistant ; les
coordonnées d'un lieu sont fournies par l'utilisateur, jamais devinées.

## Déploiement Swarm

**Préparé et validé localement, jamais exécuté : nécessite une autorisation explicite.**

1. **Prérequis** (gestionnaire Swarm) : registre accessible ; pousser `tempo-backend:<version>`
   (`backend/Dockerfile`) et `tempo-routing:v2.11.4` (`routing/Dockerfile`) ; réseau overlay
   partagé avec Home Assistant
   (`docker network create -d overlay --attachable homeassistant_tempo`, ou `TEMPO_HA_NETWORK`) ;
   `routing-update` et `routing` partagent un volume local : étiqueter leur nœud
   (`docker node update --label-add tempo.routing=true <nœud>`) ; `postgres` reste sur un seul
   nœud (volume local). Le volume de routage est construit par `routing-update` + `routing`
   (~1,8 Go, ~3,5 min d'import), puis rafraîchi chaque nuit à 02:30 UTC.
2. **Secrets** (`docker secret create <nom> -`) : `tempo_owner_db_password`,
   `tempo_app_db_password`, `tempo_owner_database_url`, `tempo_database_url`,
   `tempo_api_token`, `tempo_ics_url`, et les secrets facultatifs ci-dessus.
3. **Déployer** :
   `TEMPO_REGISTRY=... TEMPO_VERSION=x.y.z docker stack deploy -c deploy/stack.yml tempo`.
   Le job `migrate` met le schéma à jour (rôle propriétaire) puis redonne les droits au rôle
   applicatif ; api et worker attendent le schéma.
4. **Mettre à jour** : sauvegarde d'abord, puis même commande avec la nouvelle version
   (stop-first, retour arrière automatique en cas d'échec). Retour arrière de code : redéployer
   la version précédente ; les migrations sont en avant uniquement en production (restaurer la
   sauvegarde si un schéma doit être annulé).
5. **Rotation du jeton** : créer le nouveau secret, le déployer comme `tempo_api_token` avec
   l'ancien en `tempo_api_token_previous`, mettre à jour Home Assistant, retirer l'ancien.

Exploitation :
- Santé : health checks `api` et `rt-relay` ; table `job_run` (exposée par `get_sources_status`
  et les capteurs HA).
- Sauvegarde quotidienne (14 jours) dans le volume `backups` ; la copier hors du nœud.
  Exercice mensuel : `docker exec <tâche backup> sh /scripts/restore_drill.sh` (restaure le
  dernier dump dans une base temporaire, compare les comptes, la supprime).
- Rétention automatique : notifications lues 90 j, propositions décidées et activités 180 j,
  plans remplacés 30 j, conversations 7 j. `audit_entry` n'est purgé qu'avec le rôle
  propriétaire.
- Une seule réplique pour `api` (limiteur d'authentification en mémoire), `worker` et
  `rt-relay`.

## Licences des données

© contributeurs OpenStreetMap (ODbL) ; STIB, SNCB/NMBS, TEC, De Lijn — Open Data via Belgian
Mobility (CC BY 4.0, « Source : <opérateur> – Open Data – <date> »).
