import os
import threading
import uuid

import pytest

from app.db import get_db
from app.services import (
    correct_header,
    list_headers,
    list_replay_logs,
    open_snapshot,
    replay_record,
)
from app.services.errors import ServiceError


def _fresh_db() -> str:
    if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        path = f"/dev/shm/wm-svc-{uuid.uuid4().hex}.db"
    else:
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "svc.db")
    from app.db import init_db

    init_db(path)
    return path


def _drifted(headers):
    return [h for h in headers if h["drifted"]]


def _raw_header(db, header_id):
    with get_db(db) as conn:
        return conn.execute(
            "SELECT * FROM batch_headers WHERE id=?", (header_id,)
        ).fetchone()


@pytest.mark.asyncio
async def test_seed_has_drifted_headers_and_gap_cards_blocked(authed):
    headers = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    drifted = _drifted(headers)
    assert len(drifted) == 2
    for h in drifted:
        assert h["delta"] != 0
        assert h["last_mode"] is None

    cards = (await (await authed.get("/api/reconcile/gap-cards")).json())["items"]
    blocked = [c for c in cards if c["blocked"]]
    assert len(blocked) == 2
    for c in blocked:
        assert "阻断" in c["summary"]
        assert c["declared_stamped"] != c["actual_stamped"]


@pytest.mark.asyncio
async def test_batch_detail_carries_header_reconcile_info(authed):
    batches = (await (await authed.get("/api/batches")).json())["items"]
    target = next(b for b in batches if b["run_code"] == "gmv-20261114-01")
    detail = await (await authed.get(f"/api/batches/{target['id']}")).json()
    h = detail["header"]
    assert h is not None
    assert h["drifted"] is True
    assert h["declared_stamped"] == 1
    assert h["actual_stamped"] == 0
    assert h["delta"] == 1


