"""env → port / Archery instance 映射。

instances.json 与 mysql-archery-query skill 同构(instance_name + db_name),
本文件只维护映射,凭证一律走认证链,不在此存储。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import os

DEFAULT_BASE_PORT = int(os.environ.get("SQLMUX_BASE_PORT", "13306"))

# PRO 环境默认不启动,serve --envs 显式列出才监听。
DEFAULT_ENABLED = ["uat-cn", "uat-sea"]

INSTANCES_FILE = Path(__file__).resolve().parent / "instances.json"


@dataclass(frozen=True)
class EnvTarget:
    env: str
    port: int
    instance_name: str
    db_name: str
    is_pro: bool


def _load_instances() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(INSTANCES_FILE.read_text(encoding="utf-8"))
        return data.get("environments", {})
    except (OSError, json.JSONDecodeError):
        return {}


def env_targets(envs: list[str] | None = None) -> list[EnvTarget]:
    """按 env 名生成监听目标;envs=None 时用 DEFAULT_ENABLED。"""
    instances = _load_instances()
    names = envs if envs is not None else DEFAULT_ENABLED
    targets: list[EnvTarget] = []
    for idx, env in enumerate(names):
        cfg = instances.get(env)
        if not cfg:
            raise SystemExit(
                f"unknown env '{env}'; valid: {', '.join(sorted(instances))}"
            )
        targets.append(
            EnvTarget(
                env=env,
                port=DEFAULT_BASE_PORT + idx,
                instance_name=cfg["instance_name"],
                db_name=cfg.get("db_name", "wms"),
                is_pro=env.startswith("pro-"),
            )
        )
    return targets


def known_envs() -> list[str]:
    return sorted(_load_instances())
