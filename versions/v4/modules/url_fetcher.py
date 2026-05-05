"""
v4 — URL Fetcher 模块

从 pending 视频中提取真实下载链接。
降级策略: Chrome CDP → 扫描器缓存 → 直接API。
"""

import os
import sys
import json
import time
import re
import logging
from logging.handlers import RotatingFileHandler
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

# 复用 chrome_fetcher 的 API bridge
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from chrome_fetcher import ChromeAPIBridge

logger = logging.getLogger("url-fetch")


class URLFetcher:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.cdp_url = config.get("chrome", "cdp_url", default="http://127.0.0.1:9222")
        self.http_timeout = config.get("scanner", "request_timeout", default=30)
        self.user_agent = config.get("cookies", "user_agent", default=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ))
        self.cookie_str = self._load_cookies()
        self.cache_dir = Path(config.get("general", "project_root", default=".")) / "tmp_cache"
        self.cache_dir.mkdir(exist_ok=True)
        # 创建 ChromeAPIBridge 实例（复用 scanner 的底层 CDP 工具，默认使用 127.0.0.1:9222）
        self.bridge = ChromeAPIBridge()



    def _http_get_video_page(self, video_id: str) -> str:
        """HTTP 直接访问视频页面"""
        try:
            url = f"https://www.douyin.com/video/{video_id}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Cookie": self.cookie_str,
                    "Referer": "https://www.douyin.com/",
                },
            )
            resp = urllib.request.urlopen(req, timeout=self.http_timeout)
            html = resp.read().decode("utf-8", errors="replace")

            # 从 RENDER_DATA 提取
            m = re.search(
                r'"play_addr":\{"url_list":\["([^"]+)"',
                html,
            )
            if m:
                return m.group(1).replace("\\u0026", "&")

            # 从 video 标签提取
            m = re.search(r'<video[^>]*src="([^"]+)"', html)
            if m:
                return m.group(1)

        except Exception as e:
            logger.debug(f"HTTP 获取失败 {video_id}: {e}")
        return ""

    def _api_get_url(self, video_id: str) -> str:
        """通过第三方 API 获取"""
        try:
            api_url = (
                f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/"
                f"?aweme_id={video_id}"
            )
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": self.user_agent},
            )
            resp = urllib.request.urlopen(req, timeout=self.http_timeout)
            data = json.loads(resp.read().decode())
            play_addr = (
                data.get("aweme_detail", {})
                .get("video", {})
                .get("play_addr", {})
            )
            url_list = play_addr.get("url_list", [])
            if url_list:
                return url_list[0].replace("\\u0026", "&")
        except Exception as e:
            logger.debug(f"API 获取失败 {video_id}: {e}")
        return ""

    def _bridge_get_url(self, video_id: str) -> str:
        """策略1: 通过 ChromeAPIBridge 调用 douyin detail API"""
        try:
            # ChromeAPIBridge.make_api_url 会自动添加 /aweme/v1/web/ 前缀
            # 所以传 aweme/detail/ 会生成 /aweme/v1/web/aweme/detail/
            url = self.bridge.make_api_url("aweme/detail/", {
                "aweme_id": video_id,
            })
            data = self.bridge.api_call(url)
            if not data:
                return ""
            play_addr = (
                data.get("aweme_detail", {})
                .get("video", {})
                .get("play_addr", {})
            )
            url_list = play_addr.get("url_list", [])
            if url_list:
                return url_list[0].replace("\\u0026", "&").replace("\u0026", "&")
        except Exception as e:
            logger.debug(f"Bridge API 失败 {video_id}: {e}")
        return ""

    def _get_url(self, video_id: str) -> str:
        """降级策略获取下载链接"""
        # 策略1: ChromeAPIBridge（复用Scanner已验证的CDP通路）
        url = self._bridge_get_url(video_id)
        if url:
            return url

        # 策略2: HTTP 直取
        url = self._http_get_video_page(video_id)
        if url:
            return url

        # 策略3: 第三方 API
        url = self._api_get_url(video_id)
        if url:
            return url

        return ""

    def _download_now(self, video_id: str, url: str) -> bool:
        """拿到URL后立即下载，避免过期"""
        mp4_path = self.cache_dir / f"{video_id}.mp4"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Referer": "https://www.douyin.com/",
            })
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                total = 0
                with open(mp4_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
            if total < 100000:
                mp4_path.unlink(missing_ok=True)
                logger.warning(f"  {video_id}: 下载文件太小 ({total} bytes)")
                return False
            logger.info(f"  {video_id}: 下载完成 ({total/1024/1024:.1f}MB)")
            return True
        except Exception as e:
            mp4_path.unlink(missing_ok=True)
            logger.warning(f"  {video_id}: 下载失败: {e}")
            return False

    def run_once(self) -> int:
        """取一批 pending 视频的 URL 并立即下载"""
        videos = self.db.get_videos_by_status("pending", limit=5)
        if not videos:
            return 0

        processed = 0
        for v in videos:
            vid = v["id"]
            url = self._get_url(vid)

            if url:
                # 拿到URL立即下载（避免过期）
                if self._download_now(vid, url):
                    mp4_path = self.cache_dir / f"{vid}.mp4"
                    # 提取音频
                    mp3_path = self.cache_dir / f"{vid}.mp3"
                    import subprocess
                    result = subprocess.run(
                        ["ffmpeg", "-y", "-i", str(mp4_path),
                         "-vn", "-acodec", "libmp3lame",
                         "-ab", "48k", "-ar", "16000",
                         "-ac", "1", str(mp3_path)],
                        timeout=120, capture_output=True,
                    )
                    if result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 100:
                        logger.info(f"  {vid}: 音频提取完成 ({mp3_path.stat().st_size/1024:.0f}KB)")
                        self.db.update_video_status(vid, "downloaded",
                                                    download_url=url,
                                                    mp4_path=str(mp4_path),
                                                    mp3_path=str(mp3_path))
                    else:
                        self.db.update_video_status(vid, "scanned",
                                                    download_url=url)
                        logger.info(f"  {vid}: URL+下载成功，待音频提取")
                    processed += 1
                else:
                    # 下载失败但URL有效，留给downloader重试
                    self.db.update_video_status(vid, "scanned", download_url=url)
                    processed += 1
                    logger.info(f"  {vid}: URL 获取成功，但下载暂时失败")
            else:
                self.db.increment_retry(vid)
                if v["retry_count"] >= 2:
                    self.db.update_video_status(vid, "url_failed",
                                                error="3次策略均失败")
                    logger.warning(f"  {vid}: URL 全部失败，标记失败")
                else:
                    self.db.update_video_status(vid, "pending",
                                                error="URL 获取失败")
                    logger.warning(f"  {vid}: URL 失败，稍后重试")

        return processed

    def _load_cookies(self) -> str:
        """从文件加载抖音 cookie"""
        cookie_path = self.config.get("cookies", "file", default="")
        if not cookie_path:
            cookie_path = ""

        try:
            if os.path.exists(cookie_path):
                with open(cookie_path) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cookies = "; ".join(
                        f"{c['name']}={c['value']}"
                        for c in data
                    )
                    return cookies
                elif isinstance(data, dict):
                    cookies = "; ".join(
                        f"{k}={v}" for k, v in data.items()
                    )
                    return cookies
        except Exception as e:
            logger.debug(f"加载 cookie 失败: {e}")
        return ""


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [url-fetch] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "url_fetcher.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    fetcher = URLFetcher(db, config)

    logger.info("🚀 URL Fetcher 模块启动")
    logger.info(f"   Cookie: {'已加载' if fetcher.cookie_str else '未加载'}")
    db.log("url-fetch", "info", "模块启动")

    while True:
        try:
            processed = fetcher.run_once()
            if not processed:
                time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ 异常: {e}")
            db.log("url-fetch", "error", str(e))
            time.sleep(10)

    db.close()


if __name__ == "__main__":
    main()
