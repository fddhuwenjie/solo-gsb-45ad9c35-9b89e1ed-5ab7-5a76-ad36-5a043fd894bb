# 学分互认 API

用 FastAPI + SQLite 实现的外校课程学分互认服务。

## 业务规则

- **登记**：外校课程（编号、正整数学分、0~100 分成绩）、本校要求（编号、所需学分）、课程可替代的要求与最低成绩。
- **批量互认**：一次提交多条，仅当每条的外课学分足额且成绩达标、源课与要求在本批及现存"已认/待复核"记录中均未重复时，**整批确认**并按要求学分计入进度；否则**整批不写**（409 并返回全部原因）。
- **成绩更正**：更正后不再合格的"已认"互认原子地转为**待复核**，停止计分，但课程与要求两端的占用保留。
- **复核**：成绩恢复后须显式复核才能重新计分；复核仍不合格则维持待复核。
- **撤销**：删除互认记录，释放两端占用，之后可重新申请。
- **整批改派**：把"已认"记录改派到另一培养要求。互认编号与目标要求在批内均不得重复，来源必须是"已认"记录（待复核不可作为来源）；校验按**本批原占用同时释放后**的状态进行——批内两条记录互换要求不会相互阻塞，但目标要求被批外"已认/待复核"记录占用则拒绝。逐条核对替代规则、当前成绩与学分，任一不合格则整批不变；成功则保留原互认编号、仅更新要求，进度随即按新关系计算。
- **查询**：`/progress` 区分已认（计分）、待复核（不计分但占用）、空缺。
- **并发**：所有写操作（申请/更正/复核/撤销/改派）在进程锁 + `BEGIN IMMEDIATE` 事务内串行执行，检查与写入原子完成，并发操作不会重复使用或暴露中间态；读查询包裹在读事务中，一次 `/progress` 取自同一提交快照，不会混合改派前后的结果。

## 启动

```bash
docker compose up --build
```

服务监听 **8000** 端口，交互式文档见 <http://localhost:8000/docs>。
SQLite 数据库写入命名卷 `credit_data`（容器内 `/data/app.db`），首次启动自动建表，**重启后记录仍在**。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000   # 数据库默认在 ./data/app.db，可用 DB_PATH 覆盖
pytest                             # 运行测试
```

## HTTP 示例

```bash
# 1. 登记外校课程：编号、学分、成绩
curl -s -X POST localhost:8000/courses \
  -H 'Content-Type: application/json' \
  -d '{"id": "C1", "credits": 4, "score": 90}'

# 2. 登记本校要求：编号、所需学分
curl -s -X POST localhost:8000/requirements \
  -H 'Content-Type: application/json' \
  -d '{"id": "R1", "required_credits": 4}'

# 3. 登记替代规则：C1 可替代 R1，最低成绩 60
curl -s -X POST localhost:8000/substitutions \
  -H 'Content-Type: application/json' \
  -d '{"course_id": "C1", "requirement_id": "R1", "min_score": 60}'

# 4. 批量提交互认（整批确认或整批拒绝）
curl -s -X POST localhost:8000/recognitions \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"course_id": "C1", "requirement_id": "R1"}]}'

# 5. 查询进度：已认 / 待复核 / 空缺
curl -s localhost:8000/progress

# 6. 成绩更正到不达标 -> 互认转为待复核，停止计分但保留占用
curl -s -X PUT localhost:8000/courses/C1 \
  -H 'Content-Type: application/json' -d '{"score": 55}'
curl -s 'localhost:8000/recognitions?status=pending'

# 7. 成绩恢复后须显式复核才重新计分（复核仍不合格则维持待复核）
curl -s -X PUT localhost:8000/courses/C1 \
  -H 'Content-Type: application/json' -d '{"score": 88}'
curl -s -X POST localhost:8000/recognitions/1/review

# 8. 整批改派：把已认记录改派到另一要求，保留互认编号（整批成功或整批不变）
curl -s -X POST localhost:8000/requirements \
  -H 'Content-Type: application/json' \
  -d '{"id": "R2", "required_credits": 4}'
curl -s -X POST localhost:8000/substitutions \
  -H 'Content-Type: application/json' \
  -d '{"course_id": "C1", "requirement_id": "R2", "min_score": 60}'
curl -s -X POST localhost:8000/recognitions/reassign \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"recognition_id": 1, "requirement_id": "R2"}]}'
# => {"reassigned": [{"id": 1, "course_id": "C1", "from_requirement_id": "R1",
#      "to_requirement_id": "R2", "status": "confirmed"}]}

# 9. 撤销互认，释放课程与要求的占用
curl -s -X DELETE localhost:8000/recognitions/1
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/courses` | 登记外校课程 |
| PUT | `/courses/{id}` | 成绩/学分更正（不合格互认转待复核） |
| GET | `/courses` | 课程列表 |
| POST | `/requirements` | 登记本校要求 |
| GET | `/requirements` | 要求列表 |
| POST | `/substitutions` | 登记替代规则与最低成绩 |
| GET | `/substitutions` | 规则列表 |
| POST | `/recognitions` | 批量互认（整批确认或整批不写） |
| POST | `/recognitions/reassign` | 整批改派已认记录到另一要求（保留编号，整批成功或整批不变） |
| GET | `/recognitions?status=confirmed\|pending` | 按状态查询互认 |
| POST | `/recognitions/{id}/review` | 复核待复核记录 |
| DELETE | `/recognitions/{id}` | 撤销并释放占用 |
| GET | `/progress` | 进度：已认/待复核/空缺与学分统计 |
