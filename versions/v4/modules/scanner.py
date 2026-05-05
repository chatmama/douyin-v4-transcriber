"""
v4 — Scanner 模块 v3

用 v3 ChromeAPIBridge 调抖音 API。
Chrome 不可用时降级 HTTP + cookie。
"""

import os
import sys
import json
import time
import logging
from logging.handlers import RotatingFileHandler
import random
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))  # 根目录，载入 chrome_fetcher

from lib.config import Config
from lib.db import Database

logger = logging.getLogger("scanner")


class Scanner:
    """抖音用户视频扫描器"""

    def __init__(self, db: Database, config: Config):
        # 加载v3已处理视频ID列表，跳过重复
        self.v3_done = set()
        v3_path = os.path.join(os.path.dirname(__file__), "..", "data", "v3_done_ids.json")
        if os.path.exists(v3_path):
            try:
                with open(v3_path) as f:
                    self.v3_done = set(json.load(f))
                logger.info(f"已加载 {len(self.v3_done)} 个v3已处理视频ID，将跳过重复")
            except Exception as e:
                logger.warning(f"加载v3已处理列表失败: {e}")
        self.db = db
        self.config = config
        self.timeout = config.get("scanner", "request_timeout", default=30)
        self.max_videos = config.get("scanner", "max_videos_per_account", default=9999)

    def scan_account(self, name: str, sec_uid: str) -> int:
        """扫描一个账号的所有视频，返回新发现的视频数"""
        # 延迟导入，避免启动就依赖 Chrome
        from chrome_fetcher import ChromeAPIBridge

        bridge = ChromeAPIBridge(
            cookie_file=""
        )

        new_count = 0
        max_cursor = "0"
        page = 0

        while max_cursor and new_count < self.max_videos:
            try:
                url = bridge.make_api_url("aweme/post", {
                    "sec_user_id": sec_uid,
                    "count": "30",
                    "max_cursor": str(max_cursor),
                })

                data = bridge.api_call(url)
                status = data.get("status_code")

                if status != 0:
                    logger.warning(f"  {name}: status_code={status}")
                    time.sleep(10)
                    # 尝试一次重试
                    data = bridge.api_call(url)
                    status = data.get("status_code")
                    if status != 0:
                        logger.error(f"  {name}: 重试后仍失败 (status={status})")
                        return new_count

                aweme_list = data.get("aweme_list", [])
                if not aweme_list:
                    logger.info(f"  {name}: 第{page}页 无更多视频")
                    break

                for aweme in aweme_list:
                    vid = str(aweme.get("aweme_id", ""))
                    desc = aweme.get("desc", "") or ""
                    duration = aweme.get("duration", 0)
                    create_time = aweme.get("create_time", 0)

                    # 跳过v3已处理视频
                    if vid in self.v3_done:
                        continue

                    # 插入视频（带 DB 锁重试）
                    inserted = False
                    for retry in range(5):
                        try:
                            inserted = self.db.insert_video(vid, name, desc, duration, create_time)
                            break
                        except Exception as dbe:
                            if 'locked' in str(dbe).lower() and retry < 4:
                                time.sleep(1)
                                continue
                            logger.debug(f"  {name}: DB写入重试{retry}次后仍失败: {dbe}")
                            break
                    if inserted:
                        new_count += 1

                max_cursor = data.get("max_cursor", 0)
                page += 1

                if page % 20 == 0:
                    logger.info(f"  {name}: 第{page}页，累计新发现{new_count}个")

            except Exception as e:
                logger.warning(f"  {name}: {type(e).__name__}: {e}")
                time.sleep(10)
                continue

        logger.info(f"  {name}: 完成，新发现{new_count}个 (共{page}页)")
        return new_count

    def _seed_accounts(self):
        for name, sec_uid in self.config.get_accounts():
            self.db._conn.execute(
                """INSERT OR IGNORE INTO accounts (name, sec_uid, enabled)
                   VALUES (?, ?, 1)""",
                (name, sec_uid),
            )
        self.db._conn.commit()

    def run_once(self):
        self._seed_accounts()
        accounts = self.config.get_accounts()
        if not accounts:
            logger.warning("无启用的账号")
            return

        logger.info(f"🔄 开始扫描 {len(accounts)} 个账号...")

        for i, (name, sec_uid) in enumerate(accounts):
            try:
                self.scan_account(name, sec_uid)
                self.db.update_account_scanned(name, 0)
                if i < len(accounts) - 1:
                    gap = random.uniform(5, 20)
                    logger.info(f"  {name} 完成，{gap:.0f}s后扫下一个...")
                    time.sleep(gap)
            except Exception as e:
                logger.error(f"{name} 扫描失败: {e}")

        logger.info(f"✅ 本轮扫描完成")


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [scanner] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "scanner.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    scanner = Scanner(db, config)
    interval = config.get("scanner", "interval_seconds", default=120)

    logger.info("🚀 Scanner v3 模块启动（ChromeAPIBridge）")
    db.log("scanner", "info", "模块启动(v3)")

    while True:
        try:
            scanner.run_once()
            jitter = random.uniform(-10, 10)
            wait = max(interval + jitter, 30)
            logger.info(f"⏳ 等待 {wait:.0f}s 后下一轮...")
            db.log("scanner", "info", f"扫描完成，睡眠{wait:.0f}s")
            time.sleep(wait)
        except KeyboardInterrupt:
            logger.info("🛑 中断")
            break
        except Exception as e:
            logger.error(f"❌ 扫描异常: {e}")
            db.log("scanner", "error", f"异常: {e}")
            time.sleep(30)

    db.close()


if __name__ == "__main__":
    main()
