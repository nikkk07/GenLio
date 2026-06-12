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

# Phase 4: once a post is APPROVED, publishing may touch platforms in any
# order, fail per-platform, and resume — so the publish-era states form a
# fully connected sub-graph (each may move to any other, or to COMPLETE).
# The per-platform *_status columns prevent double-posting; the single state
# column is a coarse milestone for the dashboard.
_PUBLISH_STATES: set[PostState] = {
    PostState.APPROVED,
    PostState.POSTED_X,
    PostState.POSTED_IG,
    PostState.LINKEDIN_PENDING,
    PostState.FAILED_POST,
    PostState.FAILED_X,
    PostState.FAILED_IG,
}

# Allowed transitions. Phase 1 only exercises (none) -> DRAFTED, but the full
# graph is encoded now so downstream phases simply call transition().
_ALLOWED: dict[PostState, set[PostState]] = {
    PostState.DRAFTED: {PostState.AWAITING_APPROVAL, PostState.REJECTED, PostState.FAILED_VALIDATION},
    PostState.AWAITING_APPROVAL: {PostState.APPROVED, PostState.REJECTED},
    PostState.COMPLETE: set(),
    PostState.REJECTED: set(),
    PostState.FAILED_GENERATION: {PostState.DRAFTED},
    PostState.FAILED_VALIDATION: {PostState.DRAFTED},
}
for _state in _PUBLISH_STATES:
    _ALLOWED[_state] = (_PUBLISH_STATES - {_state}) | {PostState.COMPLETE}


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
        # Phase 4: scheduled posting time.
        if "scheduled_time" not in cols:
            logger.info("migrating posts: adding scheduled_time column")
            self._conn.execute("ALTER TABLE posts ADD COLUMN scheduled_time TEXT;")
        # Phase 4: per-platform publish tracking + the admin who acted.
        for col in ("x_status", "x_post_id", "ig_status", "ig_media_id",
                    "linkedin_status", "handled_by"):
            if col not in cols:
                logger.info("migrating posts: adding %s column", col)
                self._conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT;")

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
                               regeneration_count, parent_id, scheduled_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                record.scheduled_time,
            ),
        )
        self._conn.commit()
        logger.info(
            "recorded draft id=%s concept=%s parent=%s regen=%d scheduled=%s",
            record.id,
            record.concept,
            record.parent_id,
            record.regeneration_count,
            record.scheduled_time,
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

    def set_scheduled_time(self, post_id: str, scheduled_time: str) -> PostRecord:
        """Set the scheduled posting time for an approved post."""
        current = self.get_post(post_id)
        if current is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        ts = _now()
        self._conn.execute(
            "UPDATE posts SET scheduled_time = ?, updated_at = ? WHERE id = ?",
            (scheduled_time, ts, post_id),
        )
        self._conn.commit()
        logger.info("set scheduled_time id=%s time=%s", post_id, scheduled_time)
        return self.get_post(post_id)  # type: ignore[return-value]

    def get_posts_due_for_posting(
        self, before_time: str | None = None, *, include_unscheduled: bool = False
    ) -> list[PostRecord]:
        """APPROVED posts whose scheduled_time has passed (and, for the
        ``publish-due`` cron path, unscheduled APPROVED posts too)."""
        if before_time is None:
            before_time = _now()
        unscheduled = "OR scheduled_time IS NULL" if include_unscheduled else ""
        rows = self._conn.execute(
            f"""
            SELECT * FROM posts
            WHERE state = ?
              AND ((scheduled_time IS NOT NULL AND scheduled_time <= ?) {unscheduled})
            ORDER BY scheduled_time
            """,
            (PostState.APPROVED.value, before_time),
        ).fetchall()
        return [PostRecord(**dict(r)) for r in rows]

    # -- Phase 4: per-platform publish tracking --------------------------------
    def set_platform_result(
        self, post_id: str, platform: str, status: str, ref_id: str | None = None
    ) -> PostRecord:
        """Record a platform attempt outcome (and its platform-side id).

        ``platform`` is ``"x"``, ``"ig"``, or ``"linkedin"``; ``status`` is
        ``"posted"`` / ``"failed"`` (LinkedIn also uses ``"sent"``).
        """
        columns = {
            "x": ("x_status", "x_post_id"),
            "ig": ("ig_status", "ig_media_id"),
            "linkedin": ("linkedin_status", None),
        }
        if platform not in columns:
            raise ValueError(f"unknown platform {platform!r}")
        status_col, id_col = columns[platform]
        ts = _now()
        if id_col is not None and ref_id is not None:
            self._conn.execute(
                f"UPDATE posts SET {status_col} = ?, {id_col} = ?, updated_at = ? WHERE id = ?",
                (status, ref_id, ts, post_id),
            )
        else:
            self._conn.execute(
                f"UPDATE posts SET {status_col} = ?, updated_at = ? WHERE id = ?",
                (status, ts, post_id),
            )
        self._conn.commit()
        logger.info("platform result id=%s %s=%s ref=%s", post_id, platform, status, ref_id)
        record = self.get_post(post_id)
        if record is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        return record

    def set_handled_by(self, post_id: str, admin_chat_id: str) -> None:
        """Record which admin's tap decided this post (first tap wins)."""
        self._conn.execute(
            "UPDATE posts SET handled_by = ?, updated_at = ? WHERE id = ?",
            (str(admin_chat_id), _now(), post_id),
        )
        self._conn.commit()
