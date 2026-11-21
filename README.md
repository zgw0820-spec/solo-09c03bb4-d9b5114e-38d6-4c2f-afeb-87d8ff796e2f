# 冷库超温告警历史库重建

冷库监控库重建后，一个月的历史超温告警还在 CSV 里。本服务让值班员选择 UTC
月份上传 CSV，把这批历史告警作为月分区挂接到 PostgreSQL 的告警父表上；装载期间
和挂接失败后，补报与按月查询都不受影响。

## 功能

- **告警父表按 UTC 月分区**：`alarm` 是 `RANGE (occurred_month)` 分区表，另有一个
  `DEFAULT` 分区兜底。挂接前收到的迟到告警先落在默认分区。
- **上传即校验、暂存不生效**：CSV 先解析、逐行校验，再写入 `alarm_staging`。
  行有问题时整批退回、不暂存；校验通过后只是"暂存"，必须再点"挂接月分区"。
- **挂接月分区**：把该月默认分区里的行和 CSV 历史行一起写进新分区表，再
  `ALTER TABLE alarm ATTACH PARTITION`。挂接后从父表既能查到历史告警，也能查到
  装载期间已接收的补报；当月后续补报直接写入新分区。
- **同月同号去重/冲突**：同月同号且内容一致只保留一条（补报优先）；
  内容有出入则整批拒绝挂接，并在页面上报出冲突的告警号。
- **失败不影响父表**：挂接在一个事务里完成，任何失败都回滚——历史批次不生效，
  默认分区里已接收的告警原样可查，父表继续接收补报，批次可重试。
- **同月并发挂接最多成功一次**：按月加 PostgreSQL 咨询锁，其余尝试被明确退回。
- **同月重挂（手工 DETACH 后）**：值班员手工 `ALTER TABLE alarm DETACH PARTITION`
  摘下系统生成的月分区后，旧月表（仍叫 `alarm_p_YYYYMM`）保留为离线表。上传替换
  CSV 并在批次页发起挂接时：
  - 旧月表还在、表名/索引名被占用也能挂接——挂接事务会先把离线表改名腾位，再用
    新分区占用标准名称；任何一步失败整体回滚，离线表原名恢复。
  - 新 CSV 的历史告警整体替换旧历史；离线月表与默认分区里已接收的补报（含装载
    期间新到的补报）全部并入新分区，同月同号同内容只留一条并优先记为补报。
  - 时间、库房或温度有出入时报出冲突告警号并退回新批；离线月表、默认分区和批次
    暂存数据原样保留，修正 CSV 后可重试。
  - 挂接成功后旧批次标记为 `superseded`，新批次的 `attached` 状态与新分区在同一
    事务内一起生效。
- **Docker Compose** 启动 Web（FastAPI）与 PostgreSQL。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动时应用会自动建表（`db/schema.sql` 幂等）。

## CSV 格式

必须包含告警号、发生时间、库房号、温度四列，中英文表头都认：

```csv
告警号,发生时间,库房号,温度
ALM-2026-0001,2026-10-03T01:00:00Z,K1,-18.5
ALM-2026-0002,2026-10-04 02:30:00,K2,-21
```

- 时间按 UTC 解释；不带时区的按 UTC 处理，`Z` 与 `+08:00` 之类偏移都支持。
- 发生时间换算成 UTC 后必须落在所选月份内，否则该行报错、整批退回。
- 温度按 `numeric` 存储，`-18.5` 与 `-18.50` 视为相同内容。
- 支持 UTF-8（含 BOM）、GB18030 编码。

## 页面

| 路径 | 作用 |
| --- | --- |
| `/` | 上传 CSV、查看批次和按月统计 |
| `/batches/{id}` | 批次详情、冲突报告、执行挂接 |
| `/months/YYYY-MM` | 从父表查询该月全部记录及其所在分区 |
| `/late` | 补报单条告警 |

## 数据模型

- `alarm`：父表。`PRIMARY KEY (occurred_month, alarm_no)` 保证同月同号唯一。
  `occurred_month` 是分区键（PostgreSQL 不允许生成列作分区键），由应用写入，
  并用 CHECK 约束校验它与 `occurred_at` 的 UTC 月份一致。
- `alarm_default`：默认分区，接收所有尚未挂接月份的告警。
- `alarm_batch`：上传批次（`staged` / `rejected` / `attached` / `superseded`），
  保留冲突报告；同月重挂成功后，此前的 `attached` 批次转为 `superseded`。
- `alarm_staging`：批次暂存行，挂接失败时原样保留，方便排查和重试。

### 手工 DETACH 与重挂运行手册

```sql
-- 摘下系统生成的月分区（表本身保留为离线表，名称不变）
ALTER TABLE alarm DETACH PARTITION alarm_p_202610;
```

DETACH 之后该月在父表中暂时查不到离线表内的记录；新到补报继续写入
`alarm_default`，月份页会提示“离线，待重挂”。随后在页面上传替换 CSV、进入批次页
点击“挂接月分区”即可。挂接事务内部会把离线表改名为
`alarm_retired_YYYYMM_<batch_id>` 腾出标准表名与索引名，新分区挂接成功后删除该
退役表；若撞号退回或中途失败，事务回滚，退役表恢复为原离线表，数据不丢。

## 测试与试跑

集成测试需要可用的 PostgreSQL：

```bash
pip install -r requirements.txt
TEST_DATABASE_URL=postgresql://alarm:alarm@localhost:5432/alarms_test pytest -q
```

对已启动的部署做端到端试跑（覆盖补报、挂接、撞号退回、并发挂接，以及手工
DETACH 后的同月重挂与重挂撞号退回）：

```bash
docker compose up --build -d
pip install requests psycopg[binary]
TRIAL_DATABASE_URL=postgresql://alarm:alarm@localhost:5432/alarms \
  python scripts/trial_run.py http://localhost:8000 2026-10
```

第三个参数是起始月份，脚本会使用它和之后三个月。已挂接过的月份不能重复使用，
需要换月份或 `docker compose down -v` 重建数据卷。脚本通过 `TRIAL_DATABASE_URL`
直连数据库执行手工 DETACH（模拟值班员操作）；不设置时使用 compose 默认地址。

## 已知限制

- 单进程/单库假设：月度咨询锁在数据库内生效，多实例部署同样有效；但挂接是
  同步请求，超大 CSV（百万行级）会让页面等待较久。
- 重挂仅支持“分区已 DETACH、离线表仍保留标准名 `alarm_p_YYYYMM`”的场景；若离线
  表已被手工改名或删除，挂接按首次挂接处理，仅合并默认分区中的补报。
- 未做登录鉴权，按内网工具使用。
