import base64
import csv
import io
import re
import sqlite3

import pytest

from app import create_app, get_db

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(app, username):
    client = app.test_client()
    response = client.post(
        "/login", data={"username": username, "password": "demo1234"}
    )
    assert response.status_code in (302, 303)
    return client


def card_id(app, card_no):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM cards WHERE card_no = ?", (card_no,)
        ).fetchone()
        return row["id"]


def get_export(app):
    reviewer = login(app, "reviewer")
    response = reviewer.get("/reviewer/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))
    return response, rows


def review_form(app, card_no):
    reviewer = login(app, "reviewer")
    cid = card_id(app, card_no)
    page = reviewer.get(f"/reviewer/cards/{cid}").data.decode("utf-8")
    fields = dict(
        re.findall(
            r'name="(entry_[ab]_(?:id|revision))" value="(\d+)"', page
        )
    )
    assert len(fields) == 4
    return reviewer, cid, fields, page


def test_demo_login_and_roles(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/clerk").status_code == 200
    assert clerk.get("/reviewer").status_code == 403

    reviewer = login(app, "reviewer")
    assert reviewer.get("/reviewer").status_code == 200
    assert reviewer.get("/clerk").status_code == 403

    anonymous = app.test_client()
    assert anonymous.get("/clerk").status_code == 302


def test_clerk_cannot_see_peer_entry(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0715")
    page = clerk.get(f"/clerk/cards/{cid}").data.decode("utf-8")
    # The clerk sees their own 31.0 reading, but not clerk2's 13.0 reading.
    assert "31" in page
    assert ">13<" not in page
    assert "13 mm" not in page
    assert "录入员乙" not in page


def test_two_agreeing_entries_are_auto_approved(app):
    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    cid = card_id(app, "YC-1986-0714")
    for client, value in ((c1, "42.5"), (c2, "42.50")):
        response = client.post(
            f"/clerk/cards/{cid}",
            data={
                "station_no": "30501200",
                "obs_date": "1986-07-14",
                "rainfall_mm": value,
            },
        )
        assert response.status_code == 302

    _, rows = get_export(app)
    matching = [r for r in rows if r["card_no"] == "YC-1986-0714"]
    assert len(matching) == 1
    assert matching[0]["station_no"] == "30501200"
    assert matching[0]["observation_date"] == "1986-07-14"
    assert matching[0]["rainfall_mm"] == "42.5"
    assert matching[0]["verification"] == "double_entry_agreement"
    # One row per approved card.
    assert len({r["card_no"] for r in rows}) == len(rows)


def test_conflict_requires_review_and_uses_selected_version(app):
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    reviewer, cid, fields, page = review_form(app, "YC-1986-0715")
    assert "31" in page and "13" in page
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 302

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "31"
    assert match[0]["verification"] == "reviewer_adjudication"

    # Any later save (even textually identical) revokes the award.
    c2 = login(app, "clerk2")
    response = c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "13.0",
        },
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ?", (cid,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_at"] is not None
    assert decisions[0]["voided_reason"] == "entry_edited"


def test_stale_adjudication_is_refused(app):
    reviewer, cid, old_fields, _ = review_form(app, "YC-1986-0715")
    clerk2 = login(app, "clerk2")
    clerk2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "14",
        },
    )

    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**old_fields, "selected_entry_id": old_fields["entry_a_id"]},
    )
    assert response.status_code == 409
    assert "旧裁定已拒绝" in response.data.decode("utf-8")

    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,)
        ).fetchone()[0]
    assert count == 0

    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}


def test_resolution_can_be_redone_after_edit(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "12.5",
        },
    )

    reviewer2, cid2, fresh, _ = review_form(app, "YC-1986-0715")
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    response = reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fresh, "selected_entry_id": fresh["entry_b_id"]},
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "12.5"


def test_agreement_after_edit_wins_and_needs_no_adjudication(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
        },
    )

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["verification"] == "double_entry_agreement"
    assert match[0]["rainfall_mm"] == "31"
    # A decision against the pre-edit versions cannot be added now either.
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 409


def test_upload_creates_card_and_rejects_non_image(app):
    reviewer = login(app, "reviewer")
    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "UPLOAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(PNG_1PX), "card.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    cid = card_id(app, "UPLOAD-1")
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200
    assert reviewer.get(f"/reviewer/cards/{cid}").status_code == 200

    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "BAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(b"<svg><script>alert(1)</script></svg>"), "bad.svg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM cards WHERE card_no = 'BAD-1'")
            .fetchone()[0]
            == 0
        )


def test_rainfall_and_date_validation(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0714")
    base = {"station_no": "30501200", "obs_date": "1986-07-14"}
    for value in ("-1", "abc", "", "NaN", "100001"):
        response = clerk.post(
            f"/clerk/cards/{cid}",
            data={**base, "rainfall_mm": value},
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data={"station_no": "30501200", "obs_date": "not-a-date", "rainfall_mm": "4"},
    )
    assert response.status_code == 200
    with app.app_context():
        entry = get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND clerk_id = 1", (cid,)
        ).fetchone()
    assert entry is None


