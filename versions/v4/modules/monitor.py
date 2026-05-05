#!/usr/bin/env python3
"""
v4 — Monitor 完整监控模块

监控内容:
  - 主机状态 (uptime, load, CPU, 内存, 磁盘, 网络)
  - 系统状态 (进程数, 连接数)
  - GPU 状态 (利用率, 显存, 温度)
  - 管道状态 (扫描/下载/转写/发布的各阶段统计)
  - 每个博主统计 (视频数, 已完成, 字数)
  - 总完成度 (进度对比/趋势)
  - Chrome 状态
  - WordPress 状态

两种运行模式:
  1. 持续监控 (每N秒输出一行)
  2. 一键报告 (--report 输出完整状态)
"""

import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import socket
import psutil
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.config import Config
from lib.db import Database

logger = logging.getLogger("monitor")


class SystemCollector:
    """系统指标采集"""

    @staticmethod
    def cpu() -> dict:
        try:
            return {
                "percent": psutil.cpu_percent(interval=1),
                "count": psutil.cpu_count(),
                "load_avg": [round(x, 2) for x in os.getloadavg()],
            }
        except Exception:
            return {"percent": "N/A", "count": "N/A", "load_avg": "N/A"}

    @staticmethod
    def memory() -> dict:
        try:
            mem = psutil.virtual_memory()
            return {
                "total_gb": round(mem.total / 1024**3, 1),
                "used_gb": round(mem.used / 1024**3, 1),
                "percent": mem.percent,
                "available_gb": round(mem.available / 1024**3, 1),
            }
        except Exception:
            return {"total_gb": "N/A", "used_gb": "N/A", "percent": "N/A"}

    @staticmethod
    def disk() -> dict:
        """磁盘使用"""
        result = {}
        for path in ["/", "/home"]:
            try:
                d = psutil.disk_usage(path)
                result[path] = {
                    "total_gb": round(d.total / 1024**3, 1),
                    "used_gb": round(d.used / 1024**3, 1),
                    "free_gb": round(d.free / 1024**3, 1),
                    "percent": d.percent,
                }
            except Exception:
                pass
        return result

    @staticmethod
    def network() -> dict:
        try:
            net = psutil.net_io_counters()
            return {
                "bytes_sent_mb": round(net.bytes_sent / 1024 / 1024, 1),
                "bytes_recv_mb": round(net.bytes_recv / 1024 / 1024, 1),
                "packets_sent": net.packets_sent,
                "packets_recv": net.packets_recv,
            }
        except Exception:
            return {"bytes_sent": "N/A"}

    @staticmethod
    def connections() -> dict:
        """网络连接统计"""
        try:
            conns = psutil.net_connections()
            total = len(conns)
            established = sum(1 for c in conns if c.status == "ESTABLISHED")
            listening = sum(1 for c in conns if c.status == "LISTEN")
            tcp = sum(1 for c in conns if c.type == socket.SOCK_STREAM)
            udp = sum(1 for c in conns if c.type == socket.SOCK_DGRAM)
            return {
                "total": total,
                "established": established,
                "listening": listening,
                "tcp": tcp,
                "udp": udp,
            }
        except Exception:
            return {"total": "N/A"}

    @staticmethod
    def uptime() -> str:
        try:
            with open("/proc/uptime") as f:
                uptime_seconds = float(f.read().split()[0])
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            mins = int((uptime_seconds % 3600) // 60)
            return f"{days}d {hours}h {mins}m"
        except Exception:
            return "N/A"

    @staticmethod
    def processes() -> dict:
        try:
            procs = list(psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]))
            return {
                "total": len(procs),
                "pipeline": sum(1 for p in procs if "python3" in (p.info.get("name") or "")),
            }
        except Exception:
            return {"total": "N/A"}

    @staticmethod
    def hostname() -> str:
        return socket.gethostname()

    @staticmethod
    def gpu() -> dict:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                    "--format=csv,noheader",
                ],
                capture_output=True, text=True, timeout=5,
            )
            gpus = []
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(", ")]
                if len(parts) >= 6:
                    gpus.append({
                        "index": parts[0],
                        "name": parts[1],
                        "util": parts[2],
                        "mem_used": parts[3],
                        "mem_total": parts[4],
                        "temp": parts[5],
                        "power": parts[6] if len(parts) > 6 else "N/A",
                    })
            return {"count": len(gpus), "cards": gpus}
        except Exception as e:
            return {"count": 0, "error": str(e)}

    @staticmethod
    def chrome() -> dict:
        try:
            import urllib.request, json
            resp = urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=3)
            data = json.loads(resp.read().decode())
            tabs_resp = urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3)
            tabs = json.loads(tabs_resp.read().decode())
            return {
                "alive": True,
                "version": data.get("Browser", "Unknown"),
                "tabs": len(tabs),
            }
        except Exception:
            return {"alive": False, "version": "N/A", "tabs": 0}

    @staticmethod
    def cache_size(path: str) -> dict:
        """缓存目录大小"""
        p = Path(path)
        if not p.exists():
            return {"size_gb": 0, "files": 0}
        try:
            result = subprocess.run(
                ["du", "-sb", str(p)], capture_output=True, text=True, timeout=5
            )
            total_bytes = int(result.stdout.split()[0])
            files = len(list(p.rglob("*")))
            return {
                "size_gb": round(total_bytes / 1024**3, 2),
                "size_mb": round(total_bytes / 1024**2, 1),
                "files": files,
            }
        except Exception:
            return {"size_gb": "N/A", "files": "N/A"}

    @staticmethod
    def output_size(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"size_mb": 0, "files": 0}
        try:
            result = subprocess.run(
                ["du", "-sb", str(p)], capture_output=True, text=True, timeout=5
            )
            total_bytes = int(result.stdout.split()[0])
            files = len(list(p.rglob("*.md")))
            return {
                "size_mb": round(total_bytes / 1024**2, 1),
                "files": files,
            }
        except Exception:
            return {"size_mb": "N/A", "files": "N/A"}


