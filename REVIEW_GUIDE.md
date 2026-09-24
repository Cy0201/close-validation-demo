# CloseWren 整体评审与修改指导

评审日期：2026-09-24　评审范围：仓库内全部源码（`server.py`、`engine_adapter.py`、`query_worker.py`、`import_data.py`、`periods.py`、`launch.py`、`static/`、`test_demo.py`）及四份文档。

> 使用方式：第 1 节给出总评，第 2 节是按优先级排好的问题清单，第 3 节逐项给出「问题 → 证据 → 修改要求 → 验收标准 → 可直接交给 AI 编码助手的指导词」。第 4 节专门讲如何把 WrenAI 语义层用深用透，第 5–8 节是架构方向、测试、执行节奏和工程习惯。
>
> 所有修改都必须遵守 `AGENTS.md`：结算前、结算后各自独立校验，不做前后对比和自动映射；不预设业务规则；AI 不可用时手工 SQL 和已存规则仍能完整运行；测试规则不得留在用户状态库；改动执行、数据或状态逻辑后运行 `.venv\Scripts\python.exe -m unittest test_demo -v`。

---

## 1. 总评

### 做得好的地方（保留，不要在重构中丢掉）

1. **安全边界设计意识强**：查询走独立子进程并有超时；DuckDB 以只读方式 ATTACH；sqlglot 语法树白名单加上 Wren `dry_plan`，展开后还会再检查一遍；Host/Origin 校验；CSV 导出做了公式注入防护；前端基本都用 `textContent` 渲染，没有 XSS 面。
2. **数据指纹贯穿全程**：每次运行记录所有相关表的 sha256，数据刷新后旧结果不能再导出，也不能用于关账。这是结账类系统最关键的一条不变量，做对了。
3. **导入器细节扎实**：自己解析 OOXML 的稀疏单元格和共享字符串；重复列名加后缀而不是丢弃；金额先扫描精度再建 DECIMAL，超出精度的保留原文，不做静默舍入；公式缺少缓存值时明确报错。
4. **账期关账链路完整**：业务确认人、差异说明、不可覆盖的版本化归档、ZIP 下载、管理员重开都已打通，门禁检查项也比较全。
5. **AI 定位克制**：只产出草稿，服务端校验字段契约和 Wren 规划，不自动保存也不自动运行，符合“手工 SQL 为主入口”的要求。

### 主要问题（概括）

| 维度 | 结论 |
|---|---|
| WrenAI 使用深度 | 只用到 `dry_plan` 做表范围检查；语义层（描述、关系、计算字段、视图、访问控制、知识库）完全没用上；金额列类型声明 wren-core 无法识别，这个问题被“只做透传查询”的用法掩盖了（第 4 节）。 |
| 安全 | 有 3 个需要马上处理的缺陷：局域网共享模式没有鉴权（可以窃取模型密钥、关停服务、读取主机上任意 Excel）；模型调用关闭了 TLS 证书校验；DuckDB 外部访问实际没有关闭（文档写的是已关闭）。 |
| 结果正确性 | 单流程运行会沿用其他流程的旧结果，却把本次运行标成新方案版本，可能**用过期结果关账**；xlsx 与 xls 对日期和布尔值的解析不一致；数据刷新与查询之间存在竞态，运行记录可能写入与实际读取数据不符的指纹。 |
| 稳定性 | 后台运行与前台查询抢同一个非阻塞信号量，节点会被随机标记为“错误”；GET 接口遇到未预期的异常时直接断开连接，不返回错误。 |
| 可维护性 | `server.py` 1,576 行，用一长串 if 做路由；`app.js` 约 106KB，是压缩风格的单行代码，`renderPlan` 在 3 个文件里被覆盖 4 次；AI 的 `generate` 与 `clarify` 约 80% 代码重复；存在死代码。 |
| 可移植性与测试 | 首次启动依赖上级目录下两个写死文件名的样例文件；导入时金额列和期间列按固定中文列名识别；测试依赖本机真实数据和固定行数，无法在 CI 或其他电脑运行；账期、局域网共享、导入器都没有测试。 |
| 文档一致性 | README 与 ARCHITECTURE 有多处描述与代码不符（监听地址、外部访问、函数白名单、Ctrl+Enter 执行、API 表等）。 |

整体判断：**功能打通、方向正确，可以作为 Demo 演示；在用于真实月结之前，必须先完成 P0 和 P1。**

---

## 2. 问题清单与优先级

- **P0**：安全或关账结论可能出错，下一个迭代必须完成。
- **P1**：数据正确性和稳定性，在正式试用月结前完成。
- **P2**：可维护性和可移植性，与功能开发交替进行。
- **P3**：体验和文档。

| 编号 | 优先级 | 标题 | 主要位置 |
|---|---|---|---|
| S1 | P0 | 局域网共享模式没有鉴权，高危接口对整个局域网开放 | `server.py:925` `trusted()`，`server.py:1194/1202/1226` |
| S2 | P0 | 模型调用关闭了 TLS 证书校验，并且是手写 HTTP 客户端 | `server.py:506–595` `call_model` |
| S3 | P0 | DuckDB 外部访问没有关闭；函数白名单没有生效 | `engine_adapter.py:14/175` |
| S4 | P1 | 服务监听 0.0.0.0，与文档中的“只监听 127.0.0.1”不符 | `server.py:1573` |
| C1 | P0 | 单流程运行沿用旧结果，可能用过期结果关账 | `server.py:161–171`，`periods.py:79–86` |
| C2 | P1 | 导入替换数据库与写 manifest 不是原子操作；刷新与运行可以并发 | `import_data.py:247–248`，`server.py:1292` |
| C3 | P1 | xlsx 日期被导成序列号、布尔值导成 1/0，与 xls 不一致 | `import_data.py:49–97` |
| C4 | P1 | 金额列和期间列按固定列名识别；期间格式不符时永远无法关账 | `import_data.py:19/205`，`periods.py:25/88` |
| C5 | P1 | 按文件 hash 跳过导入时没有考虑工作表或导入参数的变化 | `import_data.py:147` |
| C6 | P1 | 未归零明细的下钻假设聚合维度名等于明细列名 | `server.py:1080`、`1450–1490` |
| R1 | P1 | 后台运行用非阻塞方式获取查询槽位，会被前台操作挤掉并标为错误 | `server.py:605` |
| R2 | P1 | GET 接口只捕获 3 类异常，其余异常直接断开连接 | `server.py:1108` |
| R3 | P2 | 导出先写临时文件再整体读入内存，并且文件名可能冲突 | `server.py:1037/1086/1461` |
| R4 | P2 | 方案保存的版本检查存在竞态；方案没有历史版本 | `server.py:1300–1392` |
| M1 | P2 | `server.py` 需要拆分模块，改用路由表 | 整个文件 |
| M2 | P2 | 前端需要去掉压缩风格和猴子补丁式覆盖 | `static/app.js`、`drag-ui.js`、`periods.js` |
| M3 | P2 | AI 的 generate/clarify 代码重复；清理死代码 | `server.py:439–504`、`721–906` |
| M4 | P2 | 旧版 `/api/rules` 链路前端已不使用，测试却主要覆盖它 | `server.py:1499–1537` |
| P1x | P2 | 首次启动依赖上级目录的固定样例文件 | `launch.py:33`，`import_data.py:17–21` |
| T1 | P2 | 测试依赖真实数据和固定行数；数据目录无法隔离 | `test_demo.py`，`query_worker.py:21` |
| U1 | P3 | 界面没有即席 SQL 执行和单节点试运行入口 | `static/app.js` |
| U2 | P3 | 默认容差 0.001 在前后端 7 处写死，相当于预设了业务口径 | `server.py:1342`，`app.js` 多处 |
| D1 | P3 | 文档与实现不一致 | README、ARCHITECTURE |
| W0–W10 | P1–P3 | WrenAI 只用到了“表范围检查”这一层，语义层能力基本没有用上（详见第 4 节） | `engine_adapter.py:38–49`，`server.py:668–699` |

---

## 3. 逐项指导

### S1（P0）局域网共享模式没有鉴权

**问题**：管理员打开 `lan_share` 后，`trusted()`（`server.py:925`）对任何私网 IP 都放行，除 `/api/wren/admin/*` 外的所有接口都没有身份校验。我按代码路径推演，已确认以下攻击可行：

1. **窃取模型密钥**：局域网任意一台机器先 `POST /api/ai/config`，把 `base_url` 改成自己的服务器且不传 `api_key`。`save_config` 在 `server.py:351` 会保留原密钥。随后调用 `POST /api/ai/test`，服务就会把 `Authorization: Bearer <密钥>` 发给攻击者。
2. 调用 `POST /api/shutdown` 关停服务。
3. 调用 `POST /api/sources/connect` 把数据源指向主机上任意文件夹，读取其中的 Excel，再通过 SQL 查看内容。
4. 修改或删除方案、数据源配置和人工确认。

