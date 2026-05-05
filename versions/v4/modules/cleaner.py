"""
v4 — Cleaner 模块

自动清理已完成视频的临时文件，控制磁盘使用量，备份数据库。
"""

import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

logger = logging.getLogger("cleaner")


class Cleaner:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.root = Path(config.get("general", "project_root", default="."))
        self.cache_dir = self.root / "tmp_cache"
        self.max_cache_gb = config.get("cleaner", "max_cache_gb", default=10)
        self.keep_days = config.get("cleaner", "keep_days", default=7)
        self._last_backup = 0

    def run_once(self):
        """执行一轮清理"""
        self._clean_completed()
        self._limit_cache_size()
        self._backup_database()

    def _clean_completed(self):
        """清理已发布视频的临时文件"""
        videos = self.db.get_videos_by_status("published", limit=200)
        for v in videos:
            vid = v["id"]
            cleaned = 0
            for ext in [".mp4", ".mp3"]:
                p = self.cache_dir / f"{vid}{ext}"
                if p.exists():
                    p.unlink()
                    cleaned += 1
            if cleaned:
                logger.info(f"  清理 {vid}: 删 {cleaned} 个临时文件")

    def _limit_cache_size(self):
        """限制缓存目录大小"""
        if not self.cache_dir.exists():
            return

        try:
            result = subprocess.run(
                ["du", "-sb", str(self.cache_dir)],
                capture_output=True, text=True, timeout=5,
            )
            size_bytes = int(result.stdout.split()[0])
            max_bytes = self.max_cache_gb * 1024 * 1024 * 1024

            if size_bytes > max_bytes:
                logger.warning(
                    f"缓存 {size_bytes/1024/1024/1024:.1f}GB 超限 "
                    f"(限制 {self.max_cache_gb}GB)，清理中..."
                )
                files = sorted(
                    self.cache_dir.iterdir(),
                    key=lambda f: f.stat().st_mtime,
                )
                for f in files:
                    if f.is_file() and f.suffix in (".mp4", ".mp3"):
                        f.unlink()
                        size_bytes -= f.stat().st_size
                        logger.info(f"  删旧文件: {f.name}")
                        if size_bytes <= max_bytes * 0.8:
                            break
                logger.info(f"  清理完成: {size_bytes/1024/1024/1024:.1f}GB")
        except Exception as e:
            logger.error(f"缓存清理失败: {e}")

    def _backup_database(self):
        """每日备份数据库"""
        now = time.time()
        # 每 24 小时备份一次
        if now - self._last_backup < 86400:
            return

        db_path = Path(self.config.db_path)
        if not db_path.exists():
            return

        backup_dir = db_path.parent / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"pipeline_{ts}.db"
        size_mb = db_path.stat().st_size / 1024 / 1024

        try:
            shutil.copy2(db_path, backup_path)
            # 压缩
            subprocess.run(
                ["gzip", "-f", str(backup_path)],
                capture_output=True, timeout=30,
            )
            logger.info(f"💾 数据库备份: {backup_path}.gz ({size_mb:.1f}MB)")

            # 保留最近 7 次备份
            backups = sorted(backup_dir.glob("pipeline_*.db.gz"), reverse=True)
            for old in backups[7:]:
                old.unlink()
                logger.info(f"  清理旧备份: {old.name}")

            self._last_backup = now
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")

    def clean_orphaned_files(self):
        """清理数据库中不存在的孤立文件"""
        if not self.cache_dir.exists():
            return

        # 获取 DB 中所有已知视频 ID
        known_ids = set()
        for status in ["pending", "scanned", "downloaded", "done",
                       "published", "url_failed", "download_failed",
                       "trans_failed", "pub_failed"]:
            videos = self.db.get_videos_by_status(status, limit=10000)
            known_ids.update(v["id"] for v in videos)

        cleaned = 0
        for f in self.cache_dir.iterdir():
            if f.is_file():
                stem = f.stem
                if stem not in known_ids:
                    f.unlink()
                    cleaned += 1

        if cleaned:
            logger.info(f"清理 {cleaned} 个孤立文件")


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [cleaner] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "cleaner.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    cleaner = Cleaner(db, config)
    interval = config.get("cleaner", "interval_seconds", default=300)

    logger.info("🚀 Cleaner 模块启动")
    db.log("cleaner", "info", "模块启动")

    while True:
        try:
            cleaner.run_once()
            cleaner.clean_orphaned_files()
            time.sleep(interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ 异常: {e}")
            time.sleep(60)

    db.close()


if __name__ == "__main__":
    main()