@pytest.mark.asyncio
async def test_freeze_correction_writes_db_field_and_unblocks(authed):
    headers = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    h = _drifted(headers)[0]
    hid = h["header_id"]

    snap = await (
        await authed.post(f"/api/reconcile/headers/{hid}/snapshot", json={})
    ).json()
    assert snap["snapshot"]["frozen_declared"] == h["declared_stamped"]
    assert snap["snapshot"]["frozen_actual"] == h["actual_stamped"]

    r = await authed.post(
        f"/api/reconcile/headers/{hid}/correct",
        json={"mode": "冻结", "snapshot_id": snap["snapshot"]["snapshot_id"]},
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["mode"] == "冻结"
    assert result["before_declared"] == 1
    assert result["after_declared"] == 0
    assert result["delta"] == -1
    assert result["frozen_basis"] is True

    # 必须改库字段，只改头栏展示不算
    import os

    row = _raw_header(os.environ["DATABASE_PATH"], hid)
    assert row["declared_stamped"] == 0
    assert row["version_no"] == 2
    assert row["aligned_at"] is not None

    headers2 = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    again = next(x for x in headers2 if x["header_id"] == hid)
    assert again["drifted"] is False
    assert again["delta"] == 0
    assert again["last_mode"] == "冻结"

    cards = (await (await authed.get("/api/reconcile/gap-cards")).json())["items"]
    card = next(c for c in cards if c["header_id"] == hid)
    assert card["blocked"] is False

    records = (await (await authed.get("/api/reconcile/records")).json())["items"]
    assert len(records) == 1
    assert records[0]["operator"] == "admin"


@pytest.mark.asyncio
async def test_recount_correction_uses_live_rows(authed):
    headers = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    h = _drifted(headers)[0]
    r = await authed.post(
        f"/api/reconcile/headers/{h['header_id']}/correct",
        json={"mode": "重数"},
    )
    assert r.status == 200
    result = (await r.json())["result"]
    assert result["mode"] == "重数"
    assert result["after_declared"] == result["live_actual"] == 0
    assert result["frozen_basis"] is False


@pytest.mark.asyncio
async def test_freeze_without_snapshot_rejected_in_chinese(authed):
    headers = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    hid = _drifted(headers)[0]["header_id"]
    r = await authed.post(
        f"/api/reconcile/headers/{hid}/correct", json={"mode": "冻结"}
    )
    assert r.status == 400
    body = await r.json()
    assert body["ok"] is False
    assert "快照" in body["error"]


@pytest.mark.asyncio
async def test_stale_snapshot_rejected(authed):
    import os

    db = os.environ["DATABASE_PATH"]
    headers = list_headers(db)
    hid = _drifted(headers)[0]["header_id"]
    snap = open_snapshot(hid, db)
    # 快照之后头栏被别的动作改动，版本号变化
    with get_db(db) as conn:
        conn.execute(
            "UPDATE batch_headers SET declared_stamped=declared_stamped+1, "
            "version_no=version_no+1 WHERE id=?",
            (hid,),
        )
    r = await authed.post(
        f"/api/reconcile/headers/{hid}/correct",
        json={"mode": "冻结", "snapshot_id": snap["snapshot_id"]},
    )
    assert r.status == 409
    assert "失效" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_snapshot_consumed_once(authed):
    import os

    db = os.environ["DATABASE_PATH"]
    headers = list_headers(db)
    hid = _drifted(headers)[0]["header_id"]
    snap = open_snapshot(hid, db)
    correct_header(hid, "冻结", "admin", snap["snapshot_id"], db)
    with pytest.raises(ServiceError) as exc:
        correct_header(hid, "冻结", "admin", snap["snapshot_id"], db)
    assert exc.value.status_code == 409
    assert "已被使用" in exc.value.message


@pytest.mark.asyncio
async def test_bad_mode_and_aligned_header_chinese_errors(authed):
    import os

    db = os.environ["DATABASE_PATH"]
    headers = list_headers(db)
    aligned = next(h for h in headers if not h["drifted"])
    with pytest.raises(ServiceError) as exc:
        correct_header(aligned["header_id"], "瞎改", "admin", None, db)
    assert exc.value.status_code == 400
    with pytest.raises(ServiceError) as exc2:
        correct_header(aligned["header_id"], "重数", "admin", None, db)
    assert exc2.value.status_code == 409
    assert "对齐" in exc2.value.message


def test_concurrent_correction_mutex_single_write():
    # 直接打服务层：左右仓管同进程抢同一头栏，排他锁保证只落一单
    db = _fresh_db()
    hid = _drifted(list_headers(db))[0]["header_id"]

    outcomes = []
    barrier = threading.Barrier(2)

    def worker(tag):
        try:
            barrier.wait()
            correct_header(hid, "重数", f"仓管{tag}", None, db)
            outcomes.append(("ok", tag))
        except ServiceError as exc:
            outcomes.append(("err", tag, exc.message, exc.status_code))

    t1 = threading.Thread(target=worker, args=("左",))
    t2 = threading.Thread(target=worker, args=("右",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len([o for o in outcomes if o[0] == "ok"]) == 1
    failed = next(o for o in outcomes if o[0] == "err")
    assert failed[3] == 409
    assert "另一仓管" in failed[2]

    with get_db(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM correction_records WHERE header_id=?", (hid,)
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT version_no, declared_stamped FROM batch_headers WHERE id=?",
            (hid,),
        ).fetchone()
    assert n == 1  # 没有双写
    assert row["version_no"] == 2
    assert row["declared_stamped"] == 0


def test_freeze_listens_frozen_rows_recount_listens_live():
    db = _fresh_db()
    hid = _drifted(list_headers(db))[0]["header_id"]
    snap = open_snapshot(hid, db)
    # 冻住后、纠偏前，运行码行新增一条已盖印
    with get_db(db) as conn:
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value='WM-NEW', "
            "stamped_at=datetime('now') WHERE biz_date=(SELECT biz_date FROM "
            "batch_headers WHERE id=?) AND table_name=(SELECT table_name FROM "
            "batch_headers WHERE id=?) AND status='待盖印'",
            (hid, hid),
        )

    frozen = correct_header(hid, "冻结", "仓管左", snap["snapshot_id"], db)
    # 听冻住态：写回的是冻结瞬间的实数 0，与现场最新实数 1 仍有差
    assert frozen["after_declared"] == 0
    assert frozen["aligned_live"] is False
    with get_db(db) as conn:
        live = conn.execute(
            "SELECT COUNT(*) c FROM run_batches rb JOIN batch_headers bh ON "
            "rb.biz_date=bh.biz_date AND rb.table_name=bh.table_name "
            "WHERE bh.id=? AND rb.status='已盖印'",
            (hid,),
        ).fetchone()["c"]
    assert live == 1

    # 重数听最新行：当场重数为 1，纠齐
    recounted = correct_header(hid, "重数", "仓管右", None, db)
    assert recounted["after_declared"] == 1
    assert recounted["aligned_live"] is True


@pytest.mark.asyncio
async def test_replay_two_rounds_consistent_with_mode_chip(authed):
    import os

    db = os.environ["DATABASE_PATH"]
    headers = list_headers(db)
    drifted = _drifted(headers)
    snap = open_snapshot(drifted[0]["header_id"], db)
    rec_freeze = correct_header(
        drifted[0]["header_id"], "冻结", "admin", snap["snapshot_id"], db
    )
    rec_recount = correct_header(drifted[1]["header_id"], "重数", "admin", None, db)

    r1 = await authed.post(
        f"/api/reconcile/records/{rec_freeze['record_id']}/replay", json={}
    )
    body1 = await r1.json()
    assert body1["ok"] is True
    assert body1["replay"]["consistent"] is True
    assert len(body1["replay"]["rounds"]) == 2
    assert all(x["consistent"] for x in body1["replay"]["rounds"])

    r2 = await authed.post(
        f"/api/reconcile/records/{rec_recount['record_id']}/replay", json={}
    )
    body2 = await r2.json()
    assert body2["replay"]["consistent"] is True

    logs = list_replay_logs(db)
    assert len(logs) == 4  # 两单各两轮
    assert all(log["consistent"] == 1 for log in logs)
    freeze_logs = [l for l in logs if l["record_id"] == rec_freeze["record_id"]]
    assert {l["round_no"] for l in freeze_logs} == {1, 2}


def test_replay_detects_drift_after_header_tampered():
    db = _fresh_db()
    hid = _drifted(list_headers(db))[0]["header_id"]
    rec = correct_header(hid, "重数", "admin", None, db)
    # 库内头栏被再次改漂（模拟只动展示/脏写），回放必须发现并留证
    with get_db(db) as conn:
        conn.execute(
            "UPDATE batch_headers SET declared_stamped=declared_stamped+5 WHERE id=?",
            (hid,),
        )
    result = replay_record(rec["record_id"], db)
    assert result["consistent"] is False
    assert "漂移" in result["message"]
    assert all(round_["consistent"] is False for round_ in result["rounds"])
    logs = list_replay_logs(db)
    assert all(log["consistent"] == 0 for log in logs)


@pytest.mark.asyncio
async def test_reconcile_requires_login(client):
    from blacksheep.contents import JSONContent

    for method, path in [
        ("get", "/api/reconcile/headers"),
        ("get", "/api/reconcile/gap-cards"),
        ("get", "/api/reconcile/records"),
        ("get", "/api/reconcile/replay-logs"),
    ]:
        r = await getattr(client, method)(path)
        assert r.status == 401
    r = await client.post(
        "/api/reconcile/headers/1/correct",
        content=JSONContent({"mode": "重数"}),
    )
    assert r.status == 401
    assert "未登录" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_drift_stops_gap_card_and_recovers_after_correction(authed):
    cards = (await (await authed.get("/api/reconcile/gap-cards")).json())["items"]
    assert any(c["blocked"] for c in cards)
    headers = (await (await authed.get("/api/reconcile/headers")).json())["items"]
    for h in _drifted(headers):
        await authed.post(
            f"/api/reconcile/headers/{h['header_id']}/correct",
            json={"mode": "重数"},
        )
    cards2 = (await (await authed.get("/api/reconcile/gap-cards")).json())["items"]
    assert not any(c["blocked"] for c in cards2)
    assert all(c["declared_stamped"] == c["actual_stamped"] for c in cards2)
    # 留证齐全
    records = (await (await authed.get("/api/reconcile/records")).json())["items"]
    assert len(records) == 2


def test_init_db_backfills_headers_on_legacy_database():
    from app.db import get_db, init_db

    db = _fresh_db()
    # 拆掉新表，模拟只有旧四表的老库
    with get_db(db) as conn:
        conn.execute("DROP TABLE replay_logs")
        conn.execute("DROP TABLE correction_records")
        conn.execute("DROP TABLE header_snapshots")
        conn.execute("DROP TABLE batch_headers")
        conn.execute(
            "INSERT INTO run_batches(table_name, run_code, biz_date, status, "
            "watermark_value) VALUES ('t_x','rc-1','2026-11-15','已盖印','WM1')"
        )
        conn.execute(
            "INSERT INTO run_batches(table_name, run_code, biz_date, status, "
            "watermark_value) VALUES ('t_x','rc-2','2026-11-15','待盖印',NULL)"
        )
    init_db(db)
    headers = list_headers(db)
    backfilled = next(h for h in headers if h["table_name"] == "t_x")
    assert backfilled["declared_stamped"] == 1
    assert backfilled["actual_stamped"] == 1
    assert backfilled["expected_count"] == 2
    assert backfilled["drifted"] is False


def test_db_level_lock_conflict_returns_chinese_409(monkeypatch):
    # 测试环境把写锁等待压到 50ms，避免等满生产 5s 超时
    monkeypatch.setenv("BUSY_TIMEOUT_MS", "50")
    from app import config as config_module

    monkeypatch.setattr(config_module.Config, "BUSY_TIMEOUT_MS", 50)

    from app.db import connect

    db = _fresh_db()
    hid = _drifted(list_headers(db))[0]["header_id"]
    # 外部连接先抢占写锁，纠偏连接拿不到 BEGIN IMMEDIATE，须中文 409 而非 500
    holder = connect(db)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ServiceError) as exc:
            correct_header(hid, "重数", "仓管右", None, db)
        assert exc.value.status_code == 409
        assert "另一仓管" in exc.value.message
        # 抢锁失败不能产生任何留证或头栏改动
        with get_db(db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM correction_records WHERE header_id=?",
                (hid,),
            ).fetchone()["c"]
            row = conn.execute(
                "SELECT version_no FROM batch_headers WHERE id=?", (hid,)
            ).fetchone()
        assert n == 0
        assert row["version_no"] == 1
    finally:
        holder.rollback()
        holder.close()
