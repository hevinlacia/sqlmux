"""`python3 -m sqlmux` 入口(零第三方依赖,便于裸 python 环境直接运行)。"""

from .cli import main

raise SystemExit(main())
