# 体育俱乐部现金头寸与投资闸门

青少年篮球俱乐部财务主管的现金头寸服务：汇总银行余额、应收会费、已签赛事合同、
工资税费、家长退款准备金与理财到期日，按最坏现金流情景计算**可投资上限**；
任何理财下单都必须经过**资金闸门**，预测缺口扩大时阻止新单并记录触发情景。

## 运行

需要 Python 3.11+（仅用标准库）：

```bash
python3 src/index.py            # 默认 0.0.0.0:8000
RUNTIME_DIR=/data PORT=8000 python3 src/index.py
```

健康检查：`GET /health`。测试：

```bash
python3 -m unittest discover -s tests
```

或 `docker compose up --build`。

## 核心设计

- **事件溯源**：所有业务变化以不可变事件追加到 `.runtime/events.log`（逐行 fsync）。
  状态全部由日志重放得到——重启即恢复，未完成的审批与风险告警继续处理。
- **三种时间分离**：业务时间（价值日/到期日，由调用方提供）、接收时间
  （`recorded_at`，服务时钟）、假设版本（`version`）。
- **幂等**：银行流水必须带 `idempotency_key`；同键重复推送只入账一次。
  流水与计划项（合同/会费/退款）两遍匹配，先扣款后签合同等乱序到达同样正确。
- **情景预测**：`base / adverse / stress` 三情景对未实现现金流施加流入延后折损、
  流出提前、退款激增；银行流水按价值日权威入账，预测只补"尚无流水"的部分，
  因此乱序与重复都不会重复计算。
- **资金闸门**：可投资上限 = 各情景 90 天逐日余额最低点 − 冻结底线
  （退款准备金 + 最低缓冲）的最小值。新订单模拟加入后，任一情景击穿底线即阻断，
  决策快照（含触发情景、首日缺口日、各情景数字、假设版本）随审批永久保存。
- **假设版本化**：管理员调整预测假设只产生新版本；已批准批次锁定其审批时的
  版本与完整闸门快照，永不被改写。
- **后台处理器**：worker 持续处理 `proposed` 订单（批准/阻断+告警）并按当前假设
  重算流动性告警（缺口出现即告警、消失自动解除）；工作项全部来自事件日志。

## 金额约定

金额一律用整数分（`*_cents`）传输与存储，同时提供两位小数字符串字段
（`amount`、`balance`、`investable_cap` 等）；支持 CNY/HKD/USD。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts` | 登记银行账户 |
| POST | `/bank/statement-lines` | 银行流水（**必须 idempotency_key**；负数为扣款；可带 ref_type/ref_id 对账） |
| POST | `/receivables` | 应收会费/赞助（kind: membership_due/sponsor_receipt） |
| POST | `/contracts` | 已签赛事合同 |
| POST | `/payrolls` | 工资税费计划 |
| POST | `/refunds` | 家长退款计划 |
| POST | `/refund-reserve` | 设置退款准备金（冻结底线的一部分） |
| POST | `/products` | 理财产品（期限天数、年化利率） |
| GET/PUT | `/assumptions` | 查看/版本化更新预测假设（不改写历史） |
| POST | `/investment/orders` | 提交理财订单（进入闸门，后台异步批准/阻断） |
| GET | `/investment/orders[/{id}]` | 订单列表/详情（含不可变决策快照） |
| GET | `/investment/maturities` | 到期回款安排 |
| POST | `/processing/run` | 同步执行一轮审批与告警处理（测试/运维用） |
| GET | `/alerts` | 风险告警（gate_blocked / liquidity_gap） |
| POST | `/alerts/{id}/resolve` | 手工解除告警 |
| GET | `/position` | 现金头寸汇总与可投资上限（?as_of=YYYY-MM-DD） |
| GET | `/forecast/daily?scenario=stress` | 按日滚动预测（标记低于底线的日期） |
| GET | `/forecast/scenario-diff` | 三情景差异对比 |
| GET | `/ledger` | 应收/应付与银行流水匹配明细（含未匹配流水） |

### 典型流程

```bash
# 1. 银行期初余额、应收应付、准备金、产品
curl -s localhost:8000/bank/statement-lines -d '{"idempotency_key":"open",...}'
# 2. 查看最坏情景可投资上限
curl -s 'localhost:8000/position'
# 3. 下单；订单先为 proposed，worker 跑闸门后变 approved/blocked
curl -s localhost:8000/investment/orders -d '{"product_id":"prd-30","amount":80000}'
# 4. 阻断原因（触发情景、首日缺口）在订单 decision.gate 与 /alerts 中
# 5. 银行扣款/回款乱序到达：同 idempotency_key 只入一次，ref 指向订单驱动生命周期
```

## 事件类型

`account.registered`、`statement.line_recorded`、`inflow.scheduled`、
`contract.signed`、`payroll.scheduled`、`refund.scheduled`、`refund.reserve_set`、
`product.defined`、`assumptions.versioned`、`order.proposed/approved/blocked`、
`alert.raised/resolved`、`risk.evaluated`。订单结算/到期状态由引用订单的银行流水
（`ref_type=investment_order/investment_maturity`）按价值日推导。

持久化目录 `.runtime/` 已在 `.gitignore` 中。
