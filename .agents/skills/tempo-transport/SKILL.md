---
name: tempo-transport
description: "Verify MOTIS and Belgian feed integration, real-time caveats (STIB: static GTFS only), arrival-by, phone GPS, future event-chain origin, walking, transfers, safety buffers, alerts."
---

# tempo-transport

Applique les [règles de Tempo](../../../AGENTS.md) au routage et aux flux belges.
Vérifie les possibilités de la version MOTIS utilisée pour les requêtes d'arrivée à
l'heure, la marche, les correspondances, les marges et les alertes. Réutilise les
capacités natives qui satisfont le contrat Tempo.

Avant d'écrire un traitement de flux, cherche les bibliothèques spécialisées GTFS et
GTFS-RT, les validateurs réutilisables et les outils de nettoyage. Privilégie les
traitements locaux et retire les transformations redondantes après avoir comparé les
résultats sur les flux concernés.

Vérifie la couverture réelle du temps réel par opérateur. Pour la STIB, retiens le
GTFS statique seul dans l'intégration actuelle. Contrôle la fraîcheur et la précision
du GPS du téléphone pour un trajet immédiat et la chaîne d'événements ou de lieux pour
l'origine future. Vérifie les cas de données manquantes sans inventer de trajet.
