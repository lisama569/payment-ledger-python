# Payment Ledger（支付资金流转与日终对账 · 本地可运行）

一套基于 FastAPI + 本地 JSON 文件持久化的支付记账服务，覆盖**授权（Authorize）→
捕获（Capture，支持部分/全额）→ 退款（Refund，支持部分/全额）**的状态流转，
**幂等提交**（并发/重放只产生一笔资金影响），以及与外部结算流水的**日终对账**。

> 设计目标是“资金可解释、可复现”：资金流水 append-only 不可变；写操作和幂等结论
> 都落地磁盘；对账严格只读，不改动任何已落地记录。

## 目录结构

```
app/
  errors.py         # 稳定的业务失败原因码与 LedgerError
  models.py         # 领域类型（Payment / Transaction / 幂等记录 / 对账模型）
  store.py          # JSON 文件持久化（进程锁 + 原子替换写盘）
  service.py        # 核心：状态流转 + 金额校验 + 幂等控制 + 日志
  reconciliation.py # 日终对账（只读）
  api.py            # FastAPI 路由、Pydantic 入参、错误码->HTTP 映射
main.py             # 服务入口（uvicorn/fastapi 加载 app.api:app）
tests/              # 39 个测试：状态机/幂等并发/对账/HTTP
data/               # 默认数据目录（ledger.json，首次写入自动创建）
```

## 快速开始

需要 Python 3.14+（由 `uv` 管理）：

```bash
uv sync                       # 安装依赖（含 pytest/httpx）
uv run fastapi dev main.py    # 开发模式，默认 http://127.0.0.1:8000
# 或指定端口与数据文件：
LEDGER_FILE=/tmp/my-ledger.json uv run fastapi run main.py --port 8000
```

交互式文档：http://127.0.0.1:8000/docs

## HTTP 接口

所有写接口都必须带 `idem_key`。金额 `amount` 为**最小货币单位整数**；
捕获/退款传 `null` 表示“就当前余额全额”。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/payments/authorize` | 授权，入参 `{idem_key, amount, currency}` |
| POST | `/v1/payments/capture` | 捕获，入参 `{idem_key, payment_id, amount, currency}` |
| POST | `/v1/payments/refund` | 退款，入参同上 |
| GET  | `/v1/payments/{id}` | 查询支付单（状态与累计金额） |
| POST | `/v1/reconcile` | 对账，入参 `{settlements:[{txn_id, amount, currency}, ...]}` |
| GET  | `/health` | 健康检查 |

### curl 示例

```bash
# 1) 授权 1000（单位：分）
curl -s -X POST localhost:8000/v1/payments/authorize \
  -H 'Content-Type: application/json' \
  -d '{"idem_key":"auth-1","amount":1000,"currency":"CNY"}'
# -> result.payment_id = pay_xxx，status=AUTHORIZED

# 2) 部分捕获 600
curl -s -X POST localhost:8000/v1/payments/capture \
  -H 'Content-Type: application/json' \
  -d '{"idem_key":"cap-1","payment_id":"pay_xxx","amount":600,"currency":"CNY"}'
# -> status=PARTIALLY_CAPTURED

# 3) 退款（amount=null 表示退全部已捕获金额）
curl -s -X POST localhost:8000/v1/payments/refund \
  -H 'Content-Type: application/json' \
  -d '{"idem_key":"ref-1","payment_id":"pay_xxx","amount":null,"currency":"CNY"}'

# 4) 日终对账（外部结算流水，按 txn_id 关联）
curl -s -X POST localhost:8000/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"settlements":[{"txn_id":"txn_ab...","amount":600,"currency":"CNY"}]}'
```

## 金额精度约定

- 一切金额都是最小货币单位的 **Python `int`**（如人民币“分”），
  服务端入参做 `strict int` 校验，`1.5` 这类浮点会在 HTTP 层被 422 拒绝；
  服务层额外拒绝 `float` 与 `bool`（`bool` 是 `int` 子类）。
- 金额必须 **> 0**，否则 `INVALID_AMOUNT`。
- 全程只做整数加减与比较，**不出现任何浮点运算**，因此不存在舍入误差。

## 状态流转规则

支付单状态由累计金额字段**派生**（不单独存储），状态机如下：

```
AUTHORIZED
  │  capture(部分)
  ├──────────────► PARTIALLY_CAPTURED ──capture(剩余全额)──► CAPTURED
  │  capture(全额)
  └──────────────────────────────────────────────────────► CAPTURED

任意“已捕获 > 0”的状态
  │  refund(部分)
  ├──────────────► PARTIALLY_REFUNDED ──refund(剩余全部)──► REFUNDED
  │
  └ refund(=captured_amount) ───────────────────────────► REFUNDED
