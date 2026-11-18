import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=False, timeout=Config.BUSY_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发纠偏时后来的写连接等待该上限再抛 SQLITE_BUSY
    conn.execute(f"PRAGMA busy_timeout = {Config.BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 批头栏：按 业务日 + 产出表 归组的头栏，declared_stamped 是头栏申报的已盖印水印数
CREATE TABLE IF NOT EXISTS batch_headers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_date TEXT NOT NULL,
  table_name TEXT NOT NULL,
  declared_stamped INTEGER NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  aligned_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(biz_date, table_name)
);

-- 打开核对台瞬间冻结的头栏/行数快照
CREATE TABLE IF NOT EXISTS header_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_id INTEGER NOT NULL,
  frozen_declared INTEGER NOT NULL,
  frozen_actual INTEGER NOT NULL,
  frozen_version INTEGER NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 纠偏留证（同时充当回放队列条目）
CREATE TABLE IF NOT EXISTS correction_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_id INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('冻结','重数')),
  before_declared INTEGER NOT NULL,
  after_declared INTEGER NOT NULL,
  actual_count INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  snapshot_id INTEGER,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 对账回放轮次留证
CREATE TABLE IF NOT EXISTS replay_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id INTEGER NOT NULL,
  round_no INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  basis_count INTEGER NOT NULL,
  live_actual INTEGER NOT NULL,
  header_declared INTEGER NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]

# 头栏水印数故意申报错的批头栏（业务日, 表）-> 申报数，复现「头栏与运行码行对不上」
SEED_HEADER_DRIFT = {
    ("2026-11-14", "dw_orders_d"): 1,
    ("2026-11-14", "dm_gmv_d"): 1,
}


def _seed_headers(conn: sqlite3.Connection) -> None:
    exists = conn.execute("SELECT 1 FROM batch_headers LIMIT 1").fetchone()
    if exists:
        return
    groups = conn.execute(
        "SELECT biz_date, table_name, "
        "SUM(CASE WHEN status='已盖印' THEN 1 ELSE 0 END) AS actual_count, "
        "COUNT(*) AS total_count "
        "FROM run_batches WHERE status!='已作废' "
        "GROUP BY biz_date, table_name ORDER BY biz_date, table_name"
    ).fetchall()
    for g in groups:
        actual = int(g["actual_count"])
        declared = SEED_HEADER_DRIFT.get(
            (g["biz_date"], g["table_name"]), actual
        )
        conn.execute(
            "INSERT INTO batch_headers(biz_date, table_name, declared_stamped, version_no) "
            "VALUES (?,?,?,1)",
            (g["biz_date"], g["table_name"], declared),
        )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # 老库补齐批头栏（新库会按漂移种子落库）
        _seed_headers(conn)