**修改要求**：

1. 把接口分成三级，在路由层统一判断，不要散落在各个 if 分支里：
   - `local_only`（只允许回环地址）：`/api/shutdown`、`/api/ai/config`、`/api/ai/import-ccswitch`、`/api/ai/ccswitch`、`/api/sources/*`、`/api/periods/*`、`/api/wren/admin/*`。
   - `lan_read`（局域网只读）：`/api/bootstrap`、结果查看、下载。
   - 其余写接口：局域网访问时必须携带访问令牌。
2. 打开局域网共享时由服务端生成随机访问令牌（`secrets.token_urlsafe(32)`），只在管理员界面展示一次。局域网客户端用 `Authorization: Bearer` 或 HttpOnly Cookie 携带令牌，服务端用 `secrets.compare_digest` 比较。
3. `save_config`：只要 `base_url` 发生变化，就不得沿用旧密钥，必须重新填写。
4. 在做到第 1、2 条之前，界面上的局域网共享开关应默认隐藏或禁用。

**验收标准**：新增测试，用 `unittest.mock` 模拟 `client_address=('192.168.1.20', x)` 并打开 lan_share，断言：`/api/shutdown`、`/api/ai/config`、`/api/sources/connect` 返回 403；不带令牌调用写接口返回 401；带令牌调用只读接口返回 200；修改 `base_url` 但不传 key 时，保存后 `has_api_key` 为 false。

**指导词**：

```
阅读 AGENTS.md 和 server.py 中 Handler.trusted()、do_GET、do_POST、save_config。
任务：为局域网共享模式补齐访问控制。
1. 在 Handler 中新增 access_level(path, method)，返回 'local_only' | 'lan_read' | 'lan_write'，按下列清单分类：
   local_only = /api/shutdown, /api/ai/config, /api/ai/import-ccswitch, /api/ai/ccswitch, /api/sources*, /api/periods/*, /api/wren/admin/*
   lan_read   = GET /api/bootstrap, /api/plan/run/status, /api/sources/status, /api/periods, /api/periods/archive, /api/periods/download, /api/plan-runs/*/download, 静态资源
   其余为 lan_write。
2. trusted() 保留现有 Host/Origin 校验。非回环客户端访问 local_only 返回 403；访问 lan_write 时必须携带 Authorization: Bearer <lan_token>，用 secrets.compare_digest 比较，否则返回 401。
3. /api/wren/admin/lan-share 开启时生成 lan_token 存入 admin_settings，并只在该接口的响应中返回；关闭时删除令牌。
4. save_config：新 base_url 与旧值不同且请求中没有 api_key 时，不沿用旧密钥。
5. 错误状态码要区分 400/401/403，不要统一返回 400。
6. 在 test_demo.py 新增测试类 LanAccessTests，mock client_address 与 lan_share_enabled，覆盖上述 403/401/200 分支和密钥不沿用。
不要改动查询、方案、账期逻辑。完成后运行 .venv\Scripts\python.exe -m unittest test_demo -v。
```

---

### S2（P0）模型调用关闭了 TLS 校验，并且是手写 HTTP 客户端

**问题**：`call_model`（`server.py:506`）用裸 socket 手写 HTTP/1.1 请求：

- 设置了 `ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE`（`server.py:544–545`）。密钥在一条**不校验证书**的连接上发送，同一网络里的中间人可以直接拿到。
- 不处理 `Transfer-Encoding: chunked`（许多网关默认使用），响应体会连同分块长度一起交给 `json.loads`，结果随机失败。
- 请求路径丢掉了 query string；没有响应大小上限；不支持代理。
- 注释写的是为了“规避 http.client 对非 ASCII body 的 latin-1 编码 bug”。这是误判：只有把 `str` 作为 body 传入时才会按 latin-1 编码，传 `bytes` 完全没有问题。

此外，`_direct_private_json` 和 `_private_target`（`server.py:439–504`，其中包含 `curl.exe` 兜底）没有任何调用方，属于死代码。

**修改要求**：

1. 删除手写 socket，改用 `urllib.request` 或 `http.client`，body 以 `bytes` 传入，使用默认的 `ssl.create_default_context()`。
2. 内网网关使用自签证书时，在模型配置里增加可选的 `ca_bundle` 路径，用 `create_default_context(cafile=...)` 加载。**不要提供“关闭校验”的选项。**
3. 读取响应时设置上限，例如 `response.read(5*1024*1024)`；状态码不低于 400 时沿用 `_http_status_message` 生成提示。
4. 删除 `_direct_private_json`、`_private_target`。
5. 抽出 `build_request(cfg, system, prompt) -> (url, headers, payload)` 和 `parse_response(cfg, output) -> str` 两个纯函数，分别写单元测试（不访问网络）。

**验收标准**：用 `http.server` 在本地起一个假模型服务，覆盖：chunked 响应、中文 body、HTTP 401/404/429、超大响应、HTTPS 自签证书（不配置 CA 时失败，配置 CA 后成功）。

**指导词**：

```
阅读 server.py 的 call_model、_response_text、_chat_text、_http_status_message、_direct_private_json、_private_target。
任务：重写模型 HTTP 调用。
1. 删除裸 socket 实现，改用 urllib.request.Request(url, data=body_bytes, headers=headers, method='POST')，
   传入 context=ssl.create_default_context(cafile=cfg.get('ca_bundle') or None)，超时沿用现在的 50s/180s。
2. 禁止 CERT_NONE 与 check_hostname=False。
3. 拆出纯函数 build_request(cfg, system, prompt) 与 parse_response(cfg, output)。
4. 响应最多读取 5MB；HTTPError 转换为 _http_status_message 的中文提示，URLError/超时保留现有中文文案。
5. 删除 _direct_private_json 与 _private_target（全仓库确认无引用）。
6. save_config 支持可选字段 ca_bundle（必须是存在的文件路径），public_config 返回 has_ca_bundle。
7. 新增测试：本地 ThreadingHTTPServer 模拟三种协议的正常响应、chunked 响应、4xx/5xx；build_request 对三种协议的 URL 拼接（base 以 /v1 结尾或不带 /v1）。
不要修改 generate/clarify 的提示词内容。
```

---

### S3（P0）DuckDB 外部访问没有关闭；函数白名单没有生效

**问题**：

- README 写的是“DuckDB 只读、外部访问关闭”，但 `WrenAdapter._connect`（`engine_adapter.py:175`）只设置了 threads 和 memory_limit。我用项目锁定的 duckdb 1.5.5 实测，同样方式建立的连接默认 `enable_external_access = true`，`read_text('/etc/hostname')` 可以读出文件内容。目前挡住这类查询的只有 sqlglot 语法检查这一层。
- `FUNCTIONS` 白名单（`engine_adapter.py:14`）定义了却没有任何地方使用。我实测 `getenv(...)`、`current_setting(...)` 和不存在的函数都能通过 `validate()`。

**修改要求**：

1. `_connect` 在全部 `ATTACH` 完成之后执行 `SET enable_external_access = false` 和 `SET lock_configuration = true`。已实测：执行后 `read_text` 抛出 PermissionException，并且无法再改回 true。
2. 在 `validate()` 中遍历 `exp.Func` 节点（包括 `exp.Anonymous`），函数名不在 `FUNCTIONS` 中就拒绝，错误信息要列出函数名。注意 sqlglot 会把部分函数解析成专门的表达式类（如 `exp.Sum`），应使用 `func.sql_name()` 或 `type(func).__name__` 做映射。先对现有测试 SQL 和前端提示的函数列表跑一遍，避免误伤。
3. 前端 `SQL_FUNCTIONS` 自动补全列表改为从后端获取（可放进 bootstrap），与 `FUNCTIONS` 保持同一来源。

**验收标准**：新增测试：`SELECT getenv('HOME') AS x FROM expense` 被拒绝；`SELECT SUM("本期费用") ...`、`DATE_TRUNC`、`STRING_AGG` 等常用函数仍然可用；直接连接 `_connect()` 后执行 `read_text` 抛出 PermissionException。

**指导词**：

