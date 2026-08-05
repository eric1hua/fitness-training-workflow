---
name: "fitness-training-workflow"
description: "用户专属健身训练工作流：排训练计划、训练中实时报数记录、收工汇总写入飞书 Base 和 Obsidian、生成顶组对比训练报告。凡是用户提到练腿/练胸/练背/今天练什么、报数（如「卧推 50kg 8 次 RPE 7」「倒蹬 90 10 个」）、顶组、渐进超负荷、RPE、容量、组数、换动作、设备被占、收工、训练报告、健身日志、训练基线，都要用这个 skill——包括他只是随口说了个重量次数、没有明说要记录的时候。不负责营养配餐、伤病诊断和医疗建议。"
revised: "2026-08-03T14:50+08:00"
revision_note: "P0/P1/P2 修复：报告解析器重写（按动作名配对、支持小数重量）；算法从伪代码落地为 scripts/fitness_lib.py（RPE 分档消除失控 else、部位轮换逻辑倒置、大小肌群误判）；base_writer payload 契约对齐 lark-cli；文档补真实表 ID 和正确 CLI 参数；Obsidian 路径落定"
---

# Fitness Training Workflow

用户专属健身记录工作流。覆盖训练前 / 训练中 / 训练后 3 阶段。

## When to use this skill

**调用方**：主 agent（实时训练对话辅助）+ kepano subagent（收工整理 / 计划生成）。

**触发场景**：
- 训练前：生成当日训练计划（含上次顶组重量）
- 训练中：实时记录 + **5 项反馈契约**
- 训练后：汇总 + 三写 + 报告

## How to use

### 阶段 1：训练前（生成计划）

1. 读取飞书 Base `训练日` 表过去 7 天记录（表 ID 见 references/base-schema.md）
2. 决定今日主题 —— `fitness_lib.decide_today_topic(today, recent_sessions)`
   - **窗口内没练过的部位优先级最高**，别只在练过的里面挑
   - 用户明确说"今天练 X" → 直接采用，跳过算法
3. **查询每个动作的「上次顶组」**：Base 查 `训练组` 表，
   `组类型=顶组` + 按日期 desc 取最近 1 条 → `{动作: {weight, reps, rpe, date}}`
4. 算今日目标 —— `fitness_lib.next_top_set(action, last_top)`
   - 返回 `needs_confirmation=True` 时（首练 / 上次没记 RPE）**要问用户，不要自己编重量**
   - 算出的重量过一遍 `round_to_plate()`，加不上去的重量等于没算
5. **生成训练中应答表**（关键：这一步决定训练中要不要等）

   ```bash
   python3 scripts/fitness_lib.py cheatsheet --exercises '[
     {"action":"倒蹬 45°","last_top":{"weight":90,"reps":10,"rpe":9}}, ...
   ]'
   ```

   一次调用算完所有动作的：今日顶组目标 + **五档 RPE 各自对应的下组重量** +
   已换算的备选动作。把输出整段贴进 plan 文件。

   之所以能预先算完，是因为这些计算没有一项真的依赖训练中才知道的信息——
   RPE 的取值只有 6/7/8/9/10 五档，穷举即可。

6. 生成计划写入 `<WORKSPACE>/fitness/plans/YYYY-MM-DD.md`
   （模板 assets/templates/workout-plan.md，含「上次顶组」列 + 应答表）
7. 计划结构：有氧热身（5-10min）+ 4-6 个无氧动作 × 3-4 组 + 核心 2-3 组

### 阶段 2：训练中（实时记录 + 5 项反馈契约）

**反馈契约**（用户每次报数后，agent 必须按这 5 项返回）：

