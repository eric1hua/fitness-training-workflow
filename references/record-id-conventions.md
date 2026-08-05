# 记录 ID 命名约定(record-id-conventions)

> 飞书 Base 4 张数据表的 record-id 命名规则。
> **唯一性保证**:同一天同一类型只产生 1 个 prefix,序号递增。

---

## 1. 4 类 ID

| 类型 | prefix | 格式 | 示例 |
|---|---|---|---|
| 训练日 | `session` | `session-YYYY-MM-DD` | `session-2026-07-18` |
| 训练组 | `set` | `set-YYYY-MM-DD-NNN` | `set-2026-07-18-001` |
| 有氧 | `cardio` | `cardio-YYYY-MM-DD-NNN` | `cardio-2026-07-18-001` |
| 体测 | `body` | `body-YYYY-MM-DD-NNN` | `body-2026-07-14-001` |

---

## 2. 规则详解

### 训练日:1 天 = 1 个 record

- **同一天只产生 1 个训练日**(即便有多个 session)
- 例:`session-2026-07-18`(全天训练只 1 个 record,主题「腿」+ 含临时推胸)
- 例外:早上晨练 + 晚上健身 = 2 个?目前**合并为 1 个**,备注里分时段

### 训练组:1 天 = N 个 record(顺序递增)

- **NNN 从 001 起**,按录入顺序,不按动作归零
- 例:7/18 有 21 组 → `set-2026-07-18-001` 到 `set-2026-07-18-021`
- 包含临时加项(7/18 推胸 = `set-2026-07-18-018` 到 `021`)

### 有氧:1 天 = 1+ 个 record

- **同一天可能有多个有氧**(晨跑 + 午练 + 晚走)
- 例:`cardio-2026-07-18-001`(快走)+ `cardio-2026-07-18-002`(晚散步)

### 体测:1 天 = 1 个 record(或不录)

- **非每日录**,间隔 ≥ 1 周
- 例:`body-2026-07-14-001`(7/14 是首个 body 记录)

---

## 3. 与飞书 Base 字段映射

### 训练日.记录ID

- 类型:Text(短文本)
- 唯一:✅ 是(Base 全表唯一)
- 用途:作为训练组.训练日(link)的引用 key

### 训练组.记录ID

- 类型:Text
- 唯一:✅ 是
- 用途:debug / 手动查询时定位

### 有氧.记录ID / 体测.记录ID

- 同上

---

## 4. 实现细节(lark-cli)

### 写入时显式指定记录ID

```bash
lark-cli base +record-upsert \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_DAY> \
  --json '{"记录ID": "session-2026-07-18", "日期": "2026-07-18", ...}'
```

### 查询时按 ID 过滤

```bash
lark-cli base +record-list \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_DAY> \
  --filter-json '{"logic":"and","conditions":[["记录ID","==","session-2026-07-18"]]}'
```

### 双向 link 写入

- 先创建训练日 → 获得飞书 record_id(实际 ID,如 `recvqoVzq12f3d`)
- 再批量创建训练组 → 每个训练组.训练日字段 = 训练日 record_id
- 最后更新训练日.组数明细 link 字段 → 21 个训练组 record_ids

⚠️ **注意区分**:
- **逻辑 ID** = 我们生成的 `session-2026-07-18`(存于"记录ID"字段)
- **飞书 record_id** = Base 返回的 `recvqoVzq12f3d`(用于 link 字段)
- 调试时按"记录ID"字段查,业务用 record_id 建 link

---

## 5. ID 冲突处理

### 同日已存在训练日

- 检测:`lark-cli base +record-list --filter-json '{"logic":"and","conditions":[["记录ID","==","session-2026-07-18"]]}'`
- 存在 → 报错「训练日已存在」,提示用户选择(更新 vs 跳过)
- 不存在 → 创建

### 同日序号已存在

- 例:`set-2026-07-18-005` 已存在 → 写入失败
- 解决:取该日 max NNN + 1

