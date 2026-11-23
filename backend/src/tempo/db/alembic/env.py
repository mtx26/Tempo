from alembic import context
from sqlalchemy import create_engine, text

from tempo.db.models import Base

MIGRATION_LOCK_KEY = 7_311_2026  # arbitrary constant shared by all Tempo processes

config = context.config
url = config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("sqlalchemy.url is not configured")

connectable = create_engine(url)
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        # Serialise concurrent upgrades (several Swarm replicas starting at once).
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        context.run_migrations()
connectable.dispose()
