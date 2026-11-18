import sqlite3
import threading

from app.db import connect, get_db
from app.services.errors import ServiceError

# 进程内 per-header 互斥：左右仓管同进程同时抢同一头栏时，只放一单进库
_HEADER_LOCKS: dict[int, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def _header_lock(header_id: int) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _HEADER_LOCKS.get(header_id)
        if lock is None:
            lock = threading.Lock()
            _HEADER_LOCKS[header_id] = lock
        return lock


def _count_actual(conn: sqlite3.Connection, header_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches rb "
        "JOIN batch_headers bh ON rb.biz_date = bh.biz_date "
        "AND rb.table_name = bh.table_name "
        "WHERE bh.id=? AND rb.status='已盖印'",
        (header_id,),
    ).fetchone()
    return int(row["c"])


def _count_expected(conn: sqlite3.Connection, header_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches rb "
        "JOIN batch_headers bh ON rb.biz_date = bh.biz_date "
        "AND rb.table_name = bh.table_name "
        "WHERE bh.id=? AND rb.status!='已作废'",
        (header_id,),
    ).fetchone()
    return int(row["c"])


def _get_header(conn: sqlite3.Connection, header_id: int) -> sqlite3.Row:
    header = conn.execute(
        "SELECT * FROM batch_headers WHERE id=?", (header_id,)
    ).fetchone()
    if not header:
        raise ServiceError("批头栏不存在", 404)
    return header


def list_headers(db_path: str | None = None) -> list[dict]:
    """水印核对台：每个批头栏一行，带头栏数/实数/差量/漂移态/最新形态芯片。"""
    with get_db(db_path) as conn:
        headers = conn.execute(
            "SELECT * FROM batch_headers ORDER BY biz_date, table_name"
        ).fetchall()
        result = []
        for h in headers:
            actual = _count_actual(conn, h["id"])
            expected = _count_expected(conn, h["id"])
            latest = conn.execute(
                "SELECT mode, delta FROM correction_records WHERE header_id=? "
                "ORDER BY id DESC LIMIT 1",
                (h["id"],),
            ).fetchone()
            declared = int(h["declared_stamped"])
            drift = declared - actual
            result.append(
                {
                    "header_id": h["id"],
                    "biz_date": h["biz_date"],
                    "table_name": h["table_name"],
                    "declared_stamped": declared,
                    "actual_stamped": actual,
                    "expected_count": expected,
                    "gap_count": expected - actual,
                    "delta": drift,
                    "drifted": drift != 0,
                    "version_no": h["version_no"],
                    "aligned_at": h["aligned_at"],
                    "last_mode": latest["mode"] if latest else None,
                    "last_delta": int(latest["delta"]) if latest else None,
                }
            )
        return result


def open_snapshot(header_id: int, db_path: str | None = None) -> dict:
    """打开瞬间：冻住头栏申报数与当前已盖印行数，供冻结态纠偏对账。"""
    with get_db(db_path) as conn:
        header = _get_header(conn, header_id)
        actual = _count_actual(conn, header_id)
        cur = conn.execute(
            "INSERT INTO header_snapshots(header_id, frozen_declared, frozen_actual, "
            "frozen_version) VALUES (?,?,?,?)",
            (header_id, header["declared_stamped"], actual, header["version_no"]),
        )
        return {
            "snapshot_id": cur.lastrowid,
            "header_id": header_id,
            "frozen_declared": header["declared_stamped"],
            "frozen_actual": actual,
            "frozen_version": header["version_no"],
            "frozen_delta": int(header["declared_stamped"]) - actual,
        }


def _insert_record(
    conn: sqlite3.Connection,
    header_id: int,
    mode: str,
    before: int,
    after: int,
    actual: int,
    snapshot_id: int | None,
    operator: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO correction_records(header_id, mode, before_declared, "
        "after_declared, actual_count, delta, snapshot_id, operator) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (header_id, mode, before, after, actual, after - before, snapshot_id, operator),
    )
    return int(cur.lastrowid)


def _correct_locked(
    conn: sqlite3.Connection,
    header_id: int,
    mode: str,
    operator: str,
    snapshot_id: int | None,
) -> dict:
    header = _get_header(conn, header_id)
    before = int(header["declared_stamped"])
    version = int(header["version_no"])
    live_actual = _count_actual(conn, header_id)

    if mode == "冻结":
        if snapshot_id is None:
            raise ServiceError("冻结态纠偏必须先冻结快照", 400)
        snap = conn.execute(
            "SELECT * FROM header_snapshots WHERE id=? AND header_id=?",
            (snapshot_id, header_id),
        ).fetchone()
        if not snap:
            raise ServiceError("冻结快照不存在", 404)
        if int(snap["consumed"]) != 0:
            raise ServiceError("冻结快照已被使用，请重新打开核对台", 409)
        # 快照冻的是打开瞬间的头栏：冻后被改过则快照失效，必须重新打开
        if int(snap["frozen_version"]) != version:
            raise ServiceError("头栏已被改动，冻结快照失效，请重新打开核对台", 409)
        if int(snap["frozen_declared"]) != before:
            raise ServiceError("头栏已被改动，冻结快照失效，请重新打开核对台", 409)
        target = int(snap["frozen_actual"])
        conn.execute(
            "UPDATE header_snapshots SET consumed=1 WHERE id=?", (snapshot_id,)
        )
    else:
        # 提交瞬间当场重数，听最新行
        target = live_actual

    if before == target:
        raise ServiceError("头栏水印数已与实数对齐，无需纠偏", 409)

    # 排他落库：更新的是库字段，不是只改页面展示
    cur = conn.execute(
        "UPDATE batch_headers SET declared_stamped=?, version_no=version_no+1, "
        "aligned_at=datetime('now') WHERE id=? AND version_no=?",
        (target, header_id, version),
    )
    # 版本号 CAS：另一单已抢先提交时本单影响行数为 0，双写被挡
    if cur.rowcount == 0:
        raise ServiceError("批头栏正被另一仓管纠偏，请稍后重试", 409)

    # 落库后当场自检：与本轮认定的实数对不上就回滚，漂了就停
    recheck = conn.execute(
        "SELECT declared_stamped FROM batch_headers WHERE id=?", (header_id,)
    ).fetchone()
    if int(recheck["declared_stamped"]) != target:
        raise ServiceError("纠偏落库校验失败，已回滚", 500)

    record_id = _insert_record(
        conn, header_id, mode, before, target, live_actual, snapshot_id, operator
    )
    return {
        "record_id": record_id,
        "header_id": header_id,
        "mode": mode,
        "before_declared": before,
        "after_declared": target,
        "live_actual": live_actual,
        "delta": target - before,
        "frozen_basis": mode == "冻结",
        "aligned_live": target == live_actual,
        "version_no": version + 1,
    }


def correct_header(
    header_id: int,
    mode: str,
    operator: str,
    snapshot_id: int | None = None,
    db_path: str | None = None,
) -> dict:
    """点纠偏：按互斥一次落库。mode 取『冻结』或『重数』，失败一律中文。"""
    if mode not in ("冻结", "重数"):
        raise ServiceError("纠偏形态只能是冻结或重数", 400)
    lock = _header_lock(header_id)
    if not lock.acquire(blocking=False):
        raise ServiceError("批头栏正被另一仓管纠偏，请稍后重试", 409)
    conn = connect(db_path)
    try:
        # 排他锁：抢到写锁的连接才能头栏落库，杜绝双写水印字段
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _correct_locked(conn, header_id, mode, operator, snapshot_id)
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise ServiceError("批头栏正被另一仓管纠偏，请稍后重试", 409) from exc
            raise ServiceError(f"纠偏落库失败：{exc}", 500) from exc
        except Exception:
            conn.rollback()
            raise
        return result
    finally:
        lock.release()
        conn.close()


def list_records(db_path: str | None = None) -> list[dict]:
    """回放队列：全部纠偏留证，最新在前。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM correction_records ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def replay_record(
    record_id: int,
    db_path: str | None = None,
) -> dict:
    """对账回放：重放一单纠偏，连跑两轮，轮轮与形态芯片口径一致并留证。"""
    with get_db(db_path) as conn:
        record = conn.execute(
            "SELECT * FROM correction_records WHERE id=?", (record_id,)
        ).fetchone()
        if not record:
            raise ServiceError("回放记录不存在", 404)
        header = _get_header(conn, record["header_id"])
        live_actual = _count_actual(conn, record["header_id"])
        header_declared = int(header["declared_stamped"])
        mode = record["mode"]

        rounds = []
        all_consistent = True
        prior = conn.execute(
            "SELECT MAX(round_no) AS m FROM replay_logs WHERE record_id=?",
            (record_id,),
        ).fetchone()
        base_round = int(prior["m"] or 0)

        for round_no in (base_round + 1, base_round + 2):
            if mode == "冻结":
                # 听冻住态：与留证时冻结的实数（actual_count）对账
                basis = int(record["actual_count"])
                consistent = header_declared == basis
                msg = (
                    f"第{round_no}轮：冻结口径头栏 {header_declared} 对冻住实数 {basis}"
                    + ("，一致" if consistent else "，漂移")
                )
            else:
                # 听最新行：头栏须同时等于留证重数口径与现场最新实数
                basis = int(record["after_declared"])
                consistent = (
                    header_declared == basis and header_declared == live_actual
                )
                msg = (
                    f"第{round_no}轮：重数口径头栏 {header_declared} 对最新实数 {live_actual}"
                    + ("，一致" if consistent else "，漂移")
                )
            if not consistent:
                all_consistent = False
            conn.execute(
                "INSERT INTO replay_logs(record_id, round_no, consistent, basis_count, "
                "live_actual, header_declared, message) VALUES (?,?,?,?,?,?,?)",
                (
                    record_id,
                    round_no,
                    1 if consistent else 0,
                    basis,
                    live_actual,
                    header_declared,
                    msg,
                ),
            )
            rounds.append(
                {
                    "round_no": round_no,
                    "consistent": consistent,
                    "basis_count": basis,
                    "live_actual": live_actual,
                    "header_declared": header_declared,
                    "message": msg,
                }
            )

        return {
            "record_id": record_id,
            "mode": mode,
            "consistent": all_consistent,
            "message": (
                "两轮回放均与形态芯片一致"
                if all_consistent
                else "回放发现漂移，头栏与实数对不上"
            ),
            "rounds": rounds,
        }


def list_replay_logs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM replay_logs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def gap_cards(db_path: str | None = None) -> list[dict]:
    """缺口卡片：头栏漂移时缺口摘要跟着漂，卡片亮差量并阻断对账，纠偏后解除。"""
    with get_db(db_path) as conn:
        headers = conn.execute(
            "SELECT * FROM batch_headers ORDER BY biz_date, table_name"
        ).fetchall()
        cards = []
        for h in headers:
            actual = _count_actual(conn, h["id"])
            expected = _count_expected(conn, h["id"])
            declared = int(h["declared_stamped"])
            drift = declared - actual
            blocked = drift != 0
            summary = (
                f"{h['biz_date']} {h['table_name']}：头栏申报已盖印 {declared}，"
                f"运行码实数 {actual}，差量 {drift:+d}，"
                + ("对账阻断，先纠偏" if blocked else "已对齐")
                + f"；应盖印 {expected}，缺口 {expected - actual}"
            )
            cards.append(
                {
                    "header_id": h["id"],
                    "biz_date": h["biz_date"],
                    "table_name": h["table_name"],
                    "declared_stamped": declared,
                    "actual_stamped": actual,
                    "expected_count": expected,
                    "gap_count": expected - actual,
                    "delta": drift,
                    "drifted": blocked,
                    "blocked": blocked,
                    "summary": summary,
                }
            )
        return cards