def test_csv_is_reviewer_only(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/reviewer/export.csv").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/reviewer/export.csv").status_code == 302


def test_decision_requires_both_entries_and_current_versions(app):
    reviewer = login(app, "reviewer")
    cid = card_id(app, "YC-1986-0717")  # only clerk1 has entered
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={
            "entry_a_id": "1",
            "entry_a_revision": "1",
            "entry_b_id": "2",
            "entry_b_revision": "1",
            "selected_entry_id": "1",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,))
            .fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# Incremental handoff batches to the historical archive
# ---------------------------------------------------------------------------


def get_batches(app):
    with app.app_context():
        return (
            get_db()
            .execute("SELECT * FROM archive_batches ORDER BY id")
            .fetchall()
        )


def get_pending_batch(app):
    with app.app_context():
        return (
            get_db()
            .execute("SELECT * FROM archive_batches WHERE status = 'pending'")
            .fetchone()
        )


def get_baselines(app):
    with app.app_context():
        return {
            row["card_id"]: row
            for row in get_db().execute("SELECT * FROM archive_baselines").fetchall()
        }


def create_batch(app):
    reviewer = login(app, "reviewer")
    response = reviewer.post("/reviewer/batches")
    assert response.status_code == 302
    return reviewer


def download_batch(app, reviewer, batch_id):
    response = reviewer.get(f"/reviewer/batches/{batch_id}/csv")
    assert response.status_code == 200
    return response


def batch_csv_rows(response):
    return list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))


def agree_on_card(app, card_no, station, obs_date, rain):
    cid = card_id(app, card_no)
    for clerk in ("clerk1", "clerk2"):
        client = login(app, clerk)
        response = client.post(
            f"/clerk/cards/{cid}",
            data={"station_no": station, "obs_date": obs_date, "rainfall_mm": rain},
        )
        assert response.status_code == 302


def test_batch_collects_only_unarchived_approved_cards(app):
    reviewer = create_batch(app)
    batch = get_pending_batch(app)
    assert batch is not None
    assert batch["batch_no"] == "AH-0001"
    assert batch["card_count"] == 1

    response = download_batch(app, reviewer, batch["id"])
    assert "archive-batch-AH-0001.csv" in response.headers["Content-Disposition"]
    rows = batch_csv_rows(response)
    # Only YC-1986-0716 is approved; 0714 has no entries, 0715 conflicts,
    # 0717 is missing one entry — none of them may enter the batch.
    assert [r["card_no"] for r in rows] == ["YC-1986-0716"]
    assert rows[0]["station_no"] == "30501220"
    assert rows[0]["observation_date"] == "1986-07-16"
    assert rows[0]["rainfall_mm"] == "0"
    assert rows[0]["verification"] == "double_entry_agreement"


def test_no_changes_means_no_batch(app):
    reviewer = create_batch(app)
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    assert get_pending_batch(app) is None

    reviewer.post("/reviewer/batches")
    assert len(get_batches(app)) == 1
    assert get_pending_batch(app) is None


def test_only_one_pending_batch(app):
    create_batch(app)
    create_batch(app)  # refused: a batch is already awaiting confirmation
    batches = get_batches(app)
    assert len(batches) == 1
    assert batches[0]["status"] == "pending"


def test_batch_csv_frozen_through_edits_and_restart(app):
    reviewer = create_batch(app)
    batch = get_pending_batch(app)
    before = download_batch(app, reviewer, batch["id"]).data

    # New approvals after creation must not leak into the pending batch.
    agree_on_card(app, "YC-1986-0715", "30501210", "1986-07-15", "31")
    after = download_batch(app, reviewer, batch["id"]).data
    assert after == before
    assert b"YC-1986-0715" not in after

    # A fresh app on the same database (service restart) serves the same CSV.
    restarted = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": app.config["DATABASE"],
            "UPLOAD_DIR": app.config["UPLOAD_DIR"],
        }
    )
    reviewer2 = login(restarted, "reviewer")
    assert download_batch(restarted, reviewer2, batch["id"]).data == before


def test_confirm_applies_frozen_batch_values_as_baseline(app):
    reviewer = create_batch(app)  # ships YC-1986-0716 at 0 mm
    # The card is re-approved with a new reading before the archive confirms.
    agree_on_card(app, "YC-1986-0716", "30501220", "1986-07-16", "5")

    response = reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    assert response.status_code == 302
    assert get_pending_batch(app) is None

    baselines = get_baselines(app)
    cid = card_id(app, "YC-1986-0716")
    # The baseline takes the value sent in the batch, not the live value.
    assert baselines[cid]["rainfall_mm"] == "0"

    # The re-approved reading now differs from the baseline: next batch only.
    reviewer2 = create_batch(app)
    batch2 = get_pending_batch(app)
    assert batch2["batch_no"] == "AH-0002"
    rows = batch_csv_rows(download_batch(app, reviewer2, batch2["id"]))
    assert [r["card_no"] for r in rows] == ["YC-1986-0716"]
    assert rows[0]["rainfall_mm"] == "5"

    reviewer2.post("/reviewer/batches/confirm", data={"batch_no": "AH-0002"})
    assert get_baselines(app)[cid]["rainfall_mm"] == "5"


