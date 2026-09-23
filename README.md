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

# 查看支付单与全部资金流水
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
```

---

## 2. 运行测试

测试使用标准库 `unittest`，**不依赖额外组件**；每个用例都使用独立的临时 *文件型* SQLite
库（与生产同一条持久化路径），日志会打印幂等键、请求参数、判定结果与失败原因。

```bash
# 在仓库根目录执行
PYTHONPATH=. python3 -m unittest discover -t . -s tests -v

# 或只跑某一类
PYTHONPATH=. python3 -m unittest tests.test_concurrency -v   # 并发重复提交
PYTHONPATH=. python3 -m unittest tests.test_ledger -v        # 状态机/部分金额/幂等重放/重启
PYTHONPATH=. python3 -m unittest tests.test_reconciliation -v  # 三类对账差异 + 只读保证
PYTHONPATH=. python3 -m unittest tests.test_api -v           # HTTP 端到端
```

日志示例：

```
STEP=capture-partial | idempotency_key=k-c1 | request={'payment_id': 'p1', 'amount': 3000, 'currency': 'USD'}
JUDGEMENT=ACCEPTED | result={'entry_type': 'CAPTURE', 'amount': 3000, 'status': 'PARTIALLY_CAPTURED', 'replayed': False, ...}
STEP=capture-over | idempotency_key=k-cx | request={'payment_id': 'p3', 'amount': 1}
JUDGEMENT=REJECTED | reason_code=invalid_state_transition | message=payment 'p3' is already fully captured
```

测试覆盖（共 27 个）：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_ledger.py` | 完整状态流转、部分/全额捕获与退款、最小单位（1）边界、超额捕获、超额退款、未捕获先退款、币种不一致、非正/非整数金额、幂等重放结论一致且只产生一笔流水、同键异内容拒绝、业务失败不占用幂等键、**重启后重放与拒绝结论可复现** |
| `tests/test_concurrency.py` | 16 线程同键并发只有一笔资金影响（1 个 winner + N-1 replay）、并发授权、同键并发携带不同内容（1 个成功其余 `idempotency_conflict`）、不同键并发互不干扰、并发超额捕获资金绝不出界 |
| `tests/test_reconciliation.py` | 金额不一致 / 缺失入账 / 重复入账三类可区分差异、三类并存、币种不一致、对账前后流水逐字节不变（严格只读） |
| `tests/test_api.py` | HTTP 全链路、可区分错误码、浮点金额被请求校验拒绝、缺少幂等头被拒绝、对账端点只读 |

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

## 6. 对账判定标准

对账只消费 **资金类** 流水（`CAPTURE` / `REFUND`；`AUTHORIZE` 不发生实际结算）。
对账端使用独立的 **只读数据库连接**（文件库 `mode=ro`，内存库 `query_only=ON`），
物理上无法执行任何 INSERT/UPDATE/DELETE，且对账代码只调用读方法。

按 `(payment_id, entry_type)` 分组，组内按账务流水 `seq` 与结算流水 `external_ref` 排序后对齐比较：

| 结果码 | 判定标准 |
| --- | --- |
| `AMOUNT_MISMATCH` | 账务与结算存在可配对的一笔，但金额不一致（如 5000 vs 4999，差 1 个最小单位也会报出） |
| `MISSING_IN_LEDGER` | 结算流水比台账多：外部已结算，台账无对应入账 |
| `DUPLICATE_IN_LEDGER` | 台账流水比结算多：存在没有结算对应的重复入账；同一 `external_ref` 在结算文件中出现多次也归为重复 |
| `CURRENCY_MISMATCH` | 可配对但币种不同（附加信息类差异） |
| `MISSING_IN_STATEMENT` | 台账有、结算完全没有对应行（附加信息类差异） |
| 匹配 | 金额与币种均相等 |

对账报告分为 `matched` 与 `discrepancies` 两部分，每个 item 都带双方金额、币种与可读原因，
且对账运行任意次都不会改变台账（有专门测试逐字段比对对账前后的流水与余额）。

---

## 7. 代码结构

```
payment_ledger/
  models.py               # 枚举/数据类：Payment、LedgerEntry、请求与结果模型
  errors.py               # 可区分的业务错误码
  repository.py           # SQLite：schema、写事务、append-only 流水、幂等表、只读连接
  service.py              # 状态机、金额/币种校验、幂等执行框架、请求指纹
  reconciliation_types.py # 对账枚举与数据类
  reconciliation.py       # 只读对账引擎
  api.py                  # FastAPI 路由与错误码 → HTTP 状态码映射
main.py                   # create_app() 入口（uvicorn/fastapi cli）
tests/                    # unittest 测试（含并发、重启复现、只读对账）
```

### HTTP 错误码映射

| 业务错误码 | HTTP 状态码 |
| --- | --- |
| `invalid_amount` | 400 |
| `payment_not_found` | 404 |
| `invalid_state_transition` / `idempotency_conflict` | 409 |
| `amount_exceeded` / `currency_mismatch` | 422 |