| # | 字段 | 内容 | 来源 |
|---|---|---|---|
| 1 | **目前完成** | 累计完成的组数 + 当前时间 | sessions/YYYY-MM-DD.md 实时累加 |
| 2 | **本组动作完成进度** | 当前动作 已完成组数 / 计划组数 + 当前组 reps/RPE | sessions/YYYY-MM-DD.md |
| 3 | **下组动作** | 下一个动作 + 重量 + 目标 reps + RPE 建议 + 备选动作 | plans/YYYY-MM-DD.md + progressive-overload.md |
| 4 | **今日训练进度** | 已完成动作数 / 总动作数 + 累计容量 / 预计容量 + 已耗时 | sessions/YYYY-MM-DD.md |
| 5 | **上次顶组重量** | 当前动作在训练组表最近 1 条顶组（重量 + 日期） | plans/YYYY-MM-DD.md 预查值（fallback：Base 实时查） |

**反馈模板**（agent 输出格式，必须包含 5 项）：

```
✅ 已收到 [动作] [重量]kg × [次数] RPE [值]

1️⃣ 目前完成：第 N 组 / 共 M 组（[HH:MM]）
2️⃣ 本组进度：[动作] 第 X/Y 组 完成（[reps] × [weight]kg RPE [value]）
3️⃣ 下组：[动作] [重量]kg × [目标次数] RPE 目标 [值]（备选 [备选 1 / 备选 2]）
4️⃣ 今日进度：[已完成动作数]/[总动作数] 动作｜[累计容量] kg / [预计容量] kg｜[已耗时]min
5️⃣ 上次顶组：[动作] [重量]kg（[YYYY-MM-DD]）
```

### ⚠️ 训练中不调函数

**用户在器械之间等的每一秒，都是 agent 在做本可以提前做完的事。**

每次工具调用 = 一整轮模型往返（2-5 秒）。如果每报一次数就现调
`next_top_set` / `adjust_in_session` / `find_substitutes` / `round_to_plate`，
一次腿日 20+ 组就是 80+ 次往返。

阶段 1 第 5 步的**应答表已经把答案全算好写进 plan 了**。训练中：

| 反馈项 | 数据来源 | 调用 |
|---|---|---|
| 1️⃣ 目前完成 | session 文件累加 | 无 |
| 2️⃣ 本组进度 | session 文件 | 无 |
| 3️⃣ 下组重量 + 备选 | **plan 应答表查表** | 无 |
| 4️⃣ 今日进度 | session 累计 + plan 目标 | 无 |
| 5️⃣ 上次顶组 | plan 预查值 | 无 |

会话开始时把 plan 读进上下文，之后**全程零调用**。

**只有这两种情况才实时算**（都是真的没法预知的）：
- 计划外换动作 / 临时加项 → `python3 scripts/fitness_lib.py next --action <动作> --weight W --reps R --rpe N`
  （一次调用返回目标 + 应答表 + 备选，**不要分四次调**）
- plan 里没有的动作要查历史顶组 → 异步派给 kepano 查 Base，
  前台先按应答表给估值，拿到结果再修正

---

**执行步骤**：

1. 接收用户口头报数（动作 / 重量 / 次数 / RPE）
2. 解析 + 校验（重量 / 次数 / RPE 在合理范围）
3. 实时更新 `<WORKSPACE>/fitness/sessions/YYYY-MM-DD.md`（本地缓存）
4. 按 5 项反馈契约返回 —— 第 3 项直接查 plan 应答表对应的 RPE 列
   （热身组的「≤5」查 ≤6 列；五档已覆盖全部实际取值）
5. 异常处理：
   - 设备被占 → 读 plan 应答表里那一行**已经换算好的备选**
   - RPE 偏离目标 → 查应答表对应列，不用算
   - 单动作容量 > 计划 2 倍 → 强制收尾
6. 重量单组计算：容量 = weight × reps

**日志写法**（会影响收工能不能出报告）：
- 每个动作一个 `## 动作 N：动作名` 段落 + 一张组明细表（模板 assets/templates/session-log.md）
- 没做的组画删除线或写 `❌ 跳过`，**不要留空**——留空会被当成缺数据
- 重量小数照写（37.5 / 82.5，±2.5kg 递进必然产生）；RPE 按你一贯的整数记法即可

### 阶段 3：训练后（三写 + 报告）

1. 汇总：时长 / 总容量 / 各动作组数明细 + **今日顶组重量汇总**（按动作取最新 1 条顶组）
2. 校验：
   - 容量 = sum(weight × reps)
   - 时长 ≥ 各组时间累计
   - **顶组完整度**：每个动作至少 1 条顶组记录，否则标 ⚠️