def test_confirm_and_abandon_accept_only_current_pending_batch_no(app):
    reviewer = create_batch(app)

    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-9999"})
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": ""})
    reviewer.post("/reviewer/batches/abandon", data={"batch_no": "nonsense"})
    assert get_pending_batch(app) is not None
    assert get_baselines(app) == {}

    reviewer.post("/reviewer/batches/abandon", data={"batch_no": "AH-0001"})
    assert get_pending_batch(app) is None
    assert get_baselines(app) == {}
    assert get_batches(app)[0]["status"] == "abandoned"

    # A closed batch number is no longer the current pending one.
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    reviewer.post("/reviewer/batches/abandon", data={"batch_no": "AH-0001"})
    assert get_baselines(app) == {}
    assert len(get_batches(app)) == 1

    # The delta survives abandonment, so a fresh batch carries the same card.
    reviewer2 = create_batch(app)
    batch2 = get_pending_batch(app)
    assert batch2["batch_no"] == "AH-0002"
    rows = batch_csv_rows(download_batch(app, reviewer2, batch2["id"]))
    assert [r["card_no"] for r in rows] == ["YC-1986-0716"]
    reviewer2.post("/reviewer/batches/confirm", data={"batch_no": "AH-0002"})
    assert get_baselines(app)[card_id(app, "YC-1986-0716")]["rainfall_mm"] == "0"


def test_confirm_and_abandon_without_pending_batch(app):
    reviewer = login(app, "reviewer")
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    reviewer.post("/reviewer/batches/abandon", data={"batch_no": "AH-0001"})
    assert get_batches(app) == []
    assert get_baselines(app) == {}


def test_card_pending_re_review_keeps_archive_record(app):
    reviewer = create_batch(app)
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    cid = card_id(app, "YC-1986-0716")
    assert cid in get_baselines(app)

    # One clerk edits: the card goes back to conflict. The archived record
    # stays; the card simply leaves future batches until re-approved.
    clerk = login(app, "clerk1")
    clerk.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501220",
            "obs_date": "1986-07-16",
            "rainfall_mm": "7",
        },
    )
    assert cid in get_baselines(app)

    reviewer2 = login(app, "reviewer")
    reviewer2.post("/reviewer/batches")
    assert get_pending_batch(app) is None  # nothing approved-but-changed
    _, rows = get_export(app)
    assert "YC-1986-0716" not in {r["card_no"] for r in rows}


def test_incremental_batches_send_only_changed_readings(app):
    reviewer = create_batch(app)
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})

    # 0715 becomes approved for the first time: first-ever handoff.
    agree_on_card(app, "YC-1986-0715", "30501210", "1986-07-15", "31")
    reviewer = create_batch(app)
    rows = batch_csv_rows(download_batch(app, reviewer, get_pending_batch(app)["id"]))
    assert [r["card_no"] for r in rows] == ["YC-1986-0715"]
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0002"})
    assert get_baselines(app)[card_id(app, "YC-1986-0715")]["rainfall_mm"] == "31"

    # 0716 re-approved with a changed rainfall: only that reading is re-sent.
    agree_on_card(app, "YC-1986-0716", "30501220", "1986-07-16", "12.5")
    reviewer = create_batch(app)
    rows = batch_csv_rows(download_batch(app, reviewer, get_pending_batch(app)["id"]))
    assert [r["card_no"] for r in rows] == ["YC-1986-0716"]
    assert rows[0]["rainfall_mm"] == "12.5"
    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0003"})

    # Everything archived and unchanged: no further batch.
    reviewer.post("/reviewer/batches")
    assert get_pending_batch(app) is None
    assert len(get_batches(app)) == 3


def test_batch_routes_are_reviewer_only(app):
    clerk = login(app, "clerk1")
    assert clerk.post("/reviewer/batches").status_code == 403
    assert clerk.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"}).status_code == 403
    assert clerk.post("/reviewer/batches/abandon", data={"batch_no": "AH-0001"}).status_code == 403

    reviewer = create_batch(app)
    batch = get_pending_batch(app)
    assert clerk.get(f"/reviewer/batches/{batch['id']}/csv").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get(f"/reviewer/batches/{batch['id']}/csv").status_code == 302
    assert anonymous.post("/reviewer/batches").status_code == 302


def test_dashboard_shows_handoff_entry(app):
    reviewer = login(app, "reviewer")
    page = reviewer.get("/reviewer").data.decode("utf-8")
    assert "增量交接" in page
    assert "创建交接批次" in page  # one approved card is waiting to ship
    assert "AH-0001" not in page

    create_batch(app)
    page = reviewer.get("/reviewer").data.decode("utf-8")
    assert "AH-0001" in page
    assert "下载批次 CSV" in page

    reviewer.post("/reviewer/batches/confirm", data={"batch_no": "AH-0001"})
    page = reviewer.get("/reviewer").data.decode("utf-8")
    assert "没有变化，不建批" in page
