---
name: tempo-schedule
description: "Model and test planning constraints: fixed iCal, flexible study with soft targets, 90-minute preference, alternating subjects, training flexible except races, mandatory travel, protected approvals."
---

# tempo-schedule

Applique les [règles de Tempo](../../../AGENTS.md) et consulte les contraintes
de planification d'ARCHITECTURE.md avant de modifier le modèle.

Exploite les primitives natives d'OR-Tools CP-SAT et réutilise les constructions de
contraintes en place. Évalue les bibliothèques spécialisées quand elles couvrent un
besoin précis. Simplifie la formulation et retire une contrainte redondante après
avoir vérifié son équivalence, y compris sur les cas limites. Garde un seul modèle et
le validateur indépendant du plan final.

Vérifie les événements iCal et compétitions fixes, l'étude et les entraînements
flexibles, les cibles souples sauf demande stricte, la préférence pour des blocs
d'étude de 90 minutes et l'alternance des matières. Préserve l'absence de
chevauchements, les trajets obligatoires, les accords protégés et la stabilité du
planning. Compare la faisabilité, la qualité du plan et les temps de résolution
avant et après sur des scénarios représentatifs pour un changement significatif du modèle.
