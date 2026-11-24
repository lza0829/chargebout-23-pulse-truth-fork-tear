from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH, DB_SYNCHRONOUS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
-- 脉冲流水：电量唯一真源，只追加，禁改禁删
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL CHECK(delta_kwh > 0),
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ADR-0001：脉冲回执。每一笔被接受的脉冲恰好一条回执，与流水行一一对应；
-- 旁路改累计列永远不写本表，因此「有回执 ⇔ 真脉冲」。
CREATE TABLE IF NOT EXISTS pulse_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pulse_id INTEGER NOT NULL UNIQUE,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  replayed_kwh REAL NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(pulse_id) REFERENCES pulses(id),
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- ADR-0001：对齐诊断快照。每次写口/自愈留一条审计，读侧另给实时诊断。
CREATE TABLE IF NOT EXISTS alignment_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  replayed_kwh REAL NOT NULL,
  cached_kwh REAL NOT NULL,
  drift_kwh REAL NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- ADR-0001：旁路尝试审计。被触发器拒绝的、以及夹具放行注入的旁路都在此留痕。
CREATE TABLE IF NOT EXISTS bypass_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  kind TEXT NOT NULL,
  attempted_kwh REAL,
  replayed_kwh REAL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- 写守卫开关表：三档模式，仅内部校准/chaos 夹具会切换，正常业务路径恒为 0。
-- 0=旁路一律回滚；1=chaos 夹具放行（全程留痕）；2=内部校准（写口/自愈/关账）
CREATE TABLE IF NOT EXISTS write_guard (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  kwh_write_mode INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO write_guard(id, kwh_write_mode) VALUES(1, 0);

-- 新开会话累计列必须从 0 起：电量只能由脉冲长出
CREATE TRIGGER IF NOT EXISTS trg_sessions_insert_zero
BEFORE INSERT ON sessions
WHEN NEW.kwh <> 0
BEGIN
  SELECT RAISE(ABORT, '新会话累计电量必须为0，电量只能由脉冲流水长出');
END;

-- 旁路改累计列：守卫为 0 档（阻断档）即整笔回滚（ADR-0001 不变量3）
CREATE TRIGGER IF NOT EXISTS trg_sessions_kwh_bypass
BEFORE UPDATE OF kwh ON sessions
WHEN NEW.kwh <> OLD.kwh
     AND COALESCE((SELECT kwh_write_mode FROM write_guard WHERE id=1), 0) = 0
BEGIN
  SELECT RAISE(ABORT, '旁路修改累计电量列被拒绝：电量真源为脉冲流水，请走脉冲写口');
END;

-- 脉冲流水只追加（ADR-0001 不变量4）
CREATE TRIGGER IF NOT EXISTS trg_pulses_no_update
BEFORE UPDATE ON pulses
BEGIN
  SELECT RAISE(ABORT, '脉冲流水只追加，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_pulses_no_delete
BEFORE DELETE ON pulses
BEGIN
  SELECT RAISE(ABORT, '脉冲流水只追加，禁止删除');
END;

-- 回执与诊断快照同样只追加，审计链不可被改写
CREATE TRIGGER IF NOT EXISTS trg_receipts_no_update
BEFORE UPDATE ON pulse_receipts
BEGIN
  SELECT RAISE(ABORT, '脉冲回执只追加，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_receipts_no_delete
BEFORE DELETE ON pulse_receipts
BEGIN
  SELECT RAISE(ABORT, '脉冲回执只追加，禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_alignment_no_update
BEFORE UPDATE ON alignment_snapshots
BEGIN
  SELECT RAISE(ABORT, '对齐诊断快照只追加，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_alignment_no_delete
BEFORE DELETE ON alignment_snapshots
BEGIN
  SELECT RAISE(ABORT, '对齐诊断快照只追加，禁止删除');
END;
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={DB_SYNCHRONOUS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
