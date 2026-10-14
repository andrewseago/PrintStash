"""Browse revision triggers that ignore writes which cannot change a listing, contract v2.

Library contract v1 advances ``revision`` on every write to every browse table.
An open library view treats a moved revision as "the listing may have changed":
it shows a banner and refuses to page further. Two writers moved it constantly
without changing any listing:

* starring a Model (``model_stars``, ``multipart_model_stars``) is a per-user
  flag, and the writer's own view already shows it;
* publishing a thumbnail points a Model and its File at a new image key, which
  a card shows through its own thumbnail arrival read. A cover regeneration
  wrote one such row per second.

v2 replaces only those tables' v1 browse triggers. The two star tables stop
advancing the revision. ``models`` and ``files`` still advance it on insert and
delete, and on an update that changes any column except the image pointers and
the background bookkeeping below. Every other v1 trigger is untouched. Like v1,
this module is a historical contract: add a new version instead of changing
installed DDL after release.

The column lists are frozen. ``tests/repo`` fails when a column is added to
either table without being placed in one of them, so a new column has to be
classified instead of silently counting as "changes nothing".
"""

from __future__ import annotations

from sqlalchemy import DDL, Connection, event, inspect, text
from sqlalchemy.engine.mock import MockConnection
from sqlmodel import SQLModel

# Imported for its after_create listener, which must run before this one's.
from app.db import library_contracts_v1  # noqa: F401

# Per-user flags: no write advances the revision.
SILENT_TABLES = ("model_stars", "multipart_model_stars")

# Written by derivation or scans, never by a user, and never shown in a listing
# except through the thumbnail arrival read.
IGNORED_FIELDS = {
    "models": ("thumbnail_path", "thumbnail_file_id"),
    "files": ("thumbnail_path", "viewer_requested_at", "source_verified_at"),
}
# Every other column of those tables, frozen at v2.
WATCHED_FIELDS = {
    "models": (
        "id",
        "edit_version",
        "name",
        "slug",
        "hash",
        "next_file_version",
        "collection_id",
        "description",
        "source_url",
        "deleted_at",
        "deleted_by",
        "purge_token",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    ),
    "files": (
        "id",
        "model_id",
        "path",
        "original_filename",
        "file_type",
        "version",
        "size_bytes",
        "sha256",
        "revision_label",
        "revision_status",
        "revision_notes",
        "is_recommended",
        "is_external",
        "external_library_id",
        "source_key",
        "source_mtime",
        "source_etag",
        "source_version_id",
        "ingestion_key",
        "uploaded_at",
        "deleted_at",
        "deleted_by",
        "purge_token",
    ),
}

# One trigger (SQLite) and one function (PostgreSQL) only v2 creates.
MARKER = "ps_browse_models_update_v2"
PG_MARKER = "ps_browse_models_v2"

_SQLITE_BUMP = (
    "INSERT INTO library_revision (id,epoch,revision) VALUES (1,lower(hex(randomblob(16))),1) "
    "ON CONFLICT (id) DO UPDATE SET revision = revision + 1;"
)
_PG_BUMP = (
    "INSERT INTO library_revision (id,epoch,revision) VALUES (1, md5(random()::text || clock_timestamp()::text), 1) "
    "ON CONFLICT (id) DO UPDATE SET revision = library_revision.revision + 1;"
)


def _execute(connection: Connection | MockConnection, statement: str) -> None:
    connection.execute(DDL(statement))


def _dialect(connection: Connection | MockConnection) -> str:
    dialect = connection.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("library_contract_dialect_unsupported")
    return dialect


def _drop_v1_trigger(
    connection: Connection | MockConnection, table: str, actions: tuple[str, ...]
) -> None:
    if _dialect(connection) == "postgresql":
        _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_v1 ON {table}")
    else:
        for action in actions:
            _execute(
                connection, f"DROP TRIGGER IF EXISTS ps_browse_{table}_{action}_v1"
            )


def _restore_v1(
    connection: Connection | MockConnection, table: str, actions: tuple[str, ...]
) -> None:
    """The exact v1 DDL for one table, for a downgrade."""
    if _dialect(connection) == "postgresql":
        _execute(
            connection,
            f"CREATE TRIGGER ps_browse_v1 AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ps_browse_{table}_v1()",
        )
    else:
        for action in actions:
            _execute(
                connection,
                f"""CREATE TRIGGER IF NOT EXISTS ps_browse_{table}_{action}_v1 AFTER {action.upper()} ON {table}
                BEGIN
                    {_SQLITE_BUMP}
                END""",
            )


def install(connection: Connection | MockConnection) -> None:
    """Replace v1's star and thumbnail-pointer browse triggers."""
    dialect = _dialect(connection)
    for table in SILENT_TABLES:
        _drop_v1_trigger(connection, table, ("insert", "update", "delete"))
    for table, watched in WATCHED_FIELDS.items():
        if dialect == "postgresql":
            changed = " OR ".join(
                f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in watched
            )
            _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_v1 ON {table}")
            _execute(
                connection,
                f"CREATE TRIGGER ps_browse_v1 AFTER INSERT OR DELETE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ps_browse_{table}_v1()",
            )
            _execute(
                connection,
                f"""CREATE OR REPLACE FUNCTION ps_browse_{table}_v2() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                {_PG_BUMP}
                RETURN NULL;
            END $$""",
            )
            _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_v2 ON {table}")
            _execute(
                connection,
                f"CREATE TRIGGER ps_browse_v2 AFTER UPDATE ON {table} FOR EACH ROW WHEN ({changed}) EXECUTE FUNCTION ps_browse_{table}_v2()",
            )
        else:
            changed = " OR ".join(
                f"NEW.{field} IS NOT OLD.{field}" for field in watched
            )
            _drop_v1_trigger(connection, table, ("update",))
            _execute(
                connection,
                f"""CREATE TRIGGER IF NOT EXISTS ps_browse_{table}_update_v2 AFTER UPDATE ON {table}
                WHEN {changed}
                BEGIN
                    {_SQLITE_BUMP}
                END""",
            )


def uninstall(connection: Connection | MockConnection) -> None:
    """Put v1's browse triggers back on the star, models and files tables."""
    dialect = _dialect(connection)
    for table in WATCHED_FIELDS:
        if dialect == "postgresql":
            _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_v2 ON {table}")
            _execute(connection, f"DROP FUNCTION IF EXISTS ps_browse_{table}_v2()")
            _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_v1 ON {table}")
        else:
            _execute(connection, f"DROP TRIGGER IF EXISTS ps_browse_{table}_update_v2")
        _restore_v1(connection, table, ("update",) if dialect == "sqlite" else ())
    for table in SILENT_TABLES:
        _restore_v1(connection, table, ("insert", "update", "delete"))


def installed(connection: Connection) -> bool:
    if _dialect(connection) == "postgresql":
        return (
            connection.execute(
                text("SELECT 1 FROM pg_proc WHERE proname = :name"), {"name": PG_MARKER}
            ).first()
            is not None
        )
    return (
        connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": MARKER},
        ).first()
        is not None
    )


def _fresh_contract(metadata, connection: Connection, **_kwargs) -> None:
    # Registered after v1's listener (this module imports v1), so v1's
    # triggers exist to be replaced.
    if (
        isinstance(connection, Connection)
        and "library_revision" in inspect(connection).get_table_names()
    ):
        install(connection)


event.listen(SQLModel.metadata, "after_create", _fresh_contract)
