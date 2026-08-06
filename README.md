# fitness-training-workflow

一个给 AI agent 用的健身训练 skill：训练前排计划、训练中口头报数实时记录、训练后汇总写入飞书多维表格 + Obsidian 并生成顶组对比战绩卡。

按 [Agent Skills](https://code.claude.com/docs/en/skills) 的 `SKILL.md` 约定组织，Claude Code / OpenClaw 等支持 skill 的 agent 都能直接装。

## 它解决什么

举铁时最烦的是「这个动作上次多重来着」。这个 skill 把三件事串起来：

- **训练前** —— 查历史顶组，按渐进超负荷算出今日目标重量，生成计划文件
- **训练中** —— 你只说「卧推 50kg 8 次 RPE 7」，agent 返回固定的 5 项反馈（累计进度 / 本动作进度 / 下组重量 / 今日容量 / 上次顶组）
- **训练后** —— 汇总容量与顶组，写入飞书 Base、Obsidian、本地缓存，出一张 1080 宽的战绩卡 PNG

一个刻意的设计：训练中**零工具调用**。所有「报 RPE 6/7/8/9/10 分别该上多少重量」在计划阶段就穷举算好写进计划文件了，训练中查表即可。否则每报一次数就是几秒模型往返，一次腿日 20+ 组，人全在器械上干等。

## 安装

```bash
git clone https://github.com/eric1hua/fitness-training-workflow.git ~/.claude/skills/fitness-training-workflow
```

放进 agent 的 skills 目录即可（Claude Code 是 `~/.claude/skills/`）。

## 配置

写入飞书 Base 需要你自己的表：

```bash
cp config.example.json config.json   # config.json 已在 .gitignore 里
```

填两样东西：

- `base_token` —— 多维表格 URL 里 `/base/` 后面那串
- `table_ids` —— 查法：`lark-cli base +base-block-list --base-token <你的token> --type table`

表结构（5 张表的字段定义）见 [`references/base-schema.md`](references/base-schema.md)，照着建。写入依赖 [lark-cli](https://github.com/larksuite) 且已绑定身份。

只用计划生成和战绩卡、不写 Base 的话，不配也能跑 —— `fitness_lib.py` 和 `workout_summary.py` 不碰飞书。

Obsidian 日志路径在 [`references/obsidian-path.md`](references/obsidian-path.md)，改成你自己的 vault。

## 结构

```
SKILL.md                          # 主流程：三阶段 + 训练中 5 项反馈契约
references/
  data-entry-spec.md              # 录入规范（唯一真源）—— 多个 agent 写同一个 Base 时的契约
  base-schema.md                  # 飞书 Base 5 表 schema + CellValue 形状
  record-id-conventions.md        # 记录 ID 命名规则
  progressive-overload.md         # 渐进超负荷：为什么这么递进
  exercise-substitutions.md       # 设备被占时的替换库（14 个主动作）
  muscle-rotation.md              # 部位轮换的设计意图
  obsidian-path.md                # Obsidian vault 路径与正文结构
scripts/
  fitness_lib.py                  # 训练算法（顶组递进 / 组内调整 / 部位决策 / 配重取整）
  workout_summary.py              # 日志解析 + 战绩卡 PNG（需要 Chrome headless）
  base_writer.py                  # 飞书 Base 写入（ID 续号 / link 形状 / 容量计算 / 写入前校验）
  base_audit.py                   # 按规范审计 Base，--fix 修可确定的违规
prompts/
  for-fitness-coach.md            # 给另一个实现的对齐要求
assets/templates/                 # 计划与日志的 Markdown 模板
```

算法实现全在 `fitness_lib.py`，`references/` 只讲「为什么这么设计」。改数值逻辑改脚本，别照着 markdown 现推。

## 自检

```bash
python3 scripts/fitness_lib.py --self-test    # 54 项
python3 scripts/base_writer.py --self-test    # 40 项
python3 scripts/base_audit.py  --self-test    # 21 项
```

三个自检都不需要 `config.json`。

## 多个 agent 写同一个 Base

这个 skill 不是唯一往飞书 Base 写数据的实现。规范、写入前校验、写入后审计三件套
就是为此存在的：

- [`references/data-entry-spec.md`](references/data-entry-spec.md) 是唯一真源
- `base_writer.py` 在写入前按规范校验，违规直接中止
- `base_audit.py` 查的是 **Base 本身**，所以在任意一台机器上跑一次，就覆盖了
  所有写入方的数据，包括历史脏数据

```bash
python3 scripts/base_audit.py --fix --dry-run   # 看要修什么
python3 scripts/base_audit.py --fix             # 修配速单位 / 时间格式 / 总次数缺失
```

## 已知未定项

`SKILL.md` 末尾的 Open Questions 记着几个还没拍板的设计问题（中断 session 怎么记、孤立动作递进步长、高次数动作的 RPE 可靠性）。不是 bug，是取舍未定。

## License

MIT
