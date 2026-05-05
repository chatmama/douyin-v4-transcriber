#!/usr/bin/env python3
"""
v4 — Transcriber Worker 进程 (持久化)

启动后加载 large-v3 模型一次，然后轮询数据库取任务。
不退出，不重载模型，一直运行直到被杀死。

独立进程，由 transcriber.py 启动并管理。
"""

import os
import sys
import json
import time
import logging
from logging.handlers import RotatingFileHandler
import signal
from pathlib import Path

# 添加 lib 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

logger = logging.getLogger("transcriber-worker")

# 全局模型引用，保持模型在内存中
_model = None


def get_model(model_name: str = "large-v3",
              compute_type: str = "int8_float16"):
    """获取/初始化 Whisper 模型（只加载一次）"""
    global _model
    if _model is None:
        logger.info(f"加载模型 {model_name} ({compute_type})...")
        from faster_whisper import WhisperModel
        _model = WhisperModel(
            model_name,
            device="cuda",
            compute_type=compute_type,
        )
        logger.info("模型加载完成")
    return _model


def transcribe(audio_path: str, model_name: str = "large-v3",
               compute_type: str = "int8_float16") -> dict:
    """对单个音频文件进行 GPU 转写"""
    try:
        model = get_model(model_name, compute_type)

        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
                threshold=0.5,
            ),
            language="zh",
        )

        lines = []
        for i, seg in enumerate(segments):
            # 格式: [秒.毫秒] 文本 (和v3一致)
            lines.append(f"[{seg.start:.1f}] {seg.text.strip()}")
            # 每100段打一次日志
            if (i + 1) % 100 == 0:
                logger.info(f"  已转写 {i+1} 段...")

        full_text = "\n".join(lines)
        word_count = len(full_text)

        return {
            "ok": True,
            "text": full_text,
            "word_count": word_count,
            "duration": info.duration if info else 0,
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


def main_loop():
    """Worker 主循环：轮询 DB 取任务"""
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [worker] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "transcriber_worker.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    model_name = config.get("transcriber", "model", default="large-v3")
    compute_type = config.get("transcriber", "compute_type", default="int8_float16")
    transcribe_timeout = config.get("transcriber", "timeout", default=600)
    project_root = Path(config.get("general", "project_root", default="."))
    output_dir = project_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写 PID 文件
    pid_path = project_root / "database" / "worker.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    logger.info(f"🚀 Transcriber Worker 启动 (PID={os.getpid()})")
    logger.info(f"   模型: {model_name}/{compute_type}")
    db.log("worker", "info", f"启动 PID={os.getpid()}")

    # 预加载模型
    logger.info("预加载模型...")
    get_model(model_name, compute_type)

    while True:
        try:
            # 取任务
            videos = db.get_videos_by_status("downloaded", limit=1)
            if not videos:
                time.sleep(3)
                continue

            v = videos[0]
            vid = v["id"]
            mp3_path = v.get("mp3_path", "")

            if not mp3_path or not Path(mp3_path).exists():
                # 尝试在缓存目录找
                cached = project_root / "tmp_cache" / f"{vid}.mp3"
                if cached.exists():
                    mp3_path = str(cached)
                    db.update_video_status(vid, "downloaded", mp3_path=mp3_path)
                else:
                    db.update_video_status(vid, "download_failed", error="音频丢失")
                    continue

            db.update_video_status(vid, "transcribing")
            db.set_config(f"transcribing:{vid}", str(time.time()))
            logger.info(f"开始转写 {vid} ({mp3_path})...")
            db.log("worker", "info", f"开始转写 {vid}")

            result = transcribe(mp3_path, model_name, compute_type)

            if result.get("ok"):
                text = result["text"]
                word_count = result["word_count"]

                # 保存 Markdown
                account = v.get("account", "unknown")
                from datetime import datetime
                account_dir = output_dir / account
                account_dir.mkdir(parents=True, exist_ok=True)

                desc = v.get("desc", "") or vid
                short_desc = "".join(c for c in desc[:30] if c.isalnum() or c in " _-")
                short_desc = short_desc.strip() or "no_title"
                filename = f"{datetime.now().strftime('%Y-%m-%d')}_{short_desc}_{vid}.md"
                filepath = account_dir / filename

                content = f"""---
source: douyin
account: {account}
video_id: {vid}
date: {datetime.now().strftime('%Y-%m-%d %H:%M')}
word_count: {word_count}
---

# {desc or vid}

{text}
"""
                filepath.write_text(content, encoding="utf-8")

                db.update_video_status(vid, "done", output_path=str(filepath),
                                       word_count=word_count)
                logger.info(f"✅ {vid}: 转写完成 ({word_count}字)")

                # 清理临时音频文件
                for ext in [".mp3", ".mp4"]:
                    p = Path(mp3_path).parent / f"{vid}{ext}"
                    p.unlink(missing_ok=True)

            else:
                error = result.get("error", "未知错误")
                logger.warning(f"❌ {vid}: 转写失败 - {error}")
                db.increment_retry(vid)
                if v["retry_count"] >= 2:
                    db.update_video_status(vid, "trans_failed", error=error)
                else:
                    db.update_video_status(vid, "downloaded", error=error)

            db.set_config(f"transcribing:{vid}", "")

        except KeyboardInterrupt:
            logger.info("🛑 Worker 中断")
            break
        except Exception as e:
            logger.error(f"Worker 异常: {e}")
            db.log("worker", "error", str(e))
            time.sleep(10)

    db.close()
    pid_path.unlink(missing_ok=True)


def handle_sigterm(sig, frame):
    logger.info("收到 SIGTERM，退出")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    main_loop()
