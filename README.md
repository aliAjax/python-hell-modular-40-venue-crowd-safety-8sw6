# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突，并提供医疗转送的床位预占、任务派发、作废重选和接班接收闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机、团队冲突和床位预占约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、审计查询和单写事务（Unit of Work）。
- `src/service.py`：用例编排、权限校验、版本控制和转送/医疗点复合操作。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`transfer`为医疗转送单。

## 医疗转送闭环

指挥员只能给已分诊（`triaged`）的事件选择`active`医疗点预占床位。每个医疗点维护`capacity`、`beds_held`（预占未接收）和`patients`（已收治），约束为`patients + beds_held ≤ capacity`；收治人数只在接班指挥员确认接收后增加。

转送单（`transfer`）一条事件只保留一张，状态机为：

- `reserved`：已预占床位（创建时即占用，不增加收治数）。
- `dispatched`：已派发医疗救治任务，床位继续预占。
- `released`：派发失败后床位已退回，原单保留待重试。
- `void`：医疗点状态变化（`mark_full`/`close`）时尚未接收的转送单自动作废，床位退回、收治数不变。
- `received`：接班指挥员确认接收，预占转为收治（`beds_held-1`、`patients+1`）。

关键规则：

- 并发预占通过`BEGIN IMMEDIATE`单写事务串行化：先到的指挥员占住床位，后到的收到`409 ConflictError`（"beds are already occupied"）；同一事件的第二张转送单同样被拒绝。
- `dispatch`在一个事务内完成占床校验、救治组冲突检查、建任务并直接分派（沿用团队冲突规则）、推进事件；任务派发失败时补偿事务退回床位并把转送单置为`released`。
- 重试（对同一张`released`转送单再次`dispatch`）只重新占用一次床位，`attempts`累加，不会建出第二条任务或重复占床。
- 作废后指挥员用`reselect`改选其他医疗点，沿用同一张转送单（ID不变，`medical_point_history`记录改选轨迹），新点重新占床。
- 医疗点`mark_full`/`close`会在同一事务内作废所有未接收转送单；已接收的不受影响。

转送接口（角色：创建/派发/作废/重选为`coordinator`及以上，接收允许`operator`/`supervisor`/`coordinator`）：

- `POST /api/transfers`，请求体`{"incident_id", "medical_point_id", "beds"?}`，支持`Idempotency-Key`。
- `POST /api/entities/<transfer_id>/actions`，动作为`dispatch`（`team_id`）、`receive`（`receiver_id`、`received_at`）、`reselect`（`medical_point_id`）；`release`/`void`为系统联动动作。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
