# 急症预警转运协同

社区急救台的急症预警转运协同服务：接线员登记**症状原话、发生时间、既往风险、用药、定位授权**，
系统按可解释规则给出分诊提示；达到高危阈值即**锁定事件**，向现场发出等待期间指导并启动转运选择，
覆盖医院接诊能力变化、急救车失联本地推进、家属确认与事后复盘。

所有业务事实都先成为**不可变领域事件**（带时区时间、聚合版本、因果/关联标识），
读模型由事件重放得到，因此每个判断、通知和交接时刻都能事后还原。

## 核心业务规则

- **自动提示 ≠ 诊断**：`TRIAGE_HINT_EMITTED` 携带命中依据且恒为 `is_diagnosis=false`；
  专业人员的结论只能通过 `PROFESSIONAL_DECISION_RECORDED` 单独记录。
- **阈值锁定、级别只升不降**：红色阈值首次命中即发出 `RISK_LOCKED` 与
  `ON_SCENE_GUIDANCE_ISSUED`（保持气道通畅、禁止喂水/喂食/服药、禁止随意搬动）；
  后续资料只增加上下文，任何事件都不能把级别调低或解除锁定。
- **发病时间冲突留痕**：冲突的原始时间全部保留并标记，只有指定角色
  （`supervisor` / `senior_dispatcher`）能发布权威更正，更正事件保留原值。
- **重复上报合并**：相同来电号码/设备再次上报，合并到既有事件或登记后忽略，不产生新病例。
- **容量只影响未出发路线**：医院临时满负荷时自动重算未出发路线并改派；
  急救车一旦出发，路线与目的地**冻结**；锁定时若暂无可接诊医院则挂起，容量恢复自动开线。
- **角色最小可见**：位置仅调度/医疗/主管可见，病史与用药仅医疗/主管可见；
  定位授权撤回后所有人都不再看到精确位置。
- **失联不阻断现场**：急救车失联时状态在车载本地继续推进；恢复连接后**仅补传尚无确认回执**
  的事件，服务端按 `event_id` 幂等去重。
- **可复盘**：`review.build_timeline` 跨聚合按时间还原判断、通知、交接时刻，
  系统提示永远标注为"非诊断"。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记的事件/聚合类型。
- `src/stroke_emergency_coordination/`
  - `contracts.py`：不依赖第三方包的交换层校验器。
  - `event.py` / `store.py`：事件信封与不可变事件存储（版本连续、ID 唯一、幂等）。
  - `triage.py`：分诊关键词规则、分级与锁定后现场指导。
  - `aggregate.py`：急症事件聚合（锁定、单调升级、时间更正、合并、授权、家属确认）。
  - `transport.py`：转运任务/急救车状态机、医院容量与候选排序。
  - `access.py`：按角色最小可见的只读投影。
  - `offline.py`：车载本地日志，只补传未确认回执。
  - `service.py`：把登记、分诊、锁定、转运、容量、离线、通知串起来的应用服务。
  - `review.py`：事后复盘时间线。
- `examples/walkthrough.py`：一条可疑卒中来电的端到端走查（生成 `data/sample_flow.json`）。
- `data/sample.json`：单事件信封联调样例；`data/sample_flow.json`：完整事件流样例。
- `tests/`：契约边界与全部业务规则测试。

## 快速开始

```bash
# 端到端走查（stdout 为事件 JSON，stderr 为复盘时间线）
python3 examples/walkthrough.py

# 重新生成完整样例
python3 examples/walkthrough.py > data/sample_flow.json
```

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests examples
```
