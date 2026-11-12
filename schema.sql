PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('clerk', 'reviewer')),
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no       TEXT    NOT NULL UNIQUE,
    image_filename TEXT   NOT NULL,
    clerk_a_id    INTEGER NOT NULL REFERENCES users(id),
    clerk_b_id    INTEGER NOT NULL REFERENCES users(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    CHECK (clerk_a_id <> clerk_b_id)
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    clerk_id      INTEGER NOT NULL REFERENCES users(id),
    station_no    TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    rainfall_mm   TEXT    NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (card_id, clerk_id)
);

-- A reviewer's adjudication is tied to the exact versions (entry IDs + revision
-- numbers) that were on screen. Any later entry change voids it permanently.
CREATE TABLE IF NOT EXISTS decisions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewer_id              INTEGER NOT NULL REFERENCES users(id),
    entry_a_id               INTEGER NOT NULL REFERENCES entries(id),
    entry_a_revision         INTEGER NOT NULL,
    entry_b_id               INTEGER NOT NULL REFERENCES entries(id),
    entry_b_revision         INTEGER NOT NULL,
    selected_entry_id        INTEGER NOT NULL REFERENCES entries(id),
    selected_entry_revision  INTEGER NOT NULL,
    verified_station_no      TEXT    NOT NULL,
    verified_obs_date        TEXT    NOT NULL,
    verified_rainfall_mm     TEXT    NOT NULL,
    created_at               TEXT    NOT NULL,
    voided_at                TEXT,
    voided_reason            TEXT,
    voided_by_entry_id       INTEGER REFERENCES entries(id)
);

-- One handoff batch = a frozen CSV snapshot of the approved cards that were
-- missing from the historical archive or differed from its last confirmed
-- baseline. The CSV text is stored so re-downloads survive edits and restarts.
CREATE TABLE IF NOT EXISTS archive_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT    NOT NULL UNIQUE,
    status        TEXT    NOT NULL CHECK (status IN ('pending', 'confirmed', 'abandoned')),
    csv_content   TEXT    NOT NULL,
    card_count    INTEGER NOT NULL,
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    closed_by     INTEGER REFERENCES users(id),
    closed_at     TEXT
);

-- The exact per-card values shipped in a batch. Confirmation applies these
-- frozen values to the archive baseline, never the live entry values.
CREATE TABLE IF NOT EXISTS archive_batch_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES archive_batches(id) ON DELETE CASCADE,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    card_no       TEXT    NOT NULL,
    station_no    TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    rainfall_mm   TEXT    NOT NULL,
    verification  TEXT    NOT NULL,
    verified_at   TEXT    NOT NULL,
    UNIQUE (batch_id, card_id)
);

-- Per-card baseline: the values the historical archive confirmed, and the
-- batch that confirmed them. A card pending re-review keeps its baseline.
CREATE TABLE IF NOT EXISTS archive_baselines (
    card_id       INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    station_no    TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    rainfall_mm   TEXT    NOT NULL,
    batch_id      INTEGER NOT NULL REFERENCES archive_batches(id),
    confirmed_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_card ON entries(card_id);
CREATE INDEX IF NOT EXISTS idx_entries_clerk ON entries(clerk_id);
CREATE INDEX IF NOT EXISTS idx_decisions_card ON decisions(card_id, id);

-- At most one live adjudication per card. Historical, voided decisions remain
-- available for the audit trail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_decision
    ON decisions(card_id)
    WHERE voided_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_active ON decisions(card_id, voided_at);

-- At most one batch may await archive confirmation at any time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_batch
    ON archive_batches(status)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON archive_batch_items(batch_id);