3. 三写：
   - 飞书 Base —— 走 `scripts/base_writer.py`（已封装 ID 续号、link 形状、公式字段过滤）。
     真写之前先 `--dry-run` 看一眼请求体
   - Obsidian 健身日志 —— 路径与结构见 `references/obsidian-path.md`
   - 本地缓存 `<WORKSPACE>/fitness/sessions/YYYY-MM-DD.md`
4. 生成训练报告 —— `python3 scripts/workout_summary.py --date YYYY-MM-DD --output <path>`
   - 顶组对比**按动作名配对**，跳过的动作单独标出
   - 解析失败会 exit 2 并说明原因，**不会输出一张全零的图**；
     报错就去看日志格式，别绕过去手填数字
   - 图里会自动标出三类数据问题，看到了就回去核日志：
     `†` 无显式顶组、`‡` 有正式组重于顶组（多半是顶组标错行）、
     `未匹配到基线`（动作名和历史对不上，不是首练）
5. 报告推送：飞书 / 本地存储

## Inputs

- 飞书 Base token（已知：`<YOUR_BASE_TOKEN>`）
- 用户口头报数
- 历史训练数据（来自 Base）

## Outputs

- 训练计划（Markdown，`<WORKSPACE>/fitness/plans/YYYY-MM-DD.md`，**含「上次顶组」列**）
- 训练会话日志（Markdown，`<WORKSPACE>/fitness/sessions/YYYY-MM-DD.md`，**含今日顶组汇总**）
- 飞书 Base 记录（训练日 + 训练组 + 有氧 3 张表，训练组含顶组）
- Obsidian 健身日志（含顶组小结）
- 训练战绩卡 PNG（宽 1080，高度随内容自动撑开；含逐组明细、顶组对比、突破汇总、
  下次基线预告）

## Boundary

**v1 包含**：
- 训练前计划生成（含上次顶组查询）
- 训练中实时记录 + **5 项反馈契约**
- 训练后三写 + 报告（含顶组对比）

**v1 不做（v2+）**：
- 营养建议
- 长期趋势分析
- 训练计划优化（自动调整周计划）
- 伤病康复 / 医疗建议

## References / 详细文档

- `references/base-schema.md` —— 飞书 Base 5 表 schema + **真实 table_id** + CellValue 形状
- `references/record-id-conventions.md` —— 记录 ID 命名规则（记录 ID 的唯一出处）
- `references/progressive-overload.md` —— 渐进超负荷的设计意图
- `references/exercise-substitutions.md` —— 设备替换库（~15 个动作）
- `references/muscle-rotation.md` —— 部位轮换的设计意图
- `references/obsidian-path.md` —— Obsidian vault 路径、命名、正文结构

## Scripts

> ⚠️ **算法的实现在脚本里，references 只讲「为什么」。**
> 遇到数值计算（递进多少、换算多少、选哪个部位）一律调函数，不要照着 markdown 现推——
> 那正是以前算错的地方（腿弯举被按小肌群算 +1kg、热身组的「≤5」被当成力竭减重）。

- `scripts/fitness_lib.py` —— 训练算法：`next_top_set` / `adjust_in_session` /
  `decide_today_topic` / `substitute_weight` / `is_large_muscle` / `round_to_plate`
- `scripts/workout_summary.py` —— session 解析 + 训练报告 PNG（顶组对比按动作名配对）

  **出图的视觉规范写在脚本「渲染」段开头的注释里，改样式前先读。**
  版式是 iOS 风格浅色战绩卡，四条硬约束：
  1. 卡片宽度固定 `CARD_W = 430`——所有字号间距都是照这个宽度调的。输出 1080 宽靠
     Chrome 的 `--force-device-scale-factor` 放大，**不要改 CSS 宽度**。
  2. `VIEW_W = 500` 不能再小。Chrome headless 的窗口宽度有下限，给小了它自己顶到 500
     而截图仍按你传的宽度裁，结果是右边一整列被切掉、且不报错。
  3. 高度靠跑两遍 Chrome 量出来（第一遍 `--dump-dom` 从 `<title>` 读 `scrollHeight`）。
     别改回按行高常量估算——文字一换行就裁内容。
  4. 颜色是 iOS 系统色且带语义：蓝=数据/首练，绿=突破，橙=偏重/提醒，红=力竭/跳过；
     RPE 按数值分档上色（≤7 绿 / 8 橙 / ≥9 红）。
