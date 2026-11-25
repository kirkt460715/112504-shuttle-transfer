# 山区接驳停运后的联程改签与优先座席

本地演练资料和对端协议桩使用合成编号。共享空间里的折叠座和轮椅位不能同占。占座回执不确立全程承诺，也不能代替实际登车事实。

`fixtures/registers.json` 保存模拟业务台账。车次接驳截止时间独立于前车到达时间，共享物理空间用车次和座位标识指代。登车事实与承运占座是不同凭证。

`fixtures/partner-exchanges.json` 给出合成对端调用的请求和回报示例。每个条目独立，文件顺序没有业务含义。请求正文按匹配的 `operation` 和 `request` 选择示例。对端只模拟设备或合作方的应答，不处理业务服务的规则。

使用 Python 3.11.2 和标准库，无需安装依赖。运行以下命令核验台账、对端联调协议与改签后端服务。

```sh
python3 -m unittest discover -v
python3 partner_stub.py --port 8765
python3 -m rebook.main --db /tmp/rebook.sqlite3 --port 8080
```

## 对端协议桩

对端只监听本机。向 `POST /operations` 提交 JSON 对象，含字符串 `id`、字符串 `operation` 和对象 `request`。首次接单记入对端日志，正常返回 200，格式为 `{"id": "...", "reply": {...}}`。条目配置为 `commit_disconnect` 时，接单会写入对端日志，但首次 HTTP 连接没有响应。按该 ID 调用 `GET /operations/<id>` 查询回报，未接单返回 404。同一个 ID 和内容的重试返回原回报，复用 ID 提交其他内容返回 409。无对应条目的请求返回 422。可通过 `--exchanges` 传入同格式的其他联调资料。

重启业务服务进行联调时可保持对端进程运行。若也需要重启桩，可通过 `--journal` 指定临时 SQLite 文件保留对端接单记录。

## 改签后端服务（`rebook/`）

服务面向三类使用者，所有写操作接受可选 `request_id` 实现调用方幂等。

- `rebook/domain.py`：纯领域规则，无 IO。轮椅需求只能落无台阶车辆的轮椅位；共享物理空间按连通分量互斥；换乘以后车 `boarding_cutoff` 独立判定；已关闭占座通道的车次只认登车事实。
- `rebook/store.py`：SQLite 与内存两套同语义台账。`allocations` 的部分唯一索引保证同一车次同一物理空间在任何“仍占用”状态下不会分配两次。
- `rebook/carrier.py`：承运方端口。HTTP 客户端对接对端桩；`CarrierUnavailable` 表示结果未明（可能已占座），`CarrierRejected` 表示明确未接单。
- `rebook/app.py`：应用服务，编排预占、占座、补查、登车归集、晚点重判、释放转座与三类视图。
- `rebook/web.py`：HTTP 接口；`rebook/loader.py` 从演练台账引导数据；`rebook/main.py` 是启动入口。

### 座位安排状态机

`pre_claimed`（本地预占、远端结果未明）→ `held`（承运方确认）→ `boarded`（实际登车）。
旁支：明确拒绝 → `failed`（可在重新确认时重试）；被替代或未到 → `releasable` → 值班台确认后 `released`。
`pre_claimed / held / boarded / releasable` 都仍占物理空间，其中前三者是可承诺的保障状态。

### 关键业务规则

1. **两段独立占座，全程才可确认**：只有方案内所有成员的全部剩余段都有保障、且相邻段满足换乘截止，方案才进入 `confirmed`。
2. **断线不双占**：发起远端占座前先写本地 `pre_claimed` 预占。对端断线时保留预占并锁定物理空间，值班台用同一操作编号 `GET` 探查或重投补查；补查成功转 `held`，确认未接单才作废。
3. **晚点重判**：前车晚点后重新评估未兑现联程；已登车段不会被重新占座，新方案保障落地前旧安排只转 `releasable` 不删除。
4. **登车事实幂等归集**：扫码回报按（旅客，车次）去重，归集到原票或替代凭证；已登车段不再发起占座，发车清单重复拉取不改变状态、不二次确认。
5. **释放转座**：他人取消释放的座位可转给等待中的同行组，但仅当该组余下各段与换乘条件都可兑现、且承运方应答齐备时才确认。
6. **旅客选择**：可接受整程改签（`accept`）、保留原确认等待新方案（`retain`，不再对其占座，方案不称已确认）、或明确同意拆分（`split`，未同意时子集覆盖视为不可行）。

### HTTP 接口一览

| 角色 | 方法与路径 | 用途 |
| --- | --- | --- |
| 旅客 | `GET /passenger/itinerary/<credential>` | 按凭证查每段能否乘坐、是否必须等售票台 |
| 售票台 | `POST /desk/cases` | 登记改签方案与成员需求 |
| 售票台 | `GET /desk/cases`、`GET /desk/cases/<id>` | 方案列表与详情 |
| 售票台 | `POST /desk/cases/<id>/confirm` | 尝试做成可兑现的整段确认 |
| 售票台 | `POST /desk/cases/<id>/replan` | 晚点后改到新联程 |
| 售票台 | `POST /desk/cases/<id>/decision` | 登记 accept/retain/split 选择 |
| 售票台 | `POST /desk/delays` | 上报前车晚点并重判 |
| 售票台 | `POST /desk/space-events` | 接收座席占用/释放回报 |
| 值班台 | `GET /desk/recovery` | 部分成行方案、结果未明操作、未释放座席 |
| 值班台 | `GET /desk/operations/<op_id>` | 补查结果未明的承运方操作 |
| 承运方 | `POST /carrier/<carrier>/boarding` | 回报登车扫码 |
| 承运方 | `GET /carrier/<carrier>/<leg>/manifest` | 应留座、已上车、可释放三个名单 |
| 承运方 | `POST /carrier/<carrier>/<leg>/allocations/no-show` | 发车时标记未到 |
| 承运方 | `POST /carrier/<carrier>/allocations/release` | 值班台确认释放，座位回到可用池 |

### 测试

- `test_domain.py`：空间互斥、需求匹配、换乘截止、拆分同意等纯规则。
- `test_app.py`：预占/断线/补查、确认条件、释放转座、晚点重规划、登车归集、清单与恢复、旅客决定、SQLite 持久化。
- `test_partner_integration.py`：与真实对端桩的 HTTP 协议联调（含 `commit_disconnect` 重投）。
- `test_web.py`：三类角色的 HTTP 端到端流程。
