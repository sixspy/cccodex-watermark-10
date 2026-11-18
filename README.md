# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 水印核对台（批头栏纠偏）

批头栏按「业务日 + 产出表」归组，头栏申报的已盖印水印数必须与底下运行码行的已盖印实数一致；不一致即为**漂移**，缺口摘要跟着漂并阻断对账。

四处同查（均在「核对」页与批次详情联动）：

| 位置 | 展示 |
|------|------|
| 水印核对台 | 头栏水印数 vs 运行码实数、差量芯片、冻结/重数形态芯片 |
| 批次详情 | 所属批头栏的头栏数、实数、差量、版本号 |
| 缺口卡片 | 漂移即亮差量并标「对账阻断」，纠偏后解除 |
| 回放队列 | 纠偏留证（形态/前后值/差量/操作人），支持两轮回放留证 |

纠偏的两种可行读数时机：

- **冻结**：打开核对台瞬间先冻住头栏数与行数快照（`header_snapshots`），纠偏时听冻住态、按快照实数写回；快照一次性消费，头栏被改动即失效。
- **重数**：提交瞬间当场 `COUNT` 最新已盖印行数，听最新行直接写回。

互斥与留证：

- 点纠偏在单个 SQLite 事务里 `BEGIN IMMEDIATE` 抢排他写锁，叠加进程内 per-header 锁与版本号 CAS，左右仓管同时抢只可能一单落库，失败统一回中文 409。
- 落库后当场自检，对不上即回滚；只改页面展示不改库字段不算成功（`batch_headers.declared_stamped` 与 `version_no` 必更新）。
- 每单纠偏写 `correction_records`，回放写 `replay_logs`，两轮结果须与形态芯片口径一致。

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