```
阅读 engine_adapter.py 的 FUNCTIONS、validate()、WrenAdapter._connect()。
任务：
1. _connect()：所有 ATTACH 完成后依次执行 "SET enable_external_access=false"、"SET lock_configuration=true"。
2. validate()：遍历 tree.find_all(exp.Func)；对 exp.Anonymous 取 node.name，对其他内置类取 node.sql_name()；统一转大写后不在 FUNCTIONS 中则抛出 ValueError(f'不支持的函数：{name}')。
   CASE/CAST 等语法结构不属于函数调用，确认它们不会被误拦截；如有需要，把 sqlglot 的等价名称补进 FUNCTIONS。
3. 新增 bootstrap 字段 sql_functions = sorted(FUNCTIONS)；前端 SQL_FUNCTIONS 改为读取该字段。
4. 测试：getenv/current_setting/未知函数被拒；test_02/test_08/test_10 等已有 SQL 仍然通过；_connect() 后 read_text 抛出 duckdb.PermissionException。
```

---

### S4（P1）服务监听 0.0.0.0

**问题**：`server.py:1573` 绑定 `0.0.0.0`，只靠应用层 `trusted()` 拒绝外部请求。这与 README 的“只监听 127.0.0.1”不符；Windows 首次启动会弹出防火墙授权窗口，用户很容易直接点“允许”。

**修改要求**：默认绑定 `127.0.0.1`。只有 lan_share 开启时才额外绑定局域网地址，可以用第二个 `ThreadingHTTPServer` 实例，也可以启动时读取设置决定监听地址，开关切换后提示重启服务。README 同步修改。

---

### C1（P0）单流程运行沿用旧结果，可能用过期结果关账

**问题**：`run_plan_job(plan, track_id)` 只运行一个流程时（`server.py:161–171`），会把上一次运行里其他流程的检查结果原样并入本次运行并标记 `carried_over`，但本次运行的 `plan_version` 被设为**当前**方案版本（`server.py:108`）。关账门禁（`periods.py:79–86`）只检查 `run.plan_version == plan.version`、节点集合一致、数据指纹未变化。

可复现场景：
1. 方案 V5，全量运行，流程 A、B 都通过。
2. 修改流程 A 的明细 SQL（修改后本应不通过），保存得到 V6。
3. 只运行流程 B。新运行的 plan_version 为 6，但流程 A 的检查结果仍是按 V5 的 SQL 计算的。
4. 关账门禁全部通过，A 的旧结果被归档成“最终结果”。

重开账期后也有同样问题：沿用的检查结果可能来自重开之前。

**修改要求**：

1. 每个检查结果写入 `config_fingerprint = sha256(json.dumps({dataset_id, detail_sql, aggregate_sql（最终拼接的 SQL）, check_mode, tolerance, compare_op, compare_value, mdl_hash}, sort_keys=True))`。其中 `mdl_hash` 是本次执行所用 Wren MDL 的 hash：按第 4 节引入计算字段、关系和视图之后，即使 SQL 文本不变，语义层定义变了结果也会变，所以指纹必须包含它。
2. 沿用旧结果时，只沿用指纹与当前方案对应节点一致的检查；不一致的节点写成 `status='stale'`（新增状态），界面显示“配置已变更，需重新运行”。
3. 关账门禁逐个检查：每个 check 的指纹必须等于用当前方案重新计算的指纹；存在 `stale` 时拒绝关账；重开之后，沿用的 check 的 `created_at` 必须晚于 `reopened_at`（每个 check 需要记录自己的计算时间）。
4. 人工确认的 check 同样要带指纹（现在人工确认会让 plan.version 加一，只要门禁按指纹判断即可）。
5. 前端结果页用明显的标记区分“本次计算”和“沿用上次结果”。

**验收标准**：新增集成测试，按上面 4 步复现，断言关账被拒绝并提示需要重新运行的流程名；全量重跑后可以关账。

**指导词**：

```
阅读 server.py run_plan_job、/api/plan/manual-confirm，periods.py change('close')。
任务：修复“单流程运行沿用旧结果导致用过期结果关账”。
1. 新增函数 check_fingerprint(node, aggregate_sql) -> str：对 {dataset_id, detail_sql, aggregate_sql=compose_validation_sql(detail_sql, aggregate_sql), check_mode, tolerance, compare_op, compare_value} 做 json.dumps(sort_keys=True, ensure_ascii=False) 后取 sha256。
2. run_plan_job 中每个 check 写入 fingerprint 与 computed_at；沿用旧 check 时，重新计算该节点的当前指纹，不一致则写成 {node_id, track_id, dataset_id, name, status:'stale', carried_over:True}。
3. 运行总状态：有 error 为 error，否则有 stale 为 stale，否则有 failed 为 failed，否则为 passed。
4. periods.change('close')：对每个 check 与当前方案逐一比对指纹；遇到 stale、指纹不一致，或 computed_at <= reopened_at，都拒绝并列出节点名。
5. 前端 statusText/statusIcon 增加 stale；结果页对 carried_over 的 check 显示“沿用 · <时间>”。
6. 测试：修改 A 的 SQL、只运行 B、尝试关账，应返回 400 且提示中包含 A 的节点名。测试使用临时 STATE_DB，不能写入用户状态库。
```

---

### C2（P1）数据替换不是原子操作；刷新与运行可以并发

**问题**：

- `import_data.py:247–248` 先用 `os.replace` 替换 `warehouse.duckdb`，再写 `manifest.json`。两步之间如果有查询进来，会用旧 manifest 的字段和旧 sha256 去读新库，运行记录的指纹与实际读取的数据不符，而指纹正是整个系统可信的基础。
- `/api/sources/refresh`（`server.py:1292`）不检查 `PLAN_RUN_LOCK`；`/api/sources/update` 检查了。在 Windows 上，如果某个查询进程正以只读方式打开数据库文件，`os.replace` 会失败，报 PermissionError，提示信息让用户看不懂。
- `import_status.json` 是全局唯一的进度文件。

**修改要求**（推荐方案：版本化目录加指针）：

1. 数据目录改为 `data/<id>/versions/<sha256>/warehouse.duckdb`、`manifest.json`，另有 `data/<id>/current.json`（内容为 `{"sha256": ...}`），用 `write_json` 的临时文件加 `os.replace` 原子切换。
2. `query_worker` 启动时读取 `current.json` 一次，之后只使用该版本目录里的数据库和 manifest，保证同一进程内数据与指纹一致。
3. 旧版本按保留策略清理，例如只保留最近 N 个且未被未关账运行引用的版本。附带好处：历史运行可以按原数据重新导出，不再需要“数据已刷新，禁止导出”。
4. 在引入版本化目录之前，至少先做到：刷新前获取 `PLAN_RUN_LOCK` 并等待 `WORKERS` 空闲；把 manifest 写到数据库旁边，最后一步替换一个同时指向两者的指针。
5. 进度文件改为 `data/<id>/import_status.json`。

**验收标准**：写一个压力测试：一个线程循环刷新（两份内容不同的样例文件交替），另一个线程循环查询 `COUNT(*)` 并记录返回的 snapshot；断言每次返回的行数都与该 snapshot 对应文件的行数一致。

---

### C3（P1）xlsx 日期和布尔值解析与 xls 不一致

**问题**：`read_ooxml_rows` 不读取单元格样式（`styles.xml` 中的 numFmt）。已实测：xlsx 中的日期 `2026-08-31` 被导入为 `'46265'`，布尔值 `TRUE` 被导入为 `'1'`；而 `read_xls_rows` 会把日期转成 `'2026-08-31 00:00:00'`、布尔值转成 `'TRUE'`。同一份报表另存为不同格式后字段值不同，SQL 与规则会随文件格式失效。如果“期间”列是日期格式，还会直接导致 C4 中的关账失败。

**修改要求**：

1. 解析 `xl/styles.xml` 的 `cellXfs` 与 `numFmts`，识别内置日期格式（numFmtId 14–22、45–47）和自定义格式里含 `y/m/d/h/s` 且不在引号中的格式；按工作簿的 `date1904` 设置换算成 ISO 字符串，格式与 xls 分支一致。
2. `t="b"` 输出 `'TRUE'`/`'FALSE'`。
3. 两个分支输出同一种规范化格式，并在 manifest 里记录 `importer_version: 3`。版本号变化会触发全部重新导入，需要在 README 中说明。

**验收标准**：用 openpyxl 生成含日期、日期时间、布尔、百分比、文本数字的 xlsx，再用 xlwt 或预置的 xls 夹具生成同样内容的 xls，断言两者导入后逐行逐列相等。

---

### C4（P1）金额列与期间列按固定列名识别

**问题**：

- `AMOUNTS = {'原币借方', ...}`（`import_data.py:19`）写死了金额列，其他列一律为 VARCHAR。新增的参考表或列名不同的报表里，金额只能手动 CAST，而且拿不到精度扫描的保护。
- 期间只从名为 `期间` 的列收集（`import_data.py:205`）；关账要求 `set(periods) == {当前账期}`（`periods.py:88`），账期格式只接受 `YYYY-MM`（`periods.py:25`）。只要报表的期间写成 `AUG-26`、`2026-8`、`2026年08月` 或日期格式，**就永远无法关账**，并且错误提示不会说明原因。

