# 急症预警转运协同

本项目提供急症预警转运协同所需的领域事件交换约定与基础校验库。接入方使用统一的聚合标识、事件版本和带时区的发生时间，保证业务事实在不同环节之间可以复核。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/stroke_emergency_coordination/contracts.py`：不依赖第三方包的基础校验器。
- `src/stroke_emergency_coordination/models.py`：角色、分诊级别、转运阶段等领域模型与最小可见策略。
- `src/stroke_emergency_coordination/service.py`：急症预警转运协同服务。
- `tests/test_contracts.py`：契约边界检查。
- `tests/test_service.py`：业务规则检查。

当前核心对象包括emergency_call、symptom_observation、transport_task、hospital_capacity，事件类型包括CALL_RECEIVED、RISK_LOCKED、TRANSPORT_ASSIGNED、HOSPITAL_ACCEPTED、HANDOFF_RECORDED。校验器负责交换层必填字段、类型、时间和版本检查，具体业务流程在此约定上扩展。

## 协同服务规则

`CoordinationService` 在契约之上实现以下流程：

- 登记来电、症状原话、发病时间、既往风险、用药、定位授权与家属确认；同一来电人或设备在合并窗口内的重复上报合并为同一事件。
- 系统自动提示（分诊规则命中）与专业人员判断分开记录，系统提示在复盘中标注为"非诊断结论"。
- 高危信号达到阈值即锁定事件，下发禁忌指导（保持呼吸道通畅、避免口服饮水或药物、避免随意搬动）并启动转运选择。
- 补充资料只增加上下文，不降低已经触发的级别；发病时间冲突只能由指定负责人更正并保留原值。
- 医院临时满负荷只重算未出发的转运路线；位置与病史按角色最小可见，未授权定位时不保存位置。
- 急救车失联时本地状态继续推进并生成回执，恢复后只补传未确认的回执。
- 复盘时间线还原每个判断、通知与交接时刻；所有对外领域事件发出前均通过契约校验。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
