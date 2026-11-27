# Tempo — Corrections et améliorations à réaliser

## 1. Compréhension des intentions et communication entre l’IA et le backend — Priorité critique

L’IA comprend parfois mal les demandes de l’utilisateur et transmet des informations incorrectes ou incomplètes au backend. Elle peut également perdre le contexte d’une conversation, utiliser une mauvaise localisation ou confirmer une action qui n’a pas réellement été effectuée.

Il faut auditer l’ensemble du pipeline, depuis la réception du message utilisateur jusqu’à l’exécution des outils et la formulation de la réponse finale.

L’IA doit être capable de comprendre les références implicites, les précisions successives et les modifications apportées à une demande précédente. Elle doit conserver le contexte du déplacement ou de l’événement concerné, notamment lorsque l’utilisateur emploie des expressions comme « là-bas », « le lendemain » ou « je prends le train ».

Il faut également vérifier que les outils métier disponibles permettent de traduire correctement toutes ces intentions en opérations concrètes et persistantes. L’IA doit pouvoir consulter, créer, modifier et expliquer les éléments du planning sans perdre leur contexte ni créer de doublons.

Une distinction claire doit être faite entre une demande d’information, une préférence exprimée par l’utilisateur et une demande de modification du planning.

L’IA ne doit jamais inventer un itinéraire, un horaire ou une modification. Toute confirmation de réussite doit correspondre à une opération réellement exécutée et validée par le backend. Si un nouveau planning est rejeté et que l’ancien reste actif, l’IA doit l’indiquer clairement.

**Résultat attendu :** pouvoir communiquer naturellement avec Tempo, apporter des précisions successives et modifier une situation sans devoir répéter l’ensemble du contexte.

## 2. Continuité géographique et temporelle des localisations — Priorité critique

Tempo doit disposer d’une vision continue de la position réelle et prévisionnelle de l’utilisateur. Actuellement, certains déplacements semblent être calculés indépendamment des déplacements précédents, ce qui entraîne des incohérences dans les origines et les destinations.

Il faut auditer et consolider le système existant de gestion des localisations afin de maintenir une chronologie géographique cohérente, alimentée par :

- La position GPS réelle du téléphone, récupérée depuis Home Assistant.
- Les localisations futures exprimées par l’utilisateur.
- Les événements comportant une localisation.
- Les déplacements planifiés.
- Les séjours et hébergements prévus.
- Les habitudes de localisation, notamment l’alternance parentale.

Lorsqu’un utilisateur indique qu’il se rend à un endroit, cette information doit être comprise par l’IA, transmise au backend et enregistrée durablement dans la chronologie des localisations futures.

Chaque déplacement doit entraîner un changement de position prévisionnelle. La destination d’un déplacement doit ensuite constituer l’origine logique du déplacement suivant, sauf si un autre déplacement ou une information plus pertinente intervient entre les deux.

L’alternance parentale doit rester une indication de localisation habituelle. Elle ne doit jamais provoquer un changement automatique de position incompatible avec les déplacements et les séjours déjà prévus.

Par exemple, si l’utilisateur se rend à Louvain-la-Neuve pour une proclamation le 3 décembre et doit rejoindre Mons pour un cours le lendemain matin, Tempo doit comprendre la continuité de cette situation. En l’absence de retour prévu au domicile, la recherche du trajet vers Mons doit partir de Louvain-la-Neuve, et non du domicile indiqué par l’alternance parentale. Une présence prévisionnelle ne doit toutefois pas être présentée comme un hébergement explicitement confirmé.

La position GPS réelle doit permettre de réconcilier la situation réelle avec la situation prévue. Sa fraîcheur et sa précision doivent être contrôlées avant son utilisation. Une nouvelle position GPS ne doit pas écraser les intentions de déplacement futures.

Lorsqu’un écart significatif apparaît entre la position réelle et la position prévue, Tempo doit déterminer les conséquences sur les prochains déplacements et recalculer les éléments concernés si nécessaire, conformément aux règles de validation et de confirmation existantes.

Cette continuité doit être garantie par le backend et ne doit pas dépendre de la capacité du LLM à reconstruire l’ensemble des déplacements à chaque conversation.

**Résultat attendu :** Tempo doit pouvoir déterminer à chaque instant où se trouve réellement l’utilisateur, où il est censé se trouver dans le futur, pourquoi il s’y trouve et comment il rejoint sa prochaine destination. Aucun changement de localisation prévisionnelle ne doit se produire sans justification.

## 3. Cohérence du routage et du moteur de planification — Priorité critique

