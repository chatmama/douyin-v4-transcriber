"""
v4 — WordPress REST API 客户端

用于将转写文稿发布到 WordPress。
使用 cookie + nonce 认证（已在 v3 验证过）。
"""

import json
import re
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
import subprocess
from pathlib import Path
from http.cookiejar import CookieJar
from typing import Optional

logger = logging.getLogger("wp")


class WordPressClient:
    """WordPress REST API 客户端 (cookie + nonce 认证)"""

    def __init__(self, base_url: str, username: str = "admin",
                 password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._nonce: Optional[str] = None
        self._cookie: Optional[str] = None
        self._cookiejar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPCookieProcessor(self._cookiejar),
        )

    def login(self) -> bool:
        """登录 WP 并获取 nonce 和 cookie"""
        try:
            login_url = f"{self.base_url}/wp-login.php"

            # 构造登录 POST 数据
            post_data = urllib.parse.urlencode({
                "log": self.username,
                "pwd": self.password,
                "wp-submit": "%E7%99%BB%E5%BD%95",
                "redirect_to": urllib.parse.quote(
                    f"{self.base_url}/wp-admin/", safe=""
                ),
                "testcookie": "1",
            }).encode()

            # 登录
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
                ),
                "Origin": self.base_url,
            }

            req = urllib.request.Request(login_url, data=post_data, headers=headers)
            resp = self._opener.open(req, timeout=15)
            body = resp.read().decode("utf-8", errors="replace")

            # 提取 cookie
            cookies = []
            for c in self._cookiejar:
                cookies.append(f"{c.name}={c.value}")
            self._cookie = "; ".join(cookies)

            logger.info(f"WP 登录响应: {resp.status}")
            logger.debug(f"   Cookie 数: {len(cookies)}")

            if not self._cookie:
                logger.warning("WP 登录后无 cookie")
                return False

            # 获取 nonce - 访问 wp-admin/ 页面提取 wpApiSettings
            admin_url = f"{self.base_url}/wp-admin/"
            admin_req = urllib.request.Request(
                admin_url,
                headers={"Cookie": self._cookie or ""},
            )
            admin_resp = self._opener.open(admin_req, timeout=15)
            admin_html = admin_resp.read().decode("utf-8", errors="replace")

            # 方式1: 从 wpApiSettings 提取
            m = re.search(
                r'wpApiSettings\s*=\s*\{[^}]*?"nonce"\s*:\s*"([^"]+)"',
                admin_html,
            )
            if m:
                self._nonce = m.group(1)
                logger.info(
                    f"WP 登录成功，nonce: {self._nonce[:8]}..."
                )
                return True

            # 方式2: 从 REST API 根路径获取 nonce
            rest_url = f"{self.base_url}/wp-json/"
            rest_req = urllib.request.Request(
                rest_url,
                headers={
                    "Cookie": self._cookie or "",
                    "X-WP-Nonce": "1",
                },
            )
            rest_resp = self._opener.open(rest_req, timeout=15)
            rest_data = json.loads(rest_resp.read().decode())

            # REST API 中的 nonce 通常在响应头的 X-WP-Nonce 中
            nonce_header = rest_resp.headers.get("X-WP-Nonce")
            if nonce_header:
                self._nonce = nonce_header
                logger.info(
                    f"WP nonce (from header): {self._nonce[:8]}..."
                )
                return True

            logger.warning("WP 登录成功但未获取到 nonce")
            return False

        except urllib.error.HTTPError as e:
            logger.error(f"WP 登录 HTTP {e.code}")
            return False
        except Exception as e:
            logger.error(f"WP 登录异常: {e}")
            return False

    def _ensure_login(self):
        """确保已登录"""
        if not self._nonce or not self._cookie:
            self.login()

    def create_post(self, title: str, content_html: str,
                    status: str = "publish",
                    featured_media_id: int = 0) -> Optional[int]:
        """创建 WordPress 文章"""
        self._ensure_login()
        if not self._nonce:
            logger.error("无法获取 nonce，发布失败")
            return None

        try:
            api_url = f"{self.base_url}/wp-json/wp/v2/posts"
            post_data = {
                "title": title[:200],
                "content": content_html,
                "status": status,
            }
            if featured_media_id:
                post_data["featured_media"] = featured_media_id

            body = json.dumps(post_data).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": self._cookie or "",
                    "X-WP-Nonce": self._nonce or "",
                },
            )
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            post_id = result.get("id")
            logger.info(f"文章已发布: ID={post_id}")
            return post_id

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")[:500]
            logger.error(f"WP 发布失败 {e.code}: {error_body}")
            return None
        except Exception as e:
            logger.error(f"WP 发布异常: {e}")
            return None

    def upload_image(self, image_path: str) -> Optional[int]:
        """上传图片到媒体库，返回 attachment ID"""
        self._ensure_login()
        if not self._nonce:
            return None

        try:
            api_url = f"{self.base_url}/wp-json/wp/v2/media"
            with open(image_path, "rb") as f:
                img_data = f.read()
            filename = Path(image_path).name

            req = urllib.request.Request(
                api_url,
                data=img_data,
                headers={
                    "Content-Type": "image/png",
                    "Content-Disposition":
                        f'attachment; filename="{filename}"',
                    "Cookie": self._cookie or "",
                    "X-WP-Nonce": self._nonce or "",
                },
            )
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            attach_id = result.get("id")
            logger.info(f"图片已上传: ID={attach_id}")
            return attach_id

        except Exception as e:
            logger.error(f"图片上传失败: {e}")
            return None

    def generate_featured_image(self, title: str) -> Optional[int]:
        """用 ImageMagick 生成特色图片并上传"""
        try:
            img_path = f"/tmp/wp_featured_{int(time.time())}.png"
            font = self._find_font()

            # 截断+换行
            short_title = title[:40] if len(title) > 40 else title
            display_text = "\n".join(
                short_title[i:i+10]
                for i in range(0, len(short_title), 10)
            )

            subprocess.run(
                [
                    "convert",
                    "-size", "1200x630",
                    "gradient:#1a1a2e-#16213e",
                    "-font", font,
                    "-fill", "white",
                    "-pointsize", "48",
                    "-gravity", "center",
                    "-annotate", "+0+0", display_text,
                    "-font", font,
                    "-fill", "rgba(255,255,255,0.6)",
                    "-pointsize", "20",
                    "-gravity", "southeast",
                    "-annotate", "+20+20", "文章转录",
                    img_path,
                ],
                timeout=15,
                capture_output=True,
            )

            if Path(img_path).exists():
                media_id = self.upload_image(img_path)
                Path(img_path).unlink(missing_ok=True)
                return media_id

        except Exception as e:
            logger.error(f"生成特色图片失败: {e}")
        return None

    def _find_font(self) -> str:
        """找一个可用的中文字体"""
        candidates = [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        # 默认
        return (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
        )

    def format_html(self, title: str, text: str) -> str:
        """将纯文本转换成富文本 HTML"""
        lines = text.strip().split("\n")
        paragraphs = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if len(line) < 15:
                paragraphs.append(
                    f'<p style="font-size:1.1em;color:#666;'
                    f'font-style:italic;border-left:3px solid #e74c3c;'
                    f'padding-left:15px;margin:20px 0;">'
                    f'{line}</p>'
                )
            else:
                paragraphs.append(
                    f'<p style="line-height:1.8;text-indent:2em;'
                    f'margin:10px 0;">{line}</p>'
                )

        body = "\n".join(paragraphs)
        html = (
            f'<div style="max-width:800px;margin:0 auto;padding:20px;'
            f'font-family:\'Microsoft YaHei\',\'PingFang SC\',sans-serif;">'
            f'<h1 style="font-size:1.8em;color:#2c3e50;'
            f'border-bottom:2px solid #e74c3c;padding-bottom:10px;'
            f'margin-bottom:25px;">{title}</h1>'
            f'{body}'
            f'<p style="text-align:center;color:#999;margin-top:40px;'
            f'border-top:1px solid #eee;padding-top:15px;font-size:0.85em;">'
            f'本文由 AI 自动转录整理 | 内容仅供参考</p>'
            f'</div>'
        )
        return html
