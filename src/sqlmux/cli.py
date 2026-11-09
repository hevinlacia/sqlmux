"""sqlmux CLI 入口。

serve: 按环境启动 MySQL 协议网关(loopback 多端口)
envs : 列出环境与端口映射
"""

from __future__ import annotations

import argparse

from .config import env_targets, known_envs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlmux",
        description="MySQL wire protocol gateway fronting Archery read-only queries",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start gateway listeners")
    serve.add_argument(
        "--envs",
        default=",".join(["uat-cn", "uat-sea"]),
        help="comma-separated env list (default: uat-cn,uat-sea; PRO must be explicit)",
    )

    sub.add_parser("envs", help="list known environments")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "envs":
        for target in env_targets(known_envs()):
            print(
                f"{target.env:8s} port={target.port} "
                f"instance={target.instance_name} db={target.db_name}"
            )
        return 0

    if args.command == "serve":
        targets = env_targets([e.strip() for e in args.envs.split(",") if e.strip()])
        from .server import serve_targets

        serve_targets(targets)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
