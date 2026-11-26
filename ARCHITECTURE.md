# Tempo — Architecture et spécification

Document de référence unique du produit : besoins, règles métier, architecture, modèle,
contrats, décisions techniques vérifiées, état réel et écarts. L'installation, la
configuration et l'exploitation sont dans `README.md` ; les règles de développement dans
`CLAUDE.md`. Section 12 : ce qui est réellement fait, vérifié, ou non.

## 1. Vision et besoins

Tempo est un gestionnaire personnel global auto-hébergé. **La conversation en langage naturel
(texte ou voix) est l'unique interface de gestion quotidienne.** Home Assistant fournit la
conversation, le GPS du téléphone, les notifications et l'affichage passif du calendrier et du
travail restant. Aucun formulaire métier, menu, bouton de validation, site ou compte
supplémentaire ; seule la configuration technique initiale de l'intégration passe par les
écrans de Home Assistant. L'IA comprend les demandes libres, les références contextuelles et
les demandes composées, consulte et modifie le domaine via des opérations typées et
authentifiées, et explique les résultats réels. Les automatismes essentiels sont déterministes
et indépendants du LLM.

| ID | Besoin et résultat observable |
|---|---|
| B01 | Conversation : gérer toutes les fonctionnalités métier sans commande technique ; questions uniquement si information indispensable ou ambiguïté significative. |
| B02 | Événements personnels : créer, consulter, modifier, déplacer, annuler ; horaires fixes ou flexibles, journée entière, plusieurs jours, récurrences et exceptions, lieux, verrouillage. |
| B03 | Tâches : description, durée estimée si connue, échéance facultative, priorité, fractionnement, progression, lieu éventuel ; conserver les tâches non planifiables. |
| B04 | Université : cours et examens fixes synchronisés depuis iCal, modifications et annulations sans doublon. |
| B05 | Études : volume par matière, échéance souple par défaut ou stricte explicitement, séances flexibles, minutes réellement travaillées. |
| B06 | Sport : programme du coach autorisé, entraînements flexibles, compétitions fixes, prescription et récupération respectées. |
| B07 | Disponibilités, préférences, contraintes : règles permanentes, récurrentes, temporaires et exceptions, par conversation. |
| B08 | Lieux : lieux connus, GPS réel fiable, localisations futures, origine indiquée dans la conversation. |
| B09 | Transport : itinéraires MOTIS par arrivée limite, départ minimum ou plage, critères, allers et retours. |
| B10 | Planification globale : événements, tâches, sport, études, lieux et trajets ; contraintes et préférences. |
| B11 | Stabilité : préserver le programme existant ; réorganisation globale seulement sur demande explicite. |
| B12 | Conflits : distinguer impossibilité, calcul inconclusif et panne ; préserver le réalisable, conserver le reliquat. |
| B13 | Validation : changements protégés soumis à accord naturel, propositions versionnées et revalidées. |
| B14 | Notifications : rappels ponctuels et récurrents, plusieurs rappels de départ, alertes de perturbation et de conflit. |
| B15 | Autonomie : synchronisations, surveillance, rappels, réoptimisation sûre et reprise après panne sans conversation. |
| B16 | Explication : raisons, contraintes, conflits, historique et état des sources, sans inventer. |
| B17 | Fiabilité : persistance durable, sécurité, installation HACS réelle, déploiement opérationnel. |

### D01 — Cas d'utilisation

```plantuml
@startuml
left to right direction
actor Utilisateur as U
actor "Home Assistant" as HA
actor "iCal université" as IC
actor "Messagerie autorisée" as MAIL
actor MOTIS as M
actor "Fournisseur LLM" as L
rectangle Tempo {
 usecase "Dialoguer naturellement" as C
 usecase "Gérer événements et tâches" as E
 usecase "Gérer études et progression" as S
 usecase "Gérer sport" as SP
 usecase "Gérer lieux et règles" as R
 usecase "Rechercher trajets" as T
 usecase "Consulter / optimiser planning" as P
 usecase "Résoudre conflits et valider" as V
 usecase "Gérer rappels et expliquer" as N
 usecase "Synchroniser et surveiller" as W
}
U -- C
L -- C
C ..> E : <<extend>>
C ..> S : <<extend>>
C ..> SP : <<extend>>
C ..> R : <<extend>>
C ..> T : <<extend>>
C ..> P : <<extend>>
C ..> V : <<extend>>
C ..> N : <<extend>>
HA -- R
HA -- N
IC -- W
MAIL -- W
M -- T
@enduml
```

## 2. Règles métier non négociables

- Les cours universitaires (iCal) sont fixes ; iCal reste l'autorité sur ses événements.
- L'étude personnelle est déplaçable, y compris les séances acceptées, sauf verrouillage
  explicite. Les dates cibles sont **souples** par défaut, strictes seulement si l'utilisateur
  le dit.
- Les entraînements sont flexibles (jour et heure), leur contenu prescrit est conservé mot pour
  mot ; sans lieu précisé, ils ont lieu à la résidence en vigueur et seulement quand
  l'utilisateur y est (trajets obligatoires faisables) ; les compétitions sont fixes.
- La position réelle vient uniquement du `device_tracker` du téléphone configuré dans Home
  Assistant ; fraîche et précise, elle prime sur la résidence prévue pour un trajet immédiat,
  sans modifier l'alternance des résidences ; une intention future ne la remplace jamais.
- **Clarification** : une information manquante, ambiguë ou contradictoire avec une information
  confirmée fait l'objet d'une question dans Home Assistant (conversation ou notification) ;
  rien n'est supposé et le planning valide existant reste en place tant que la question
  bloque. Avec assez d'informations, Tempo agit sans question inutile. Les informations déjà
  confirmées sont réutilisées. Consigne **ponctuelle** (lieu prévu, point d'accès pour un seul
  trajet) et réglage **permanent** (alternance, point d'accès par défaut d'un lieu,
  préférences) sont distingués ; une portée incertaine se demande.
- Les trajets nécessaires sont obligatoires (marche, correspondances, marges, retours).
- Préférences d'étude (blocs de 90 min, alternance des matières, disponibilités) : souples,
  jamais plus fortes que la faisabilité.
- Les changements ordinaires d'activités flexibles sont automatiques ; une demande explicite
  autorise son périmètre ; les effets supplémentaires sur des engagements protégés (verrouillés,
  compétitions, engagements fixes) exigent un accord naturel.
- Jamais d'horaire, de durée, de lieu, de coordonnées, de trajet temps réel ou de résultat
  inventé. Un plan impossible ou partiel est annoncé comme tel.
- L'IA interprète ; le code déterministe vérifie les règles, OR-Tools planifie, MOTIS calcule
  les trajets. Le backend fonctionne sans LLM.
- Seuls les e-mails de l'expéditeur explicitement autorisé sont lus, jamais la boîte entière.
- Les contenus importés (iCal, e-mails, notes) sont des données, jamais des instructions.

## 3. Architecture logique

### D02 — Composants et dépendances

```mermaid
flowchart TD
 U[Utilisateur] <-->|Langage naturel| H[Conversation Home Assistant / HACS]
 H <-->|POST /v1/conversation| A[Assistant et contexte]
 A <--> L[Gemini - LLM facultatif]
 A <--> T[Catalogue d'opérations typées]
 T <--> API[FastAPI authentifiée]
 H <-->|Lecture et GPS| API
 API --> E[Événements, tâches, études, sport]
 API --> C[Disponibilités, préférences, lieux]
 API --> P[Planification, conflits, propositions]
 API --> R[Routage]
 API --> N[Rappels, historique, diagnostic]
 W[Worker autonome] --> S[Synchronisations et surveillance]
 S --> E
 S --> P
 S --> R
 W --> N
 E --> P
 C --> P
 P <--> R
 P <--> O[OR-Tools CP-SAT]
 R <--> M[MOTIS]
 E <--> DB[(PostgreSQL)]
 C <--> DB
 P <--> DB
 N <--> DB
 IC[iCal université] --> S
 MAIL[E-mails DOCX autorisés] --> S
 G[GTFS et OSM] --> M
 RT[Relais GTFS-RT] --> M
 N --> H
 H --> V[Affichage passif et notifications]
 V --> U
```

