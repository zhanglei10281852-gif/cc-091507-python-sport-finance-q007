# 体育俱乐部现金头寸与投资闸门

面向体育俱乐部日常现金预测、赛事支出和短期理财审批的 Python 后端服务（仅标准库）。

服务汇总银行余额、应收会费、赞助款、已签赛事合同、工资税费、家长退款与理财到期回款，按**最坏现金流情景**给出可投资上限；任何投资下单必须通过资金闸门，预测缺口扩大则阻止新单并记录触发情景。银行对账与产品回款乱序/重复到达保持幂等；管理员调整预测假设只升版本，不回溯改写已批准批次；重启后未完成审批与风险告警自动继续处理。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py            # 默认 0.0.0.0:8000，状态写入 .runtime/
RUNTIME_DIR=/data PORT=8000 python3 src/index.py
```

健康检查：`GET /health`。测试：

```bash
python3 -m unittest discover -s tests
```

## 模型与口径

- **基准情景 (base)**：所有已承诺现金流按计划日发生。
- **最坏情景 (worst)**（假设可按版本调整）：
  - 赞助款延期 7 天且只到账 85%；应收会费延期 3 天、收缴 90%；
  - 家长退款提前 3 天且放大 1.5 倍；联赛报名费提前 5 天扣款；
  - 产品到期回款延迟 2 天。
- **可投资上限** = 最坏情景滚动预测窗口（默认 30 天）内最低余额 − 最低保底余额，按币种分别计算。
- **冻结资金** = 工资税费 + 最坏口径退款准备金 + 已签赛事报名费 + 最低保底 + 在投/待结算批次本金。
- 业务时间（`scheduled_date`/`value_date`）、接收时间（`received_at`）、审批时间（`decided_at`）与假设版本分别记录。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/accounts` | 开立银行账户（account_id, currency） |
| POST | `/bank/entries` | 银行对账流水（支持 `Idempotency-Key`） |
| POST | `/events` | 登记现金流事件（支持 `Idempotency-Key`） |
| GET | `/position?as_of=&horizon=` | 现金头寸：余额、冻结原因、可投资上限、双情景滚动预测 |
| GET | `/forecast?as_of=&horizon=` | 仅取按日滚动预测与情景差异 |
| GET | `/frozen` | 冻结资金原因明细 |
| GET | `/maturities` | 到期回款安排 |
| GET/POST | `/assumptions` | 查询假设历史 / 发布新版本假设 |
| POST | `/investments` | 提交投资批次（支持 `Idempotency-Key`，`auto_evaluate:true` 即过闸） |
| POST | `/investments/{id}/evaluate` | 对 proposed 批次执行资金闸门 |
| POST | `/investments/{id}/settle` | 已批准批次下单，扣减银行余额 |
| POST | `/investments/{id}/maturity` | 登记到期回款（乱序/重复幂等） |
| GET | `/investments`, `/investments/{id}` | 批次查询（含闸门快照） |
| GET | `/alerts`, POST `/alerts/{id}/ack` | 风险告警（记录触发情景）查询/确认 |
| POST | `/admin/recover` | 手动触发恢复（启动时自动执行） |

投资批次状态：`proposed → approved/blocked → settled → matured`。

## 关键行为

- **资金闸门**：以不含候选批次的预测计算可投资上限，再模拟加入批次后的每日最坏余额；任一日击穿保底或金额超过上限即阻止，批次置 `blocked` 并生成 `gate_blocked` 告警，违规明细记录日期、币种、缺口与触发情景 `worst`。批次冻结审批时的假设版本与完整预测快照。
- **假设版本化**：`POST /assumptions` 只追加新版本；已 `approved`/`blocked` 的批次不被重新判定，重复 evaluate 返回原判；新单按新版本审批。
- **幂等**：写接口接受 `Idempotency-Key` 请求头或 `idempotency_key` 字段，同键重放返回首次结果；到期回款先于结算到达也安全，重复回款不产生第二笔入账。
- **重启恢复**：状态以原子写 JSON 持久化于 `.runtime/state.json`；服务启动时自动补判所有 `proposed` 批次（按批次自身下单日），`open` 告警继续保留可确认，重复重启不产生重复告警。
