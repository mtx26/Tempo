---
name: review
description: Review changes for correctness, security, test coverage, HA-only UI, fixed/flexible scheduling and routing assumptions; report prioritized actionable findings.
---

# review

Applique les [règles de Tempo](../../../AGENTS.md). Examine le diff et les chemins touchés
pour repérer les erreurs, risques de sécurité et régressions insuffisamment couvertes.
Vérifie selon la portée l'interface métier via Assist, les règles fixe/flexible, les
confirmations protégées et les hypothèses de routage. Classe les constats par gravité
avec leur emplacement, leur impact et une correction exploitable.

## OPPORTUNITÉS DE SIMPLIFICATION

Inclus cette rubrique dans chaque revue. Compare la complexité avant et après pour
repérer ce que les changements récents ont ajouté sans bénéfice suffisant. Cherche les
duplications, les fonctions inutilement complexes, les capacités de bibliothèques
sous-exploitées, les implémentations remplaçables, les dépendances inutiles, les
abstractions ou couches superflues et le code obsolète. Décris les gains et les garanties
à préserver. Indique si aucune opportunité pertinente ressort du diff.