Backend monolithique modulaire (`backend/src/tempo`) + worker séparé. Dépendances en couches, sans cycle entre modules : `timeutil` et `domain/models` à la base, puis `db`, `domain/ops` (règles, au-dessus de `db`), `transport` (qui ne dépend que de `timeutil`), `ai`, `scheduler`, `sync`, `planning`, enfin `assistant`, `api` et `worker`. PostgreSQL est la source
de vérité ; Home Assistant n'est pas une seconde base de planning. MOTIS calcule les
itinéraires, OR-Tools résout les contraintes, Tempo orchestre. Aucune passerelle IA
supplémentaire.

| Module | Responsabilité |
|---|---|
| `assistant/catalog.py` | Catalogue des opérations (schéma Pydantic strict, résultat typé, `mutates`) : **unique surface métier**, lectures comprises, pour l'IA, Home Assistant et tout client (`/v1/operations`). |
| `assistant/agent.py` | Boucle d'appels d'outils (`ToolLoopAssistant`) sur Gemini via Google Cloud Vertex AI (`GeminiChat`, SDK `google-genai`, compte de service, modèle `TEMPO_LLM_MODEL`, `gemini-3.5-flash-lite` par défaut, appel automatique de fonctions désactivé : chaque appel passe par `run_operation`) ; client partagé avec l'interprète des e-mails (`ai/llm.py`, sortie JSON contrainte par schéma) ; un blocage de sécurité suit le chemin déterministe ; bornée à 16 étapes et à un budget de 60 s (aucun nouvel appel au-delà, réponse avec ce qui a réellement été fait). |
| `assistant/fallback.py` | Conversation sans LLM : phrases reconnues (`assistant/actions.py`, `assistant/questions.py`, `ai/text_parser.py`) exécutées via `run_operation`, comme l'agent. `ai/` ne fait qu'interpréter (analyseur déterministe, LLM facultatif, application validée des imports). |
| `api/` | FastAPI : `/health` (public), `/v1/status`, `/v1/conversation`, `/v1/operations` (liste et exécution), `/v1/location` (GPS réel poussé par HA : télémétrie, jamais une opération accessible au LLM), `/v1/llm` (`api/llm.py`, réglage technique : modèles Gemini du projet listés au plus une fois par heure, modèle choisi testé par une mini-requête avant d'être enregistré en base, lu à chaque conversation et à chaque tour du worker, `TEMPO_LLM_MODEL` restant la valeur par défaut ; jamais exposé au LLM), et `/calendar/<jeton>.ics` (`api/calendar_feed.py`) : flux iCalendar **en lecture seule** du plan publié (éléments du calendrier HA sauf les cours, déjà présents via l'agenda de l'université ; de J−14 à J+60 ; la description d'un trajet liste ses tronçons en heure locale : ligne, arrêts, heures, marche, dépôt) pour l'abonnement d'un agenda de téléphone ; `/v1/status` donne `calendar_path` à Home Assistant, qui affiche l'URL d'abonnement dans ses options (jamais comme entité) ; jeton dédié `calendar_token` distinct du jeton d'API, comparé en temps constant, mauvais jeton = 404 et tentative comptée par le limiteur ; absent si non configuré. Aucune modification possible par ce flux. |
| `domain/ops.py` | Règles partagées : journal de travail, séances de tâches, séries RRULE et exceptions. |
| `db/` | Modèles et migrations ; `store.py` : services de persistance partagés par tous les paquets (réglages, audit, notifications par une seule fonction, propositions avec leur révision, version active, annuaire des lieux). |
| `timeutil.py` | Primitives de temps uniques : conversion UTC (refus des dates sans fuseau), bornes de jour locales sûres aux changements d'heure, `Interval`. |
| `planning/` | Chargement des entrées, moteur (`engine.py`, dont la détection unique des conflits `find_conflicts`), publication versionnée (`service.py`), décisions sur propositions (`proposals.py`), vues. |
| `scheduler/` | CP-SAT : étude (`solver.py`), activités flexibles (`activities.py`), validateur indépendant (`validate.py`). |
| `transport/` | `itinerary.py` : **service d'itinéraire unique** (bornes, objectif, critères, pagination MOTIS, alternatives, cache par exécution) utilisé par la recherche, les trajets du planning, l'estimation des activités flexibles et le contrôle de départ ; `location.py` : position réelle et base à un instant ; client MOTIS, trajets obligatoires (`travel.py`), contrôle de départ, relais GTFS-RT, flux belges, mise à jour des données de routage. |
| `sync/` | iCal (`ics.py`, enregistrement `ics_repository.py`), programme du coach `.docx` (`coach_plan.py`, `coach_import.py`), IMAP restreint (`training_mail.py`). |
| `worker/` | Boucle de jobs durables (`job_run`) : iCal, séries, replanification, rappels, contrôle de départ, e-mails, rétention. |
| `../tempo-homeassistant` | Intégration HACS `custom_components/tempo` (dépôt séparé). |

## 4. Conversation et contrat d'opérations

### D03 — Séquence d'une demande composée

```mermaid
sequenceDiagram
 actor U as Utilisateur
 participant H as HA / HACS
 participant A as Assistant
 participant L as LLM
 participant API as Opérations
 participant P as Planificateur
 U->>H: Demain je pars de chez papa et je rentre chez maman après les cours
 H->>A: Texte + identifiant de conversation
 A->>L: Historique récent, date et fuseau, catalogue
 L-->>A: Appels d'opérations
 A->>API: get_context, list_places, get_schedule
 API-->>A: Données structurées
 alt Information indispensable inconnue
 A-->>U: Question naturelle
 U->>A: Précision
 end
 A->>API: set_planned_location puis plan_now
 API->>P: Calcul sur le planning publié
 P-->>API: Résultat, diff, conflits
 API-->>A: État réel
 A->>L: Résultats des opérations
 L-->>A: Réponse fidèle
 A-->>H: Réponse
 H-->>U: Réponse naturelle
```

Contrat (implémenté) :
- Chaque opération valide ses arguments (Pydantic, `extra="forbid"`) avant exécution et
  s'exécute dans un point de sauvegarde : un échec ne laisse aucune écriture. Erreurs métier
  renvoyées comme résultat d'outil (`is_error`), jamais comme succès.
- Une mutation réussie est auditée (`audit_entry`, acteur `assistant`/`api`, entité touchée) et, si elle touche
  le planning, demande une replanification (`replan_requested`) ; l'assistant appelle
  `plan_now` pour obtenir le résultat réel.
- `POST /v1/operations/{name}` accepte un en-tête `Idempotency-Key` (réponse rejouée à
  l'identique pendant 2 jours, puis purgée par le worker ; deux requêtes simultanées avec la
  même clé sont sérialisées). Le journal de travail est idempotent par clé.
- Contexte LLM : prompt système stable, 12 derniers tours de la conversation (6 h), date et
  fuseau Europe/Brussels dans le message ; données métier lues à la demande. Aucun accès SQL,
  shell, Docker ou secret.
- Sans LLM (non configuré, panne, blocage) : `/v1/conversation` répond via le repli déterministe
  (`assistant/fallback.py`) qui exécute les mêmes opérations du catalogue et l'indique
  (`used_llm: false`, `fallback: true`) ; ses tours restent dans l'historique de la conversation.
  Si le LLM tombe après avoir modifié quelque chose, la demande n'est jamais rejouée par le
  repli : la réponse liste les opérations réellement effectuées.
