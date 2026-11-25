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
`TEMPO_TEST_BMC_KEY` (passerelle Belgian Mobility réelle, 3 requêtes).

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
| `TEMPO_VERTEX_PROJECT_ID`, secret `tempo_gcp_credentials`, `TEMPO_LLM_MODEL` (`gemini-3.5-flash-lite` par défaut), `TEMPO_VERTEX_REGION` (`global` par défaut) | api, worker | LLM facultatif (assistant conversationnel et e-mails du coach) : Gemini sur Google Cloud Vertex AI. Projet Google Cloud (API Vertex AI / Agent Platform activée) et clé JSON d'un compte de service ayant le rôle Vertex AI User ; le modèle doit être accessible au projet. Sans projet ou sans clé : mode déterministe. Chaque appel journalise une ligne `llm usage` (usage, modèle, latence, jetons d'entrée dont servis par le cache implicite, de sortie, de réflexion), jamais le contenu. |
| `TEMPO_MOTIS_URL` | api, worker | `http://routing:8080`. |
| `TEMPO_TIMEZONE` | api, worker | `Europe/Brussels` par défaut. |
| `tempo_ics_url` (secret) | worker | URL iCal de l'université (contient souvent un jeton personnel). |
| `TEMPO_TRAINING_IMAP_HOST`, `TEMPO_TRAINING_IMAP_USER`, `TEMPO_TRAINING_ALLOWED_SENDER`, `TEMPO_TRAINING_AUTHSERV_ID`, secret `tempo_training_imap_password` | worker | Import du programme du coach : seule l'adresse exacte autorisée est lue, et seulement si le serveur de réception désigné par `TEMPO_TRAINING_AUTHSERV_ID` (premier mot de son en-tête `Authentication-Results`, ex. `mx.google.com`) a validé DMARC ou DKIM pour le domaine de l'expéditeur. Désactivé tant que tout n'est pas défini. |
| `tempo_bmc_partner_key` (secret, **obligatoire**) | relais / mise à jour des données | Clé Belgian Mobility Standard (data.belgianmobility.io), envoyée uniquement à `api-management-opendata-production.azure-api.net` dans l'en-tête `bmc-partner-key` : horaires statiques et temps réel SNCB, TEC et De Lijn (trajets chaque minute, alertes toutes les 5 min, 43 % du quota de 12 000/jour). Sans clé, le relais refuse de démarrer et la mise à jour s'arrête avec un message explicite ; une clé refusée (401/403) est signalée comme telle. État et pause éventuelle après un 429 : `GET /health` du relais (`paused_until`, `requests_last_24h`, `last_error`). |

Côté Home Assistant (intégration) : URL interne du backend, jeton, `device_tracker` du
téléphone, service `notify` facultatif, puis option *Utiliser la zone domicile comme domicile
Tempo*. Le reste (lieux, disponibilités, préférences, rappels) se dit à l'assistant ; les
coordonnées d'un lieu sont fournies par l'utilisateur, jamais devinées.

## Déploiement Swarm (Portainer)

Déploiement manuel par l'utilisateur via Portainer sur le serveur Debian (Swarm). La pile
`deploy/stack.yml` est autonome : images GHCR, aucun fichier du dépôt à monter.

1. **Images** : `git tag v0.1.0 && git push origin v0.1.0`. Après les tests, la CI publie
   `ghcr.io/mtx26/tempo-backend`, `tempo-routing` (MOTIS v2.11.4 + `routing/config/belgium.yml`)
   et `tempo-postgres` (postgres 16 + rôles et scripts de sauvegarde), étiquette `0.1.0`.
   Paquets privés : dans Portainer, *Registries > Add registry > Custom registry*, URL
   `ghcr.io`, identifiant GitHub et un jeton (classic) limité à `read:packages`.
2. **Nœud** : `docker node update --label-add tempo.routing=true <nœud>` (MOTIS et la mise à
   jour des données partagent un volume local). Prévoir ~8 Go de RAM libres (MOTIS : 6 Go max).
3. **Réseau partagé avec Home Assistant** :
   `docker network create -d overlay --attachable homeassistant_tempo`, puis y relier le
   conteneur Home Assistant existant (`docker network connect homeassistant_tempo <conteneur>`,
   ou `networks:` externe dans sa pile). Seul `api` rejoint ce réseau ; aucun port Tempo n'est
   publié. Autre nom : variable `TEMPO_HA_NETWORK`. Home Assistant en `network_mode: host` ne
   peut pas rejoindre un overlay : publier alors le port de `api` en mode `host` et bloquer
   le port 8000 au pare-feu sauf depuis l'hôte.
4. **Secrets** : `python deploy/make_swarm_secrets.py` écrit les 9 fichiers dans
   `deploy/secrets/swarm/` (ignoré par git ; mots de passe, URL de base et jeton générés une
   fois, clés validées recopiées). Dans Portainer, *Secrets > Add secret* : nom = nom du
   fichier, contenu = contenu du fichier : `tempo_owner_db_password`, `tempo_app_db_password`,
   `tempo_owner_database_url`, `tempo_database_url`, `tempo_api_token`, `tempo_ics_url`,
   `tempo_bmc_partner_key`, `tempo_gcp_credentials` (JSON du compte de service),
   `tempo_training_imap_password`.
5. **Pile** : *Stacks > Add stack*, nom `tempo`, *Web editor* = contenu de `deploy/stack.yml`,
   *Environment variables* (valeurs dans `deploy/.env`, ignoré par git) : `TEMPO_VERSION`
   (`0.1.0`), `TEMPO_VERTEX_PROJECT_ID`, `TEMPO_TRAINING_IMAP_HOST`, `TEMPO_TRAINING_IMAP_USER`,
   `TEMPO_TRAINING_ALLOWED_SENDER`, `TEMPO_TRAINING_AUTHSERV_ID` ; facultatifs
   `TEMPO_REGISTRY` (défaut `ghcr.io/mtx26`), `TEMPO_HA_NETWORK`. Une valeur manquante bloque
   le déploiement avec son nom. *Deploy the stack*.
6. **Premier démarrage** : `migrate` crée le schéma et redonne les droits à `tempo_app` ;
   `routing-update` télécharge les GTFS et OSM (~1 Go), puis `routing` importe le graphe
   (~4 min ; santé « starting » jusque-là). Vérifier : `tempo_api` sain, `tempo_rt-relay`
   `/health` sans `last_error`.
7. **Home Assistant** : dépôt privé, donc pas de HACS ; copier
   `custom_components/tempo` (dépôt `tempo-homeassistant`) dans `config/custom_components/`,
   redémarrer, ajouter l'intégration Tempo : URL `http://tempo_api:8000`, jeton =
   `deploy/secrets/swarm/tempo_api_token`, « Vérifier le SSL » décoché (réseau interne),
   puis le `device_tracker` du téléphone.
8. **Mettre à jour** : sauvegarde, nouvelle étiquette `vX.Y.Z`, puis *Update the stack* avec
   `TEMPO_VERSION` (stop-first, retour arrière automatique). Retour arrière : version
   précédente ; les migrations sont en avant uniquement (restaurer la sauvegarde si besoin).
9. **Rotation du jeton** : nouveau secret déployé comme `tempo_api_token`, l'ancien en
   `tempo_api_token_previous`, mettre à jour Home Assistant, retirer l'ancien.

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