```

判定规则（按顺序）：

**捕获**
1. 支付单不存在 → `PAYMENT_NOT_FOUND`
2. 币种与授权不一致 → `CURRENCY_MISMATCH`
3. 已发生过退款 → `INVALID_STATE_TRANSITION`（退款一旦开始即不可再捕获）
4. 可捕获余额为 0（已全额捕获）→ `INVALID_STATE_TRANSITION`
5. 请求金额 > 可捕获余额 → `OVER_CAPTURE`
6. `amount=null` 解析为“剩余可捕获余额”（全额捕获）

**退款**
1. 支付单不存在 → `PAYMENT_NOT_FOUND`
2. 币种不一致 → `CURRENCY_MISMATCH`
3. 尚未捕获（captured=0）→ `INVALID_STATE_TRANSITION`
4. 可退款余额为 0（已全额退款）→ `INVALID_STATE_TRANSITION`
5. 请求金额 > 可退款余额 → `OVER_REFUND`
6. `amount=null` 解析为“剩余可退款余额”（全额退款）

> 授权仅冻结额度、不产生结算，因此不参与对账；捕获与退款才是资金出入账。
> 资金流水（AUTHORIZE/CAPTURE/REFUND）只追加，任何接口都不修改/删除历史流水。

### 失败原因码（稳定，可程序化区分）

| code | 含义 | HTTP |
| --- | --- | --- |
| `INVALID_AMOUNT` | 金额非正整数/浮点/bool | 422 |
| `PAYMENT_NOT_FOUND` | 支付单不存在 | 404 |
| `CURRENCY_MISMATCH` | 币种不一致 | 422 |
| `OVER_CAPTURE` | 超额捕获 | 422 |
| `OVER_REFUND` | 超额退款 | 422 |
| `INVALID_STATE_TRANSITION` | 非法状态迁移 | 422 |
| `IDEMPOTENCY_CONFLICT` | 同幂等键但请求内容不同 | 409 |

## 幂等语义

每个写请求带 `idem_key`，服务端在**同一把存储锁**内完成
“查键 → 校验 → 落地流水 → 写幂等结论”：

- **并发去重**：同一 `idem_key` 的并发请求，只有一个线程真正产生资金影响，
  其余线程拿到与首次完全相同的结论（`replayed=true`、同一 `txn_id`、同一金额）。
- **重放一致**：键命中且请求指纹一致时，原样返回首次结论；
  首次若为业务失败（如超额捕获），重放仍以**同一个原因码**失败，且不会补记流水。
- **同键异参拒绝**：请求体（操作类型 + payment_id + 金额语义 + 币种）规范化后做
  SHA-256 指纹；指纹不一致直接 `IDEMPOTENCY_CONFLICT`，不产生任何资金影响。
  注意“部分捕获 300”与“全额捕获(null)”语义不同，指纹也不同。
- **重启可复现**：幂等记录（成功/失败结论 + 指纹）与资金流水都持久化到
  `LEDGER_FILE`，进程重启后重放结论、冲突结论依旧一致。
- **失败也幂等**：业务失败同样占用幂等键并持久化，避免客户端安全重试时
  “先失败后换条件又成功”造成歧义。

## 对账判定标准

`POST /v1/reconcile` 入参是外部结算流水列表，以 **`txn_id`** 与内部
CAPTURE/REFUND 流水关联。对账过程只调用只读快照，**绝不写盘/改动流水**。

| 差异类型 | 判定标准 |
| --- | --- |
| `MISSING_SETTLEMENT` | 内部有 CAPTURE/REFUND 流水，外部无任何对应入账（缺失入账） |
| `AMOUNT_MISMATCH` | 同一 `txn_id` 金额不一致 |
| `SETTLEMENT_CURRENCY_MISMATCH` | 同一 `txn_id` 币种不一致（币种优先于金额判定） |
| `DUPLICATE_SETTLEMENT` | 外部同一 `txn_id` 出现多笔（重复入账，多出的每笔算一条） |
| `ORPHAN_SETTLEMENT` | 外部流水在内部找不到对应 CAPTURE/REFUND 流水 |

- 金额与币种都一致计为一笔匹配（重复组的第一笔可计匹配，其余记重复）。
- 报告按 `(txn_id, 差异类型)` 排序，**相同输入多次对账结果完全一致**；
  打乱输入顺序同样可复现（重复组内部按金额、币种确定性排序）。
- AUTHORIZE 不纳入对账（无资金出入账）。

## 本地验证方式

```bash
# 全部测试（39 个），并发重复提交/非法迁移/部分金额边界/三类对账差异均有覆盖
uv run pytest -q

# 同时在控制台查看业务日志（幂等键、请求参数、判定结果、失败原因）
uv run pytest -q -o log_cli=true -o log_cli_level=INFO

# 只跑某一类
uv run pytest tests/test_idempotency.py -q      # 含 16/24/32 线程并发重复提交
uv run pytest tests/test_state_machine.py -q    # 状态迁移与边界
uv run pytest tests/test_reconciliation.py -q   # 金额/缺失/重复/孤儿/币种 + 只读
uv run pytest tests/test_api.py -q              # HTTP 状态码与原因码
```

### 测试如何覆盖并发重复提交

`tests/test_idempotency.py` 用 `ThreadPoolExecutor`（最多 16 线程）同时提交
24~32 个相同 `idem_key` 的授权/捕获请求，断言：

- CAPTURE/AUTHORIZE 流水各只有 1 条，支付单金额不会被重复累加；
- 恰好 1 个 `replayed=false`，其余全部 `replayed=true` 且 `txn_id` 相同。

### 日志字段

服务用标准 `logging`（logger 名 `payment_ledger.service` /
`payment_ledger.reconciliation`）打印中文结构化行，例如：

```
操作成功 idem_key=cap-1 type=CAPTURE params={'payment_id':'pay_x','amount':600,'currency':'CNY'} 判定=首次成功 result={...}
重放成功结论 idem_key=cap-1 type=CAPTURE 判定=重放(成功) result={...}
操作失败 idem_key=cap-x type=CAPTURE params={...} 判定=拒绝 reason=OVER_CAPTURE
幂等冲突 idem_key=cap-1 ... 判定=拒绝 reason=IDEMPOTENCY_CONFLICT
对账 txn_id=txn_ab.. 内部=600/CNY 外部=599/CNY 判定=AMOUNT_MISMATCH
```

## 持久化与并发说明

- 数据默认写入 `data/ledger.json`，可用环境变量 `LEDGER_FILE` 覆盖。
- 写盘走“临时文件 + `os.replace` 原子替换 + fsync”，崩溃不会留下半写文件。
- 并发安全面向**单进程多线程**（FastAPI 线程池/测试线程池）；
  多进程部署请改用带行锁/事务的外部存储（当前定位为本地单机版）。