**修改要求**：这属于“数据接入配置”，不是业务规则，应做成每个数据源可配置的项目：

1. 在数据源配置中增加 `import_options`：`header_row`（默认 1）、`sheet`、`amount_columns`（默认沿用现有的 AMOUNTS 集合，保证兼容）、`period_column`（默认 `期间`）、`period_format`（例如 `%Y-%m`、`%b-%y`，或正则加映射）。
2. 导入时按 `period_format` 把期间规范化为 `YYYY-MM` 再写入 manifest 的 `periods`，同时保留原始值 `raw_periods`。
3. 规范化失败时，在导入结果中明确提示“期间列存在无法识别的值：xxx（共 N 行）”，不要静默跳过。
4. 数据源编辑界面提供上述字段；表头行允许跳过报表顶部的标题行（EBS 导出常见）。
5. 关账门禁的报错要带出实际识别到的期间集合。

---

### C5（P1）按文件 hash 跳过导入时没有考虑导入参数

**问题**：`import_data.py:147` 只要 `sha256` 和 `importer_version` 相同就跳过导入。用户改了工作表（或者以后改了 header_row、金额列）而文件没变时，仍会沿用旧的导入结果，界面还提示“文件内容未变化”。另外 `refresh_source` 从不传 `--force`。

**修改要求**：跳过条件改为 `(文件 sha256, importer_version, 规范化后的 import_options 的 hash)` 三者都相同；manifest 记录 `options_hash`；界面在数据源卡片上增加“强制重新导入”按钮，对应 `--force`。

---

### C6（P1）明细下钻假设聚合维度名等于明细列名

**问题**：下钻（`/detail`、`/detail-export`、`failed-detail`，见 `server.py:1080` 与 `1450–1490`）用 `明细.维度列 IS NOT DISTINCT FROM 值` 反查。只有当聚合 SQL 的输出列与明细 SQL 的列**同名同值**时才正确。只要聚合 SQL 里有 `SELECT "科目" AS "科目名称"` 或 `DATE_TRUNC('month', "日期") AS "月份"` 这类写法，下钻就会报“字段不存在”；更糟的情况是恰好存在同名但含义不同的列，结果静默出错。

**修改要求**：不要在聚合结果上反推条件，改为直接复用聚合 SQL 的分组逻辑：

```sql
WITH rule_detail AS (<明细SQL>),
     agg AS (<聚合SQL>)
SELECT d.* FROM rule_detail d
WHERE EXISTS (SELECT 1 FROM agg g WHERE g.<维度> IS NOT DISTINCT FROM <所选值> ...)
```

这样写仍然不能解决维度是派生值的情况。可行的通用做法是：**要求聚合 SQL 只能按 rule_detail 的原始列分组，并在保存或运行时校验**（用 sqlglot 检查 GROUP BY 的每一项都是 `rule_detail` 的裸列，且 SELECT 中的别名与原列名相同）。校验不通过时，节点仍然可以运行，但要把 `drilldown_supported=false` 写入结果，界面对该节点禁用下钻并说明原因。这比现在的“可能静默出错”更安全。

**验收标准**：测试三种聚合写法（裸列分组、别名分组、派生列分组）：第一种下钻结果正确；后两种运行成功但 `drilldown_supported=false`，下钻接口返回明确的中文提示。

---

### R1（P1）后台运行被前台操作挤掉

**问题**：`execute()`（`server.py:605`）用 `WORKERS.acquire(blocking=False)` 获取槽位。方案运行时，用户只要同时做 Wren 规划预览和 AI 生成聚合 SQL（后者会逐个节点执行 contract 查询），两个槽位就被占满，正在运行的节点会得到“已有两个查询正在执行”的 ValueError，被**记为 error 并写入运行记录**。

**修改要求**：

1. `execute` 增加参数 `wait: float = 0`。后台运行调用 `WORKERS.acquire(timeout=wait)`，建议等待时长为 `runtime.timeout + 10` 秒；前台交互仍然立即失败并提示。
2. 更好的做法：保留一个专给后台运行用的槽位（例如总共 3 个，其中 1 个只给运行使用），前台最多占 2 个。
3. 运行增加“取消”能力：`PLAN_RUN_STATE['cancel']=True`，每个节点开始前检查。

---

### R2（P1）GET 异常处理不完整

**问题**：`do_GET` 只捕获 `ValueError/KeyError/FileNotFoundError`（`server.py:1108`）。`sqlite3.OperationalError`、`decimal.InvalidOperation`（`failed-detail` 分支会触发）、`TypeError`、`OSError` 等都会导致连接被直接断开，前端只看到“Failed to fetch”。`do_DELETE` 只捕获 ValueError。

**修改要求**：三个 do_ 方法统一走同一个 `dispatch()`：业务错误返回 400，鉴权错误返回 401/403，找不到资源返回 404，其他异常返回 500，并用 `logging.exception` 写入 `logs/server-error.log`，响应里只放通用中文提示。建议定义 `class AppError(Exception): status=400`，业务代码抛 AppError 而不是 ValueError，这样能与 Python 内置的 ValueError 区分，避免内部异常信息原样返回给前端。

---

### R3（P2）导出方式

**问题**：三处导出都是先让子进程写 `DOWNLOADS/<固定名>.csv`，再 `read_bytes()` 整体读入内存（25 万行时约几十 MB），然后删除。文件名由 run_id 和 node_id 拼接，同一用户连点两次会互相覆盖或被删除。

**修改要求**：临时文件名加上 `uuid4().hex`；响应改为 `shutil.copyfileobj` 流式发送；在 `finally` 中删除临时文件。三处重复代码合并为 `send_csv_export(did, sql, filename)`。

---

### R4（P2）方案版本

**问题**：`/api/plan` 先读取旧版本做 `base_version` 比较，再在另一个事务里写入（`server.py:1322` 与 `1387`），两个标签页同时保存时存在竞态。另外 `INSERT OR REPLACE ... 'current'` 只保存最新方案，**中间版本全部丢失**，无法回答“V7 与 V6 相比改了什么”，这对结账审计来说是缺口。

**修改要求**：

1. 在同一个 `BEGIN IMMEDIATE` 事务内完成读取、比较和写入。
2. 新增表 `validation_plan_versions(version INTEGER PRIMARY KEY, payload TEXT, saved_at TEXT, saved_by TEXT, note TEXT)`，每次保存和人工确认都追加一行；`validation_plans` 只作为指向当前版本的指针。
3. 界面在方案页提供“历史版本”列表，至少能查看任意版本的 SQL，最好能与当前版本做文本 diff（Ace 自带的 diff 或简单的逐行对比即可）。

---

### M1（P2）拆分 `server.py`

**目标结构**（保持零构建依赖、只用标准库 HTTPServer）：

```
closewren/
  app.py            # 启动、ThreadingHTTPServer、路由注册
  http.py           # Handler 基类：body 解析、send_json/send_file、错误映射、访问级别
  routes/
    plan.py         # /api/plan*, /api/plan-runs/*
    sources.py      # /api/sources*
    periods.py      # /api/periods*（调用 services/periods）
    ai.py           # /api/ai/*
    wren.py         # /api/wren/*
  services/
    plans.py        # 方案校验/保存/版本、指纹
    runner.py       # run_plan_job、取消、进度状态
    datasets.py     # dataset()/datasets()/flow_workspace_ids/source_config
    exports.py      # CSV 导出与下钻 SQL 构造
    llm.py          # build_request/parse_response/call_model
    ai_drafts.py    # generate/clarify 合并后的流水线
    admin.py        # 密码、会话、运行参数、局域网共享
  storage.py        # SQLite 连接、迁移（schema_version 表）
  config.py         # DATA_DIR 等路径，读取 CLOSEWREN_DATA_DIR 环境变量
engine_adapter.py / query_worker.py / import_data.py 保持独立脚本
```

**要点**：

