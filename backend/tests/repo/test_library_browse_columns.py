"""Every column of the tables with column-aware browse triggers is classified.

Browse contract v2 advances the revision when a watched column changes and
ignores the ignored ones. A column added later that is in neither list would
silently count as "changes no listing"; this fails until someone decides.
"""

import pytest
from sqlmodel import SQLModel

import app.db.models  # noqa: F401  (registers every table)
from app.db import library_browse_v2


class TestBrowseColumns:
    @pytest.mark.parametrize("table", sorted(library_browse_v2.WATCHED_FIELDS))
    def test_every_column_is_watched_or_deliberately_ignored(self, table: str) -> None:
        columns = {column.name for column in SQLModel.metadata.tables[table].columns}
        watched = set(library_browse_v2.WATCHED_FIELDS[table])
        ignored = set(library_browse_v2.IGNORED_FIELDS[table])

        assert not watched & ignored
        assert columns == watched | ignored, (
            f"{table}: classify new columns in library_browse_v2 (a new contract version "
            "if v2 is released) — unclassified "
            f"{sorted(columns - watched - ignored)}, gone {sorted((watched | ignored) - columns)}"
        )