Il faut auditer les interactions entre le système de résolution des localisations, MOTIS, OR-Tools et le mécanisme de validation finale du planning.

Les recherches d’itinéraires doivent systématiquement utiliser la position prévisionnelle de l’utilisateur au moment du départ. Elles doivent tenir compte des événements précédents et suivants, des contraintes horaires, des préférences de transport et des déplacements déjà planifiés.

Les recherches MOTIS doivent correctement gérer les contraintes d’heure de départ, d’heure d’arrivée et de plage horaire. Les temps de marche, les correspondances et les marges nécessaires doivent être intégrés au calcul.

Les itinéraires alternatifs doivent être conservés afin de permettre à l’utilisateur de comparer plusieurs possibilités ou de modifier son déplacement en langage naturel.

Il faut également vérifier la distinction entre un événement bloquant, une période de présence dans un lieu et un séjour. Une période de présence géographique ne doit pas être interprétée automatiquement comme une activité empêchant tout déplacement.

Lorsqu’un conflit apparaît entre un déplacement et un événement fixe, Tempo doit rechercher les solutions réalisables avant de proposer de déplacer, supprimer ou manquer cet événement.

Une modification de déplacement doit entraîner une réévaluation de ses conséquences sur la chronologie des localisations et les activités suivantes. Le recalcul doit cependant préserver au maximum la stabilité du planning existant et éviter les modifications inutiles.

Le planning final doit faire l’objet d’une validation géographique et temporelle complète avant son application. Il doit être impossible d’appliquer un planning contenant un déplacement irréalisable, une incohérence de localisation ou un chevauchement incompatible avec les contraintes métier.

**Résultat attendu :** un planning réellement réalisable, dans lequel les déplacements, les événements et les localisations restent cohérents sur plusieurs jours consécutifs.

## 4. Informations de transport en temps réel dans le planning — Priorité haute

Tempo doit permettre de consulter directement dans le planning les informations nécessaires pour effectuer chaque déplacement en transport en commun, sans devoir ouvrir une application externe.

Il faut vérifier les informations réellement disponibles dans les flux statiques et temps réel de Belgian Mobility ainsi que leur prise en charge par la version de MOTIS utilisée.

Pour chaque déplacement, Tempo doit notamment pouvoir afficher, lorsque les données sont disponibles :

- Le numéro et le type de train ou de bus.
- L’opérateur et la destination finale.
- Les arrêts et gares concernés.
- Les heures de départ et d’arrivée théoriques.
- Les heures de départ et d’arrivée actualisées.
- Les quais et voies de départ, d’arrivée et de correspondance.
- Les changements de quai.
- Les retards et suppressions.
- Les temps de correspondance.
- Les éventuelles perturbations affectant le déplacement.

Les identifiants nécessaires au suivi individuel des trains, bus et arrêts doivent être conservés afin de permettre l’actualisation d’un trajet existant sans devoir systématiquement rechercher un nouvel itinéraire.

Les informations théoriques et actualisées doivent être conservées séparément. L’interface doit permettre de distinguer les données statiques, les données temps réel, les données périmées et les informations indisponibles.

L’actualisation doit être automatique. Sa fréquence doit tenir compte de la proximité du déplacement, de son état et de la fréquence réelle de publication des données par les opérateurs. Les intervalles d’actualisation doivent être centralisés dans les constantes de Tempo.

Les changements importants, notamment les changements de quai, les suppressions, les retards significatifs et les correspondances devenues impossibles, doivent pouvoir entraîner une notification via Home Assistant.

Un simple changement de quai ne doit pas provoquer un recalcul complet du planning. En revanche, lorsqu’un retard ou une suppression compromet la faisabilité du déplacement ou d’un événement suivant, Tempo doit rechercher des alternatives et recalculer les éléments affectés.

L’ensemble de ce mécanisme doit fonctionner automatiquement, sans intervention du LLM et sans consommation de quota IA.

**Résultat attendu :** disposer dans Tempo d’un planning dynamique intégrant les informations de transport réellement disponibles et actualisées, notamment les quais des trains en temps réel.

## 5. Fiabilité, architecture, diagnostic et tests — Priorité critique

Il faut auditer l’architecture actuelle afin d’identifier les causes profondes des dysfonctionnements plutôt que de corriger uniquement les symptômes ou d’ajouter des exceptions spécifiques.

Les responsabilités doivent être clairement réparties. Le LLM doit comprendre les demandes, sélectionner les outils appropriés et expliquer les résultats. Le backend doit appliquer les règles métier, gérer les localisations, garantir la cohérence des données et contrôler la faisabilité des opérations. MOTIS doit fournir les informations de routage et OR-Tools doit assurer l’optimisation temporelle du planning.

