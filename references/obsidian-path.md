# Obsidian 落地路径与命名约定

> 2026-08-03 实测确认。此前这是 SKILL.md 里悬了很久的 Open Question,
> 而 Boundary 又把「Obsidian 健身日志」算进 v1 —— 路径未知却声称已交付,
> 是个自相矛盾。现在按磁盘实况定下来。

---

## 1. Vault 与目录

```
~/Library/Mobile Documents/iCloud~md~obsidian/Documents/<YOUR_VAULT>/20 Areas/健身/
```

⚠️ iCloud 同步目录,路径含空格,shell 里务必加引号。

同一个 iCloud 目录下可能并存多个 vault,**健身内容只进上面这一个目录**,别散到别的 vault 去。

> 本机的实际 vault 名和绝对路径写在 `LOCAL.md`(未纳入版本库)。

---

## 2. 文件命名

| 类型 | 命名 | 示例 |
|---|---|---|
| 无氧训练 | `练{部位}计划-YYYY-MM-DD.md` | `练腿计划-2026-08-02.md` |
| 有氧 / 恢复 | `有氧恢复-YYYY-MM-DD.md` | `有氧恢复-2026-07-18.md` |
| 汇总索引 | `健身日历.md` | 单文件,追加不新建 |
| 跑步专题 | `Running.md` | 单文件 |

「计划」是历史叫法,文件里其实是**计划 + 实际记录 + 复盘**的合体,不要另开一个「记录」文件。

---

## 3. Frontmatter

```yaml
---
title: "练腿计划 · 2026-08-02"
date: 2026-08-02
tags: [fitness, workout/legs, ankle-safe, day/leg4]
---
```

`tags` 的约定:
- 固定打 `fitness`
- 部位 `workout/legs` · `workout/chest` · `workout/back`
- 训练日序号 `day/leg4`
- 特殊模式按需 `ankle-safe`

---

## 4. 正文结构(取自 2026-08-02 实际文件)

```
# 练腿计划 · YYYY-MM-DD(周几 · 版本 · 模式)
## 概览
## 顶组对比(今日 vs 上次)
## 主训练记录
### N. 动作名 · N 组 · N kg · 肌群
## 突破亮点
### 🏆 新顶组
### 🆕 首练基线
### ⚠️ 待解决
## 反思
## 下次{部位}日基线(预计 YYYY-MM-DD)
## 数据来源
## 🔗 关联
```

「顶组对比」和「下次基线」两节的数据,直接取 `scripts/workout_summary.py --dump`
的解析结果,不要重新手算 —— 手算是顶组数字对不上的主要来源。

---

## 5. 与飞书 Base 的分工

| | 飞书 Base | Obsidian |
|---|---|---|
| 定位 | 结构化数据源,供算法查询 | 叙事性复盘,供人读 |
| 内容 | 每组的重量/次数/RPE | 判断、感受、突破、反思 |
| 谁读 | `next_top_set()` 查顶组基线 | 用户自己回顾 |

**基线只从 Base 查,不从 Obsidian 解析** —— Obsidian 是自由格式,靠正则抽数字迟早出错。