class PipelineCollector:
    """管道状态采集"""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.root = Path(config.get("general", "project_root", default="."))

    def status_summary(self) -> dict:
        """管道各阶段统计"""
        return {
            "videos": self.db.count_by_status(),
            "accounts": self.db.get_account_stats(),
            "total_words": self.db.get_total_word_count(),
            "timeline": self.db.get_timeline_stats(24),
        }

    def modules_running(self) -> dict:
        """检查各模块进程状态"""
        modules = {
            "scanner": "scanner.py",
            "url_fetcher": "url_fetcher.py",
            "downloader": "downloader.py",
            "transcriber": "transcriber.py",
            "publisher": "publisher.py",
            "monitor": "monitor.py",
            "cleaner": "cleaner.py",
        }
        result = {}
        for name, script in modules.items():
            try:
                pid = subprocess.run(
                    ["pgrep", "-f", f"python3.*modules/{script}"],
                    capture_output=True, text=True, timeout=3,
                )
                pids = pid.stdout.strip().split()
                result[name] = {
                    "running": len(pids) > 0,
                    "pids": pids,
                }
            except Exception:
                result[name] = {"running": False, "pids": []}
        return result

    def per_account_progress(self) -> list:
        """每个博主的完成度对比"""
        return self.db.get_account_stats()

    def progress_percentage(self) -> dict:
        """完成度百分比"""
        by_status = self.db.count_by_status()
        total = sum(by_status.values()) or 1
        done = by_status.get("done", 0) + by_status.get("published", 0)
        failed = sum(by_status.get(s, 0) for s in
                     ["url_failed", "download_failed", "trans_failed", "pub_failed"])
        pending = by_status.get("pending", 0)
        in_progress = sum(by_status.get(s, 0) for s in
                          ["scanned", "downloading", "downloaded", "transcribing", "publishing"])

        return {
            "total": sum(by_status.values()),
            "done": done,
            "done_pct": round(done / total * 100, 1) if total else 0,
            "pending": pending,
            "pending_pct": round(pending / total * 100, 1) if total else 0,
            "in_progress": in_progress,
            "in_progress_pct": round(in_progress / total * 100, 1) if total else 0,
            "failed": failed,
            "failed_pct": round(failed / total * 100, 1) if total else 0,
        }


