# AGENTS.md — sqlmux

MySQL wire protocol 网关:对外说标准 MySQL 协议(127.0.0.1 多端口),对内转发 Archery 只读查询 API,让 DBeaver/DataGrip 等数据库软件像连 test 直连库一样查 UAT/PRO。需求背景见 Agent Panel `REQ-268-sqlmux-archery-jdbc-gateway`。

## 定位与红线

- **只读网关**:语句预检白名单(SELECT/SHOW/DESC/DESCRIBE/EXPLAIN)写死在 `guard.py`,写/DDL/多语句一律回 MySQL ERR 包并带指引信息,无 bypass 开关。
- **不直连数据库**:后端只有 Archery HTTP API(认证链复用 mysql-archery-query:Chrome cookie > env cookie > hub 密钥库账密)。任何"拿到 DB 凭证直连"的实现都是违规。
- **仅绑定 127.0.0.1**:禁止监听 0.0.0.0/对外网卡;PRO 环境端口默认不启动,需 CLI 显式 `--envs` 传入。
- 凭证不落盘、不打印;日志不含 cookie/密码。

## 运行

纯标准库、零依赖，有 python3 即可运行：

```bash
PYTHONPATH=src python3 -m sqlmux envs                          # 列出环境与端口映射
PYTHONPATH=src python3 -m sqlmux serve --envs uat-cn,uat-sea   # PRO: 显式加 pro-cn,pro-sea
```

端口:13306=uat-cn 13307=uat-sea 13308=pro-cn 13309=pro-sea(`SQLMUX_BASE_PORT` 可整体偏移)。**端口与环境固定绑定**(`ENV_PORT_ORDER`),不随 `--envs` 顺序变化。

## 结构

```text
src/sqlmux/
├── cli.py       # argparse 入口(serve/envs)
├── config.py    # env→port/instance/db 映射,读 instances.json
├── wire.py      # MySQL 协议:handshake/OK/ERR/resultset 编解码
├── guard.py     # 语句预检:白名单/SET·USE 拦截/多语句检测
├── archery.py   # Archery client:session 缓存+/query/+双形态错误
└── server.py    # asyncio 多端口 server,连接级 db_name 状态
tests/           # 协议与预检单测(不依赖真实 Archery)
```

## 命令

`just check` = fmt + lint + test。协议改动后最小验证:`just test`;联调验证见 README「验证」节。

## 已知边界

- 第一版只实现 COM_QUERY 文本协议(Connector/J 默认 useServerPrepStmts=false);COM_STMT_PREPARE 回 ERR 提示文本协议。
- Archery limit_num 截断在网关侧提升(SHOW/内省大值),无法完全消除平台截断——README 说明。
- information_schema 内省查询透传 Archery,不兼容行为按 ERR 透传处理。
