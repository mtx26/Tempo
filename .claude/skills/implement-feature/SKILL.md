---
name: implement-feature
description: Implement one approved Tempo feature end-to-end. Inspect context, define acceptance criteria, write tests first when practical, implement, run checks, update ARCHITECTURE.md / README.md.
---

# implement-feature

Applique les [règles de Tempo](../../../AGENTS.md) pour livrer la fonctionnalité approuvée.

1. Établis les critères d'acceptation à partir du comportement observable et des contrats.
   Repère les points d'entrée existants et la couverture qui protège leurs appelants.
2. Avant d'écrire du code, inventorie les mécanismes réutilisables et les dépendances
   installées. Vérifie leurs capacités natives et cherche une bibliothèque spécialisée
   adaptée. Compare la réutilisation, l'extension et le remplacement. Estime le bilan
   de code et de complexité, migration et exploitation comprises.
3. Utilise la bibliothèque adaptée pour tout comportement qu'elle couvre. Implémente le
   strict nécessaire pour le besoin restant. Lorsque le remplacement est vérifié, retire
   l'ancien chemin devenu inutile et ses appels. Maintiens les contrats et la compatibilité
   avec les données existantes.
4. Vérifie les critères d'acceptation via les points d'entrée habituels et applique les
   contrôles et mises à jour documentaires pertinents prévus dans les règles de Tempo.