Les garanties essentielles ne doivent pas dépendre uniquement du prompt système ou du comportement du modèle IA.

Il faut assurer une traçabilité suffisante de l’ensemble du pipeline, notamment des messages transmis au LLM, des appels aux outils, de leurs paramètres et résultats, des décisions de résolution des localisations, des recherches MOTIS, des calculs du solveur et des validations du planning.

Cette journalisation doit permettre de reproduire et de diagnostiquer un dysfonctionnement sans nécessairement effectuer de nouveaux appels IA, tout en protégeant les données sensibles.

Les différents états d’un planning doivent être clairement distingués : généré, validé, appliqué ou rejeté. Les réponses de l’IA doivent toujours correspondre à l’état réellement enregistré.

Les constantes, seuils et paramètres configurables doivent être centralisés. Il faut également éviter les mécanismes redondants, les appels IA inutiles et les corrections spécifiques à un seul scénario.

Des tests unitaires, d’intégration et de bout en bout doivent couvrir notamment :

- Le scénario de la proclamation à Louvain-la-Neuve suivie d’un cours à Mons le lendemain.
- Les séjours hors domicile et les conflits avec l’alternance parentale.
- Les déplacements successifs et les déplacements à cheval sur minuit.
- Les modifications de localisation ou de destination en cours de conversation.
- Les écarts entre la position GPS réelle et la position prévue.
- Les préférences de transport exprimées en langage naturel.
- Les demandes d’information ne nécessitant aucune modification.
- Les conflits de planning et les échecs du planificateur.
- Les confirmations de réussite et d’échec.
- Les changements de quai, retards et suppressions en temps réel.
- Les correspondances devenues impossibles.
- Les données temps réel indisponibles ou périmées.
- La cohérence entre les réponses de l’IA et l’état final de la base de données.

Les tests déterministes doivent pouvoir être exécutés sans consommer de quota IA. Des évaluations complémentaires avec un véritable LLM doivent également permettre de vérifier la compréhension des intentions et la qualité des appels aux outils.

**Résultat attendu :** un système fiable, cohérent, maintenable et testable, capable de gérer naturellement les demandes complexes de l’utilisateur sans inventer d’informations ni produire de planning irréalisable.

## 6. Simplification et nettoyage de l’existant avant toute modification — Principe obligatoire

Avant toute correction, amélioration ou ajout de fonctionnalité, il faut systématiquement analyser le code et l’architecture existants.

L’objectif ne doit pas être d’empiler de nouvelles fonctionnalités, de nouvelles couches d’abstraction ou de nouveaux mécanismes de correction sur un système déjà complexe.

Il faut d’abord identifier :

- Le code mort, inutilisé ou devenu obsolète.
- Les fonctions, classes, services et outils redondants.
- Les anciennes implémentations qui coexistent inutilement avec leurs remplacements.
- Les mécanismes de secours et les exceptions qui ne sont plus nécessaires ou qui masquent des problèmes plus profonds.
- Les responsabilités dupliquées entre plusieurs composants.
- Les abstractions et couches intermédiaires qui ajoutent de la complexité sans apporter de bénéfice réel.
- Les constantes, configurations et règles métier dupliquées ou contradictoires.
- Les dépendances et appels externes devenus inutiles.

Pour chaque modification envisagée, respecter l’ordre de réflexion suivant :

1. Vérifier si le problème peut être résolu en supprimant un mécanisme inutile, incorrect ou contradictoire.
2. Vérifier si une modification ou une simplification du code existant suffit.
3. Vérifier si plusieurs mécanismes existants peuvent être fusionnés ou mutualisés.
4. Réutiliser les composants existants lorsqu’ils sont adaptés.
5. Ajouter du nouveau code ou un nouveau composant uniquement lorsque les solutions précédentes sont insuffisantes.

Lorsqu’un mécanisme est remplacé, supprimer proprement son ancienne implémentation si elle n’est plus nécessaire. Ne pas conserver plusieurs systèmes concurrents remplissant la même fonction sans justification technique.

Avant toute suppression, vérifier les usages, les dépendances, les contrats d’API, les migrations éventuelles et les conséquences sur les fonctionnalités existantes. La suppression de code ne doit pas entraîner de perte de fonctionnalité ni de régression.

