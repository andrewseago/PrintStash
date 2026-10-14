"""The browse revision advances when a listing can change, and only then.

An open library view shows "the library has changed" when this revision moves.
Starring a Model, or a cover regeneration pointing Models at new thumbnails every
second, changed no listing yet moved it constantly.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.db import library_browse_v2
from app.db.models import ModelStar
from app.modules.library.model_views.browse import revision
from tests.factories import identity, library


def _browse(session: Session) -> str:
    session.commit()
    return revision(session).browse_revision


class TestStars:
    def test_starring_a_model_keeps_the_revision(self, db_session: Session) -> None:
        user = identity.build_user(db_session)
        model = library.build_model(db_session)
        before = _browse(db_session)

        star = ModelStar(user_id=int(user.id), model_id=int(model.id))
        db_session.add(star)
        assert _browse(db_session) == before
        db_session.delete(star)
        assert _browse(db_session) == before

    def test_starring_a_multipart_set_keeps_the_revision(
        self, db_session: Session
    ) -> None:
        user = identity.build_user(db_session)
        parts = library.build_multipart_model(db_session)
        before = _browse(db_session)

        library.build_multipart_model_star(db_session, user, parts)

        assert _browse(db_session) == before


class TestThumbnailPointers:
    def test_publishing_a_thumbnail_keeps_the_revision(
        self, db_session: Session
    ) -> None:
        model = library.build_model(db_session)
        file = library.build_file(db_session, model)
        before = _browse(db_session)

        db_session.execute(
            text("UPDATE files SET thumbnail_path='thumbs/a.png' WHERE id=:id"),
            {"id": file.id},
        )
        db_session.execute(
            text(
                "UPDATE models SET thumbnail_path='thumbs/a.png', thumbnail_file_id=:file WHERE id=:id"
            ),
            {"id": model.id, "file": file.id},
        )

        assert _browse(db_session) == before

    def test_background_bookkeeping_on_a_file_keeps_the_revision(
        self, db_session: Session
    ) -> None:
        file = library.build_file(db_session, library.build_model(db_session))
        before = _browse(db_session)

        db_session.execute(
            text(
                "UPDATE files SET viewer_requested_at=:now, source_verified_at=:now WHERE id=:id"
            ),
            {"id": file.id, "now": datetime.now(timezone.utc)},
        )

        assert _browse(db_session) == before


class TestListingChanges:
    def test_a_new_model_advances_the_revision(self, db_session: Session) -> None:
        before = _browse(db_session)

        library.build_model(db_session, "Second")

        assert _browse(db_session) != before

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE models SET name='Renamed' WHERE id=:id",
            "UPDATE models SET description='notes' WHERE id=:id",
            "UPDATE models SET deleted_at=:now WHERE id=:id",
            "UPDATE models SET collection_id=NULL, updated_at=:now WHERE id=:id",
            "DELETE FROM models WHERE id=:id",
        ],
        ids=["renamed", "described", "trashed", "moved", "deleted"],
    )
    def test_an_edit_to_a_model_advances_the_revision(
        self, db_session: Session, statement: str
    ) -> None:
        model = library.build_model(db_session)
        before = _browse(db_session)

        db_session.execute(
            text(statement), {"id": model.id, "now": datetime.now(timezone.utc)}
        )

        assert _browse(db_session) != before

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE files SET original_filename='b.stl' WHERE id=:id",
            "UPDATE files SET deleted_at=:now WHERE id=:id",
            "UPDATE files SET is_recommended=1 WHERE id=:id",
            "DELETE FROM files WHERE id=:id",
        ],
        ids=["renamed", "trashed", "recommended", "deleted"],
    )
    def test_an_edit_to_a_file_advances_the_revision(
        self, db_session: Session, statement: str
    ) -> None:
        file = library.build_file(db_session, library.build_model(db_session))
        before = _browse(db_session)

        db_session.execute(
            text(statement), {"id": file.id, "now": datetime.now(timezone.utc)}
        )

        assert _browse(db_session) != before

    def test_an_edit_next_to_a_thumbnail_pointer_still_advances_the_revision(
        self, db_session: Session
    ) -> None:
        model = library.build_model(db_session)
        before = _browse(db_session)

        db_session.execute(
            text("UPDATE models SET thumbnail_path='t.png', name='Both' WHERE id=:id"),
            {"id": model.id},
        )

        assert _browse(db_session) != before


class TestDowngrade:
    def test_uninstalling_restores_v1_then_reinstalling_replaces_it(
        self, db_session: Session
    ) -> None:
        user = identity.build_user(db_session)
        model = library.build_model(db_session)
        db_session.commit()

        library_browse_v2.uninstall(db_session.connection())
        before = _browse(db_session)
        db_session.add(ModelStar(user_id=int(user.id), model_id=int(model.id)))
        assert _browse(db_session) != before

        library_browse_v2.install(db_session.connection())
        assert library_browse_v2.installed(db_session.connection())
        before = _browse(db_session)
        db_session.execute(
            text("UPDATE models SET thumbnail_path='t.png' WHERE id=:id"),
            {"id": model.id},
        )
        assert _browse(db_session) == before
