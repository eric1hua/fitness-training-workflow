# 飞书 Base Schema 参考

> **数据源**:飞书多维表格 token `<YOUR_BASE_TOKEN>`
> **blocks**:11 个(5 表 + 1 workflow + 5 dashboards)
> **录入工具**:lark-cli `base` 域(优先)/ 原生 `feishu_bitable_*`(备援)
> **最后核对**:2026-08-01(`lark-cli base +base-block-list` 实测)

## 表 ID(直接用,不要每次去查)

| 表 | table_id |
|---|---|
| 训练日 | `<TABLE_ID_TRAINING_DAY>` |
| 训练组 | `<TABLE_ID_TRAINING_SET>` |
| 有氧 | `<TABLE_ID_CARDIO>` |
| 体测 | `<TABLE_ID_BODY_METRICS>` |
| 训练计划 | `<TABLE_ID_PLAN>`(v1 暂不主动用) |

同一份常量在 `scripts/base_writer.py` 的 `TABLE_IDS` 里。
本文早先所有示例都写 `<训练组表ID>` 占位符,导致每次都要先跑一遍
`base-block-list` 去查 —— 白花一个 round-trip。

---

## 1. 训练计划 表

> ⚠️ 当前 base 只有 1 条记录(7/14 初次录入),录入流程没跑起来。
> **v1 暂不主动用本表** — 直接生成本地 `<WORKSPACE>/fitness/plans/YYYY-MM-DD.md` 即可。

| 字段 | 类型 | 备注 |
|---|---|---|
| 日期 | Date | YYYY-MM-DD |
| 主题 | SingleSelect | 推/拉/腿/上肢/下肢/全身/有氧 |
| 计划动作 | LongText | Markdown 表格(含上次顶组)|
| 训练者 | User | 用户 |

---

## 2. 训练日 表

| 字段 | 类型 | 备注 |
|---|---|---|
| 日期 | Date | YYYY-MM-DD |
| 主题 | SingleSelect | **主体无氧主题**(详见下方「口径约定」)|
| 开始 | DateTime | HH:MM |
| 结束 | DateTime | HH:MM |
| 时长min | Number | **只算无氧**(有氧单算)|
| 总组数 | Number | 无氧组数总和 |
| 总次数 | Number | 无氧 reps 总和 |
| 总容量kg | Number | sum(weight × reps)|
| 组数明细 | Link → 训练组 | 双向 link |
| 记录ID | Text | `session-YYYY-MM-DD` |
| 训练者 | User | 用户 |
| 备注 | LongText | 自由文本 |

---

## 3. 训练组 表

| 字段 | 类型 | 备注 |
|---|---|---|
| 日期 | Date | YYYY-MM-DD |
| 训练日 | Link → 训练日 | 双向 link,必填 |
| 动作 | Text | 中文,如「倒蹬 45°」「罗马尼亚硬拉」 |
| 肌群 | SingleSelect | 股四/腘绳/臀/小腿/胸/背/肩/臂/核心 |
| 类别 | SingleSelect | 推/拉/腿/核心/肩/臂 |
| 组类型 | SingleSelect | **热身/正式/顶组/收尾**(顶组定义见 progressive-overload.md) |
| 组序 | Number | 1, 2, 3, ... |
| 重量kg | Number | 杠铃/器械重 |
| 次数 | Number | reps |
| RPE | Number | 0-10 |
| 容量kg | Formula | weight × reps |
| 记录ID | Text | `set-YYYY-MM-DD-NNN`(NNN 从 001 起)|
| 训练者 | User | 用户 |
| 备注 | LongText | 自由文本 |

---

## 4. 有氧 表

| 字段 | 类型 | 备注 |
|---|---|---|
| 日期 | Date | YYYY-MM-DD |
| 方式 | SingleSelect | 跑步/单车/椭圆机/游泳/步行 |
| 距离km | Number | |
| 时长min | Number | **有氧时长单算**(不进训练日.时长min) |
| 平均心率 | Number | bpm |
| 卡路里 | Number | kcal |
| 配速 | Text | min/km(如「6:30」「11:49」) |
| 记录ID | Text | `cardio-YYYY-MM-DD-NNN` |
| 训练者 | User | 用户 |
| 备注 | LongText | |

---

## 5. 体测 表