- Durée : l'assistant ne lance plus d'appel au modèle après 60 s (chaque appel borné par le
  temps restant) ; Home Assistant attend jusqu'à 150 s, de sorte que l'utilisateur entend le
  résultat réel plutôt qu'une expiration côté client pendant qu'un changement s'applique.

| Domaine | Opérations |
|---|---|
| Lecture | `get_context`, `get_schedule`, `get_summary`, `list_activities`, `list_tasks`, `list_places`, `get_position`, `get_location`, `get_rules`, `list_proposals`, `list_notifications`, `list_reminders`, `explain_plan`, `get_sources_status`, `get_history` |
| Événements | `create_event` (fixe/flexible, journée entière occupée/informative, plusieurs jours, RRULE, distanciel), `update_event` (portée occurrence / suivantes / série), `cancel_event`, `set_lock`, `report_activity`, `confirm_activity` |
| Tâches et études | `create_task`, `update_task`, `log_work` (séance entière ou partielle, minutes, correction négative) |
| Lieux et règles | `upsert_place` (dont point d'accès), `delete_place`, `update_settings`, `set_weekly_availability`, `set_category_windows`, `add_availability_exception`, `delete_availability_exception`, `set_planned_location`, `clear_planned_location`, `set_residence_alternation`, `clear_residence_alternation`, `get_residence_schedule`, `find_address`, `set_temporary_access`, `clear_temporary_access` |
| Rappels et validation | `create_reminder`, `cancel_reminder`, `decide_proposal`, `acknowledge_notification` |
| Planning et transport | `plan_now` (local / global, simulation `dry_run`), `propose_alternatives`, `search_journey` |

## 5. Modèle métier

### D04 — Classes conceptuelles

```mermaid
classDiagram
 class Activity {
  id
  title
  kind
  source
  status
  scheduling
  locked
  priority
  duration
  earliestStart
  latestEnd
  allDay
  busy
  remote
 }
 class Task {
  id
  title
  kind
  estimatedMinutes
  targetDate
  hardDeadline
  splittable
  priority
  remainingMinutes()
 }
 class WorkLog {
  minutes
  recordedAt
  idempotencyKey
 }
 class RecurrenceSeries {
  rrule
  dtstart
  timezone
  template
 }
 class RecurrenceException {
  occurrenceKey
  cancelled
  overrides
 }
 class Place {
  id
  name
  latitude
  longitude
 }
 class PhonePosition {
  observedAt
  accuracy
 }
 class PlannedLocation {
  start
  end
 }
 class Availability {
  weekday
  start
  end
 }
 class AvailabilityException {
  day
  start
  end
  available
 }
 class ScheduleVersion {
  state
  solverStatus
  summary
 }
 class StudyBlock {
  start
  end
  locked
  state
 }
 class Proposal {
  kind
  status
  inputRevision
 }
 class Reminder {
  targetType
  targetId
  offsetMinutes
  remindAt
  rrule
  status
 }
 Task "1" --> "0..*" StudyBlock : séances d'étude
 Task "1" --> "0..1" Activity : séance située ou non fractionnable
 Task "1" --> "0..*" WorkLog
 WorkLog --> "0..1" StudyBlock
 Activity --> "0..1" Place
 RecurrenceSeries "1" --> "0..*" Activity : occurrences
 RecurrenceSeries "1" *-- "0..*" RecurrenceException
 PlannedLocation --> Place
 Availability --> Place
 ScheduleVersion "1" *-- "0..*" StudyBlock
 ScheduleVersion ..> Availability
 ScheduleVersion ..> AvailabilityException
 Reminder ..> Activity
```

Correspondance avec PostgreSQL (`db/models.py`, migrations Alembic `0001`–`0006`) :
`activity` (catégories `personal`, `training`, `race`, `task` ; `scheduling` fixe/flexible ;
une compétition est toujours fixe par contrainte de base), `study_task` (tâches d'étude et
personnelles), `work_log`, `recurrence_series`, `recurrence_exception`, `study_block`,
`schedule_version`, `fixed_event` (iCal), `place`, `place_alias`, `phone_position`,
`availability_window`, `availability_exception`, `proposal`, `notification`, `reminder`,
`conversation_turn`, `audit_entry`, `job_run`, `sync_run`, `app_setting` (réglages, lieux
prévus `stays`, préférences).

Règles du modèle :
- Catégorie, mode (fixe/flexible) et verrouillage sont indépendants. Une activité fixe occupe
  `earliest_start_at..latest_end_at` ; une flexible se place dans cette fenêtre et son horaire
  (`planned_start_at`) appartient au plan.
- Événement journée entière : `busy` le fait bloquer ses jours, sinon il est seulement affiché.
  Une activité `remote` n'a pas de trajet.
- Une tâche n'est pas une séance : le reste dû = estimation − somme du `work_log`
  (`completed_minutes` n'est qu'un cache recalculé). Durée inconnue : tâche conservée, non
  planifiée. Sans échéance : planifiée comme cible lointaine souple. Une tâche située ou non
  fractionnable devient une seule activité flexible (`kind = task`) dont la fenêtre finit à la
  fin locale du jour d'échéance : une échéance stricte n'est jamais repoussée (reliquat et
  échéance manquée signalés), une cible souple laisse de la place après elle (retard pénalisé
  et signalé) ; la fenêtre est recalculée avant chaque replanification.
- Rien n'est supprimé en silence : un lieu encore utilisé (domicile, activités, tâches,
  disponibilités, lieux prévus) ne peut pas être supprimé ; ses alias sont conservés quand une
  mise à jour ne les mentionne pas ; verrouiller ou déverrouiller une séance la garde en place.
