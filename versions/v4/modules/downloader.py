"""
v4 — Downloader 模块

从 scanned 视频下载 mp4 + 提取 mp3 音频。
"""

import os
import sys
import time
import logging
import urllib.request
from logging.handlers import RotatingFileHandler
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

logger = logging.getLogger("download")


class Downloader:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.cache_dir = Path(config.get("general", "project_root", default=".")) / "tmp_cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.curl_timeout = config.get("downloader", "curl_timeout", default=30)
        self.min_size = config.get("downloader", "min_file_size", default=100000)

    def run_once(self) -> int:
        """下载一批视频"""
        videos = self.db.get_videos_by_status("scanned", limit=3)
        if not videos:
            return 0

        processed = 0
        for v in videos:
            vid = v["id"]
            url = v.get("download_url", "")
            if not url:
                self.db.update_video_status(vid, "download_failed", error="无下载链接")
                continue

            result = self._download(vid, url)
            if result:
                mp4_path, mp3_path = result
                self.db.update_video_status(
                    vid, "downloaded",
                    mp4_path=str(mp4_path),
                    mp3_path=str(mp3_path),
                )
                processed += 1
                logger.info(f"  {vid}: 下载+音频提取完成")
            else:
                self.db.increment_retry(vid)
                if v["retry_count"] >= 2:
                    self.db.update_video_status(vid, "download_failed", error="下载失败超限")
                else:
                    self.db.update_video_status(vid, "scanned", error="下载失败将重试")

        return processed

    def _download(self, video_id: str, url: str):
        """下载mp4并提取mp3"""
        mp4_path = self.cache_dir / f"{video_id}.mp4"
        mp3_path = self.cache_dir / f"{video_id}.mp3"

        # 检查 mp4 是否已存在且有效
        if mp4_path.exists() and mp4_path.stat().st_size > self.min_size:
            logger.info(f"  {video_id}: mp4已存在，跳过下载")
        else:
            logger.info(f"  {video_id}: 开始下载...")
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    "Referer": "https://www.douyin.com/",
                })
                with urllib.request.urlopen(req, timeout=self.curl_timeout) as resp:
                    mp4_path.parent.mkdir(parents=True, exist_ok=True)
                    total = 0
                    with open(mp4_path, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            total += len(chunk)
            except Exception as e:
                mp4_path.unlink(missing_ok=True)
                logger.warning(f"  {video_id}: 下载异常: {e}")
                return None

            if total < self.min_size:
                mp4_path.unlink(missing_ok=True)
                logger.warning(f"  {video_id}: 文件太小或不存在 ({total} bytes)")
                return None

            size_mb = total / 1024 / 1024
            logger.info(f"  {video_id}: 下载完成 ({size_mb:.1f}MB)")

        # 提取 mp3
        if mp3_path.exists() and mp3_path.stat().st_size > 1000:
            logger.info(f"  {video_id}: mp3已存在")
        else:
            logger.info(f"  {video_id}: 提取音频...")
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp4_path),
                 "-vn", "-acodec", "libmp3lame",
                 "-ab", "48k", "-ar", "16000",
                 "-ac", "1", str(mp3_path)],
                timeout=120,
                capture_output=True,
            )
            if result.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size < 100:
                logger.warning(f"  {video_id}: ffmpeg提取音频失败")
                return None

            size_kb = mp3_path.stat().st_size / 1024
            logger.info(f"  {video_id}: 音频提取完成 ({size_kb:.0f}KB)")

        return mp4_path, mp3_path


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [download] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "downloader.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    downloader = Downloader(db, config)

    logger.info("🚀 Downloader 模块启动")
    db.log("download", "info", "模块启动")

    while True:
        try:
            processed = downloader.run_once()
            if not processed:
                time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ 异常: {e}")
            db.log("download", "error", str(e))
            time.sleep(10)

    db.close()


if __name__ == "__main__":
    main()