class Monitor:
    """综合监控器"""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.root = Path(config.get("general", "project_root", default="."))
        self.sys = SystemCollector()
        self.pipeline = PipelineCollector(db, config)

    def report_full(self) -> dict:
        """完整状态报告"""
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "host": {
                "hostname": self.sys.hostname(),
                "uptime": self.sys.uptime(),
                "cpu": self.sys.cpu(),
                "memory": self.sys.memory(),
                "disk": self.sys.disk(),
                "network": self.sys.network(),
                "connections": self.sys.connections(),
                "processes": self.sys.processes(),
            },
            "gpu": self.sys.gpu(),
            "chrome": self.sys.chrome(),
            "pipeline": {
                "modules": self.pipeline.modules_running(),
                "progress": self.pipeline.progress_percentage(),
                "accounts": self.pipeline.per_account_progress(),
                "total_words": self.db.get_total_word_count(),
            },
            "storage": {
                "cache": self.sys.cache_size(str(self.root / "tmp_cache")),
                "output": self.sys.output_size(str(self.root / "output")),
            },
        }

    def format_report(self, report: dict = None) -> str:
        """格式化为可读文本"""
        if report is None:
            report = self.report_full()

        lines = []
        lines.append("=" * 60)
        lines.append(f"📊 抖音转写流水线 — 状态报告")
        lines.append(f"   时间: {report['timestamp']}")
        lines.append(f"   主机: {report['host']['hostname']} (运行 {report['host']['uptime']})")
        lines.append("=" * 60)

        # CPU / 内存
        cpu = report["host"]["cpu"]
        mem = report["host"]["memory"]
        lines.append(f"\n💻 系统资源")
        lines.append(f"   CPU: {cpu.get('percent','N/A')}% ({cpu.get('load_avg','N/A')})")
        lines.append(f"   内存: {mem.get('used_gb','N/A')}/{mem.get('total_gb','N/A')}GB ({mem.get('percent','N/A')}%)")
        lines.append(f"   网络: ↑{report['host']['network'].get('bytes_sent_mb','N/A')}MB ↓{report['host']['network'].get('bytes_recv_mb','N/A')}MB")
        lines.append(f"   连接: 共{report['host']['connections'].get('total','N/A')} (已建立{report['host']['connections'].get('established','N/A')})")
        lines.append(f"   进程: 共{report['host']['processes'].get('total','N/A')} (管道{report['host']['processes'].get('pipeline','N/A')})")

        # 磁盘
        lines.append(f"\n💾 磁盘")
        for mp, d in report["host"]["disk"].items():
            lines.append(f"   {mp}: {d.get('used_gb','N/A')}/{d.get('total_gb','N/A')}GB ({d.get('percent','N/A')}%)")

        # GPU
        lines.append(f"\n🎮 GPU")
        gpu = report["gpu"]
        for card in gpu.get("cards", []):
            lines.append(f"   [{card['index']}] {card['name']} | "
                         f"核心{card['util']} | 显存{card['mem_used']}/{card['mem_total']} | "
                         f"温度{card['temp']} | 功耗{card['power']}")

        # Chrome
        chrome = report["chrome"]
        lines.append(f"\n🌐 Chrome: {'✅ 在线' if chrome.get('alive') else '❌ 离线'}")
        if chrome.get("alive"):
            lines.append(f"   {chrome.get('version')} | 标签页: {chrome.get('tabs')}")

        # 管道模块
        lines.append(f"\n⚙️ 管道模块")
        for mod_name, info in report["pipeline"]["modules"].items():
            icon = "✅" if info["running"] else "❌"
            pids = ",".join(info["pids"]) if info["pids"] else ""
            lines.append(f"   {icon} {mod_name:15s} {pids}")

        # 完成进度
        prog = report["pipeline"]["progress"]
        lines.append(f"\n📈 完成进度")
        lines.append(f"   总计: {prog['total']} 个视频")
        lines.append(f"   ✅ 已完成: {prog['done']} ({prog['done_pct']}%)")
        lines.append(f"   ⏳ 进行中: {prog['in_progress']} ({prog['in_progress_pct']}%)")
        lines.append(f"   📋 待处理: {prog['pending']} ({prog['pending_pct']}%)")
        lines.append(f"   ❌ 已失败: {prog['failed']} ({prog['failed_pct']}%)")

        # 详细状态分布
        lines.append(f"\n📋 各状态分布")
        pipeline_stats = report["pipeline"].get("pipeline_stats", {})
        # get from report or re-fetch
        videos_summary = self.db.count_by_status()
        for status in ["pending", "scanned", "url_failed", "downloading", "downloaded",
                       "download_failed", "transcribing", "done", "publishing",
                       "published", "pub_failed"]:
            cnt = videos_summary.get(status, 0)
            if cnt > 0:
                lines.append(f"   {status:20s} {cnt}")

        # 每个博主
        lines.append(f"\n👤 每个博主统计")
        total_words = 0
        for acct in report["pipeline"]["accounts"]:
            w = acct.get("total_words", 0)
            total_words += w
            lines.append(
                f"   {acct['name']:12s} | 共{acct['total_videos']}个 | "
                f"已完成{acct['done_videos']}个 | "
                f"转写{self._fmt_words(w)}字"
            )
        lines.append(f"   合计: {self._fmt_words(total_words)}字")

        # 转写字数
        lines.append(f"\n📝 总转写字数: {self._fmt_words(report['pipeline']['total_words'])}")

        # 存储
        cache = report["storage"]["cache"]
        output = report["storage"]["output"]
        lines.append(f"\n📦 存储")
        lines.append(f"   缓存: {cache.get('size_mb','N/A')}MB ({cache.get('files','N/A')}个文件)")
        lines.append(f"   输出: {output.get('size_mb','N/A')}MB ({output.get('files','N/A')}个文件)")

        lines.append("\n" + "=" * 60)
        return "\n".join(lines)

    def format_short(self, report: dict = None) -> str:
        """简短一行状态"""
        if report is None:
            report = self.report_full()

        prog = report["pipeline"]["progress"]
        gpu = report["gpu"]
        gpu_str = gpu["cards"][0]["util"] if gpu.get("cards") else "N/A"
        mem = report["host"]["memory"]
        cpu = report["host"]["cpu"]

        return (
            f"📊 {prog['total']}个 | "
            f"✅{prog['done']}({prog['done_pct']}%) | "
            f"⏳{prog['in_progress']} | "
            f"📋{prog['pending']} | "
            f"❌{prog['failed']} | "
            f"CPU{cpu.get('percent','N/A')}% | "
            f"MEM{mem.get('used_gb','N/A')}/{mem.get('total_gb','N/A')}G | "
            f"GPU{gpu_str}"
        )

    @staticmethod
    def _fmt_words(n: int) -> str:
        """格式化字数"""
        if n >= 10000:
            return f"{n/10000:.1f}万"
        elif n >= 1000:
            return f"{n/1000:.1f}千"
        return str(n)

    def run_once(self, verbose: bool = False):
        """执行一轮监控"""
        report = self.report_full()
        if verbose:
            text = self.format_report(report)
            for line in text.split("\n"):
                logger.info(line)
        else:
            logger.info(self.format_short(report))

        # 写入 DB 日志
        prog = report["pipeline"]["progress"]
        self.db.log("monitor", "info",
                    f"完成度 {prog['done_pct']}% | "
                    f"已完成{prog['done']} | 待处理{prog['pending']} | 失败{prog['failed']}")

        return report