| 字段 | 类型 | 备注 |
|---|---|---|
| 日期 | Date | |
| 体重kg | Number | |
| BMI | Number | |
| 体脂率 | Number | % |
| 体脂kg | Number | |
| 去脂体重kg | Number | |
| 骨骼肌kg | Number | |
| 基础代谢 | Number | kcal |
| 内脏脂肪等级 | Number | |
| 腰臀比 | Number | |
| 体成分评分 | Number | |
| 代谢年龄 | Number | |
| 单选 | SingleSelect | (元数据字段) |
| 记录ID | Text | `body-YYYY-MM-DD-NNN` |
| 训练者 | User | 用户 |
| 备注 | LongText | |

---

## 记录 ID 命名约定

见 `references/record-id-conventions.md` —— 那里是唯一出处。
本文早先复制了一份简表,两边各自改过,已经开始对不上了。

---

## 口径约定(必读)

### 主题选择
- **以主体无氧主题主导**:即使当日含跑步/快走,主题仍按无氧主体选择
- 例:7/14 备注含跑步 → 主题「腿」(7/14 是 leg day)
- 例:7/18 主体无氧=腿 + 临时加推胸 → 主题「腿」(推胸是临时加项)

### 时长口径
- **训练日.时长min 只算无氧**:有氧时长进 `有氧表`
- 例:7/18 训练日 67 min(纯无氧) + 有氧 25 min(快走单算)

### 单动作容量
- `容量 = weight × reps`
- 例:80 kg × 10 reps = 800 kg

---

## 操作命令(lark-cli)

> ⚠️ **参数名以 `lark-cli base +<cmd> --help` 为准**(2026-08-03 实测)。
> 本文早先的示例用的是 `--filter` / `--sort` / `--fields` / `--records`,
> **这四个参数都不存在**,那是原生 OpenAPI 的字段名。照抄必然失败。
> 正确的是 `--filter-json` / `--sort-json` / `--json`。

### 列出所有 blocks
```bash
lark-cli base +base-block-list --base-token <YOUR_BASE_TOKEN>
```

### 查某日所有训练组
```bash
lark-cli base +record-list \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_SET> \
  --filter-json '{"logic":"and","conditions":[["日期","==","ExactDate(2026-07-18)"]]}' \
  --format json
```

### 查某动作最近 1 条顶组(渐进超负荷基线)
```bash
lark-cli base +record-list \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_SET> \
  --filter-json '{"logic":"and","conditions":[["动作","==","倒蹬 45°"],["组类型","==","顶组"]]}' \
  --sort-json '[{"field":"日期","desc":true}]' \
  --format json --limit 1
```

### 写入流程(训练日 + N 训练组 + 有氧)

优先用 `scripts/base_writer.py`(已封装续号、link 形状、公式字段过滤)。
手动写的话:

```bash
# 1. 创建训练日(--json 收顶层字段 map,不要包 fields;不带 --record-id = 新建)
lark-cli base +record-upsert \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_DAY> \
  --json '{"日期":"2026-08-01","主题":"腿","总容量kg":9914,"总组数":21,"开始":"2026-08-01 11:30:00"}'

# 2. 批量创建训练组(create_records 数组,每条一个字段 map,单次上限 200)
lark-cli base +record-batch-create \
  --base-token <YOUR_BASE_TOKEN> \
  --table-id <TABLE_ID_TRAINING_SET> \
  --json '{"create_records":[{"日期":"2026-08-01","训练日":[{"id":"recXXX"}],"动作":"倒蹬 45°","重量kg":85,"次数":10}]}'
```

### CellValue 形状(踩过的坑)

| 字段类型 | 正确写法 | 曾经写错成 |
|---|---|---|
| datetime(开始/结束) | `"2026-08-01 11:30:00"` | `"2026-08-01T11:30:00+08:00"` |
| link(训练日/组数明细) | `[{"id":"recXXX"}]` | `["recXXX"]` |
| 单选(主题/组类型) | `"腿"` | — |
| **公式(容量kg)** | **不可写,别放进 payload** | 当普通数字字段塞值 |

公式 / lookup / 附件 / 系统字段一律不可写 —— lark-cli 的 `--help` 里也明确写了这条。
`容量kg` 由 Base 自己按 `重量kg × 次数` 算。

---

## 已知坑

- bot 缺 base scopes(2026-07-25 已申请,2026-07-25 已开通 35 → 82)
- 验证命令:`lark-cli auth status` + `feishu_app_scopes`(看 granted 列表)
- 单条训练组写入用 `+record-upsert`,批量用 `+record-batch-create`