- Séries : RRULE (python-dateutil) développée en heure locale du fuseau de la série (sûr au
  changement d'heure), matérialisée sur 60 jours glissants ; l'identité d'une occurrence est son
  heure locale d'origine. Une exception (déplacement, annulation) est conservée lors des
  réexpansions ; « cette occurrence et les suivantes » coupe la série (UNTIL) et en crée une
  nouvelle. Les lignes annulées restent pour l'historique.

### D05 — Cycle de vie d'une activité

```mermaid
stateDiagram-v2
 [*] --> Unscheduled
 Unscheduled --> Scheduled : Attribution horaire
 Unscheduled --> Cancelled
 Scheduled --> Scheduled : Déplacement autorisé
 Scheduled --> Unscheduled : Retrait du créneau
 Scheduled --> Completed
 Scheduled --> Partial
 Scheduled --> Missed
 Scheduled --> Cancelled
 Completed --> [*]
 Partial --> [*]
 Missed --> [*]
 Cancelled --> [*]
```

Statuts stockés : `active` (non placée si `planned_start_at` est nul), `to_confirm` (durée du
coach inconnue), `done`, `partial`, `missed`, `cancelled`. Un travail réalisé n'est jamais
déduit du passage de l'heure : une séance d'étude passée non déclarée devient `missed`, son
temps est replanifié, et l'utilisateur peut encore la déclarer faite.

## 6. Entrées par domaine

### D06 — Synchronisation des sources

```mermaid
flowchart TD
 I[iCal université] --> IP[icalendar + recurring-ical-events]
 M[Messagerie] --> F{Expéditeur exact, DMARC/DKIM, objet}
 F -->|Non| IGN[Ignorer]
 F -->|Oui| D[DOCX du programme hebdomadaire]
 D --> DP[Extraction du tableau lundi-dimanche]
 IP --> V[Validation et identité source]
 DP --> V
 V --> OK{Données suffisantes et import réussi}
 OK -->|Non| ERR[Conserver l'état connu et signaler]
 OK -->|Oui| DIFF[Ajouts, modifications, annulations]
 DIFF --> DB[(Enregistrement idempotent + audit)]
 DB --> IMP[Analyse des conséquences]
 IMP --> P[Replanification selon protections]
```

- **Université** (`sync/ics.py`, `sync/ics_repository.py`) : identité (UID, RECURRENCE-ID UTC) ;
  suppression logique avec historique ; flux vide ou plus de 50 % de suppressions (au moins 3)
  ⇒ sync `suspect`, suppressions retenues ; UID invalides jamais supprimés ; événements journée
  entière et repères de durée nulle (congés, échéances) jamais traités comme des engagements,
  signalés seulement s'ils tombent dans la fenêtre synchronisée ; verrou consultatif par
  source ; heures inexistantes (passage à l'heure d'été) normalisées, ambiguës résolues sur la
  première occurrence, avec avertissement ; une série déplacée est appariée par UID et mise à
  jour en place. Une panne ou un import partiel n'est jamais une suppression générale.
- **Sport** (`sync/coach_plan.py`, `sync/coach_import.py`, `sync/training_mail.py`) : seul
  l'expéditeur exact configuré est lu (filtre IMAP + revérification), et seulement si le
  serveur de réception de l'utilisateur (son `authserv-id`, réglage obligatoire) a enregistré
  DMARC aligné sur `header.from` ou DKIM aligné sur le domaine de l'expéditeur, dans le plus
  récent `Authentication-Results` à son nom (un en-tête écrit par l'expéditeur n'est jamais
  cru) ; boîte en lecture seule ; plan lu dans le `.docx` au format réel du coach (tableau
  « S3 | 23 au 29/11/2026 », puis lignes « Objectif », « Thème de la séance », « Contenu » suivies
  de 7 colonnes lundi→dimanche sans noms de jours ; ligne fusionnée ignorée ; apostrophes
  typographiques acceptées). Durée totale : celle du thème, sinon la première ligne du contenu
  si elle n'est qu'une durée (`1h15'`) ; `RAC : 10'`, `r 30''`, `3x80m` restent des parties. Jour suggéré (plan ou texte de l'e-mail) = préférence
  souple dans la semaine ; contenu conservé ; durée absente ⇒
  `to_confirm` (jamais devinée) ; ordre et récupération de la semaine conservés (au plus 2 jours
  imposés) ; plan renvoyé ⇒ remplacement sans doublon, séances faites/verrouillées/modifiées par
  l'utilisateur jamais écrasées (proposition `coach_plan_change`) ; compétitions fixes avec
  approbation.
- **Études** : volume par tâche, cible souple par défaut ; une cible au-delà de l'horizon ne
  planifie que sa part proportionnelle ; journal de travail idempotent, corrections négatives.
- **Personnel** : rendez-vous fixes, sorties/activités flexibles dans une fenêtre ; durée
  inconnue ⇒ question, jamais de durée arbitraire.
- **Disponibilités** : fenêtres hebdomadaires par lieu + exceptions datées (temps bloqué ou
  temps libre supplémentaire à un lieu). Elles s'appliquent au travail flexible ; les
  engagements fixes restent prioritaires.

## 7. Localisation et transport

### D07 — Résolution de la localisation

```mermaid
flowchart TD
 Q[Besoin de trajet] --> X{Origine explicitement indiquée ?}
 X -->|Oui| R[Lieu connu]
 X -->|Non| NOW{Trajet immédiat ?}
 NOW -->|Oui| GPS[GPS téléphone Home Assistant]
 GPS --> G{Frais ≤ 10 min et précis ≤ 200 m ?}
 G -->|Oui| O[Origine fiable]
 G -->|Non| ASK[Question naturelle]
 NOW -->|Non| FUT[Chaîne d'activités et lieux prévus]
 FUT --> K{Origine future connue ?}
 K -->|Oui| O
 K -->|Non| ASK
 ASK --> R
 R --> O
 DEST[Destination explicite ou lieu de l'activité] --> GEO[Lieu connu]
 O --> REQ[Requête MOTIS]
 GEO --> REQ
```

Résolution centralisée (`transport/location.py`) : position réelle seulement si fraîche et
précise (`assess_position`, utilisée aussi par `get_position`) ; chaque sortie part de la base
en vigueur à son début et revient à celle en vigueur à sa fin (`base_at` : lieu prévu couvrant
l'instant, sinon résidence de l'alternance, sinon domicile), la base pouvant donc changer dans la journée (« je pars de chez
papa et je rentre chez maman après les cours ») ; deux lieux prévus qui se chevauchent ⇒
origine inconnue signalée, jamais devinée ; entre deux engagements, l'origine est
le lieu de l'engagement précédent : le trajet est direct, sauf si le retour à la base laisse
au moins 1 h sur place une fois les deux trajets réels comptés. `get_location` donne à
l'assistant où l'utilisateur est ou sera à un instant (GPS réel maintenant, sinon trajet ou
activité située prévus, sinon lieu prévu, sinon résidence de l'alternance, sinon domicile),
avec la source utilisée.

Résidences multiples (générique, rien de personnel dans le code) :
- **Alternance** (`Alternation`, réglage `residence_alternation`) : résidences dans l'ordre de
  rotation, changement tous les `period_days` jours à la même heure locale `switch_at`
  (heure d'été comprise) ; une seule déclaration datée l'initialise (« ce vendredi soir je vais
  chez papa » ⇒ `set_residence_alternation`), la redéclarer est un changement définitif,
  `get_residence_schedule` la montre avec ses exceptions. Les **lieux prévus**
  (`set_planned_location`) sont des exceptions temporaires : ils priment sans décaler la
  récurrence ; une nouvelle exception (ou `clear_planned_location`) ne retire que la période
  qu'elle recouvre, les parties non concernées des exceptions existantes sont conservées. Chaque déclaration replanifie (plan local, stabilité préservée).
- **Point d'accès aux transports** par lieu, un seul par défaut et persistant
  (`access_place_id` + `access_minutes`, les deux ou aucun, contrainte en base ; durée donnée
  par l'utilisateur, jamais devinée). Un point d'accès différent pour **un seul** trajet se
  donne en conversation (`search_journey` : `access_place_id`, `access_minutes`,
  `access_side`) et n'est jamais enregistré ; pour une **période** (« cette semaine on me
  dépose à la gare »), `set_temporary_access` / `clear_temporary_access` (réglage JSON
  `access_overrides`, pas de table) s'appliquent aussi aux trajets planifiés et aux départs de
  cette période, prioritaires sur l'accès par défaut qui reste inchangé. Un seul
  adaptateur de routage (`transport/access.py`, `db.store.access_routing`) interroge MOTIS
  depuis/vers le point d'accès et ajoute une étape `ACCESS` de durée fixe ; il sert à la
  planification, aux trajets du jour, au contrôle de départ (GPS à moins de 300 m du lieu
  compris) et aux recherches en conversation.
- **Entraînements à la résidence** : les plages « à la résidence » (hors sorties vers les
  engagements fixes, retours compris) servent au placement ET au validateur indépendant, qui
  contrôle aussi tout le reste du plan (engagements, trajets de toutes les activités, autres
  activités) ; un lieu d'entraînement explicitement demandé reste permis.
- **Questions bloquantes** : un engagement au lieu inconnu (texte iCal sans alias) est une
  question (« Où a lieu… ? ») notifiée dans Home Assistant ; tant qu'elle n'a pas de réponse,
  un plan valide déjà publié est conservé (le nouveau, incomplet, est enregistré « rejeté »
  avec `kept_previous_plan`) ; sans plan antérieur, le plan partiel est publié et le trajet
  concerné n'est pas planifié.
- **Heures transmises au modèle** : les horodatages des résultats d'outils sont convertis
  dans le fuseau configuré avant d'être donnés à Gemini (constaté en production : le modèle
  lisait des heures UTC comme locales, cours de l'après-midi et bus annoncés une heure trop
  tôt).
- **Clarification** (dans la conversation, sans couche supplémentaire) : une seule règle de
  la consigne système de l'agent : chaque valeur passée à une modification vient de
  l'utilisateur, d'un résultat d'outil, ou est omise pour garder le défaut documenté du champ ;
  si une valeur manque ou si plusieurs lectures changent ce qui serait modifié (ponctuel ou
  permanent, période, lieu, élément), aucune modification, même réversible : une question, puis
  exécution avec la réponse combinée à la demande en attente. Les champs obligatoires des
  opérations (ex. durée d'accès) restent refusés s'ils manquent. Gemini est appelé avec un
  raisonnement « medium » : vérifié avec le vrai Gemini (`gemini-3.5-flash-lite` et
  `gemini-3.5-flash`), il ne pose plus 18:00 pour « vendredi soir » et demande « Je vais chez
  papa » (loger ou trajet ? dès maintenant ?) au lieu de créer une exception. Limite connue :
  le modèle léger interprète parfois une réponse groupée (« juste ce week-end ») pour deux
  questions à la fois.
- **Itinéraires irréalistes** rejetés : durée porte-à-porte maximale `max_journey_minutes`
  (180 par défaut, réglable en conversation), transmise à MOTIS (`maxTravelTime`) et
  revérifiée accès compris. Toutes les activités situées (cours, rendez-vous, entraînements, compétitions,
sessions de tâches situées, études hors base) reçoivent leurs trajets. Un texte de lieu iCal inconnu
bloque le temps sans trajet et est signalé (ajouter un alias). Coordonnées jamais devinées. Une
adresse dite par l'utilisateur est cherchée par `find_address` dans le géocodeur local de MOTIS
(`geocoding: true`, données OpenStreetMap du graphe, rien n'est envoyé ailleurs) ; l'assistant
relit les correspondances et ne crée le lieu qu'avec celle que l'utilisateur confirme.

### D08 — Données et moteur de transport

```mermaid
flowchart LR
 G[GTFS TEC, SNCB, STIB, De Lijn] --> I[Actualisation nocturne ETag + validation]
 O[OpenStreetMap] --> I
 I --> M[MOTIS v2.11.4]
 RT[TripUpdates et Alerts TEC, SNCB, De Lijn] --> R[Relais interne tempo-rt-relay]
 R --> M
 M <-->|/api/v6/plan| C[Client Tempo]
```

Faits vérifiés par requêtes réelles (23 et 25/11/2026) :
- Belgian Mobility Open Data : 4 GTFS statiques + TripUpdates et Alerts pour TEC, SNCB et
  De Lijn, uniquement par la passerelle authentifiée
  `api-management-opendata-production.azure-api.net/api/gtfs/feed/<op>/...` (centralisée dans
  `transport/feeds.py`), clé **obligatoire** dans l'en-tête `bmc-partner-key`, envoyée à ce
  seul hôte. Aucun accès anonyme. `?format=protobuf` obligatoire pour le temps réel ;
  TripUpdates avec `unroll=off` (arrêts sans prévision omis, données identiques, ~30 % plus
  léger). STIB : GTFS statique seulement. MOTIS est la seule source d'itinéraires : les
  trajets STIB (et tout exploitant sans temps réel) suivent le GTFS statique.
- Quota indicatif (conditions d'utilisation §5) : Standard 12 000/jour et 500/min ; au-delà,
  la passerelle peut répondre 429.
- Clé absente : le relais refuse de démarrer et la mise à jour des données s'arrête, chacun
  avec un message nommant le secret `tempo_bmc_partner_key` ; clé refusée (401/403) :
  journalisée et visible dans `/health` du relais, ou dans le rapport de la mise à jour.
- Interrogation : TripUpdates toutes les minutes (l'éditeur publie toutes les 30 s), alertes
  toutes les 5 min, pour les six flux : 5 184 requêtes/jour, soit 43 % du quota (54 % du
  plafond de 80 % que le relais s'impose) ; s'y ajoutent au plus 4 téléchargements statiques
  par jour. MOTIS interroge le relais (interne, gratuit) toutes les 30 s.
- Ce que fait MOTIS nativement (v2.11.4, sources lues) et que Tempo ne refait pas : interrogation
  à intervalle fixe, en-têtes par flux, abandon du temps réel d'un flux en échec
  (`incremental_rt_update: false`), donc retour aux horaires statiques. Ce qu'il ne fait pas,
  seule raison d'être du relais : quota, 429/`Retry-After`, rejet d'un flux périmé, réparation
  des `start_time` TEC.
- Relais (`transport/rt_relay.py`, une seule réplique) : détient la clé, plafonne à 80 % du
  quota, recul exponentiel. Un 429 suspend **tous** les flux (le quota est par compte) jusqu'à
  `Retry-After` (secondes ou date HTTP, jamais raccourci, au moins 5 min) ; un 401/403 est
  journalisé comme clé refusée. Les copies encore fraîches restent servies ; au-delà de 10 min
  le relais répond 503 et MOTIS abandonne ce temps réel (vérifié) : le routage continue sur
  les horaires statiques. Il retire `start_time` des TEC (décalage de 12 h en amont ;
  résolution 50 % → 100 %).
- Alertes : consultatives, filtrées par période d'impact, jamais d'exclusion de trajet ;
  annulations et retards viennent des TripUpdates. Un retard réel de +7 min a été observé.
- MOTIS national : import 3 min 29 s, 1,8 Go, serveur 0,7–0,9 Go, requête médiane 100–130 ms.

Contrat de recherche (`search_journey`) : origine et destination quelconques (`place:<id>`,
`activity:<id>`, `event:<id>` pour un cours, `gps` pour un trajet immédiat) ; bornes : arrivée
avant, départ après ou plage, par défaut déduites des activités (fin de l'origine, début de la
destination) ; une borne seule ⇒ fenêtre réglable (`window_minutes`, 2 h par défaut, jusqu'à
24 h) ; objectif (durée minimale par défaut, arrivée au plus tôt, départ au plus tard) ;
critères (correspondances, marche) ; nombre d'alternatives (`max_alternatives`, 3 par défaut,
jusqu'à 10) ; retour facultatif. La pagination s'adapte à la fenêtre (environ une requête par
demi-heure, de 5 à 20). Statuts : `ok` (meilleur + alternatives, « meilleur parmi les candidats
explorés », `exploration_incomplete` si une page suivante a échoué), `no_candidate`,
`origin_unknown`, `place_unknown`, `unavailable`, `error` (panne, jamais présentée comme
impossibilité). Le temps réel reste distingué en interne (`realtime` par trajet et par
segment).
Toute sélection d'itinéraire passe par `itinerary.find_trips` (MOTIS calcule, Tempo choisit
selon le contexte) :

| Contexte | Bornes | Objectif |
|---|---|---|
| Aller vers un engagement | départ après l'engagement précédent, arrivée avant le début − marge (10 min) | départ au plus tard |
| Trajet direct entre deux engagements | départ après le premier, arrivée avant le second − marge | départ au plus tard |
| Retour à la base | départ après la fin, arrivée avant la sortie suivante du jour (sinon 6 h) | arrivée au plus tôt |
| Estimation d'une activité flexible | autour de l'heure préférée (6 h) | durée minimale |
| Contrôle de départ | départ maintenant, arrivée avant le début − marge ; sinon au plus tôt | départ au plus tard, puis arrivée au plus tôt |
| Recherche en conversation | choisies par l'utilisateur (2 h autour d'une borne seule) | choisi par l'utilisateur |

Départage : perturbations sévères datées, puis correspondances. Toutes les alternatives
acceptables explorées sont conservées sur chaque trajet du planning publié. Aucun trajet ⇒
`impossible` ; panne ⇒ `unknown` ; le créneau reste bloqué et le problème est signalé. Aucune
durée de trajet n'est inventée : sans trajet connu, rien n'est réservé autour d'une activité
flexible (signalé), et un retour inconnu bloque le temps jusqu'à la borne connue suivante. Une
exécution de planification partage un cache : une requête identique n'atteint MOTIS qu'une fois.

## 8. Planification, stabilité et absence de solution

### D10 — Replanification

```mermaid
flowchart TD
 TR[Changement ou demande] --> LOCK[Verrou PostgreSQL de replanification]
 LOCK --> BASE[Données + planning publié]
 BASE --> MODE{Réorganisation globale demandée ?}
 MODE -->|Oui| GLOBAL[Sans pénalité de stabilité]
 MODE -->|Non| LOCAL[Pénalité de déplacement des séances existantes]
 GLOBAL --> ACT[Activités flexibles + trajets MOTIS]
 LOCAL --> ACT
 ACT --> CP[CP-SAT étude]
 CP --> VALID{Validateur indépendant}
 VALID -->|Valide| DIFF[Diff : ajoutées, déplacées, conservées, décalage]
 VALID -->|Violations| REJ[Version rejetée, plan précédent conservé]
 DIFF --> PUB[Publication atomique + propositions pour les protégés]
```

- Pipeline (`planning/engine.py`) : engagements fixes (cours, compétitions, activités fixes,
  verrouillées ou en cours) et leurs trajets → activités flexibles placées autour d'eux dans
  leur fenêtre avec trajets réservés (`scheduler/activities.py`) → trajets obligatoires jour
  par jour → fenêtres d'étude réduites par les trajets → CP-SAT d'étude → validation
  indépendante du plan entier.
- Contraintes dures : pas de chevauchement, horaires imposés, verrouillages, activités en
  cours, disponibilités, fenêtres de catégorie, durées prescrites, échéances strictes (étude et
  séances de tâche), ordre et récupération des séances du coach, trajets, bornes de longueur de
  bloc configurées. Jamais relâchées silencieusement.
- Préférences souples (jamais plus fortes que la faisabilité) : plafond d'étude quotidien,
  nombre de blocs par matière et par jour, longueur préférée, alternance, heures d'activité
  (réglage `activity_hours`, 06:00–22:00 par défaut), jour suggéré du coach, cible souple.
- Objectif (pondérations bornées, `scheduler/solver.py`) : travail non planifié (poids
  dominant, multiplié par la priorité 1–3) ≫ dépassement du plafond quotidien > retard sur
  cible souple > stabilité (déplacer une séance existante coûte 200) > alternance des matières
  > longueur préférée de 90 min > dépassement de volume. Seconde passe : les tâches encore
  incomplètes peuvent utiliser autant de blocs par jour que leur temps libre le permet
  (chaque bloc supplémentaire pénalisé). Une solution équivalente sans déplacement est donc
  préférée.
- Validateur indépendant (`scheduler/validate.py`) : séances d'étude, et pour tout le reste
  activités dans leur fenêtre, leurs fenêtres de catégorie et avant une échéance stricte,
  ordre et récupération du coach, aucun chevauchement avec engagements et trajets calculés.
  Deux engagements fixes qui se chevauchent sont un conflit de sources, signalé, pas une
  violation du plan.
- Statut : `complete` seulement si tout le travail, toutes les activités et tous les trajets
  sont placés ; `partial` sinon (détail signalé) ; `no_solution` si un solveur n'a pas répondu
  dans son temps (inconclusif, l'ancien plan reste) ; `invalid` en cas de violation.
- Mode `global` uniquement sur demande explicite (`plan_now(mode="global")`) : la stabilité est
  ignorée ; verrouillé et en cours restent protégés. `dry_run` simule sans publier.
- Concurrence : `pg_advisory_xact_lock` sérialise API et worker ; une seule version `active`.
- Performance : borne inférieure prouvée + démarrage glouton ; 8 semaines / 8 matières en
  ~1 s (prouvé optimal) sur le poste de développement. Limites : 10 s par passe d'étude (deux
  passes au plus) et 5 s pour les activités.

### D11 — Diagnostic des résultats

```mermaid
flowchart TD
 S[Résultat] --> T{Statut}
 T -->|complete / partial| V[Validation finale]
 T -->|no_solution: UNKNOWN| U[Incertitude explicite, plan précédent conservé]
 T -->|invalid| ERR[Violations listées, rien publié]
 T -->|routage indisponible| RU[Panne, pas une impossibilité]
 V --> RES[Placé, reliquat par tâche, échéances strictes manquées, activités non placées, trajets en échec]
 RES --> AI[Explication fidèle : explain_plan]
 AI -->|Accord explicite| NEW[Modifier seulement ce qui est autorisé puis plan_now]
```

Exemple vérifié par test : « 50 h de chimie avant demain, stricte » ⇒ statut `partial`,
minutes non planifiées par tâche, échéance stricte signalée comme manquée, jamais comme
réussie. `explain_plan` donne pour chaque échéance stricte manquée la cause chiffrée
(`deadline_capacity` : minutes nécessaires et minutes d'étude libres avant l'échéance, après
engagements, trajets et entraînements). `propose_alternatives` simule ensuite, sans rien publier ni modifier, des alternatives
déterministes par tâche touchée (au plus 3) : reporter l'échéance (plus petit report essayé
parmi +2, +7, +14, +28 jours qui résout), rendre souple une échéance stricte, réduire le volume
à ce qui tient. Chaque alternative donne le résultat réel de sa simulation (reliquat, retard,
échéance stricte manquée). Élargir une disponibilité reste une simulation `dry_run` de
l'assistant (les heures à ouvrir sont un choix de l'utilisateur). Rien n'est appliqué sans
accord.

## 9. Validation, propositions et concurrence

### D14 — États d'une proposition

```mermaid
stateDiagram-v2
 [*] --> Pending
 Pending --> Rejected : Refus naturel
 Pending --> Approved : Accord naturel, entrées inchangées
 Pending --> Obsolete : Entrées modifiées
 Approved --> [*]
 Rejected --> [*]
 Obsolete --> [*]
```

- Une proposition (`unlock_block`, `unlock_activity`, `coach_plan_change`, `training_import`)
  enregistre l'empreinte de son entrée (`input_revision`) ; à l'accord, l'empreinte est
  recalculée : différente ⇒ `obsolete`, rien n'est appliqué, une replanification est demandée.
- Conflits entre engagements de toutes catégories (cours, compétitions, rendez-vous fixes,
  activités verrouillées), détectés en un seul endroit (`planning/engine.find_conflicts`) et
  repris dans le résumé de chaque version, même rejetée : une activité flexible verrouillée en conflit produit une
  proposition de déverrouillage, une séance d'étude verrouillée aussi ; deux engagements fixes
  produisent une notification de conflit (rien ne bouge). Une activité déjà commencée n'est
  jamais déplacée ni proposée. Modification iCal nocturne : le cours officiel est enregistré
  immédiatement ; l'étude flexible est réoptimisée automatiquement (changement ordinaire). Les
  rappels relatifs suivent le planning publié, jamais une proposition.
- Accord et refus se font en conversation (`decide_proposal` sur une proposition identifiée ;
  repli déterministe « accepte » / « refuse » s'il n'y en a qu'une).

## 10. Worker, rappels et notifications

### D15 — Cycle autonome

```mermaid
flowchart TD
 W[Worker, tick 30 s] --> J[job_run : état et erreur de chaque job]
 J --> SYNC[iCal toutes les 30 min, e-mails du coach]
 J --> SER[Expansion des séries]
 J --> RP[Replanification : demandée, nocturne 03:00, périmée]
 J --> CHECK[Contrôle de départ toutes les 2 min]
 J --> REM[Rappels arrivés à échéance]
 J --> RET[Rétention quotidienne]
 CHECK --> STATUS{Encore faisable ?}
 STATUS -->|Oui| KEEP[Rappels de départ 60 et 15 min, configurables]
 STATUS -->|Non| ALERT[Partir plus tôt, en retard, impossible]
 STATUS -->|Inconnu| WARN[Position ou routage inconnu]
 RP --> DB[(État durable)]
 REM --> N[Notifications]
 ALERT --> N
 N --> HA[Home Assistant : persistantes + notify facultatif]
```

- Rappels utilisateur (`reminder`) : à heure fixe (éventuellement RRULE) ou N minutes avant une
  activité ou un cours ; l'échéance est recalculée depuis la cible à chaque contrôle (un
  déplacement déplace le rappel, une annulation l'annule). Identité = rappel + instant dû :
  pas de doublon après redémarrage ; un rappel manqué pendant une panne part au redémarrage
  (jusqu'à 6 h, marqué « en retard »).
- Rappels de départ : plusieurs seuils (`update_settings.departure_reminder_minutes`), sur le
  prochain trajet aller (jamais un retour à la base), recalculés avec la position réelle et le
  temps réel ; sans position fiable ou sans routage, l'utilisateur est averti et les rappels
  suivent le départ du planning publié, en le disant.
- Notifications créées par une seule fonction backend, dédupliquées par clé (une panne de
  routage : une notification par jour) ; Home Assistant les affiche et les acquitte à la
  remise, sans en fabriquer d'autres ; informatives uniquement (aucune action).
- Aucun job ne dépend du LLM ; l'import d'e-mails par LLM (texte libre) passe par une
  proposition à approuver.

## 11. Déploiement et exigences non fonctionnelles

### D16 — Déploiement cible

```mermaid
flowchart TD
 U[Téléphone / ordinateur] <--> HA[Home Assistant existant + intégration HACS]
 subgraph SW[Docker Swarm sur serveur Debian]
 API[tempo-api]
 W[tempo-worker]
 DB[(tempo-postgres)]
 M[tempo-routing / MOTIS]
 RT[tempo-rt-relay]
 UP[tempo-routing-update]
 BK[backup]
 API <--> DB
 W <--> DB
 API <--> M
 W <--> M
 RT --> M
 UP --> M
 BK --> DB
 end
 HA <-->|Réseau overlay privé, jeton| API
 API <--> L[Gemini facultatif : Google Cloud Vertex AI]
 I[iCal et IMAP autorisé] --> W
 G[GTFS et OSM] --> UP
 R[GTFS-RT Belgian Mobility] --> RT
```

- Réseaux : `backend` et `routing` internes ; `ha` partagé avec Home Assistant ; `egress` pour
  le worker (iCal, IMAP), le relais, la mise à jour des données et l'API (LLM seulement).
  PostgreSQL et le relais ne sont jamais publiés.
- Sécurité : jeton de service fort (≥ 16 caractères distincts), rotation avec un jeton précédent,
  comparaison à temps constant, blocage après 10 échecs/60 s (429), `/health` seul public,
  OpenAPI désactivé côté HTTP ; secrets Docker (`/run/secrets`) ; images non root, système de
  fichiers en lecture seule, dépendances épinglées ; deux rôles PostgreSQL (propriétaire DDL,
  rôle applicatif DML, audit en ajout seul) ; logs sans jeton ni données personnelles.
- Fiabilité : migrations Alembic sérialisées par verrou consultatif ; transactions et points de
  sauvegarde ; versions de planning ; sauvegarde quotidienne + exercice de restauration ;
  health checks (API, relais, worker par battement, MOTIS) ; retries bornés ; calculs en UTC,
  règles en heure locale (sûr aux changements d'heure). Mise à jour des données de routage
  chaque nuit (02:30 UTC) sur le nœud qui porte MOTIS (volume local partagé, contrainte de
  placement `node.role == manager`).
- Performance : horizon glissant (14 jours par défaut, cibles lointaines conservées en part
  proportionnelle), séries matérialisées sur 60 jours, solveur limité à 10 s.
- Pas de dépendance à Claude Code en production.

## 12. État réel et recette

Légende : **Vérifié** = test automatisé sur PostgreSQL réel (et MOTIS réel quand indiqué) ;
**Partiel** = implémenté, une partie de la vérification manque ; **Non** = non fait.

| Besoin | État | Preuve / écart |
|---|---|---|
| B01 Conversation | Partiel | Agent Gemini à outils + repli déterministe testés avec un client LLM simulé (`tests/integration/test_assistant.py`) et test de contrat HA ↔ backend ; **recette dans un vrai Home Assistant 2026.11.2** (Docker, `scripts/ha_e2e.py`, 15/15) : flux de configuration, agent de conversation Assist, ajout de tâche et question par la conversation (repli déterministe), refus de deviner, GPS du téléphone transmis et jugé fiable, liste de tâches à jour, aucun service métier ; budget de 60 s, panne du LLM après une modification sans rejeu, délai de 150 s côté HA (`test_assistant.py`). **Jamais exécuté avec un vrai LLM** (aucune clé). |
| B02 Événements | Vérifié | Fixe, flexible, journée entière occupée/informative, plusieurs jours, série + exception + « suivantes » + série entière, verrouillage (`test_operations.py`). |
| B03 Tâches | Vérifié | Sans durée puis estimée, planifiée, progression partielle idempotente, séance unique située ; échéance stricte d'une séance située en heure locale, jamais repoussée, manquée signalée (`test_domain_rules.py`). |
| B04 Université | Partiel | Import, modifications, annulations, sync suspecte, DST testés sur fixtures ; éléments ignorés (journée entière, repères, invalides) notifiés. **Flux réel de l'université validé localement** le 25/11/2026 (secret `tempo_ics_url`) : téléchargement sécurisé, 36 cours importés dans une base jetable, seconde synchronisation sans changement ; ses congés codés en durée nulle ont révélé un défaut corrigé (`test_ics.py`). Pas encore en service continu (worker non déployé). |
| B05 Études | Vérifié | 15 h, blocs de 90 min, souple/stricte, correction sans double comptage. |
| B06 Sport | Partiel | Entraînements (flexibles) et compétitions (fixes) créés en conversation (`create_event` avec `kind`) ; DOCX, mise à jour sans doublon, durée inconnue, protections testés (fixtures + vrai serveur IMAP local) ; authentification par le serveur de réception désigné, en-têtes falsifiés refusés (`test_text_parser.py`, `test_worker_mail.py`). **Adresse réelle du coach, boîte réelle et vrais en-têtes DMARC/DKIM non testés.** |
| B07 Règles | Vérifié | Fenêtres hebdomadaires d'étude + exceptions datées + préférences ; fenêtres par catégorie d'activité flexible (`set_category_windows` : entraînement, personnel, tâche située), contrainte dure dans CP-SAT et revérifiée par le moteur (`test_activities.py`, `test_operations.py`). |
| B08 Lieux | Vérifié | GPS frais/périmé/imprécis, lieu prévu sans altérer le GPS, origine inconnue ⇒ question ; base qui change dans la journée (`test_engine.py`) ; lieu utilisé jamais supprimé, alias conservés. |
| B09 Transport | Vérifié | `search_journey` sur un **vrai MOTIS** (Bruxelles et national) : arrivée avant, départ après, plage, aucun candidat (`test_motis_live.py`) ; exploration paginée (jusqu'à 5 requêtes, panne après la première page signalée sans perdre les candidats) ; critères (correspondances, marche) et retour dans la même recherche, testés avec des fournisseurs simulés et exécutés sur le MOTIS national (aller et retour, 5 pages chacun) ; une borne seule reste dans une fenêtre de 2 h. Résultat : « meilleur parmi les candidats explorés ». |
| B10 Planning mixte | Vérifié | Cours, étude, entraînements, compétition, activités personnelles, trajets ; validateur indépendant du plan entier (étude, fenêtres, catégories, échéances, ordre du coach, chevauchements) ; préférences d'étude et heures d'activité souples (`test_scheduler_soft.py`)., y compris les trajets choisis pour une activité flexible contre les engagements fixes. |
| B11 Stabilité | Vérifié | Ajout en créneau libre sans déplacement ; conflit local avec changements minimaux ; mode global sur demande ; activité flexible en cours jamais déplacée. Poids bornés, pas d'optimisation lexicographique stricte. |
| B12 Conflits | Vérifié | Manque de temps, reliquat, échéance stricte manquée, panne de routage, version invalide rejetée ; diagnostic par échéance stricte manquée (`deadline_capacity` : travail nécessaire vs minutes d'étude libres avant l'échéance, après engagements, trajets et entraînements) ; alternatives toutes simulées (`propose_alternatives`, rien publié) ; statut `partial` dès qu'une activité, un trajet ou du travail reste, `no_solution` si un solveur ne répond pas ; conflits entre toutes catégories. Le diagnostic n'est pas un noyau minimal calculé par le solveur : la capacité libre est partagée entre les tâches (borne supérieure). |
| B13 Validation | Vérifié | Proposition périmée rejetée ; replanifications concurrentes sérialisées (3 fils). |
| B14 Notifications | Vérifié | Rappel relatif qui suit sa cible, sans doublon, annulé avec la cible, clos après elle ; plusieurs rappels de départ, y compris sans GPS (départ du plan) ; position ou routage inconnus notifiés ; une seule source de notifications (HA ne les duplique plus). |
| B15 Autonomie | Vérifié | Jobs testés individuellement, sans LLM ; endurance : la vraie fonction `tick` du worker sur 7 jours simulés (1 008 cycles de 10 min, PostgreSQL réel) avec un cours déplacé par l'iCal, une panne de routage de 4 h et du travail déclaré chaque soir : tous les jobs sains à la fin, rappels émis une seule fois par échéance, replanification au moins nocturne, aucune séance sur un cours, croissance bornée, cycle p95 0,03 s / max 0,4 s (`test_soak.py`). Pas encore mesuré en temps réel sur le serveur. |
| B16 Explication | Vérifié | `explain_plan`, `get_sources_status`, `get_history`. |
| B17 Fiabilité | Partiel | PostgreSQL 16 réel, migrations montantes/descendantes, rôles, authentification ; pile Docker complète (postgres, migrate, api, worker, relais temps réel, MOTIS national) démarrée et utilisée : jobs du worker sains, replanification et recherche d'itinéraire réelles ; `scripts/backup.sh` et `restore_drill.sh` exécutés contre le PostgreSQL de la pile (6 tables identiques, schéma à jour) ; mise à jour des données de routage (`gtfs_update`) exécutée contre le portail réel. Pile Swarm prête pour un déploiement manuel via Portainer (images GHCR `tempo-backend`, `tempo-routing`, `tempo-postgres` publiées par la CI sur étiquette de version, aucun fichier du dépôt monté ; images construites et image PostgreSQL vérifiée localement : rôle `tempo_app` créé, sauvegarde produite). **Non fait** : déploiement sur le serveur et exercice de restauration (à faire par l'utilisateur), installation HACS (dépôt privé : installation manuelle). |

Recette sur la pile complète (`scripts/live_scenario.py`, PostgreSQL et MOTIS national réels,
worker actif) : 15/15 — rendez-vous en créneau libre sans aucun déplacement, rendez-vous sur un
bloc ⇒ un seul bloc déplacé, trajets réels sans étude pendant le trajet, bloc verrouillé en
conflit ⇒ proposition approuvée en conversation, échéance stricte impossible ⇒ plan partiel,
capacité chiffrée et alternatives simulées, réorganisation globale simulée sans publication,
trajet direct entre deux activités, localisation future.

Dernière exécution complète (25/11/2026, après la suppression de l'accès anonyme Belgian
Mobility) : backend **541 tests réussis, 0 ignoré** (PostgreSQL 16 réel, MOTIS Bruxelles et
MOTIS national réels réimportés, contrat avec le vrai client HA, IMAP GreenMail, passerelle
Belgian Mobility réelle avec la clé) ; Ruff et mypy strict propres. Pile de développement avec
la vraie clé : relais sain (6 flux, aucune erreur), MOTIS résout 100 % des TripUpdates TEC ;
`scripts/live_scenario.py` 15/15 et vrai Home Assistant 2026.11.2 (`scripts/ha_e2e.py`) 15/15.
Intégration HA inchangée depuis sa dernière exécution (63 tests réussis).

Bloquants nécessitant l'utilisateur : compte de service Vertex AI et choix du modèle Gemini (tester B01 réellement), installation de
l'intégration dans le vrai Home Assistant (le `device_tracker` s'y choisit), adresse de
l'expéditeur du coach + accès IMAP, clé
Belgian Mobility Standard, création et visibilité des dépôts GitHub, licence de l'intégration,
autorisation de déploiement Swarm.

## 13. Décisions techniques conservées

- Deux dépôts : ce dépôt privé (backend, worker, base, routage, déploiement) et
  `tempo-homeassistant` (HACS exige `custom_components/<domaine>/` à la racine d'un dépôt
  public). Aucun des deux n'est publié sans accord.
- Python 3.13 + `uv` (verrou `backend/uv.lock`), Ruff, mypy `--strict`, pytest ; `tzdata`
  explicite ; client HTTP `httpx2` ; LLM : uniquement Gemini sur Google Cloud Vertex AI (SDK
  `google-genai`, facturé au projet Google Cloud, éligible à ses crédits). Modèle par défaut
  `gemini-3.5-flash-lite`, réglable (`TEMPO_LLM_MODEL`), choisi sur mesure réelle (2026-11) :
  le plus rapide des modèles accessibles (2 à 5 s par demande), sans jetons de réflexion
  facturés, appels d'outils corrects ; `gemini-3.8-flash` dépassait le délai (504).
  Cache : le cache implicite de Gemini suffit (57 % des jetons d'entrée servis depuis le cache
  sur une conversation réelle) grâce à un préfixe stable (consignes puis outils, ordre fixe,
  vérifié par test) ; un cache explicite ajouterait un stockage facturé à l'heure et un cycle
  de vie à gérer pour un usage intermittent : non retenu. Mesure : une ligne `llm usage` par
  appel (jetons, cache, latence), sans contenu.
- iCal : `icalendar` + `recurring-ical-events`, avec pré-analyse de la source car la
  bibliothèque ajoute un RECURRENCE-ID aux événements non récurrents et inverse silencieusement
  DTSTART/DTEND incohérents.
- Tests d'intégration PostgreSQL locaux via `pgserver` (PostgreSQL 16) ; PostgreSQL ≥ 15 requis
  (`NULLS NOT DISTINCT`).
- Image de routage : binaire officiel MOTIS v2.11.4 épinglé par SHA-256 ; reconstruction des
  graphes dans un volume séparé avec bascule atomique et conservation de la version précédente.
- Les e-mails libres interprétés par LLM ne sont jamais appliqués sans approbation.
- Une seule surface métier : le catalogue d'opérations (`/v1/conversation`, `/v1/operations`).
  L'intégration HA lit elle aussi par opérations (`get_summary`, `get_schedule`, `list_tasks`,
  `list_proposals`, `list_notifications`, `get_context`, `list_places`) et règle ses paramètres
  techniques par `update_settings`. L'interpréteur LLM `ai/llm.py` ne sert qu'aux e-mails libres
  du coach (proposition à approuver).
