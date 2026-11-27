---
name: debug
description: Reproduce the failure, isolate root cause, create regression test, apply minimal fix, run relevant tests/lint and report evidence.
---

# debug

Applique les [règles de Tempo](../../../AGENTS.md) et limite le changement au défaut établi.

1. Reproduis le symptôme avec le cas le plus ciblé qui le montre. Compare l'attendu et
   l'observé puis isole la cause racine. Vérifie les mécanismes analogues dans les modules
   concernés pour savoir s'ils partagent la même cause.
2. Réutilise un test existant s'il détecte la régression. Sinon, ajoute un cas qui échoue
   pour la cause identifiée, lorsque la régression est reproductible en test.
3. Corrige le mécanisme en place et retire les anciens contournements devenus inutiles.
   Évite d'ajouter un contournement ou d'élargir la correction sans preuve du même défaut.
4. Rejoue le cas, les contrôles des chemins voisins et les vérifications pertinentes.
   Relie les résultats à la cause et indique les cas qui restent à vérifier.