- 路由改为表驱动：`ROUTES = [('POST', r'^/api/plan$', plan.save, 'lan_write'), ...]`，由 dispatch 统一完成访问级别、账期是否打开、错误映射。现在 `do_POST` 里 `guarded` 与 `_do_POST` 的两层判断就能去掉。
- 全局可变状态（`PLAN_RUN_STATE`、`REFRESH_STATE`、`ADMIN_SESSIONS`）放进带锁的小类，不要在线程中直接给全局变量重新赋值。
- `periods.py` 现在通过 `sys.modules[__name__]` 拿到整个 server 模块作为 `app`，属于隐式依赖。改为显式传入所需服务对象，或者定义一个 `AppContext` 数据类。
- 数据库迁移：新增 `schema_version` 表，`initialize()` 按版本号依次执行迁移函数，替代现在分散在 `bootstrap()`、`periods.state()`、`semantic_model()` 里的 `CREATE TABLE IF NOT EXISTS` 和就地兼容代码。
- 拆分要**分多次提交、每次都能运行**：先抽出没有副作用的纯函数（指纹、SQL 拼接、导出 SQL 构造），再抽服务，最后换路由。每一步都运行全部测试。

**指导词（第一步）**：

```
在不改变任何对外行为的前提下，从 server.py 中抽出纯函数模块 closewren/sqlbuild.py：
compose_validation_sql、quote_identifier，以及三处下钻/导出 SQL 构造（/detail、/detail-export、failed-detail 分支中拼接 SQL 的部分，提取为 build_group_detail_sql(item, groups) 与 build_failed_detail_sql(item)）。
server.py 改为 import 这些函数。为每个函数新增不依赖真实数据的单元测试（只断言生成的 SQL 字符串，并用 sqlglot 解析通过）。
不得修改 API 响应结构；运行全部测试。
```

---

### M2（P2）前端整理

**问题**：

- `app.js` 约 106KB、502 行，其中一行长达 9,305 字符；像压缩后的产物，但仓库里没有源文件。
- 用“保存旧函数、再覆盖”的方式叠加功能：`renderPlan` 在 `app.js:206/244/309` 和 `drag-ui.js:42–43` 被覆盖，`renderFlow` 在 `app.js:429–430` 和 `drag-ui.js:38–39` 被覆盖，`renderAll` 在 `periods.js:49–50` 被覆盖。行为取决于 script 加载顺序，EXPERIENCE_AUDIT.md 自己也记了这一条。
- 默认容差 `'0.001'` 在前端出现 6 次（见 U2）。
- `workbench.css` 67KB 且存在多轮叠加的覆盖样式。

**修改要求**（不引入构建工具）：

1. 用 Prettier（`npx prettier --write static/*.js static/*.css`，只做格式化）把代码展开成可读形式，**单独提交**，并在提交说明中写明“仅格式化，无逻辑变化”。
2. 改用原生 ES Module：`<script type="module" src="/js/main.js">`，按页面拆分为 `api.js`、`state.js`、`overview.js`、`plan/*.js`、`flow.js`、`sources.js`、`ai.js`、`periods.js`、`drag.js`。静态文件白名单（`server.py` 中写死的列表）改为“`static/` 目录下存在且扩展名在允许集合内”，并防止路径穿越（`resolve()` 后必须位于 STATIC 目录内）。
3. 每个页面只保留一个 `render()`，排序、账期等功能通过显式的钩子或组件组合接入，删除全部“保存旧函数再覆盖”的写法。
4. 常量集中定义：默认容差、状态文案、图标。
5. CSS 按组件合并，删除失效选择器（可以用 Chrome DevTools 的 Coverage 面板辅助判断）。

**验收标准**：用 Playwright 编写冒烟脚本（本仓库环境已有 Chromium）：打开首页，切换 5 个页面，新增节点，编辑 SQL，保存方案，运行，打开结果，下钻，导出，拖动排序，走完关账流程。每一步前端控制台都没有错误。每次重构提交前都要跑这个脚本。

---

### M3（P2）AI 流水线去重；清理死代码

**问题**：`generate()`（`server.py:806`）和 `clarify()`（`server.py:721`）有 80% 的代码逐行重复：节点契约收集、system prompt、校验、结果组装。两份提示词已经开始出现差异，一个用中文引号，一个用英文引号。`clarify` 不校验 `target`，传入非法值时会**跳过全部 SQL 校验**，直接把模型输出返回给前端。

**修改要求**：

1. 抽出 `collect_contracts(req) -> (did, node_contracts, common_columns)`、`build_system_prompt(target, did)`、`validate_draft(target, sql, source, node_contracts) -> (planned, validated_nodes)`、`assemble_result(...)`。`generate` 与 `clarify` 只在用户消息和是否允许返回澄清问题上有区别。
2. `target` 与 `action` 做白名单校验。
3. 提示词里“所有节点共用的聚合校验 SQL”改为“当前流程的聚合校验 SQL”，与现在按流程维护聚合 SQL 的实现一致。
4. 死代码清单（全部确认没有引用后删除）：`_direct_private_json`、`_private_target`、重复的 `import socket`、`server.py:1241/1243` 重复的 `REFRESH_STATE.update`、`body()` 里 `errors='replace'` 的 JSON 兜底（第二次解析同样会失败，实际没有作用）。

---

### M4（P2）旧版 `/api/rules` 链路

**问题**：前端已经不调用 `/api/rules*`、`/api/runs/*/download`、`/api/query`、`/api/preview`，但 `test_06`、`test_09` 等测试仍然主要覆盖这条旧链路，而新的主流程（方案、运行、账期）覆盖不足。

**修改要求**：

- 先确认用户是否还需要查看旧版规则的历史数据。如果需要，保留只读的 GET 接口，删除写接口。
- `/api/query` 保留，它是 U1 即席查询要用的后端接口。
- 把测试迁移到新主流程（见第 6 节）。

---

### P1x（P2）首次启动依赖固定样例文件

**问题**：`launch.py:33` 在没有 manifest 且没有 workspace.sqlite3 时，会运行 `import_data.py`，而它会去 `../三大表校验/` 读取两个写死文件名的 Excel（`import_data.py:17–21`）。源码包不带数据，新电脑首次双击 `start.cmd` 就会失败。

**修改要求**：删除首次自动导入。首次启动显示空工作区，并在总览页引导用户去“数据源”页新增数据源、连接文件夹。`import_data.py` 的 `SOURCES`/`SOURCE` 常量删除，命令行必须传 `--dataset --name --source`。`query_worker.py` 中的 `isinstance(source_cfg, dict)` 旧格式兼容和 `/api/preview` 的默认 `'before'` 一并清理。

---

### T1（P2）测试（详见第 6 节）

---

### U1（P3）界面缺少即席查询和单节点试运行

**问题**：README 写着“Ctrl/⌘+Enter 执行、结果预览及完整行数”，但前端已经不再调用 `/api/query`，编辑器里也没有这个快捷键。现在想验证一段节点 SQL，只能**保存整个方案再运行整个流程**。`AGENTS.md` 要求“手工 SQL 是常驻完整入口”，而这个入口目前是缺失的。

**修改要求**：

1. 节点明细 SQL 编辑器增加“试运行”（Ctrl/⌘+Enter）：调用 `/api/query`（加上 `allowed_dataset_ids`），显示前 200 行和总行数，**不保存、不写运行记录**。
2. 聚合 SQL 编辑器增加“用当前节点试算”：用当前编辑中（未保存）的明细 SQL 和聚合 SQL 调用 `mode='validate'`，展示判定结果，同样不写运行记录。
3. 保存方案前可选“检查全部节点”：对每个节点执行一次 `mode='contract'`，列出不兼容的节点，但不阻止保存。

---

### U2（P3）默认容差

**问题**：`tolerance` 默认 `'0.001'`，出现在 `server.py:1342` 和 `app.js` 的 6 处（15、70、205、235、378、470 行）；而引擎的默认值是 `'0'`（`engine_adapter.py:82`）。容差本身是业务口径，按 `AGENTS.md` 不应由系统预设。

**修改要求**：统一为一个常量。新建节点时容差为空，并在保存时要求用户显式填写（可以提供“0”这个快捷选项），或者至少统一为 0 并在节点卡片上明显展示。需要先和业务方确认选哪种方式。

---

### D1（P3）文档与实现不一致

需要逐条修正（修改代码后按实际状态更新）：

