"""
v4 — Publisher 模块

将转写完成的文稿格式化排版后发布到 WordPress。
"""

import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database
from lib.wordpress import WordPressClient

logger = logging.getLogger("publish")


class Publisher:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        wp_url = config.get("wordpress", "url", default="http://localhost:8080")
        wp_user = config.get("wordpress", "username", default="admin")
        wp_pass = config.get("wordpress", "password", default="")
        self.wp = WordPressClient(wp_url, wp_user, wp_pass)
        self.gen_image = config.get("wordpress", "generate_featured_image", default=True)

    def _check_wp_health(self) -> bool:
        """检查 WordPress 是否在线"""
        try:
            import urllib.request, json
            resp = urllib.request.urlopen(
                f"{self.wp.base_url}/wp-json/", timeout=10
            )
            data = json.loads(resp.read().decode())
            return "name" in data
        except Exception:
            return False

    def run_once(self) -> int:
        """发布一篇 done 状态的文稿"""
        videos = self.db.get_videos_by_status("done", limit=1)
        if not videos:
            return 0

        # 先检查 WP 健康
        if not self._check_wp_health():
            logger.warning("WordPress 不可用，跳过本轮发布")
            return 0

        v = videos[0]
        vid = v["id"]
        output_path = v.get("output_path", "")

        if not output_path or not Path(output_path).exists():
            self.db.log("publish", "error", f"输出文件不存在: {output_path}", vid)
            self.db.update_video_status(vid, "pub_failed", error="文件不存在")
            return 0

        # 读取文稿
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                md_content = f.read()
        except Exception as e:
            self.db.update_video_status(vid, "pub_failed", error=f"读取失败: {e}")
            return 0

        # 提取标题
        title = v.get("desc", "") or "未命名视频"
        if len(title) > 100:
            title = title[:100]

        # 提取正文 (去掉 front matter)
        text = md_content
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2].strip()
        # 去掉第一行标题
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1].strip()

        # 生成 HTML
        html = self.wp.format_html(title, text)

        self.db.update_video_status(vid, "publishing")

        # 生成特色图片
        media_id = None
        if self.gen_image:
            try:
                media_id = self.wp.generate_featured_image(title)
            except Exception as e:
                logger.warning(f"  生成图片失败: {e}")

        # 发布
        post_id = self.wp.create_post(title, html, featured_media_id=media_id or 0)

        if post_id:
            self.db.update_video_status(vid, "published", wp_post_id=post_id)
            logger.info(f"  {vid}: 已发布为文章ID={post_id}")
        else:
            self.db.update_video_status(vid, "pub_failed", error="WP发布失败")

        return 1 if post_id else 0

    def run_once_full(self) -> int:
        """完整发布流程"""
        return self.run_once()


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [publish] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "publisher.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    publisher = Publisher(db, config)

    logger.info("🚀 Publisher 模块启动")
    db.log("publish", "info", "模块启动")

    # 启动时尝试登录
    publisher.wp.login()

    while True:
        try:
            db.reset_stale_status("publishing", "done", older_than_minutes=5)
            published = publisher.run_once_full()
            if not published:
                time.sleep(10)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ 异常: {e}")
            db.log("publish", "error", str(e))
            time.sleep(30)

    db.close()


if __name__ == "__main__":
    main()
