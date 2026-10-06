# Payment Ledger（支付资金流转与对账）

一个可本地运行的支付资金台账服务，覆盖 **授权（authorize）→ 捕获（capture）→ 退款（refund）** 的状态流转、
**幂等提交** 与 **日终对账**。所有资金动作以 append-only 流水落地，SQLite 持久化，
在并发、重放与服务重启场景下结论均可解释、可复现。

---

## 1. 本地运行

依赖 Python 3.14+ 与 [uv](https://docs.astral.sh/uv/)（也可直接用系统 `python3` + FastAPI）。

```bash
# 安装依赖（fastapi[standard]）
uv sync

# 启动 HTTP 服务（默认在当前目录使用 payment_ledger.db 文件库）
uv run fastapi dev main.py
# 或：PYTHONPATH=. uv run python -m uvicorn main:app --reload

# 自定义数据库文件位置
PAYMENT_LEDGER_DB=/tmp/my-ledger.db uv run fastapi dev main.py
```

服务启动后：

- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>

### 快速体验（curl）

所有写请求必须带 `Idempotency-Key` 请求头。金额一律为 **最小货币单位整数**（如分）。

```bash
# 授权 100.00 USD = 10000 分
curl -s -X POST localhost:8000/v1/payments/authorize \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: key-auth-1' \
  -d '{"payment_id":"P1","amount":10000,"currency":"USD"}'

# 部分捕获 30.00 USD
curl -s -X POST localhost:8000/v1/payments/P1/capture \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: key-cap-1' \
  -d '{"amount":3000,"currency":"USD"}'

# 捕获剩余全部授权金额（amount 省略 / 为 null）
curl -s -X POST localhost:8000/v1/payments/P1/capture \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: key-cap-2' \
  -d '{"currency":"USD"}'

# 部分退款 10.00 USD
curl -s -X POST localhost:8000/v1/payments/P1/refund \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: key-ref-1' \
  -d '{"amount":1000,"currency":"USD"}'

# 带结算方对外流水号的捕获（可选字段 external_ref，全局唯一）
curl -s -X POST localhost:8000/v1/payments/P2/capture \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: key-cap-p2' \
  -d '{"amount":5000,"currency":"USD","external_ref":"SETTLE-20261003-001"}'

# 查看支付单与全部资金流水（流水中可见 external_ref）
curl -s localhost:8000/v1/payments/P1

# 日终对账（传入外部结算流水）
curl -s -X POST localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"statement":[
    {"external_ref":"E1","payment_id":"P1","entry_type":"CAPTURE","amount":2999,"currency":"USD"},
    {"external_ref":"E2","payment_id":"P1","entry_type":"CAPTURE","amount":7000,"currency":"USD"},
    {"external_ref":"E3","payment_id":"P1","entry_type":"REFUND","amount":1000,"currency":"USD"},
    {"external_ref":"E9","payment_id":"P-X","entry_type":"CAPTURE","amount":50,"currency":"USD"}
  ]}'

# 窗口对账（只覆盖 seq 100 之前的流水）并留痕为一次对账运行
curl -s -X POST localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"statement":[...],"scope":{"to_seq":100},"record":true}'

# 增量对账（只覆盖自上次留痕运行之后新增的流水）
curl -s -X POST localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"statement":[...],"scope":{"incremental":true},"record":true}'

# 查看历史对账运行 / 单次运行详情 / 连续两次运行的差异变化
curl -s localhost:8000/v1/reconcile/runs
curl -s localhost:8000/v1/reconcile/runs/1
curl -s 'localhost:8000/v1/reconcile/diff?older=1&newer=2'
```

---

## 2. 运行测试

测试使用标准库 `unittest`，**不依赖额外组件**；每个用例都使用独立的临时 *文件型* SQLite
库（与生产同一条持久化路径），日志会打印幂等键、请求参数、判定结果与失败原因。

```bash
# 在仓库根目录执行
PYTHONPATH=. python3 -m unittest discover -t . -s tests -v
# 或：uv run pytest tests/ -q

# 或只跑某一类
PYTHONPATH=. python3 -m unittest tests.test_concurrency -v        # 并发重复提交
PYTHONPATH=. python3 -m unittest tests.test_ledger -v             # 状态机/部分金额/幂等重放/重启
PYTHONPATH=. python3 -m unittest tests.test_reconciliation -v     # 三类对账差异 + 只读保证
PYTHONPATH=. python3 -m unittest tests.test_reconciliation_v2 -v  # 流水号配对/窗口增量/留痕/差异变化
PYTHONPATH=. python3 -m unittest tests.test_incremental_checkpoint -v  # 空增量水位保留/重启/并发增量
PYTHONPATH=. python3 -m unittest tests.test_external_ref -v       # 对外流水号登记与兼容
PYTHONPATH=. python3 -m unittest tests.test_api -v                # HTTP 端到端
```

日志示例：

```
STEP=capture-partial | idempotency_key=k-c1 | request={'payment_id': 'p1', 'amount': 3000, 'currency': 'USD'}
JUDGEMENT=ACCEPTED | result={'entry_type': 'CAPTURE', 'amount': 3000, 'status': 'PARTIALLY_CAPTURED', 'replayed': False, ...}
STEP=capture-over | idempotency_key=k-cx | request={'payment_id': 'p3', 'amount': 1}
JUDGEMENT=REJECTED | reason_code=invalid_state_transition | message=payment 'p3' is already fully captured
```

测试覆盖（共 49 个）：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_ledger.py` | 完整状态流转、部分/全额捕获与退款、最小单位（1）边界、超额捕获、超额退款、未捕获先退款、币种不一致、非正/非整数金额、幂等重放结论一致且只产生一笔流水、同键异内容拒绝、业务失败不占用幂等键、**重启后重放与拒绝结论可复现** |
| `tests/test_concurrency.py` | 16 线程同键并发只有一笔资金影响（1 个 winner + N-1 replay）、并发授权、同键并发携带不同内容（1 个成功其余 `idempotency_conflict`）、不同键并发互不干扰、并发超额捕获资金绝不出界 |
| `tests/test_reconciliation.py` | 金额不一致 / 缺失入账 / 重复入账三类可区分差异、三类并存、币种不一致、对账前后流水逐字节不变（严格只读） |
| `tests/test_external_ref.py` | 捕获/退款登记对外流水号、同一流水号同支付单与跨支付单均拒绝（捕获与退款之间也互斥）、拒绝后幂等键不被占用可修正重试、不带流水号的老调用照旧、**升级前落过的幂等键重放结论一致**、带/不带流水号重启重放 |
| `tests/test_reconciliation_v2.py` | 带流水号与不带流水号混着对账、按流水号配对后比金额币种、两个方向的单向差异可区分、同一流水号在对账单中重复使用、窗口/增量与全量结论一致、时间窗对账、连续两次运行的新增/存在/消除差异、留痕运行重启后可复跑且结论一致、8 线程并发对账互不干扰且流水不变 |
| `tests/test_incremental_checkpoint.py` | **空增量窗口之后再来新流水**的增量场景且与同范围全量逐项一致、空窗口携带对账单行（`MISSING_IN_LEDGER` 两方向口径一致）、连续多次空跑水位稳定、空跑后重启再增量只覆盖新流水、8 线程并发增量对账水位只进不退且资金流水不变 |
| `tests/test_api.py` | HTTP 全链路、可区分错误码、浮点金额被请求校验拒绝、缺少幂等头被拒绝、对账端点只读、对外流水号 HTTP 流程与 409 拒绝、对账留痕与 diff 端点 |

---

## 3. 状态流转规则

支付单状态（`payments.status`）：

```
AUTHORIZED ──capture(部分)──────────► PARTIALLY_CAPTURED
AUTHORIZED ──capture(全额/剩余全部)─► CAPTURED
PARTIALLY_CAPTURED ──capture(剩余)─► CAPTURED
CAPTURED / PARTIALLY_CAPTURED ──refund(部分)──► PARTIALLY_REFUNDED
任意已捕获状态 ──refund(已捕获总额)──────────► REFUNDED
```

每次操作的校验顺序与拒绝原因：

1. 金额必须是 **正整数**（`0`、负数、浮点、布尔均拒绝）→ `invalid_amount`
2. 币种必须是 3 位大写字母 ISO 代码（如 `USD`）→ `invalid_amount`
3. 支付单不存在 → `payment_not_found`（HTTP 404）
4. 请求币种与支付单币种不一致 → `currency_mismatch`
5. 捕获金额 > 剩余可捕获（授权额 − 已捕获）→ `amount_exceeded`
   - 已全额捕获后再捕获 → `invalid_state_transition`
6. 退款金额 > 可退（已捕获 − 已退款）；未捕获先退款 / 退完再退 → `invalid_state_transition`

金额约束是累加不变量：

- `0 ≤ captured_amount ≤ authorized_amount`
- `0 ≤ refunded_amount ≤ captured_amount`

每次成功操作都会向 `ledger_entries` 追加一条不可变流水（带单调自增 `seq` 与幂等键），
资金路径可从流水完整重放解释；台账行只做余额汇总更新（乐观版本号 `version`）。

## 4. 幂等语义

- 客户端在每个写请求上携带 `Idempotency-Key` 头；服务端在 `idempotency` 表中以该键为主键。
- 请求内容指纹：`SHA256("authorize|capture|refund" + 规范化 JSON(参数))`
  （`sort_keys`、无多余空白），结果随首次结论一并持久化。

| 场景 | 行为 |
| --- | --- |
| 同键 + 同内容（重放，包括重启后） | 不产生新流水，返回与首次**完全一致**的结论（金额、状态、已捕获/已退款），响应中 `replayed=true` |
| 同键 + 不同内容 | 明确拒绝 `idempotency_conflict`（HTTP 409），不产生任何资金影响；重启后同样拒绝 |
| 同键并发 | 数据库主键约束 + `BEGIN IMMEDIATE` 写事务：恰好 1 个请求落库，其余全部读到首次结论 |
| 业务失败（如超额） | 失败不写幂等记录，同一个键允许修正后重试 |

并发安全由两层保证：进程内 `RLock` 串行化写事务；跨进程由 SQLite 写锁 + `busy_timeout` 串行化，
`idempotency.idempotency_key` 的 PRIMARY KEY 是“同一键只能落一笔”的最终硬约束。

## 5. 金额精度约定

- 所有金额字段（请求、数据库、对账）均为 **整数、最小货币单位**（例如人民币分、美分）。
- 系统内不存在任何 float/double 金额运算；HTTP 层用 Pydantic 严格整数模式拒绝 `10.5` 这类请求体。
- 币种为 ISO 4217 三位字母代码，作为不参与金额换算的独立维度；跨币种操作直接拒绝。
- 数据库列类型为 `INTEGER`，从存储层杜绝小数误差。

## 6. 对外流水号（external_ref）登记

捕获与退款请求可携带可选字段 `external_ref`（结算方给出的对外流水号），
登记后该笔资金流水与结算流水一一对应：

- **全局唯一**：同一个 `external_ref` 全库只能挂到一笔资金流水上——跨支付单不行，
  捕获与退款之间也不行。重复登记拒绝 `duplicate_external_ref`（HTTP 409），
  由 `ledger_entries.external_ref` 上的部分唯一索引（`WHERE external_ref IS NOT NULL`）
  在数据库层硬保证；唯一性冲突与幂等键冲突可区分（前者 `duplicate_external_ref`，
  后者 `idempotency_conflict`）。
- **被拒绝的重复登记不产生任何资金影响**，也不占用幂等键，修正流水号后可原键重试。
- **老调用方式不受影响**：不带 `external_ref` 的请求完全按原逻辑执行，流水上该列为 `NULL`。
- **老幂等键兼容**：`external_ref` 只在实际提供时才计入请求指纹，因此升级前落库的
  幂等键在升级后重放，指纹不变、返回与首次完全一致的结论（有专门测试模拟升级前的
  库存记录直接重放）。
- 老库文件自动迁移：启动时 `ALTER TABLE ledger_entries ADD COLUMN external_ref`，
  历史行该列为 `NULL`。

## 7. 对账判定标准

对账只消费 **资金类** 流水（`CAPTURE` / `REFUND`；`AUTHORIZE` 不发生实际结算）。
对账端使用独立的 **只读数据库连接**（文件库 `mode=ro`，内存库 `query_only=ON`），
物理上无法执行任何 INSERT/UPDATE/DELETE，且对账代码只调用读方法；
留痕只写独立的 `reconciliation_runs` 表，任何已落地的资金流水都不会被改动或删除。

**配对口径**（按优先级）：

1. **按对外流水号一一配对**：台账中登记了 `external_ref` 的流水，与对账单中相同
   `external_ref` 的行配对，配上后再比金额与币种。
2. **老数据位置对齐**：未登记流水号的台账流水（`external_ref IS NULL`）与按流水号
   配对后剩余的对账单行，仍按升级前的方式对齐——按 `(payment_id, entry_type)` 分组，
   组内按账务 `seq` 与结算 `external_ref` 排序后逐位比较。因此纯老数据的对账结果
   与升级前完全一致，不会冒出新差异。

| 结果码 | 判定标准 |
| --- | --- |
| `AMOUNT_MISMATCH` | 已配对（按流水号或按位置），但金额不一致（差 1 个最小单位也会报出） |
| `MISSING_IN_LEDGER` | **外部已结算、台账没记**：对账单行没有对应台账流水 |
| `MISSING_IN_STATEMENT` | **台账记了、外部没结算**：台账流水登记的 `external_ref` 在对账单中不存在 |
| `DUPLICATE_IN_LEDGER` | 位置对齐口径下台账流水比对账单多：存在没有结算对应的重复入账 |
| `DUPLICATE_EXTERNAL_REF` | 同一个 `external_ref` 在对账单中出现多次（台账侧写入时已被唯一索引拦截） |
| `CURRENCY_MISMATCH` | 已配对但币种不同（附加信息类差异） |
| 匹配 | 金额与币种均相等 |

两个方向的单向差异（`MISSING_IN_LEDGER` vs `MISSING_IN_STATEMENT`）是两种不同的结果码，
不会糊在一起。对账报告分为 `matched` 与 `discrepancies` 两部分，每个 item 都带双方金额、
币种、流水号与可读原因；输出按稳定键排序，同样输入任何时候重跑结论逐项一致。

## 8. 对账留痕、复跑与增量

- **留痕**：`POST /v1/reconcile` 带 `"record": true`（或服务端
  `reconcile_and_record`）会把本次结论连同**解析后的范围**与**对账单摘要**
  （SHA-256）追加到 `reconciliation_runs` 表（append-only）。服务重启后
  `GET /v1/reconcile/runs/{id}` 仍可取回；拿同样的对账单与范围再跑一遍，
  结论与留痕逐项一致（有重启复跑测试）。
- **窗口对账**：`scope` 支持 `from_seq`/`to_seq`（按账务流水号）与
  `from_time`/`to_time`（按入账时间，可圈定某个营业日），只覆盖范围内的流水。
- **增量对账**：`"scope": {"incremental": true}` 自动从上次留痕运行覆盖到的最大
  `seq`（水位 `max_seq`，即该次结论所覆盖的最高账务流水号）**之后**开始，解析为
  具体窗口 `last_max_seq < seq`。因为过滤只按流水自身属性（seq/时间），窗口/增量
  跑出的结论与全量对账在同一范围内的结论一致（有一致性测试逐项比对）。
- **水位怎么推进与保留**：一次留痕运行的水位取“本次窗口内实际覆盖到的最高 `seq`”；
  窗口内没有任何可对流水时，**水位保持在本次起点（上次的 `max_seq`）不动**，只有显式
  给定 `to_seq` 的空窗口才会把水位推进到 `to_seq`。因此某个营业日没有任何新资金动作、
  定时任务照常空跑一次时，不会把“上次对到哪”丢掉；之后再来新流水继续增量，仍然只覆盖
  `last_max_seq` 之后新增的那部分，已经对上的老流水不会被重新配对或重新报差异，
  连续多次、反复空跑行为也保持一致。空跑本身仍是一条 append-only 运行记录
  （结论为空），其结论与把全量对账限定在同一空范围内的结论逐项一致；空窗口里若仍带了
  对账单行，会照常报 `MISSING_IN_LEDGER`，口径与全量限定范围相同。
- **水位与结论同事务落库**：增量对账“读取上次水位 → 解析窗口 → 计算结论 → 落运行记录
  与新水位”在同一个写事务内完成（进程内由仓储写锁串行化，跨进程由 SQLite 写锁 +
  `busy_timeout` 串行化）。并发发起增量对账时，先拿锁的运行覆盖新窗口并推进水位，
  其余运行拿锁后读到的是已推进的水位，只会在新的（空）窗口上落结论；**任何运行都不会
  把水位覆盖成更早的值**，也不会改动任何已落地的资金流水（资金表对账全程不写）。
  水位本身随运行记录持久化，进程重启后用同样的输入复跑结论逐项一致。
- **连续两次运行的差异变化**：`GET /v1/reconcile/diff?older=A&newer=B`
  （或服务端 `diff_runs`）把两次留痕的差异项按稳定签名比对，输出
  `new_discrepancies`（这次新冒出来）、`persisting_discrepancies`（仍存在）、
  `resolved_discrepancies`（已消除）。
- **并发对账**：留痕写入由仓储写锁串行化，并发发起的每次对账各自落一行独立运行
  记录，结论互不串扰；对账全程不触碰资金流水表（有 8 线程并发全量对账 + 流水逐字段
  比对、以及 8 线程并发增量对账不回退水位的测试）。

### 8.1 本地复现空跑导致的水位回退（修复前行为）与验证

```bash
# 1) 跑全部用例（新增的空增量回归测试在 test_incremental_checkpoint.py）
PYTHONPATH=. python3 -m unittest tests.test_incremental_checkpoint -v
PYTHONPATH=. python3 -m unittest discover -t . -s tests -v
```

复现场景（也可直接用 HTTP 端点对 `payment_ledger.db` 操作）：

1. 记一笔带 `external_ref` 的捕获，做一次留痕对账（水位推进到该流水的 `seq`）；
2. 不新增任何流水，发起一次 `"incremental": true` 的空增量对账（模拟无资金动作营业日的
   定时任务）；
3. 再新增一笔流水，继续增量对账。

修复前第 2 步会把 `max_seq` 落成 `0`，第 3 步从 `seq=0` 开始，把第 1 步已对上的老流水
重新报成 `MISSING_IN_STATEMENT`，且结论与同范围全量对账对不上；修复后第 2 步水位保持
不变，第 3 步只覆盖新增流水。可通过 `GET /v1/reconcile/runs` 查看每次运行解析后的
`scope`（`from_seq`）与结论，确认空跑后 `from_seq` 不回退、老流水不再出现。

## 9. 兼容范围

| 老资产 | 升级后行为 |
| --- | --- |
| 不带 `external_ref` 的捕获/退款调用 | 完全照旧，流水 `external_ref` 为 `NULL` |
| 升级前落库的幂等键 | 请求指纹算法对无流水号请求不变，重放返回与首次一致的结论 |
| 老库文件 | 启动时自动加列（`external_ref` 可空）并建部分唯一索引，历史数据不受影响 |
| 无流水号的历史流水参与对账 | 仍按 `(payment_id, entry_type)` 位置对齐，结论与升级前一致 |
| 无流水号请求的对账端点 | `POST /v1/reconcile` 不传 `scope`/`record` 时行为与升级前一致 |

---

## 10. 代码结构

```
payment_ledger/
  models.py               # 枚举/数据类：Payment、LedgerEntry（含 external_ref）、请求与结果模型
  errors.py               # 可区分的业务错误码（含 duplicate_external_ref）
  repository.py           # SQLite：schema 与迁移、写事务、append-only 流水、幂等表、
                          #         对账运行留痕表、只读连接
  service.py              # 状态机、金额/币种/流水号校验、幂等执行框架、请求指纹
  reconciliation_types.py # 对账枚举、范围（Scope）、运行（Run）与差异变化（Diff）数据类
  reconciliation.py       # 只读对账引擎：流水号配对 + 老数据位置对齐、窗口/增量、留痕与 diff
  api.py                  # FastAPI 路由与错误码 → HTTP 状态码映射
main.py                   # create_app() 入口（uvicorn/fastapi cli）
tests/                    # unittest 测试（含并发、重启复现、只读对账、流水号与留痕）
```

### HTTP 错误码映射

| 业务错误码 | HTTP 状态码 |
| --- | --- |
| `invalid_amount` | 400 |
| `payment_not_found` | 404 |
| `invalid_state_transition` / `idempotency_conflict` / `duplicate_external_ref` | 409 |
| `amount_exceeded` / `currency_mismatch` | 422 |
