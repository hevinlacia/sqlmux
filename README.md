# sqlmux

把 Archery 只读查询封装成标准 MySQL wire protocol 服务:数据库软件(DBeaver/DataGrip/JDBC 客户端)像连 test 直连库一样连 UAT/PRO,实际后端转发 Archery API——天然只读、有 Archery 审计、写操作显式报错。

需求:`REQ-268-sqlmux-archery-jdbc-gateway`(Agent Panel, project=personal)。

## 为什么是这个形态

- JDBC 是驱动 API 不是网络协议;数据库软件连 MySQL 实际说的是 **MySQL wire protocol**(Connector/J 等 Type 4 驱动)。
- SEA UAT/PRO 数据库 VPC 从开发机不可达(已实测),查询通道只有 Archery;网关不做任何数据库直连。
- 与 CLI 封装相比,MySQL 协议网关让**所有现成数据库工具**零插件复用:连接池、结果表格、内省浏览器全部白拿。

## 环境与端口

| env | 端口 | 后端 Archery instance | 默认库 |
|---|---|---|---|
| uat-cn | 13306 | cwh-uat-wms-polarx | wms |
| uat-sea | 13307 | cwhsea-uat-wms-polarx | wms |
| pro-cn | 13308 | pro-cwh-wms-mysql | wms |
| pro-sea | 13309 | cwhsea-pro-wms-mysql | wms |

全部仅绑定 127.0.0.1。默认 `serve --envs` 决定监听哪些环境;生产 unit(`deploy/sqlmux.service`)四环境常驻(2026-11-09 用户拍板),PRO 查询走个人 Archery 账号、平台有审计。

## 运行

```bash
uv sync
uv run sqlmux envs                          # 查看环境/端口映射
uv run sqlmux serve --envs uat-cn,uat-sea   # 启动网关
```

## 连接示例(DataGrip / DBeaver / JDBC)

驱动:MySQL Connector/J(8.x)。网关接受任意用户名(建议填 `sqlmux`),密码未设置时留空。

```text
Host: 127.0.0.1        Port: 13307        Database: wms
jdbc:mysql://127.0.0.1:13307/wms?useSSL=false&allowPublicKeyRetrieval=true
```

- 不要开 `useServerPrepStmts`(默认就是 false);驱动属性保持默认即可。
- `SHOW`/`DESC`/`EXPLAIN`/`SELECT` 直通;`INSERT/UPDATE/DELETE/DDL` 返回 ERR,消息含正确路径指引。
- `USE db` / `SET ...` 由网关本地处理(会话状态),不转发。
- 结果可能被 Archery 平台行数上限截断(网关已按语句类型提升 limit),超长结果集以平台返回为准。

## 安全边界

- 只读白名单写死(`src/sqlmux/guard.py`),无 bypass。
- 不存储数据库凭证;Archery 认证复用 `mysql-archery-query` 链路(Chrome 登录态 > env cookie > hub 密钥库)。
- PRO 环境显式开启才监听;日志不含凭证。

## 验证

```bash
just check          # fmt + lint + test
# 手动联调(pymysql):
uv run --with pymysql python -c "..."
```

联调步骤与 Connector/J 验证见 AGENTS.md 与 REQ-268 test.md。
