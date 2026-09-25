# 考古年代证据服务

在考古研究协作基础服务之上扩展的**年代证据服务**。年代学实验室收到来自地层与灰坑的碳十四样品时，
必须先证明样品链路可靠，再讨论遗址年代。本服务完全离线运行（FastAPI + SQLite，内置小型校准曲线，
不依赖外部数据库、队列或网络），覆盖：

- 取样登记（以**实验室编号**为身份主键，记录地层/灰坑来源、材质、采集信息）；
- **封签交接**事件链（加封、交接、破损、重封），以事件序列保证样品身份连续；
- 前处理风险标记与处置；
- 只追加、不可覆盖的原始测值与误差；
- 离线校准：内置小型曲线、概率分布、最高密度区间（HPD）与多峰摘要；
- 校准任务保存输入摘要与算法版本，**相同输入重复提交返回同一任务结果**；
- 研究者采用/拒绝决定；
- 多份测年组成的**阶段证据集**：成员可带排除理由，发布后不可变，更换曲线或成员生成可比较的新版本。

## 环境与安装

运行环境为 Python 3.11：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

## 可靠性闸门（先证链路，再谈年代）

- 登记样品时自动写入首条 `seal` 加封事件；交接（`transfer`）必须写明代交出方与接收方，
  且交出方必须等于当前保管人，**禁止跳过交接环节**。
- 样品当前封签必须完好。封签 `break` 后，未经 `reseal` 恢复前：
  - `chain_ok=false`、`publishable=false`；
  - 不能作出"采用"决定，阶段证据集也不能发布该成员。
- 存在未处置的**高风险**前处理标记时同样禁止采用/发布；风险处置后放行。
- 原始测值表 `measurements`、封签事件表 `seal_events`、校准任务表 `calibration_tasks`
  由 SQLite 触发器保护为**只追加/不可变**；已发布的阶段版本及其成员同样不可改写。

## 校准算法（离线）

内置曲线版本 `mini-1`（日历年 BP 0–6000，每 100 年一个节点，含摆动与负斜率段，可产生多峰后验），
算法版本 `cal-gauss-grid-hdi-1`：

- 在日历网格上逐点计算似然 `N(测量值 | 曲线均值, √(测量误差² + 曲线σ²))`；
- 曲线均值与 σ 均做**线性插值**，越界用端点常量并以 `boundary_extrapolated=true` 标记；
- 后验概率**归一化到 1**；
- 最高密度区间（HPD，默认 95.4%）：1 年分箱按概率降序选取，临界分箱按线性比例计入，
  摆动区段天然呈现彼此分离的多个区间（中间为缺失区间）；
- 多峰摘要按显著鞍部切分，给出各峰日历范围、峰内概率、峰顶点，并稳定排序。

阶段证据集的组合分布使用 `independent-likelihood-product-v1`：在成员共享日历网格上把独立
似然相乘后重新归一化，再计算同样的 HPD 与多峰摘要。

## 主要 HTTP 接口（前缀 `/api/dating`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/curves`、`/curves/{version}` | 内置曲线版本与数据 |
| POST | `/samples` | 取样登记（实验室编号唯一，自动加封） |
| GET | `/samples`、`/samples/{lab_no}` | 样品列表/详情（来源、封签链、风险、测值、`chain_ok`、`publishable`） |
| POST | `/samples/{lab_no}/seal-events` | 加封/交接/破损/重封 |
| POST | `/samples/{lab_no}/risks`、`.../risks/{id}/resolve` | 前处理风险登记与处置 |
| POST | `/samples/{lab_no}/measurements` | 追加原始测值（不可覆盖） |
| POST | `/calibrations` | 直接提交测量值做校准（幂等） |
| POST | `/samples/{lab_no}/measurements/{id}/calibrate` | 对某条原始测值做校准（幂等） |
| GET | `/calibration-tasks/{id}` | 校准任务（输入摘要、算法版本、分布、HPD、多峰） |
| POST/GET | `/samples/{lab_no}/decisions` | 研究者采用/拒绝决定与历史 |
| POST/GET | `/phase-sets[/{id}]` | 阶段证据集 |
| POST | `/phase-sets/{id}/versions` | 生成新版本（成员可带 `exclusion_reason`，相同内容去重） |
| POST | `/phase-versions/{id}/publish` | 发布（不可变，发布前再次校验每个成员链路） |
| GET | `/phase-versions/{id}` | 版本详情（成员、来源、风险与置信区间） |
| GET | `/phase-versions/{a}/compare/{b}` | 版本差异：曲线、增删成员、排除项、HPD 变化 |

详情响应展示置信区间（`hpd_intervals`）、样品来源（`context_type/context_name`）、
风险标记（`risks/high_risk_open`）与版本差异。

## 测试

```bash
python -m pytest
```

数值测试覆盖：概率归一化、曲线节点/中点/边界插值、缺失区间（分离的 HPD 与多峰）、
超出曲线支撑与非法输入的失败、失败后恢复、峰的稳定排序与确定性；
流程测试覆盖：身份连续与禁止跳过交接、封签破损闸门与重封恢复、风险闸门、
测值/任务/封签/已发布版本不可变、校准幂等、采用决定取代历史、阶段版本化与版本差异、权限。

## 编译检查与冒烟

```bash
python -m compileall -q app tests
python -m app.cli smoke
```
