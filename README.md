# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

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

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 碳十四年代证据模块（`/api/chrono`）

在讨论遗址年代之前，先用该模块证明样品链路可靠。模块完全离线：校准曲线为内置小型数据
（`INTCAL23-MINI` 斜线段+平台折返段、`MARINE23-MINI` 海洋储库版本），校准算法只用标准库。

### 证据链与状态机

样品以实验室编号（`LAB-000001`…）唯一标识，封签事件按自增序号保存，身份连续性由
“编号 + 封签序列”共同保证：

```
registered ──seal──▶ sealed ──handover──▶ in_transit ──receive──▶ received ──open──▶ opened
                          ▲ damage 在任意状态都会进入 quarantined
quarantined ──reseal──▶ sealed（必须重新 handover→receive→open，不得跳步）
```

- 禁止跳过交接：未 `seal` 不能 `handover`，未走完接收不能 `open`。
- 封签破损（`damage`，或接收时封签不完整）立即隔离；破损未恢复时前处理、测值、发布一律拒绝。
- 前处理结论为 `fail` 时不能登记测值；前处理风险（腐殖酸、根须、胶原产率等）随样品保留。
- **原始测值与误差不可覆盖、不可删除**（SQLite 触发器在数据库层强制；HTTP 也没有修改入口）。

### 校准任务

`POST /api/chrono/samples/{lab_no}/calibrations` 在 1 年等间距网格上计算后验概率：

- 保存**输入摘要**（原始测值、曲线名/版本/校验和、储库校正、算法名+版本）与 `input_hash`；
  相同输入重复提交返回**同一任务**（按内容哈希幂等）。
- 输出归一化后验、众数、68.3% 与 95.4% **最高密度区间（HPD/HPI）**，平台段自动给出
  **多峰摘要**与峰间缺失区间；置信区间带 cal BP 与 BCE/CE 标签。
- 分布被曲线边界截断时给出 `truncated_at_curve_boundary` 标记；测值完全超出曲线覆盖时
  任务落为 `failed` 并保存错误，更换曲线/参数后可重新提交恢复（得到新任务）。
- 研究者通过 `POST .../publication` 作出 `adopted`/`rejected` 决定；边界截断结果采纳前
  必须显式 `boundary_acknowledged`。发布记录**不可变**（触发器禁止改删），且只允许在
  链路完整（`opened`）、前处理未失败、校准成功时发布——封签破损后无法直接发布结果。

### 阶段证据集

`/api/chrono/phases/{code}` 把多份测年组成阶段证据：

- 每个成员必须已发布；`rejected` 的样品只能以 `excluded=true` 携带**排除理由**纳入。
- 版本是内容寻址的：成员、排除、曲线任一变化生成新版本号；相同输入返回同一版本。
- 发布后版本与成员**不可变**（触发器强制）；`GET .../diff?from=1&to=3` 比较换曲线或
  换成员的差异（新增、移除、排除变化、校准任务变化）。
- 版本与样品视图均展示置信区间、样品来源（地层/灰坑、材质）、风险标记。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/chrono/curves` | 内置曲线版本、节点数、覆盖范围、校验和 |
| POST | `/api/chrono/samples` | 取样登记，分配实验室编号 |
| GET | `/api/chrono/samples/{lab_no}` | 样品全链路、风险标记、测值、校准、发布 |
| POST | `.../seal-events` | seal/handover/receive/open/damage/reseal |
| POST | `.../pretreatments` | 前处理方法、结论（pass/caution/fail）、风险 |
| POST | `.../measurements` | 登记原始测值（不可变） |
| POST | `.../calibrations` | 提交校准（幂等），失败也持久化 |
| GET | `/api/chrono/calibrations/{task_no}` | 查询任务（含失败错误与输入摘要） |
| POST | `.../publication` | 研究者采纳/拒绝决定（不可变） |
| POST | `/api/chrono/phases` | 创建阶段证据集 |
| POST | `/api/chrono/phases/{code}/versions` | 创建版本（换曲线/成员→新版本） |
| POST | `.../versions/{n}/publish` | 发布版本（不可变） |
| GET | `.../versions/{n}` | 版本成员、置信区间、来源、风险 |
| GET | `.../diff?from=&to=` | 版本差异 |

除 `/curves` 外所有接口需要 Bearer 会话；挂到项目下的样品/阶段按项目成员角色鉴权。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。
