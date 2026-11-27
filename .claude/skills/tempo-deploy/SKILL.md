---
name: tempo-deploy
description: Prepare reversible Swarm deployment, health checks, backups, least privilege, secret management and rollback. Do not deploy without explicit approval.
---

# tempo-deploy

Applique les [règles de Tempo](../../../AGENTS.md). Consulte les sections utiles du
README et d'ARCHITECTURE.md pour préparer un déploiement Swarm réversible.

Vérifie les contrôles de santé, la sauvegarde et la restauration, le moindre privilège,
la gestion des secrets et la procédure de retour arrière. Valide localement ce que
le changement touche et rapporte les contrôles qui requièrent l'environnement cible.
Attends l'accord explicite de l'utilisateur avant tout déploiement ou accès à la production.
