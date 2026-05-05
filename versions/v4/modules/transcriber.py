"""
v4 — Transcriber 模块（管理器）

负责:
1. 启动/监控持久 Worker 进程（模型只加载一次）
2. 重置卡在 transcribing 的超时任务
3. 监控 Worker 存活，崩溃后自动重启
"""

import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import signal
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

logger = logging.getLogger("transcribe")


class TranscriberManager:
    """管理持久 Worker 进程"""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.project_root = Path(config.get("general", "project_root", default="."))
        self.transcribe_timeout = config.get("transcriber", "timeout", default=600)
        self._worker_proc = None
        self._worker_pid_path = self.project_root / "database" / "worker.pid"
        self._concurrency = config.get("transcriber", "concurrency", default=2)

    @property
    def worker_alive(self) -> bool:
        """检查 Worker 是否存活"""
        # 检查已知 PID
        if self._worker_proc and self._worker_proc.poll() is None:
            return True
        # 检查 PID 文件
        if self._worker_pid_path.exists():
            try:
                pid = int(self._worker_pid_path.read_text().strip())
                os.kill(pid, 0)
                return True
            except (ValueError, OSError, ProcessLookupError):
                self._worker_pid_path.unlink(missing_ok=True)
        return False

    def ensure_worker(self):
        """确保 Worker 进程在运行"""
        if self.worker_alive:
            return

        logger.info("启动持久 Worker 进程...")
        worker_path = Path(__file__).parent / "transcriber_worker.py"

        try:
            self._worker_proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # 设置进程组，便于一起清理
                start_new_session=True,
            )
            logger.info(f"Worker 启动 PID={self._worker_proc.pid}")
            self.db.log("transcribe", "info",
                        f"Worker 启动 PID={self._worker_proc.pid}")

            # 等待 10 秒确认存活
            time.sleep(10)
            if self._worker_proc.poll() is not None:
                logger.error("Worker 启动后立即退出！")
                self.db.log("transcribe", "error",
                            f"Worker 启动失败 rc={self._worker_proc.returncode}")
                self._worker_proc = None

        except Exception as e:
            logger.error(f"启动 Worker 失败: {e}")
            self.db.log("transcribe", "error", str(e))

    def run_once(self):
        """管理循环：保活 + 超时重置"""
        # 1. 确保 Worker 在运行
        self.ensure_worker()

        # 2. 重置卡在 transcribing 超过 timeout 的任务
        # timeout 秒后仍处于 transcribing 说明 worker 挂了
        stale_minutes = max(self.transcribe_timeout // 60 + 1, 15)
        reset = self.db.reset_stale_status(
            "transcribing", "downloaded",
            older_than_minutes=stale_minutes,
        )
        if reset:
            logger.warning(f"重置 {reset} 个超时转写任务")

        # 3. 重置卡在 publishing 超过 5 分钟的任务
        self.db.reset_stale_status(
            "publishing", "done",
            older_than_minutes=5,
        )

        # 4. 重置卡在 downloading 超过 5 分钟的任务
        self.db.reset_stale_status(
            "downloading", "scanned",
            older_than_minutes=5,
        )

        # 5. 检查 Worker 最近是否在干活
        stats = self.db.get_stats()
        by_status = stats.get("by_status", {})
        transcribing = by_status.get("transcribing", 0)
        downloaded = by_status.get("downloaded", 0)

        if downloaded > 0 and transcribing == 0 and self.worker_alive:
            logger.info(f"Worker 空闲: {downloaded} 个待转写")
        elif transcribing > 0:
            logger.info(f"Worker 工作中: {transcribing} 个正在转写")

    def stop_worker(self):
        """停止 Worker 进程"""
        if self._worker_proc:
            logger.info("停止 Worker...")
            try:
                os.killpg(os.getpgid(self._worker_proc.pid), signal.SIGTERM)
                self._worker_proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(os.getpgid(self._worker_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self._worker_proc = None
            logger.info("Worker 已停止")

        # 清理 PID 文件
        self._worker_pid_path.unlink(missing_ok=True)


def main():
    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [transcribe] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "transcriber.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    db = Database(config.db_path)
    manager = TranscriberManager(db, config)

    logger.info("🚀 Transcriber 管理器启动")
    db.log("transcribe", "info", "管理器启动")

    try:
        while True:
            try:
                manager.run_once()
                time.sleep(15)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"❌ 异常: {e}")
                db.log("transcribe", "error", str(e))
                time.sleep(30)
    finally:
        manager.stop_worker()
        db.close()


if __name__ == "__main__":
    main()