### ID 跨日复用

- 绝对禁止:`set-2026-07-18-001` 不可用于 7/19
- 检测:NNN 永远基于 date prefix

---

## 6. ID 生成 + 续号(实现)

**实现在 `scripts/base_writer.py`,不要另写一份。**

```python
from datetime import date
from base_writer import gen_session_id, gen_set_id, next_seq, max_seq_from_ids

gen_session_id(date(2026, 8, 2))          # 'session-2026-08-02'
gen_set_id(date(2026, 8, 2), 4)           # 'set-2026-08-02-004'
next_seq("训练组", "set", date(2026, 8, 2))  # 查库 → 该日下一个可用序号
```

自检:`python3 scripts/base_writer.py --self-test`

⚠️ **序号必须查库续号,不能从 1 写死。**
本节第 5 条早就写明「取该日 max NNN + 1」,但旧实现把起始序号硬编码成 1,
有氧更是永远生成 `-001` —— 同一天只要写第二次(补录 / 上次写到一半 / 晨跑+晚走),
就会撞 ID。规范写了、代码没实现,是这套东西最容易出的问题。

---

## 7. 校验逻辑

### 写入前

1. **训练日 ID 唯一**:`create_training_session()` 会先查该日已有记录,
   撞了就抛 `BaseWriteError` 不写(v1 默认跳过,避免误覆盖)。
   确实要覆盖,显式传 `allow_duplicate=True`。
2. **训练组 / 有氧序号续号**:`next_seq()` 取该日 max NNN + 1。

### 写入后

```python
# 校验双向 link:训练日.组数明细 应等于 N 个训练组 record_id
session = get_record("训练日", session_record_id)
assert {r["id"] for r in session.组数明细} == set(set_record_ids)
```

注意 link 字段读回来是 `[{"id": "recXXX"}, ...]` 的形状,不是裸字符串数组。

---

## 8. 边界 case

### 跨日训练(00:00 前后)

- 例:7/18 23:50 开始,7/19 00:30 结束
- 归属:**开始日期**(7/18),不是结束日期
- 备注里标「跨日训练」

### 多个 session 同日

- 例:7/18 早晨空腹 + 晚上健身
- v1:**合并为 1 个 session**(全天记录),备注里分时段
- v2+:拆为 2 个 session(需加 session 编号后缀)

### 删除 record

- 软删除?硬删除?目前 Base 没有软删除机制
- 硬删除 → 关联 link 自动解除(飞书机制)
- 谨慎:删除训练组 record_id 会破坏训练日.组数明细 link

### 重新写入(幂等性)

- 同一 session_id 二次写入 → 失败(已存在)
- 解决:检测到已存在 → 询问用户「更新 vs 跳过 vs 删除重写」
- v1 默认:**跳过**(避免误覆盖)

---

## 9. 调试辅助

### 按 ID 查 record

```bash
# 7/18 训练日
lark-cli base +record-list \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_DAY> \
  --filter-json '{"logic":"and","conditions":[["记录ID","==","session-2026-07-18"]]}'

# 7/18 所有训练组
lark-cli base +record-list \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_SET> \
  --filter-json '{"logic":"and","conditions":[["日期","==","ExactDate(2026-07-18)"]]}'

# 某 set 的训练日 link(返回 record_id)
lark-cli base +record-get \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_SET> \
  --record-id <某 set 的飞书 record_id>
```

### 验证双向 link

```bash
# 训练日.组数明细 应等于 N 个训练组 record_id
lark-cli base +record-get \
  --table-id <TABLE_ID_TRAINING_DAY> \
  --record-id <训练日 record_id> \
  --jq '.data.record.fields.组数明细'

# 训练组.训练日 应等于 1 个训练日 record_id
lark-cli base +record-get \
  --table-id <TABLE_ID_TRAINING_SET> \
  --record-id <某 set 的飞书 record_id> \
  --jq '.data.record.fields.训练日[0]'
```