Il ne faut pas chercher à réduire artificiellement le nombre de lignes de code. La priorité est de réduire la complexité réelle, d’éliminer les incohérences et de maintenir une architecture claire, avec une seule source de vérité pour chaque responsabilité métier.

Toute nouvelle couche, dépendance, abstraction ou duplication volontaire doit être justifiée par un besoin réel.

**Résultat attendu :** une architecture plus simple, plus cohérente et plus maintenable après chaque modification, plutôt qu’un système qui devient progressivement plus complexe à chaque correction.

## 7. Gestion des préférences permanentes de transport — Priorité haute

Tempo doit permettre à l’utilisateur d’exprimer naturellement des préférences de transport permanentes ou temporaires, sans devoir les répéter à chaque génération du planning.

Par exemple, lorsque l’utilisateur indique « Privilégie toujours le bus E110 de 7h14 plutôt que celui de 6h58 », l’IA doit comprendre qu’il s’agit d’une préférence durable, la transmettre au backend et l’enregistrer de manière persistante.

Il faut auditer les mécanismes de préférences existants avant d’ajouter une nouvelle implémentation. Dans la mesure du possible, étendre et consolider le système existant plutôt que créer un système indépendant pour les transports.

Les préférences doivent pouvoir concerner les lignes, les arrêts, les horaires, les correspondances, les modes de transport, les itinéraires et les destinations intermédiaires.

Il faut distinguer les contraintes obligatoires des préférences souples. Le terme « privilégier » signifie qu’un itinéraire doit être favorisé lorsque cela est possible, mais qu’il ne doit pas être imposé lorsqu’il rend le planning irréalisable.

Dans l’exemple du bus E110, le départ de 7h14 doit être privilégié lorsque les contraintes horaires le permettent. Si ce départ entraîne un retard supérieur à la tolérance autorisée, Tempo doit sélectionner un autre itinéraire réalisable ou présenter les alternatives à l’utilisateur. La préférence doit néanmoins rester enregistrée pour les prochains déplacements.

Il faut donc distinguer l’enregistrement d’une préférence permanente de son application à une situation particulière. L’impossibilité de respecter une préférence à une date donnée ne doit pas empêcher son enregistrement.

Les préférences doivent être intégrées aux critères de sélection et d’optimisation des itinéraires, sans compromettre les contraintes obligatoires, les événements fixes et la stabilité du planning.

L’utilisateur doit pouvoir consulter, modifier et supprimer ses préférences en langage naturel. Elles doivent être conservées entre les conversations, les redémarrages et les recalculs du planning.

Le système doit également gérer les modifications d’horaires des opérateurs. Une préférence pour un départ habituel ne doit pas dépendre uniquement d’un identifiant de trajet susceptible de changer lors d’une mise à jour des données GTFS.

La prise en compte des préférences enregistrées doit être déterministe et ne doit pas nécessiter de nouvel appel au LLM lors de chaque calcul.

Des tests doivent vérifier la persistance des préférences, leur modification, leur suppression, leur application lors des recalculs et leur comportement lorsqu’elles entrent en conflit avec des contraintes obligatoires.

**Résultat attendu :** Tempo doit progressivement s’adapter aux habitudes et aux préférences exprimées par l’utilisateur, tout en garantissant la faisabilité du planning.

## Principes généraux de réalisation

- Analyser et simplifier l’existant avant d’ajouter quoi que ce soit.
- Privilégier la suppression, la correction, la réutilisation et la mutualisation du code existant avant toute nouvelle implémentation.
- Ne pas créer une nouvelle architecture ou de nouveaux composants lorsqu’une correction ou une consolidation de l’existant est suffisante.
- Corriger les causes profondes plutôt que d’accumuler des mécanismes compensatoires.
- Ne pas limiter les corrections aux exemples mentionnés. Les solutions doivent être générales et fonctionner pour toutes les situations similaires.
- Préserver les fonctionnalités existantes, la stabilité du planning et la cohérence globale de l’architecture.
- Centraliser les règles métier, les constantes et les paramètres configurables.
- Accompagner chaque correction de tests adaptés et d’une mise à jour de la documentation concernée.
- Documenter les éléments supprimés, simplifiés, modifiés et ajoutés, ainsi que les raisons de ces décisions.
- Signaler explicitement les éventuels problèmes non résolus, limitations et compromis techniques.
- Ne pas considérer une fonctionnalité comme terminée tant qu’elle n’est pas réellement implémentée, intégrée, fonctionnelle et testée.

L’objectif final est d’obtenir un système fiable et complet, avec le minimum de complexité nécessaire, sans dette technique évitable ni accumulation de corrections temporaires.