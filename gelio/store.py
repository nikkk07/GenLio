"""SQLite persistence: topic dedup, run history, and the post state machine.

The store is deliberately thin and synchronous. It owns one table, ``posts``,
which doubles as the source of truth for "which concepts have been used" (a
concept is used once it has a row). Allowed state transitions are enforced in
code so later phases get a real state machine, not a free-for-all status column.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gelio.schemas import PostRecord, PostState

logger = logging.getLogger("gelio.store")

# Allowed transitions. Phase 1 only exercises (none) -> DRAFTED, but the full
# graph is encoded now so downstream phases simply call transition().
_ALLOWED: dict[PostState, set[PostState]] = {
    PostState.DRAFTED: {PostState.AWAITING_APPROVAL, PostState.REJECTED, PostState.FAILED_VALIDATION},
    PostState.AWAITING_APPROVAL: {PostState.APPROVED, PostState.REJECTED},
    PostState.APPROVED: {PostState.POSTED_X, PostState.POSTED_IG, PostState.LINKEDIN_PENDING, PostState.FAILED_POST},
    PostState.POSTED_X: {PostState.POSTED_IG, PostState.LINKEDIN_PENDING, PostState.COMPLETE, PostState.FAILED_POST},
    PostState.POSTED_IG: {PostState.POSTED_X, PostState.LINKEDIN_PENDING, PostState.COMPLETE, PostState.FAILED_POST},
    PostState.LINKEDIN_PENDING: {PostState.COMPLETE, PostState.FAILED_POST},
    PostState.COMPLETE: set(),
    PostState.REJECTED: set(),
    PostState.FAILED_GENERATION: {PostState.DRAFTED},
    PostState.FAILED_VALIDATION: {PostState.DRAFTED},
    PostState.FAILED_POST: {PostState.APPROVED},
}


class StateTransitionError(RuntimeError):
    """Raised when an illegal post state transition is attempted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """A connection-owning wrapper around the gelio SQLite database."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self.init_db()

    # -- lifecycle -----------------------------------------------------------
    def init_db(self) -> None:
        """Create tables if they do not exist."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id           TEXT PRIMARY KEY,
                concept      TEXT NOT NULL,
                date         TEXT NOT NULL,
                state        TEXT NOT NULL,
                brief_path   TEXT,
                content_path TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_concept ON posts(concept);"
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Apply additive, idempotent column migrations.

        Phase 2 adds ``rendered_at``. Existing Phase 1 databases are upgraded in
        place — never dropped — by checking the live schema first.
        """
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "rendered_at" not in cols:
            logger.info("migrating posts: adding rendered_at column")
            self._conn.execute("ALTER TABLE posts ADD COLUMN rendered_at TEXT;")
        # Phase 3: regeneration lineage.
        if "regeneration_count" not in cols:
            logger.info("migrating posts: adding regeneration_count column")
            self._conn.execute(
                "ALTER TABLE posts ADD COLUMN regeneration_count INTEGER NOT NULL DEFAULT 0;"
            )
        if "parent_id" not in cols:
            logger.info("migrating posts: adding parent_id column")
            self._conn.execute("ALTER TABLE posts ADD COLUMN parent_id TEXT;")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- topic dedup ---------------------------------------------------------
    def used_concepts(self) -> set[str]:
        """Return the set of concepts that already have a post row."""
        rows = self._conn.execute("SELECT DISTINCT concept FROM posts").fetchall()
        return {r["concept"] for r in rows}

    def count_regenerations(self, date: str) -> int:
        """How many regenerations (posts with a parent) were made on ``date``.

        Drives the Phase 3 daily regenerate cap.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE date = ? AND parent_id IS NOT NULL",
            (date,),
        ).fetchone()
        return int(row["n"])

    # -- post records --------------------------------------------------------
    def get_post(self, post_id: str) -> PostRecord | None:
        row = self._conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None:
            return None
        return PostRecord(**dict(row))

    def record_draft(
        self,
        record: PostRecord,
    ) -> PostRecord:
        """Insert a new DRAFTED post row.

        Raises ``sqlite3.IntegrityError`` if the id already exists; callers
        should check :meth:`get_post` first for idempotency.
        """
        self._conn.execute(
            """
            INSERT INTO posts (id, concept, date, state, brief_path,
                               content_path, created_at, updated_at,
                               regeneration_count, parent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.concept,
                record.date,
                record.state.value,
                record.brief_path,
                record.content_path,
                record.created_at,
                record.updated_at,
                record.regeneration_count,
                record.parent_id,
            ),
        )
        self._conn.commit()
        logger.info(
            "recorded draft id=%s concept=%s parent=%s regen=%d",
            record.id,
            record.concept,
            record.parent_id,
            record.regeneration_count,
        )
        return record

    def mark_rendered(self, post_id: str) -> PostRecord:
        """Stamp ``rendered_at`` after Phase 2 finishes building visuals.

        State stays ``DRAFTED`` (approval happens in Phase 3); this only records
        that the slides + PDF exist.
        """
        current = self.get_post(post_id)
        if current is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        ts = _now()
        self._conn.execute(
            "UPDATE posts SET rendered_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, post_id),
        )
        self._conn.commit()
        logger.info("marked rendered id=%s at=%s", post_id, ts)
        return self.get_post(post_id)  # type: ignore[return-value]

    def transition(self, post_id: str, new_state: PostState) -> PostRecord:
        """Move a post to ``new_state`` if the transition is allowed."""
        current = self.get_post(post_id)
        if current is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        if new_state not in _ALLOWED.get(current.state, set()):
            raise StateTransitionError(
                f"illegal transition {current.state.value} -> {new_state.value} "
                f"for {post_id!r}"
            )
        ts = _now()
        self._conn.execute(
            "UPDATE posts SET state = ?, updated_at = ? WHERE id = ?",
            (new_state.value, ts, post_id),
        )
        self._conn.commit()
        logger.info(
            "transition id=%s %s -> %s", post_id, current.state.value, new_state.value
        )
        return self.get_post(post_id)  # type: ignore[return-value]

    def all_posts(self) -> list[PostRecord]:
        rows = self._conn.execute(
            "SELECT * FROM posts ORDER BY created_at"
        ).fetchall()
        return [PostRecord(**dict(r)) for r in rows]
