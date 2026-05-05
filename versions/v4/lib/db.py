"""
v4 — 数据库访问层

提供 SQLite 数据库的读写接口，所有模块通过此层访问数据。
"""

import sqlite3
import os
import time
from pathlib import Path
from typing import Optional


import time as _time


class Database:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _exec_safe(self, sql, params=None, retries=5):
        """安全执行SQL，自动重试锁冲突"""
        for i in range(retries):
            try:
                c = self._get_conn()
                if params:
                    c.execute(sql, params)
                else:
                    c.execute(sql)
                c.commit()
                return True
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and i < retries - 1:
                    _time.sleep(0.5 * (i + 1))
                    continue
                raise
        return False

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), timeout=60.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=60000")
        return self._conn

    def _init_db(self):
        stmts = [
            "CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, sec_uid TEXT NOT NULL, enabled INTEGER DEFAULT 1, total_videos INTEGER DEFAULT 0, last_scanned TIMESTAMP, scan_interval INTEGER DEFAULT 300, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, account TEXT NOT NULL, desc TEXT, duration INTEGER, create_time INTEGER, status TEXT DEFAULT 'pending', retry_count INTEGER DEFAULT 0, error TEXT, mp4_path TEXT, mp3_path TEXT, output_path TEXT, download_url TEXT, word_count INTEGER DEFAULT 0, wp_post_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)",
            "CREATE INDEX IF NOT EXISTS idx_videos_account ON videos(account)",
            "CREATE INDEX IF NOT EXISTS idx_videos_create_time ON videos(create_time)",
            "CREATE TABLE IF NOT EXISTS pipeline_log (id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL, level TEXT DEFAULT 'info', message TEXT, video_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS idx_log_module ON pipeline_log(module, created_at)",
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)",
        ]
        for stmt in stmts:
            self._exec_safe(stmt)

    # -- 账号管理 --

    def get_enabled_accounts(self):
        """获取所有启用账号"""
        cur = self._get_conn().execute(
            "SELECT * FROM accounts WHERE enabled=1 ORDER BY name"
        )
        return [dict(r) for r in cur.fetchall()]

    def upsert_account(self, name: str, sec_uid: str, enabled: int = 1):
        self._exec_safe("INSERT OR REPLACE INTO accounts(name, sec_uid, enabled) VALUES (?, ?, ?)",
                       (name, sec_uid, enabled))

    def update_account_scanned(self, name: str, total: int):
        self._exec_safe("UPDATE accounts SET last_scanned=CURRENT_TIMESTAMP, total_videos=? WHERE name=?",
                       (total, name))

    # -- 视频管理 --

    def video_exists(self, vid: str) -> bool:
        cur = self._get_conn().execute("SELECT 1 FROM videos WHERE id=?", (vid,))
        return cur.fetchone() is not None

    def insert_video(self, vid: str, account: str, desc: str = "",
                     duration: int = 0, create_time: int = 0) -> bool:
        try:
            self._exec_safe("INSERT OR IGNORE INTO videos(id, account, desc, duration, create_time, status) VALUES (?,?,?,?,?,'pending')",
                          (vid, account, desc[:500], duration, create_time))
            cur = self._get_conn().execute("SELECT changes()")
            return cur.fetchone()[0] > 0
        except Exception:
            return False

    def update_video_status(self, vid: str, status: str, error: str = "",
                            **extra):
        fields = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
        values = [status]
        if error:
            fields.append("error=?")
            values.append(error)
        for k, v in extra.items():
            if v is not None:
                fields.append(f"{k}=?")
                values.append(v)
        values.append(vid)
        sql = f"UPDATE videos SET {', '.join(fields)} WHERE id=?"
        self._exec_safe(sql, tuple(values))

    def get_videos_by_status(self, status: str, limit: int = 5, account: str = None):
        """获取指定状态的视频"""
        sql = "SELECT * FROM videos WHERE status=? AND retry_count<3"
        params = [status]
        if account:
            sql += " AND account=?"
            params.append(account)
        sql += f" ORDER BY create_time ASC LIMIT {limit}"
        cur = self._get_conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_videos_by_statuses(self, statuses: list, limit: int = 5):
        """获取匹配多个状态的视频"""
        placeholders = ",".join("?" for _ in statuses)
        cur = self._get_conn().execute(
            f"SELECT * FROM videos WHERE status IN ({placeholders}) AND retry_count<3 ORDER BY create_time ASC LIMIT {limit}",
            statuses,
        )
        return [dict(r) for r in cur.fetchall()]

    def count_by_status(self, status: str = None) -> dict:
        """按状态统计视频数"""
        if status:
            cur = self._get_conn().execute(
                "SELECT status, COUNT(*) as cnt FROM videos WHERE status=? GROUP BY status",
                (status,),
            )
        else:
            cur = self._get_conn().execute(
                "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status"
            )
        result = {}
        for r in cur.fetchall():
            result[r["status"]] = r["cnt"]
        return result

    def reset_stale_status(self, stale_status: str, reset_to: str,
                           older_than_minutes: int = 10):
        sql = f"UPDATE videos SET status=?, retry_count=retry_count+1, error='超时重置', updated_at=CURRENT_TIMESTAMP WHERE status=? AND updated_at < datetime('now', '-{older_than_minutes} minutes')"
        self._exec_safe(sql, (reset_to, stale_status))
        affected = self._get_conn().execute("SELECT changes()").fetchone()[0]
        if affected > 0:
            self._log("system", "warning", f"重置{affected}个{stale_status}→{reset_to}")
        return affected

    def increment_retry(self, vid: str):
        self._exec_safe("UPDATE videos SET retry_count=retry_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (vid,))

    # -- 配置键值 --

    def get_config(self, key: str, default: str = "") -> str:
        cur = self._get_conn().execute(
            "SELECT value FROM config WHERE key=?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_config(self, key: str, value: str):
        self._exec_safe("INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                       (key, value))

    # -- 日志 --

    def log(self, module: str, level: str, message: str, video_id: str = None):
        self._log(module, level, message, video_id)

    def _log(self, module: str, level: str, message: str, video_id: str = None):
        self._exec_safe(
            "INSERT INTO pipeline_log(module, level, message, video_id) VALUES (?,?,?,?)",
            (module, level, message, video_id),
        )

    def get_recent_logs(self, limit: int = 20, module: str = None):
        sql = "SELECT * FROM pipeline_log"
        params = []
        if module:
            sql += " WHERE module=?"
            params.append(module)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = self._get_conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # -- 统计 --

    def get_stats(self) -> dict:
        """获取汇总统计"""
        by_status = self.count_by_status()
        total = sum(by_status.values())
        cur = self._get_conn().execute("SELECT COUNT(*) as cnt FROM accounts WHERE enabled=1")
        accounts_cnt = cur.fetchone()["cnt"]
        return {
            "total": total,
            "by_status": by_status,
            "accounts": accounts_cnt,
        }

    # -- 账号统计 --

    def get_account_stats(self) -> list:
        """每个账号的视频数、字数统计"""
        cur = self._get_conn().execute("""
            SELECT
                a.name,
                COUNT(v.id) AS total_videos,
                SUM(CASE WHEN v.status='done' OR v.status='published' THEN 1 ELSE 0 END) AS done_videos,
                COALESCE(SUM(CASE WHEN v.status='done' OR v.status='published' THEN v.word_count ELSE 0 END), 0) AS total_words
            FROM accounts a
            LEFT JOIN videos v ON a.name = v.account
            GROUP BY a.name
            ORDER BY a.name
        """)
        return [dict(r) for r in cur.fetchall()]

    def get_total_word_count(self) -> int:
        """转写总字数"""
        cur = self._get_conn().execute(
            "SELECT COALESCE(SUM(word_count), 0) FROM videos WHERE status IN ('done','published')"
        )
        return cur.fetchone()[0]

    def get_timeline_stats(self, hours: int = 24) -> list:
        """按时间统计完成量"""
        cur = self._get_conn().execute("""
            SELECT
                strftime('%%Y-%%m-%%d %%H:00', created_at) AS hour,
                COUNT(*) AS completed,
                COALESCE(SUM(word_count), 0) AS words
            FROM videos
            WHERE status IN ('done','published')
              AND created_at >= datetime('now', '-? hours')
            GROUP BY hour
            ORDER BY hour
        """, (hours,))
        return [dict(r) for r in cur.fetchall()]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
