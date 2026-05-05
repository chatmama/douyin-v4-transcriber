"""
v4 — 配置加载模块

从 TOML 配置文件加载全局配置。
"""

import os
import tomllib
from pathlib import Path
from typing import Any


class Config:
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.environ.get(
                "V4_CONFIG",
                str(Path(__file__).parent.parent / "config" / "config.toml"),
            )
        self.path = Path(config_path)
        self._data: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.path}")
        with open(self.path, "rb") as f:
            self._data = tomllib.load(f)

    def get(self, *keys: str, default: Any = None) -> Any:
        """按层级取配置值"""
        d = self._data
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
                if d is None:
                    return default
            else:
                return default
        return d if d is not None else default

    @property
    def db_path(self) -> str:
        root = self.get("general", "project_root", default=".")
        rel = self.get("database", "path", default="database/pipeline.db")
        return str(Path(root) / rel) if not os.path.isabs(rel) else rel

    @property
    def log_dir(self) -> str:
        root = self.get("general", "project_root", default=".")
        rel = self.get("general", "log_dir", default="logs")
        return str(Path(root) / rel) if not os.path.isabs(rel) else rel

    def get_accounts(self) -> list:
        """获取账号列表: [(name, sec_uid), ...]"""
        entries = self.get("accounts", "entries", default=[])
        return [(e["name"], e["sec_uid"]) for e in entries if e.get("enabled", True)]