- `scripts/base_writer.py` —— 飞书 Base 写入（ID 续号、link 形状、公式字段过滤）

三个脚本都有自检，改完跑一遍：

```bash
python3 scripts/fitness_lib.py --self-test      # 37 项
python3 scripts/base_writer.py --self-test      # 13 项
python3 scripts/workout_summary.py --date <YYYY-MM-DD> --dump   # 看解析结果
```

## Assets

- `assets/templates/workout-plan.md` —— 训练计划模板（含「上次顶组」列）
- `assets/templates/session-log.md` —— 训练日志模板（**逐动作表格式**，
  与 workout_summary.py 的解析格式一致）

## 调用示例

```bash
# 训练前
# 主 agent 调用 skill: 生成今日训练计划（含每个动作上次顶组重量）→ 写入 <WORKSPACE>/fitness/plans/2026-08-01.md

# 训练中（5 项反馈契约示例）
# 用户：「卧推 50kg 8 次 RPE 7」
# agent 输出：
# ✅ 卧推 50kg × 8 RPE 7
# 1️⃣ 目前完成：第 3 组 / 共 12 组（10:35）
# 2️⃣ 本组进度：卧推 第 2/3 组 完成（8 × 50kg RPE 7）
# 3️⃣ 下组：卧推 52.5kg × 8 RPE 目标 7（备选 哑铃卧推 25kg / 器械推胸 40kg）
# 4️⃣ 今日进度：1/5 动作｜1,360kg / 5,800kg｜27min
# 5️⃣ 上次顶组：卧推 50kg（2026-07-28）

# 训练后
# kepano 派活 + skill invocation: 写入 Base + Obsidian + 报告推送（含今日 vs 上次顶组对比）
```

## Open Questions

已结（2026-08-03）：

- ~~Obsidian vault 路径~~ → 实测确认，见 `references/obsidian-path.md`
- ~~初版替换库动作清单~~ → 已落为 `fitness_lib.SUBSTITUTIONS`（14 个主动作）
- ~~「上次顶组」查询 fallback~~ → 定为 **plan 阶段预查 + 训练中直读**，
  临时换动作才回落到 Base 实时查（见阶段 2 第 5 条）

仍未定，需用户拍板：

- **半途中断的 session 怎么记**。8/01 那次 12:16 叫停，只做了 1 组，
  日志里没有结构化数据 → 报告脚本报错退出（不出图）。
  是接受"不出图"，还是要规定一个"中断也能出图"的最小记录格式？
- **孤立动作的递进步长**。现按肌群分 2.5/1 kg，腿弯举属腿→2.5 kg，
  但它是孤立动作、基数只有 25 kg，一次 +10% 偏猛。
  要不要改成"取 min(步长, 当前重量 5%)"？
- **配重片粒度**。`round_to_plate` 默认 2.5 kg，但 8/02 腿弯举实际卡在
  "没有 1 kg 片"。要不要按器械分别记录可用片重？
- **高次数动作的 RPE 该不该照单全收**。自评 RPE 在低次数大重量组上准，
  在高次数组上误差很大（MASS 引的数据：70% 1RM 下训练有素者平均偏差
  5.15 ± 2.92 次）。用户的提踵 ×15、髋内收 ×15、腿屈伸 ×12 都在这个区间，
  而算法现在对所有动作同等信任 RPE。选项：(a) 维持现状；
  (b) 高次数动作改报 RIR（"还能再做几个"是数出来的，比估 RPE 可靠）；
  (c) 高次数动作的递进步长打折。
  → 定义与出处见 `references/progressive-overload.md` 第 1 节