def main():
    import argparse

    parser = argparse.ArgumentParser(description="抖音转写管道监控器")
    parser.add_argument("--report", action="store_true", help="输出完整报告后退出")
    parser.add_argument("--short", action="store_true", help="输出简短一行")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--interval", type=int, default=30, help="监控间隔(秒)")
    args = parser.parse_args()

    config = Config()
    log_dir = config.log_dir
    os.makedirs(log_dir, exist_ok=True)

    db = Database(config.db_path)
    monitor = Monitor(db, config)

    # 一键报告模式
    if args.report or args.short or args.json:
        report = monitor.report_full()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.short:
            print(monitor.format_short(report))
        else:
            print(monitor.format_report(report))
        db.close()
        return

    # 持续监控模式
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monitor] %(message)s",
        handlers=[
            RotatingFileHandler(os.path.join(log_dir, "monitor.log"),
                                encoding="utf-8", maxBytes=10*1024*1024),
            logging.StreamHandler(),
        ],
    )

    logger.info("🚀 Monitor 完整监控模块启动")
    logger.info(f"  系统: {SystemCollector.hostname()}, "
                f"运行 {SystemCollector.uptime()}")
    db.log("monitor", "info", "完整监控模块启动")

    interval = args.interval

    while True:
        try:
            monitor.run_once(verbose=False)
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("🛑 中断")
            break
        except Exception as e:
            logger.error(f"❌ 异常: {e}")
            time.sleep(30)

    db.close()


if __name__ == "__main__":
    main()
