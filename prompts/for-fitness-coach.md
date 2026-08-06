# 给 fitness-coach（Rock）的对齐要求

> 把下面 `---` 之间的内容整段发给 Mac mini 上的 Rock。
>
> 背景：`fitness-coach` 和 iMac 上的 `fitness-training-workflow` 在往**同一个飞书 Base**
> 写数据。两边的记录 ID 约定恰好一致所以一直没打架，但没有共同契约，已经写出过
> 整列为空的 `容量kg`、12 小时制的时间、带单位的配速、以及挂不上训练日的组记录。
>
> 现在立了一份规范，两边都要遵守。A 侧（iMac）的改动已完成。

---

## 任务：让 fitness-coach 遵守飞书 Base 录入规范

同一个飞书 Base 有两个独立实现在写：你（`fitness-coach`，Mac mini）和 iMac 上的
`fitness-training-workflow`。现在有了一份共同的录入规范，你这边需要对齐。

### 第一步：读规范

```
https://raw.githubusercontent.com/eric1hua/fitness-training-workflow/main/references/data-entry-spec.md
```

这是唯一真源。下面列的是你这边**具体要改什么**，但判断标准以规范文档为准。

### 第二步：署名不用你管（但要知道它存在）

四张表都启用了飞书系统字段 `创建人`，按调用方的**应用身份**自动填。

你走 `inherit: openclaw` 用 Mac mini 的 `channels.feishu` 应用（`cli_a96d77f8…`），
iMac 那边的 lark-cli 用的是另一个（`cli_aaac626d…`）——**两个不同的应用，所以这一列
天然区分得开谁写的**。审计报告会直接显示它。

你要做的只有一件：**别往 payload 里塞 `创建人`**。系统字段不可写，塞了会被 API 拒。

### 第三步：补双向链接（这是你目前完全没有的能力）

`训练日` 和 `组数明细` 是一对双向 link。**只挂一边等于没挂**：训练日展开看不到明细，
按训练日聚合的仪表盘会算空。

你现在的 `map_record()` 两个方向都没写。要做两件事：

1. **set 分支加 `训练日` link** —— 指向当天的训练日记录：

   ```python
   "训练日": [{"id": <当日 session 的远端 record_id>}],
   ```

   CellValue 必须是 `[{"id": "recXXX"}]`，不是裸字符串数组。

2. **session 同步完成后回填 `组数明细`** —— 把当天全部组记录的远端 id 挂上去：

   ```python
   "组数明细": [{"id": rid} for rid in <当日全部 set 的远端 record_id>],
   ```

**你已经有需要的工具**：`bitable_sync.py` 里的 `find_remote_record_ids(app_token,
table_id, token, logical_ids)` 能把业务 ID（`session-2026-08-04`）换成远端
`record_id`。顺序上先同步 session、再同步 set、最后回填 link 最省事。

这一条是纯代码改动，光在 prompt 里说"注意录入规范"约束不住——`map_record()` 里没有
这个字段，它就永远不会被写出去。

### 第四步：格式规范

| 表 | 字段 | 要求 | 你现在可能违反的地方 |
|---|---|---|---|
| 训练日 | `开始` / `结束` | `HH:MM`，24 小时制 | 曾出现 12 小时制 |
| 训练日 | `总次数` | 必填，= 当日训练组 `次数` 之和 | 曾缺失 |
| 有氧 | `配速` | `MM:SS`，**不带 `/km`** | 曾写成 `11:41 /km` |
| 有氧 | `备注` | 跑步记坡度 `坡度 3%`，椭圆机记阻力 `阻力 8` | 目前没记 |
| 训练组 | `容量kg` | 必填，`重量kg × 次数` | **你这边是对的，保持不变** |

关于 `容量kg`：iMac 那边此前把它当公式字段过滤掉了（文档记载有误），导致那边写的
记录这一列全空。**你一直在写它是正确的做法，不要改。**

### 第五步：`训练者` 字段已废弃

每个训练者现在各自一张独立的多维表格，这个字段没有区分作用了。

你继续写不算错，历史值也保留。但不必再维护，想去掉就去掉。

### 第六步：加写入前校验

规范要靠代码强制，不能靠自觉。建议在 `bitable_sync.py` 里加一个校验函数，在
`map_record()` 之后、发请求之前跑，违规就中止同步并打印原因。

A 侧的实现可以参考（`validate_record()`），逻辑不复杂：必填字段、时间正则、
配速正则、容量一致性、link 非空。

你有 20+ 个测试文件，新增的校验和 link 逻辑请一并补测试。

### 验证

改完之后：

1. 跑你自己的测试套件
2. 造一条违规记录（比如配速写 `6:30 /km`），确认被校验拦下
3. 同步一条真实训练，去飞书里展开训练日，确认能看到组数明细
4. 可选：clone iMac 那边的仓库跑一次审计，它查的是 Base 本身，能验证你写进去的数据：

   ```bash
   git clone https://github.com/eric1hua/fitness-training-workflow.git
   cd fitness-training-workflow
   cp config.example.json config.json   # 填 base_token 和 table_ids
   python3 scripts/base_audit.py --since 2026-08-01
   ```

### 不要做的事

- 不要改记录 ID 约定（`set-YYYY-MM-DD-NNN` 等）——两边靠它保持一致
- 不要动 iMac 那边写的历史记录
- 存量脏数据不用你修，审计脚本的 `--fix` 会统一处理

---

## 备注（不用发给 Rock）

署名机制在 2026-08-06 改过一次：原本设计的是让每个 agent 写一个自定义的
`录入agent` 字段，后来发现飞书系统字段「创建人」自动填、伪造不了、还能回填历史
记录，于是整个拆掉了。规范和代码都已同步。

规范里有两项待实测确认，会影响上面的措辞：

1. **训练组 `容量kg` 的真实类型** —— 若实测发现它其实是公式字段，那第四步那句
   「你这边是对的」要反过来，改成让 B 去掉这个字段，A 也要退回过滤。
   当前按「不是公式字段」执行。

2. **训练日 `开始`/`结束` 是 DateTime 还是文本** —— 决定 payload 写 `HH:MM` 还是
   完整 datetime。面向录入方的输入格式都是 `HH:MM`，所以第四步的措辞两种情况下
   都成立，不用改。

验证命令：

```bash
lark-cli base +field-list --base-token <token> --table-id <table_id> --format json
```