| 文档位置 | 当前描述 | 实际情况 |
|---|---|---|
| README「技术与执行边界」 | 服务只监听 127.0.0.1 | 绑定 0.0.0.0，另有局域网共享开关（见 S4） |
| README 同上 | DuckDB 外部访问关闭 | 没有关闭（见 S3） |
| README 同上 | 只允许受支持的函数 | 白名单没有生效（见 S3） |
| README「当前可用功能」 | Ctrl/⌘+Enter 执行、结果预览及完整行数 | 前端没有这个入口（见 U1） |
| README 顶部 | 整个工作区维护一份汇总校验方案 | 每个流程有自己的聚合 SQL，`shared_sql` 只作兼容兜底 |
| ARCHITECTURE 流程图 | 按所选空间建立 expense 模型 | 按流程主表加全部参考表建立模型 |
| ARCHITECTURE「主要 API」 | 以 /api/rules 为主 | 主流程是 /api/plan、/api/plan/run、/api/plan-runs/*、/api/sources/*、/api/periods/*、/api/wren/*、/api/ai/* |
| ARCHITECTURE 功能表 | 一条规则一个节点 | 流程/轨道 → 节点，两种判定方式，人工确认 |
| TEST_RESULTS.md | 8 项测试通过 | 现在有 18 项，而且依赖本机数据 |
| README「AI 配置」 | 没有提到局域网共享 | 需要补充局域网共享的风险和鉴权方式 |

另外建议给每个文档加上“最后核对日期、对应提交号”，每次改动接口时同步更新 ARCHITECTURE 的 API 表。

---

## 4. WrenAI 深度使用指导

### 4.1 现状：Wren 只当成了“表名检查器”

对照 `engine_adapter.py` 与 `server.py`，目前对 Wren 的用法可以概括为三点：

1. 每次查询都在子进程里**临时拼一个 MDL**，内容只有“模型名 → 物理表”和“列名 + 类型”，没有描述、主键、关系、计算字段、视图和访问控制（`engine_adapter.py:38–49`）。
2. 只调用 `dry_plan()`，拿到展开后的 SQL 再自己执行。Wren 在这里起的作用基本就是“SQL 只能引用这几张表”。项目自己的 sqlglot 白名单已经做了同样的事，Wren 的语义层价值几乎没有发挥出来。
3. 用户在界面上维护的“业务说明”存放在 SQLite 的 `semantic_notes` 表（`server.py:668`），**没有写进 MDL**，只被拼进 AI 提示词。Wren 自己并不知道这些语义。

这说明作者把 Wren 当成了一个 SQL 校验库，没有把它当成**语义层**来用。结账校验恰恰最需要语义层：口径集中定义、多处复用，改一处全局生效，并且能追溯。

### 4.2 实测发现的隐藏问题（必须先修）

我在隔离环境安装项目锁定的 `wrenai==0.14.0`、`wren-core-py==0.7.6`，用和本项目相同的 MDL 结构做了实验：

| 实验 | 结果 |
|---|---|
| 金额列类型声明为 `DECIMAL(38,10)`（`import_data.py` 生成的正是这种类型），再定义计算字段 `"人民币借方" - "人民币贷方"` | **规划失败**：`Cannot coerce arithmetic expression Utf8 - Utf8`。wren-core 不认识带精度的 DECIMAL 类型串，把它当成了字符串。`wren.type_mapping.parse_type` 输出的 `DECIMAL(38, 10)` 同样不行 |
| 类型改为 `DECIMAL`、`NUMERIC` 或 `DOUBLE` | 计算字段、关系字段、视图都能正常规划，DuckDB 实际返回的仍是原始精度的 Decimal（`Decimal('70.0000000000')`），因为执行时读的是物理列 |
| 普通查询 `SUM("人民币借方")` | 两种类型都能通过，因为不涉及类型推断。这就是这个问题**到现在都没暴露**的原因 |

**结论（W0，P1）**：目前的 MDL 在类型上等于没有声明，只要开始使用计算字段、视图或指标就会立刻出错。修复办法：生成 MDL 时建立一张类型映射表，把 `DECIMAL(p,s)` 映射为 wren-core 能识别的 `DECIMAL`（精度仍由 DuckDB 物理列保证），并加一个回归测试：“对金额列定义计算字段后，能规划、能执行，并且返回值精度与直接查询物理列一致”。每次升级 Wren 都要重新跑这个测试。

### 4.3 深化方向（按投入产出排序）

> 与 `AGENTS.md` 的边界：下面提到的关系、计算字段、视图都是**通用能力**，由用户在界面上定义，系统不预置任何业务公式、不按列名猜测关联。**结算前主表与结算后主表之间禁止建立关系**，只允许“校验主表 ↔ 参考表”和“参考表 ↔ 参考表”。文中的示例公式只用于说明机制，不要写进默认配置。

#### W1（P1）MDL 成为唯一的语义来源，并做成有版本的资产

- **现在的问题**：MDL 在每次查询时临时生成，用完就丢；业务说明在 MDL 之外；归档里保存的是 `semantics`，而不是实际执行用的 MDL；运行记录也不知道自己是按哪一版语义执行的。
- **要求**：
  1. 新增 `services/semantic.py`，负责根据“数据源 manifest + 业务说明 + 关系 + 计算字段 + 视图”生成 MDL JSON，计算 `mdl_hash`，并保存为 `semantic_versions(hash, mdl_json, created_at)`。
  2. 业务说明写进 MDL 的 `properties.description`（模型级和列级），`displayName` 可以放中文展示名。AI 提示词改为**从 MDL 读取**语义，不再单独读 `semantic_notes`（这张表可以保留为编辑草稿，保存时同步到 MDL）。
  3. `query_worker` 不再自己拼 MDL，而是按 `mdl_hash` 读取已生成的 MDL，只做“按流程裁剪可见模型”这一步。
  4. 运行记录、关账归档都写入 `mdl_hash`，并在归档中保存完整 MDL，保证审计时能复现“当时的口径”。
  5. 管理员“查看 MDL”页面直接展示这份已保存的 MDL，并提供两个版本之间的 diff。

#### W2（P1）用 Wren 的访问策略替代或加固自研校验

- **实测**：`WrenConfig(strict_mode=True, denied_functions=frozenset({...}))` 对被禁用的函数返回结构化错误 `[BLOCKED_FUNCTION] ... phase=SQL_POLICY_CHECK`；strict_mode 下 Wren 会拦截 `read_csv` 这类文件读取函数；但 `current_setting()` 这类函数 Wren 默认放行。
- **要求**：
  1. 保留项目自己的 sqlglot 白名单作为第一层（S3），同时把高风险函数（`current_setting`、`getenv`，以及 DuckDB 中其他能读取系统信息的函数）配置进 `denied_functions`，作为第二层。两层配置从同一份常量生成。
  2. Wren 抛出的 `WrenError` 带有 `code`、`phase` 和 `metadata`（其中有 dialect SQL），**不要只取 `str(exc)`**。在 `query_worker` 中把这些字段原样返回，前端按阶段显示：“策略拦截 / 语义规划失败 / 执行失败”。AI 的“修复 SQL”功能把阶段和错误码一并传给模型，提示会准确得多。

#### W3（P2）用户自定义关系：参考表关联不再手写 JOIN

- **实测**：MDL 中定义 `relationships: [{name, models:[主表, 参考表], joinType:'MANY_TO_ONE', condition}]`，在主表上加关系列 `{name:'科目', type:'参考表模型名', relationship:'关系名'}`，再加计算字段 `{name:'科目名称', isCalculated:true, expression:'"科目"."科目名称"'}`。之后节点 SQL 直接写 `SELECT "科目名称", ... FROM expense_before`，Wren 会自动展开 JOIN，结果正确。
- **价值**：主表与参考表怎么关联只需定义一次，所有节点共用；关联条件修改后，受影响的节点可以通过 `mdl_hash` 识别出来；AI 生成 SQL 时不必再猜 JOIN 条件。
- **要求**：
  1. 在数据源页（或 Wren 语义面板）新增“关系”编辑：选择两张表、各自的关联列、关联类型（多对一 / 一对一），保存前用 `dry_plan` 做一次校验，并用 `COUNT(*)` 对比检查“多对一”是否真的不会让主表行数膨胀，膨胀时给出提示。
  2. 服务端强制只能建立“校验主表 ↔ 参考表”和“参考表 ↔ 参考表”两类关系，**禁止两张校验主表之间建立关系**。
  3. **别名问题**：现在 `expense` 是主表的一个复制模型（`engine_adapter.py:44`）。定义在 `expense_before` 上的关系列和计算字段，`expense` 并不会自动拥有。生成 MDL 时必须把主表的关系列、计算字段和相关关系一起复制到 `expense` 别名上，并写测试，保证 `SELECT "科目名称" FROM expense` 与 `FROM expense_before` 结果一致。

#### W4（P2）计算字段：把重复的口径表达式沉淀下来

- **实测**：类型修正（W0）之后，`isCalculated` 计算字段可以正常规划和执行；展开后的 SQL 只包含查询实际引用到的列（Wren 会裁剪列）。
- **要求**：在语义面板中允许用户为表新增计算字段（名称、表达式、类型、说明），保存时做 `dry_plan` 校验，并做一次 `LIMIT 1` 试执行。节点 SQL 可以直接引用这些字段。计算字段属于 MDL 的一部分，修改后 `mdl_hash` 会变化，从而让相关节点的运行结果失效（与 C1 联动）。
- **注意**：计算字段的公式是业务口径，**只能由用户填写**。系统可以提供“从当前 SQL 的某个表达式提取为计算字段”这种辅助操作，但不能预置公式。

#### W5（P2）视图：可复用的明细取数

- **实测**：MDL 中的 `views: [{name, statement}]` 可以直接查询。注意：在 0.14.0 中 `SELECT * FROM 视图` 会规划失败（`* is not Alias`），必须写明列名。
- **用法**：用户可以把多个节点共用的明细取数逻辑保存成视图（例如“剔除某类凭证后的有效明细”，具体口径由用户定义），节点明细 SQL 只写 `SELECT ... FROM 视图名 WHERE ...`。视图修改后，所有引用它的节点都能通过 `mdl_hash` 识别出受影响。
- **要求**：在前端 SQL 编辑器的字段补全里加入视图及其列；保存视图时校验语句里没有 `SELECT *`（或者自动展开成显式列）；视图只能引用当前流程可见的模型。

#### W6（P2）规划结果做成“口径追溯”

- 目前“Wren 规划”面板只展示展开后的 SQL 文本，审核人员很难看懂。
- **要求**：对 `dry_plan` 的输出使用 `sqlglot.lineage` 逐列追溯，生成“输出列 → 物理表.物理列”的对应关系，例如 `校验金额 ← ds_before.raw_expense.人民币借方 − 人民币贷方`，经过关系 `expense_coa` 取 `ds_coa.raw_expense.科目名称`。在节点结果页和关账归档里都展示这张表。对结账审计来说，这比展开后的 SQL 有用得多。

#### W7（P3）行级权限：为事业部权限做准备

- **实测**：在模型上定义 `rowLevelAccessControls: [{name, requiredProperties:[{name:'session_dept', required:true}], condition:'"事业部编码" = @session_dept'}]`，`dry_plan(sql, properties={'session_dept': "'D01'"})` 只返回该事业部的行；缺少这个属性时直接报错拒绝，不会返回全部数据。
- **要求**（ARCHITECTURE 里规划了多人协作和事业部权限，现在可以先把机制接好）：
  1. 行级权限规则是 MDL 的一部分，由管理员配置。
  2. `properties` 的值**只能由服务端根据登录身份注入**，绝不能从前端请求透传。值要做字面量转义（属性值会作为 SQL 字面量拼进条件）。
  3. 列级权限（`columnLevelAccessControl`）我按官方字段名做了一次配置，结果全部被拒绝，说明配置方式还需要对照官方文档确认，**先不要纳入交付**。

#### W8（P3）AI 生成用上 Wren 的知识机制

Wren 0.14.0 内置了一套给 AI 用的上下文机制：`knowledge/rules/*.md`（规则说明，通过 `wren context instructions` 读取）、NL→SQL 问答对（`wren memory store` / `recall`），以及基于向量检索的 schema 召回（`wren memory index` / `fetch`）。

- **现在的做法**：`_ai_schema_context` 把当前流程所有表的**全部列**拼进提示词。EBS 报表一张就有几十列，参考表增多后提示词会迅速膨胀，模型也更容易“看错列”。
- **要求（分两步）**：
  1. **轻量版（不增加依赖）**：用户确认并**实际运行通过**的节点 SQL，连同节点名称和用户当初输入的描述，保存为问答对（新表 `sql_examples`），`mdl_hash` 一起记录。AI 生成时按关键词和表名匹配取 3 条作为少样本示例，只取与当前 `mdl_hash` 兼容的样本。提示词中的 schema 改为：流程主表全部列，参考表只保留关系列、说明非空的列，以及用户描述中提到的列。
  2. **Wren memory（需要先评估）**：`wren memory` 依赖 `sentence-transformers`、`lancedb`、`onnxruntime`，并且首次使用要从 HuggingFace 下载嵌入模型。**内网离线环境必须先验证能否预置模型文件**，确认之前不要引入。
- **规则说明**：`knowledge/rules` 适合放用户自己写的口径说明（例如“金额单位为元”）。只能由用户填写，系统不预置。

#### W9（P3）用 cube 定义聚合（探索项）

Wren 0.14.0 支持 cube（度量 + 维度 + 时间维度）。本项目的“聚合校验 SQL”本质上就是“按若干维度汇总校验金额”。如果把聚合改用 cube 定义，维度就是明确声明的列，**C6 的下钻问题可以从根本上解决**：下钻条件直接由 cube 的维度定义生成，不再需要反推 SQL。这项我没有实测，建议先做一个技术验证：用一个节点跑通“cube 定义 → 生成聚合 SQL → 判定 → 按维度下钻”，确认可行后再决定是否替换现在的手写聚合 SQL。

#### W10（P3）为迁移 PostgreSQL 做准备时的真实边界

`WrenEngine` 支持多种数据源（postgres、mysql、oracle 等），但 `dry_plan` 的**输入 SQL 本身就要求是目标方言**。所以从 DuckDB 迁移到 PostgreSQL 时，用户已经保存的节点 SQL 不会被 Wren 自动改写。ARCHITECTURE 中“不能保证零修改迁移”的判断是对的。要求：

1. 数据源方言做成配置项，不要在代码里写死 `'duckdb'`。
2. 准备一个迁移脚本，用 `sqlglot.transpile(read='duckdb', write='postgres')` 批量转换已保存的 SQL，再逐条 `dry_plan` 并试执行，输出无法自动转换的清单。
3. 计算字段、视图、关系集中在 MDL 里定义得越多，节点 SQL 就越简单，迁移成本也越低，这也是推进 W3–W5 的额外收益。

### 4.4 建议的实施顺序与验收

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | W0 类型映射 + 回归测试 | 在金额列上定义计算字段，能规划、能执行，精度不丢 |
| 2 | W2 结构化错误 + 禁用函数配置 | 界面能区分策略拦截 / 规划失败 / 执行失败 |
| 3 | W1 MDL 版本化，运行记录和归档写入 `mdl_hash` | 修改业务说明后 hash 变化；归档中有完整 MDL |
| 4 | W3 关系（含别名复制）+ W4 计算字段 | `FROM expense` 与 `FROM <模型名>` 查询结果一致；关系膨胀检查生效；禁止两张主表之间建立关系 |
| 5 | W6 口径追溯 | 每个节点的“校验金额”都能追溯到物理列 |
| 6 | W5 视图、W8 少样本、W7 行级权限 | 各自附测试 |
| 7 | W9 cube 技术验证 | 输出验证报告，再决定是否替换现有聚合 SQL |

**指导词（W0 + W2，第一步）**：

```
阅读 AGENTS.md、engine_adapter.py（WrenAdapter.__init__、plan）、query_worker.py、import_data.py 中金额列类型的生成方式。
背景：wren-core 0.7.6 无法识别 'DECIMAL(38,10)' 这类带精度的类型串，会把它当作 Utf8，导致计算字段、视图中的算术和聚合规划失败。实测改为 'DECIMAL' 后可以规划，DuckDB 执行时的精度不变。
任务：
1. engine_adapter.py 新增 wren_type(duckdb_type) 映射：DECIMAL(p,s)/NUMERIC(p,s) -> 'DECIMAL'，BIGINT/INTEGER/VARCHAR/DATE/TIMESTAMP/BOOLEAN 原样返回，未知类型 -> 'VARCHAR' 并记录警告。构造 MDL 时所有列都经过这个映射。
2. WrenConfig 增加 denied_functions，来源为 engine_adapter 中新定义的 DENIED_FUNCTIONS 常量（至少包含 current_setting、getenv）。
3. query_worker 捕获 wren.model.error.WrenError，返回 {'error': 中文提示, 'error_code': e.code 名称, 'error_phase': e.phase 名称}；其他异常保持现有行为。前端按 error_phase 显示“策略拦截 / 语义规划失败 / 执行失败”。
4. 测试（不依赖真实数据，用临时 DuckDB 夹具）：
   a) 在 DECIMAL(38,10) 的两列上定义计算字段做减法，dry_plan 成功，执行结果与直接查询物理列逐行相等（Decimal 精度一致）；
   b) SELECT current_setting('threads') ... 返回 error_phase 为 SQL_POLICY_CHECK；
   c) 现有 test_demo 全部通过。
不要引入关系、视图等新功能；本步只修类型和错误结构。
```

**指导词（W3，关系与别名）**：

```
前置：W0、W1 已完成，MDL 由 services/semantic.py 统一生成。
任务：支持用户自定义“校验主表 ↔ 参考表”关系。
1. 数据结构：relationships 表（id, name, left_dataset_id, left_column, right_dataset_id, right_column, join_type in ('MANY_TO_ONE','ONE_TO_ONE'), relation_column_name, created_at）。
2. 服务端校验：两端数据源都存在；左右两边不能同时是 role='validation'（禁止主表之间建立关系）；列存在；relation_column_name 不与已有列重名。
3. 生成 MDL：在左模型上添加 {name: relation_column_name, type: 右模型名, relationship: 关系名}，并加入 relationships 条目 {name, models, joinType, condition:'"左模型"."左列" = "右模型"."右列"'}。
   如果左模型是某个流程的主表，把同样的关系列、计算字段和关系复制到别名模型 expense 上（关系名加后缀避免重名）。
4. 保存前检查：dry_plan 一条引用关系字段的查询；执行 SELECT COUNT(*) FROM 左模型 与 通过关系取右表字段后的 COUNT(*)，二者不相等时返回提示“关联后行数由 X 变为 Y，请检查关联列是否唯一”，但不阻止保存（由用户判断）。
5. 界面：语义面板新增“关系”标签页，可以新增、删除、查看关系，并列出引用它的节点。
6. 测试：关系查询结果正确；FROM expense 与 FROM 模型名结果一致；两张主表之间建立关系被拒绝；行数膨胀提示生效；修改关系后 mdl_hash 变化，相关节点的运行结果被标记为 stale（依赖 C1）。
```

---

## 5. 架构方向（中期，P0/P1 完成后再推进）

1. **数据版本化**（C2 的完整方案）：`versions/<sha>` 加 `current` 指针，运行记录只引用版本号。这样可以带来：历史结果可复现、可导出；关账归档只需记录引用关系而不必每次复制整库（`periods.py:103` 现在每次关账都完整复制所有 DuckDB 文件再打 ZIP，月度累积后磁盘增长很快）；刷新数据不影响正在进行的运行。
2. **配置入库**：`data/source-config.json` 迁入 SQLite（`sources` 表），与方案版本、导入参数一起纳入迁移体系，避免 JSON 文件并发写入时损坏，也能在重开账期时精确恢复。现在重开账期会用归档里的整份 source-config 覆盖当前配置，关账后新增的数据源会丢失（`periods.py:166`）。
3. **查询进程**：每次查询都新建 Python 进程并重新导入 wren 和 duckdb，冷启动在 1 秒以上，节点多时总耗时主要花在启动上。可以保留进程隔离，改为常驻的 2–3 个工作进程（`multiprocessing` 加任务队列，每个任务单独计时，超时就杀掉进程并补一个新的）。先用计时数据确认瓶颈再动手。
4. **`query()` 执行两次查询**（`engine_adapter.py:57`，先 COUNT 再 LIMIT）：大查询耗时翻倍。可以用 `SELECT *, COUNT(*) OVER () AS __total ... LIMIT n` 一次取回，或者先取前 n+1 行判断是否截断，只在用户需要时再计数。
5. **状态与审计**：运行、人工确认、关账、重开都写入 `audit_log`（谁、何时、做了什么、前后差异）。现在的操作人只有关账时填写的“业务确认人”一项。

---

## 6. 测试策略

**现状**：只隔离了 `STATE_DB`。数据目录、`source-config.json`、`model-config.json` 都是真实文件；`query_worker.py:21` 把 `data_root` 写死为代码目录下的 `data/`，无法重定向。测试断言固定行数（139,203 / 252,391），每月换数据后就会失败；依赖的真实文件也不在仓库里，CI 无法运行。

**目标**：

1. **路径可配置**：新增 `CLOSEWREN_DATA_DIR` 环境变量，由 `config.py` 统一读取，`server`、`periods`、`query_worker`、`import_data` 全部使用它。测试的 `setUpClass` 创建临时目录并设置该变量，子进程会自动继承。
2. **合成夹具**：`tests/fixtures/make_fixtures.py` 用 openpyxl 生成确定性的 xlsx（几百行，覆盖中文列名、重复列名、稀疏单元格、日期、布尔、大金额、公式缓存值），xls 夹具预先生成并提交（体积很小）。导入这些夹具后得到的行数是固定常量。
3. **分层**（Wren 语义层相关的测试见第 4.4 节）：
   - `tests/unit/`：validate 白名单、SQL 构造、指纹、build_request/parse_response、期间规范化。不启动子进程，秒级完成。
   - `tests/integration/`：夹具导入 → 保存方案 → 运行 → 下钻/导出 → 关账 → 新建账期 → 重开，全部走 HTTP 接口。
   - `tests/e2e/`：第 3 节 M2 中的 Playwright 冒烟脚本。
   - 保留一个 `tests/real_data/`，只在设置了 `CLOSEWREN_REAL_DATA=1` 时运行，断言改为结构性质（按“期间”分组的行数之和等于总数），不断言固定数字。
4. **必须补上的用例**：C1 过期结果关账、S1 局域网访问控制、S3 函数和外部访问、C2 刷新并发、C3 两种格式一致、C4 期间格式、C6 下钻三种写法、R1 运行期间并发的前台查询、账期的关账/新建/重开完整链路、方案版本冲突。
5. **CI**：GitHub Actions 在 `windows-latest`（主要运行环境）和 `ubuntu-latest` 上执行 unit 和 integration 测试。Wren 目前从相邻源码安装，需要先确认能否改为固定版本的 wheel（放在内部制品库或 Release 附件里），否则 CI 无法安装依赖，这一点先与导师确认。

---

## 7. 执行节奏建议

| 迭代 | 内容 | 产出 |
|---|---|---|
| 第 1 周 | T1 第 1–2 条（路径可配置、合成夹具）→ S1、S2、S3、S4、C1 | 每项单独提交并附测试；README 同步 |
| 第 2 周 | R1、R2、C2（先做最小方案）、C3、C5、M3、P1x | 导入与运行稳定；删除死代码 |
| 第 3 周 | W0、W2（Wren 类型与策略）、C4、C6、U1、U2、R3、R4 | 数据源配置界面，试运行，方案历史，Wren 结构化错误 |
| 第 4–5 周 | W1 MDL 版本化、W3 关系、W4 计算字段、W6 口径追溯 | 语义层成为唯一口径来源，运行和归档带 mdl_hash |
| 第 6 周起 | M1 拆分后端、M2 前端整理（先格式化再模块化）、W5/W7/W8、W9 技术验证、第 5 节架构项 | 每步都有 Playwright 冒烟和集成测试护航 |

## 8. 工程习惯（给实习生）

1. **小步提交**：仓库现在只有两个“打包式”提交（`Prepare sanitized source package`），无法审查和回溯。今后在仓库里直接开发，一个问题对应一个分支和一个 PR，提交说明写清“为什么”和“怎么验证的”。格式化和逻辑修改分开提交。
2. **不要提交压缩风格的代码**：源码就是源码，需要压缩时另建构建步骤，并且只对产物压缩。
3. **写文档前先对照代码**：本次发现 10 处文档与实现不一致。文档里的每一句“已实现”都应该能对应到一个测试或一段代码。
4. **错误信息**：面向用户的中文提示保留；日志里写英文加异常堆栈；不要把内部异常原文返回给前端（R2）。
5. **安全相关的改动一律附测试**：没有测试的安全措施，下一次重构时很容易被悄悄删掉，S3 的白名单就是这样失效的。
6. **遵守 `AGENTS.md`**：没有业务规则时不要自行补写；结算前、结算后各自独立校验；AI 只产出草稿；测试不能污染用户状态库。

---

## 附：本次评审的验证方式

- 通读全部 Python 与前端源码，用 grep 核对前端实际调用的 API 列表。
- 在隔离环境安装项目锁定的 duckdb 1.5.5 与 sqlglot 30.18.0，实测：
  - `validate()` 对 `getenv(...)`、`current_setting(...)`、未知函数全部放行（S3）；
  - 按 `_connect()` 的方式建立连接后，`enable_external_access` 为 true，`read_text` 可以读取本地文件；执行 `SET enable_external_access=false` 后抛出 PermissionException，且无法改回（S3）；
  - `read_ooxml_rows` 把日期读成 `'46265'`、布尔值读成 `'1'`（C3）。
- 安装项目锁定的 wrenai 0.14.0 与 wren-core-py 0.7.6，用临时 DuckDB 夹具实测第 4 节涉及的能力：`DECIMAL(38,10)` 类型导致计算字段规划失败、改为 `DECIMAL` 后正常；关系字段、计算字段、视图（需显式列名）、`denied_functions`、行级权限均可用；列级权限按字段名配置未成功；cube 未实测。
- S1、C1、R1 是按代码路径推演得出的结论，修复前请先按各节“验收标准”写出复现测试，确认问题存在。
- 没有真实数据和 Windows 环境，没有运行 `test_demo.py`，也没有进行浏览器实测。
