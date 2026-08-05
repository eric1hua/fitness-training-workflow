#!/usr/bin/env python3
"""
workout_summary.py — 训练报告生成器(fitness-training-workflow)

功能:
1. 从 session markdown 解析数据(支持两种日志格式,见下)
2. 生成 HTML 报告(中文,内联 CSS,1080×1920)
3. Chrome headless 截图(PNG)

支持的 session 格式:
  A「逐动作」格式(2026-08-02 起的实际写法,首选)
     ## 动作 N:<动作名>
     **上次顶组基线**:{w}×{r} RPE {rpe}({date})
     | 组 | 重量 | 次数 | RPE | 类型 | 容量 | 累计 |
     总容量 / 总组数由各组明细累加得出,顶组取 类型=顶组 的行。
     「上次顶组」直接读同一动作段落里的基线行 —— 按动作名归属,不靠位置。

  B「汇总表」格式(assets/templates/session-log.md 模板 / 7-18 的写法)
     头部 **总容量** / **总组数** / **总时长**
     ## 🎯 今日顶组汇总 表格
     「上次顶组」从 plans/YYYY-MM-DD.md 里按**动作名**匹配。

解析原则(踩过的坑):
- 重量/RPE 允许小数。渐进算法按 ±2.5kg 递进,37.5 / 82.5 / 66.5 是必然出现的值。
- 跳过的组(~~删除线~~ / ❌ 跳过 / —)不计入容量和组数。
- 「≤ 5」这类带比较符的 RPE 按数值取 5,并标记为近似值。
- 今日顶组与上次顶组**按动作名配对**。早先版本按列表下标配对,一旦有动作被跳过或首练,
  就会拿 A 动作的成绩去比 B 动作的历史,算出一个看着很合理的假数字。
- 解析不出任何动作 → 直接报错退出。宁可不出图,也不要出一张全零但看起来正常的图。

依赖:Chrome / Google Chrome(headless 模式)

Usage:
    # 生成 PNG
    python3 scripts/workout_summary.py --date 2026-07-18 --output /tmp/Workout-Summary-2026-07-18.png

    # 只输出 HTML(调试)
    python3 scripts/workout_summary.py --date 2026-07-18 --html

    # 只看解析结果(调试)
    python3 scripts/workout_summary.py --date 2026-07-18 --dump
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

# macOS Chrome 路径(MEMORY 验证)
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class SessionParseError(RuntimeError):
    """session markdown 解析不出可用数据。"""


# =====================================================================
# 单元格解析
# =====================================================================

# 表格单元格里的装饰:加粗、删除线、状态 emoji
_STRIKE_RE = re.compile(r"~~.*?~~")
_DECOR_RE = re.compile(r"[*`]")
_STATUS_EMOJI_RE = re.compile(r"[✅❌⚠️⭐🏆🆕📊📉📌⚡🟢🟡🔴]")
# 「≤ 5」「<6」「~7」这类近似 RPE
_APPROX_RE = re.compile(r"[≤≥<>~约]")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def clean_cell(cell: str) -> str:
    """去掉 markdown 装饰和状态 emoji,保留文字内容。

    删除线整段丢弃 —— 日志里 ~~30 kg~~ 表示这一组没做。
    """
    s = _STRIKE_RE.sub("", cell)
    s = _DECOR_RE.sub("", s)
    s = _STATUS_EMOJI_RE.sub("", s)
    return s.strip()


def parse_num(cell: str) -> float | None:
    """解析数值单元格,拿不到数就返回 None(而不是 0)。

    能吃:'85 kg' / '**90 kg**' / '1,200 kg' / '82.5' / '≤ 5'
    返回 None:'❌ 跳过' / '—' / '' / '~~30 kg~~'
    """
    s = clean_cell(cell)
    if not s or "跳过" in s:
        return None
    s = s.replace(",", "")
    m = _NUM_RE.search(s)
    if not m:
        return None
    return float(m.group(0))


def is_approx(cell: str) -> bool:
    """单元格是否是「≤ 5」这类近似值。"""
    return bool(_APPROX_RE.search(clean_cell(cell)))


def fmt_num(x: float | None) -> str:
    """数值显示:整数不带小数点,小数保留一位。None → '—'。"""
    if x is None:
        return "—"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


# session 里常用简称,plan 里写全称。只收录确实出现过的,不臆造。
ACTION_ALIASES = {
    "RDL": "罗马尼亚硬拉",
    "硬拉": "罗马尼亚硬拉",
}


def norm_action(name: str) -> str:
    """动作名归一化,用于跨表匹配。

    去掉括号补充说明和空白,这样「倒蹬 45°(低脚位)」能匹配上基线里的「倒蹬 45°」。
    """
    s = _STATUS_EMOJI_RE.sub("", name)
    s = re.sub(r"[（(][^)）]*[)）]", "", s)
    s = re.sub(r"[\s·]+", "", s)
    s = s.strip()
    return ACTION_ALIASES.get(s, s)


def match_key(name: str, keys) -> str | None:
    """在一组归一化动作名里找 name 对应的那个。

    先精确匹配;不中再退一步做包含匹配(「腿弯举」↔「俯卧腿弯举」)。
    包含匹配只在**唯一命中**时才采纳 —— 命中多个说明名字有歧义,
    这时宁可判定为「无基线」,也不要随手挑一个,那正是旧版本按下标配对犯的错。
    """
    key = norm_action(name)
    if not key:
        return None
    if key in keys:
        return key
    hits = [k for k in keys if key in k or k in key]
    return hits[0] if len(hits) == 1 else None


def match_action(name: str, table: dict[str, dict]) -> dict | None:
    k = match_key(name, table.keys())
    return table[k] if k is not None else None


def split_table_row(line: str) -> list[str] | None:
    """把 markdown 表格行切成单元格;分隔行和非表格行返回 None。"""
    s = line.strip()
    if not s.startswith("|"):
        return None
    cells = [c for c in s.split("|")[1:-1]]
    if not cells:
        return None
    if all(set(c.strip()) <= set("-: ") for c in cells):  # |---|---| 分隔行
        return None
    return cells


# =====================================================================
# 格式 A:逐动作段落(首选)
# =====================================================================

_ACTION_HEAD_RE = re.compile(r"^##\s*动作\s*\d+\s*[:：]\s*(.+?)\s*$")
_BASELINE_RE = re.compile(r"上次顶组基线\s*\**\s*[:：]\s*(.+)")
_BASELINE_VAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[×x*]\s*(\d+)(?:\s*RPE\s*(\d+(?:\.\d+)?))?")
_BASELINE_DATE_RE = re.compile(r"[（(]\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})\s*[)）]")

# 组明细表的列名(用于定位各列下标,避免写死顺序)
_SET_COLS = {"重量": "weight", "次数": "reps", "RPE": "rpe", "类型": "kind", "容量": "volume"}


def _parse_set_table(lines: list[str]) -> tuple[list[dict], list[tuple[int, str]]]:
    """解析一段文本里的第一张组明细表。

    返回 (已完成的组, 被跳过的组)。被跳过的组形如 `(插在第几组之后, "30×6")` ——
    日志里写成 `| 热身 2 | ~~30 kg~~ | ~~6~~ | ... |`,重量次数被删除线包着。
    这些组不计容量也不计组数(容量算错就是从这儿来的),但报告里要显示出来:
    「原计划做、实际没做」和「压根没安排」是两回事。
    """
    header_idx = None
    col_map: dict[str, int] = {}

    for i, line in enumerate(lines):
        cells = split_table_row(line)
        if not cells:
            continue
        names = [clean_cell(c) for c in cells]
        hit = {}
        for j, n in enumerate(names):
            for key, field in _SET_COLS.items():
                if n == key or n.startswith(key):
                    hit.setdefault(field, j)
        if "weight" in hit and "reps" in hit:
            header_idx, col_map = i, hit
            break

    if header_idx is None:
        return [], []

    sets = []
    skipped: list[tuple[int, str]] = []
    for line in lines[header_idx + 1:]:
        cells = split_table_row(line)
        if cells is None:
            if line.strip().startswith("|"):
                continue
            if sets:  # 表格结束
                break
            continue
        if len(cells) <= max(col_map.values()):
            continue

        weight = parse_num(cells[col_map["weight"]])
        reps = parse_num(cells[col_map["reps"]])
        if weight is None or reps is None:  # 跳过 / 未做的组
            # 从删除线里面把原计划的重量次数捞出来。捞不到(整行都是「❌ 跳过 / —」)
            # 就不记 —— 没有数字的「跳过」在报告里也说明不了什么。
            w_raw = _NUM_RE.search(cells[col_map["weight"]])
            r_raw = _NUM_RE.search(cells[col_map["reps"]])
            if w_raw and r_raw:
                skipped.append((len(sets), f"{w_raw.group(0)}×{r_raw.group(0)}"))
            continue

        rpe_cell = cells[col_map["rpe"]] if "rpe" in col_map else ""
        kind = clean_cell(cells[col_map["kind"]]) if "kind" in col_map else ""

        sets.append({
            "weight": weight,
            "reps": int(reps),
            "rpe": parse_num(rpe_cell),
            "rpe_approx": is_approx(rpe_cell),
            "kind": kind,
            "volume": weight * int(reps),
        })

    return sets, skipped


def _pick_top_set(sets: list[dict]) -> dict | None:
    """选出该动作的顶组。

    优先 类型 标了「顶组」的组;都没标时退回「最重的已完成组」并标为准顶组
    —— progressive-overload.md 第 1 节:RPE 全程 ≤7 时,最高强度组也算基线。
    """
    working = [s for s in sets if "热身" not in s["kind"]]
    if not working:
        return None

    explicit = [s for s in working if "顶组" in s["kind"]]
    if explicit:
        top = explicit[-1]
        return {**top, "approx_top": False}

    top = max(working, key=lambda s: (s["weight"], s["reps"]))
    return {**top, "approx_top": True}


def parse_format_a(text: str) -> dict | None:
    """逐动作格式。解析不到任何动作段落时返回 None。"""
    lines = text.split("\n")

    # 切出各动作段落
    heads = [(i, _ACTION_HEAD_RE.match(l)) for i, l in enumerate(lines)]
    heads = [(i, m.group(1)) for i, m in heads if m]
    if not heads:
        return None

    exercises = []
    for k, (start, raw_name) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        block = lines[start:end]
        body = "\n".join(block)

        # 动作名:去掉「· ❌ 跳过」这类状态后缀
        name = raw_name.split("·")[0].strip()
        name = _STATUS_EMOJI_RE.sub("", name).strip()

        sets, skipped_sets = _parse_set_table(block)

        # 上次顶组基线(就在本段落里 → 天然按动作归属)
        last_top = None
        first_time = False
        m = _BASELINE_RE.search(body)
        if m:
            baseline_txt = m.group(1)
            vm = _BASELINE_VAL_RE.search(baseline_txt)
            if vm:
                dm = _BASELINE_DATE_RE.search(baseline_txt)
                last_top = {
                    "weight": float(vm.group(1)),
                    "reps": int(vm.group(2)),
                    "rpe": float(vm.group(3)) if vm.group(3) else None,
                    "date": dm.group(1) if dm else "—",
                }
            elif "首练" in baseline_txt or "首次" in baseline_txt:
                first_time = True

        exercises.append({
            "name": name,
            "sets": sets,
            "skipped_sets": skipped_sets,
            "top_set": _pick_top_set(sets),
            "last_top": last_top,
            "first_time": first_time,
            "skipped": not sets,
            "skip_reason": _find_skip_reason(body),
        })

    done = [e for e in exercises if e["sets"]]
    if not done:
        return None

    total_volume = sum(s["volume"] for e in done for s in e["sets"])
    total_sets = sum(len(e["sets"]) for e in done)

    return {
        "format": "per-exercise",
        "exercises": exercises,
        "total_volume": total_volume,
        "total_sets": total_sets,
        "planned_volume": _find_planned_volume(text),
        "duration_min": _find_duration(text),
        "topic": _find_topic(text),
        "mode_tag": _find_mode_tag(text),
        "start_time": _find_start_time(text),
        "training_day_n": _find_training_day(text),
    }


# =====================================================================
# 格式 B:顶组汇总表(旧模板)
# =====================================================================

_SUMMARY_SECTION_RE = re.compile(r"##[^\n]*今日顶组汇总.*?(?=\n##\s|\Z)", re.DOTALL)


def parse_format_b(text: str) -> dict | None:
    """汇总表格式。表格为空(全是 —)时返回 None。"""
    m = _SUMMARY_SECTION_RE.search(text)
    if not m:
        return None

    section = m.group(0).split("\n")
    exercises = []
    for line in section:
        cells = split_table_row(line)
        if not cells or len(cells) < 4:
            continue
        name = clean_cell(cells[1])
        if not name or name == "—" or name == "动作":
            continue

        vm = _BASELINE_VAL_RE.search(clean_cell(cells[2]))
        if not vm:
            continue

        weight, reps = float(vm.group(1)), int(vm.group(2))
        rpe = parse_num(cells[3]) if len(cells) > 3 else None
        kind = clean_cell(cells[5]) if len(cells) > 5 else ""

        exercises.append({
            "name": name,
            "sets": [],
            "skipped_sets": [],
            "skip_reason": "",
            "top_set": {
                "weight": weight, "reps": reps, "rpe": rpe,
                "rpe_approx": False, "kind": kind,
                "volume": weight * reps,
                "approx_top": "准顶组" in kind,
            },
            "last_top": None,   # 由 plan 文件补,按动作名匹配
            "first_time": False,
            "skipped": False,
        })

    if not exercises:
        return None

    return {
        "format": "summary-table",
        "exercises": exercises,
        "total_volume": _find_header_num(text, "总容量"),
        "total_sets": _find_header_num(text, "总组数"),
        "planned_volume": _find_planned_volume(text),
        "duration_min": _find_duration(text),
        "topic": _find_topic(text),
        "mode_tag": _find_mode_tag(text),
        "start_time": _find_start_time(text),
        "training_day_n": _find_training_day(text),
    }


# =====================================================================
# 头部字段
# =====================================================================

def _find_header_num(text: str, label: str) -> float | None:
    m = re.search(rf"{label}\s*\**\s*[:：]\s*\**\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _find_planned_volume(text: str) -> float | None:
    """计划容量。写成区间(13,691-13,776)时取下限。"""
    m = re.search(r"(?:计划|预计)\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _find_duration(text: str) -> float | None:
    """无氧时长。收工汇总没填(___)时返回 None,报告显示「—」。

    能吃的写法:
    - `总耗时：75 min` / `总时长：72 分钟`
    - `总耗时：~75 min` / `约 75 分钟` / `≈75min`
    - `总耗时：**75** min`(加粗数字)
    - `总耗时：75 min（23:21-00:33）`(后面跟括号说明)
    """
    for pat in (r"总时长\s*\**\s*[:：]\s*\**\s*[~约≈]?\s*\**\s*(\d+)\s*\**\s*(?:分钟|min)",
                r"总耗时\s*\**\s*[:：]\s*\**\s*[~约≈]?\s*\**\s*(\d+)\s*\**\s*(?:分钟|min)"):
        m = re.search(pat, text)
        if m:
            return float(m.group(1))
    return None


def _find_topic(text: str) -> str:
    m = re.search(r"主题\s*\**\s*[:：]\s*\**\s*([^\n·|]+)", text)
    if not m:
        return "—"
    topic = clean_cell(m.group(1)).split()[0] if clean_cell(m.group(1)) else "—"
    return topic.rstrip("日") + "日" if topic.endswith("日") else topic


def _find_skip_reason(text: str) -> str:
    """整组跳过的原因,来自动作段落里的 `**状态**：❌ **整组跳过**（用户 12:09 决定）`。"""
    m = re.search(r"状态\s*\**\s*[:：]\s*(.+)", text)
    if not m:
        return ""
    # 只取第一段。日志里常跟着「→ 直接进动作 4 髋内收」这类后续动作说明,
    # 对报告没用,还会把红卡撑成两行。
    s = clean_cell(m.group(1)).split("→")[0].split("·")[0]
    return re.sub(r"\s+", " ", s).strip()


def _find_start_time(text: str) -> str:
    """开练时间,来自头部 `**启动**: 11:30`。"""
    m = re.search(r"启动\s*\**\s*[:：]\s*\**\s*(\d{1,2}:\d{2})", text)
    return m.group(1) if m else ""


def _find_mode_tag(text: str) -> str:
    """主题行里 `腿日 #4` 之后的备注,如「v4 safe · 踝保护模式」。

    这是当天的训练模式(护踝、减量周之类),跟部位主题是两回事,
    单独拎出来当一个标签显示。没有就返回空,标签整个不渲染。
    """
    m = re.search(r"主题\s*\**\s*[:：]\s*\**\s*([^\n|]+)", text)
    if not m:
        return ""
    parts = [p.strip() for p in clean_cell(m.group(1)).split("·")]
    # 主题行里还会挂别的字段(7-18 写的是「腿 · 状态:已收工(18:07)」)。
    # 收工状态不是训练模式,挂进这个标签里会变成一句莫名其妙的话。
    drop = ("状态", "收工", "总时长", "总容量", "总组数")
    tail = [p for p in parts[1:] if p and not any(k in p for k in drop)]
    return " · ".join(tail)


def _find_training_day(text: str) -> str:
    m = re.search(r"训练日\s*#\s*(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"#\s*(\d+)", text)
    return m.group(1) if m else "—"


# =====================================================================
# plan 基线(格式 B 用)
# =====================================================================

def load_plan_baselines(plan_path: Path) -> tuple[dict[str, dict], set[str]]:
    """从 plan 的主体动作表里读「上次顶组」列。

    返回 ({归一化动作名: 基线}, {明确标了首练的动作名})。
    按动作名建索引 —— 报告的顶组对比靠它配对,不靠行序。
    """
    if not plan_path or not plan_path.exists():
        return {}, set()

    baselines: dict[str, dict] = {}
    first_time: set[str] = set()

    for line in plan_path.read_text(encoding="utf-8").split("\n"):
        cells = split_table_row(line)
        if not cells or len(cells) < 3:
            continue
        # 第一个看起来像动作名的单元格(排除序号和 w×r 之类的数据格)
        name = None
        for c in cells:
            t = clean_cell(c)
            if t and not _BASELINE_VAL_RE.search(t) and not t.isdigit() and t not in ("#", "—"):
                name = t
                break
        if not name:
            continue
        key = norm_action(name)

        # 「上次顶组」列:带日期的才是历史基线,不带日期的是今日计划/目标
        best = None
        for c in cells:
            t = clean_cell(c)
            vm = _BASELINE_VAL_RE.search(t)
            dm = _BASELINE_DATE_RE.search(t)
            if vm and dm:
                best = {
                    "weight": float(vm.group(1)),
                    "reps": int(vm.group(2)),
                    "rpe": float(vm.group(3)) if vm.group(3) else None,
                    "date": dm.group(1),
                }

        if best:
            baselines[key] = best
        elif any("首次" in clean_cell(c) or "首练" in clean_cell(c) for c in cells):
            first_time.add(key)

    return baselines, first_time


# =====================================================================
# 入口:解析 session
# =====================================================================

def parse_session_md(session_path: Path, plan_path: Path | None = None) -> dict:
    """解析 session markdown。两种格式都试,都失败就抛 SessionParseError。"""
    text = session_path.read_text(encoding="utf-8")

    data = parse_format_a(text) or parse_format_b(text)
    if data is None:
        raise SessionParseError(
            f"无法从 {session_path.name} 解析出任何动作数据。\n"
            f"支持的格式:(a)『## 动作 N:xxx』+ 组明细表,"
            f"(b)『## 🎯 今日顶组汇总』表格。\n"
            f"请检查 session 文件结构是否与 assets/templates/session-log.md 一致。"
        )

    data["date"] = session_path.stem

    # 格式 B 的上次顶组来自 plan,按动作名匹配
    if data["format"] == "summary-table" and plan_path:
        baselines, first_time = load_plan_baselines(plan_path)
        for ex in data["exercises"]:
            ex["last_top"] = match_action(ex["name"], baselines)
            if ex["last_top"] is None:
                ex["first_time"] = match_key(ex["name"], first_time) is not None

    # 计划偏差自己算,比从正文里抓百分比可靠(正文里的符号常常是错的)
    if data["total_volume"] and data["planned_volume"]:
        pct = (data["total_volume"] / data["planned_volume"] - 1) * 100
        data["deviation_pct"] = f"{pct:+.1f}%"
    else:
        data["deviation_pct"] = "—"

    return data


# =====================================================================
# 渲染
# =====================================================================
#
# 视觉规范:iOS 风格浅色战绩卡,照参考稿「腿日战绩卡-导出.html」复刻。
# 改样式前先读完这段。
#
# 尺寸:卡片固定 430px 宽 —— 参考稿的所有字号、圆角、间距都是按这个宽度调的,
#   改宽度等于把整套比例推翻。输出 1080px 宽是靠 Chrome 的
#   --force-device-scale-factor 放大,不是靠改 CSS。
#   VIEW_W 取 500 是因为 Chrome headless 的窗口宽度有下限,给小了它自己会顶到 500,
#   而截图仍按你传的宽度裁 —— 结果就是右边一整列被切掉。多出来的留白和背景同色,看不出来。
#
# 高度:内容高度事先算不出来(卡片行数不定、文字会换行),所以跑两遍 Chrome:
#   第一遍 --dump-dom,页面里的脚本把 scrollHeight 写进 <title>,读回来;
#   第二遍才按这个高度截图。别改回「按行高常量估算」,那样一换行就裁内容。
#
# 配色:iOS 系统色。蓝=数据/首练,绿=突破/轻松,橙=偏重/提醒,红=力竭/跳过。
#   RPE 的颜色是按数值分档的(≤7 绿 / 8 橙 / ≥9 红),不是随便标的。

CARD_W = 430
VIEW_W = 500
SCALE = 2.16          # 500 × 2.16 = 1080

C_BLUE = "#007aff"
C_GREEN = "#34c759"
C_ORANGE = "#ff9500"
C_RED = "#ff3b30"
INK = "rgba(0,0,0,0.9)"
INK_75 = "rgba(0,0,0,0.75)"
INK_60 = "rgba(60,60,67,0.6)"
INK_30 = "rgba(60,60,67,0.3)"

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 动作 → 目标肌群。只收录 references/exercise-substitutions.md 里确实出现过的,
# 认不出来的动作就不显示肌群,不臆造。
MUSCLE_BY_ACTION = {
    "倒蹬": "股四", "深蹲": "股四", "哈克深蹲": "股四", "腿举": "股四", "腿屈伸": "股四",
    "罗马尼亚硬拉": "后链", "硬拉": "后链", "挺身": "后链",
    "臀冲": "臀", "髋外展": "臀中肌", "髋内收": "内收肌",
    "腿弯举": "腘绳", "提踵": "小腿",
    "卧推": "胸", "推胸": "胸", "飞鸟": "胸", "夹胸": "胸",
    "划船": "背", "引体": "背", "下拉": "背",
    "推举": "肩", "侧平举": "肩",
}

TOPIC_EMOJI = {"腿": "🦵", "胸": "💪", "背": "💪", "推": "💪", "拉": "💪", "肩": "💪"}


def short_date(d: str) -> str:
    """日期统一压成 M/D。

    格式 A 的基线写的是「7/18」,格式 B 从 plan 里读到的是「2026-07-14」,
    完整 ISO 日期会把对比那一行挤到换行。
    """
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", d)
    return f"{int(m.group(2))}/{int(m.group(3))}" if m else d


def short_name(name: str) -> str:
    """去掉括号补充,「倒蹬 45°(低脚位)」→「倒蹬 45°」。用于突破汇总这类窄的地方。"""
    return re.sub(r"\s*[（(][^)）]*[)）]", "", name).strip()


def muscle_of(name: str) -> str:
    key = norm_action(name)
    for k, v in MUSCLE_BY_ACTION.items():
        if k in key:
            return v
    return ""


def topic_part(topic: str) -> str:
    """「腿日」→「腿」。主题解析不出来时返回空。"""
    t = (topic or "").strip()
    if not t or t == "—":
        return ""
    return t[:-1] if t.endswith("日") else t


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def weight_delta(ex: dict) -> float | None:
    """今日顶组 vs 上次顶组的重量差。没有可比基线返回 None。"""
    top, last = ex.get("top_set"), ex.get("last_top")
    if not top or not last:
        return None
    return top["weight"] - last["weight"]


def _progress(ex: dict) -> tuple[str, str, str]:
    """顶组进展 → (类别, 颜色, 文案)。

    类别:up 突破 / new 首练 / flat 维持 / down 回退 / unknown 没匹配到基线。

    两处不能含糊:
    1. `last_top` 为空时,「确实是首练」和「只是没匹配到基线」必须分开 ——
       混为一谈会把老动作误标成新动作(旧版本的 bug)。
    2. 重量次数都没变时不能只写「维持」:同样的 25×10,RPE 从 7 涨到 9 是实打实的
       退步(同样的活变吃力了)。老版本这一格显示「+0kg / +0 reps」,恰好把唯一
       有信息量的部分抹掉了。
    """
    top, last = ex["top_set"], ex["last_top"]
    if last is None:
        if ex.get("first_time"):
            return "new", C_BLUE, "首练基线"
        return "unknown", C_ORANGE, "未匹配到历史基线"

    dw = top["weight"] - last["weight"]
    dr = top["reps"] - last["reps"]

    if dw > 0:
        extra = f" · {dr:+d} 次" if dr else ""
        return "up", C_GREEN, f"+{fmt_num(dw)} kg 突破{extra}"
    if dw < 0:
        extra = f" · {dr:+d} 次" if dr else ""
        return "down", C_RED, f"{fmt_num(dw)} kg{extra}"
    if dr > 0:
        return "up", C_GREEN, f"同重 +{dr} 次 突破"
    if dr < 0:
        return "down", C_RED, f"同重 {dr} 次"

    drpe = None
    if top.get("rpe") is not None and last.get("rpe") is not None:
        drpe = top["rpe"] - last["rpe"]
    if drpe is not None and drpe >= 2:
        return "flat", C_ORANGE, (f"RPE {fmt_num(last['rpe'])}→{fmt_num(top['rpe'])}"
                                  f"，同重更吃力")
    if drpe is not None and drpe <= -2:
        return "up", C_GREEN, (f"RPE {fmt_num(last['rpe'])}→{fmt_num(top['rpe'])}"
                               f"，同重更轻松")
    return "flat", INK_60, "维持基线"


def _rpe_cell(s: dict) -> str:
    """RPE 单元格。近似值(日志写「≤ 5」)保留 ≤ 号,不要显示成一个精确的 5。"""
    if s.get("rpe") is None:
        return f'<div class="c-rpe" style="color:{INK_30}">—</div>'
    v = s["rpe"]
    color = C_GREEN if v <= 7 else (C_ORANGE if v < 9 else C_RED)
    txt = ("≤" if s.get("rpe_approx") else "") + fmt_num(v)
    return f'<div class="c-rpe" style="color:{color}">{txt}</div>'


def _top_index(sets: list[dict], top: dict) -> int:
    """顶组在组明细里的下标。

    不能用 `s is top` 判断 —— `_pick_top_set` 返回的是 `{**top, "approx_top": ...}`,
    是个新 dict,身份比较永远不成立,顶组就会被渲染成普通的 Working 行。
    按字段从后往前找,和 `_pick_top_set` 取 `explicit[-1]` 的口径一致。
    """
    for i in range(len(sets) - 1, -1, -1):
        s = sets[i]
        if (s["weight"] == top["weight"] and s["reps"] == top["reps"]
                and s.get("kind") == top.get("kind")):
            return i
    return -1


def _set_row(idx: int, s: dict, label_html: str) -> str:
    return (
        f'<div class="row">'
        f'<div class="c-idx">{idx}</div>'
        f'<div>{label_html}</div>'
        f'<div class="c-set">{fmt_num(s["weight"])}kg × {s["reps"]}</div>'
        f'{_rpe_cell(s)}'
        f'<div class="c-vol">{int(s["weight"] * s["reps"]):,}</div>'
        f'</div>'
    )


def _exercise_card(n: int, ex: dict, part: str) -> str:
    name = esc(ex["name"])
    muscle = muscle_of(ex["name"])
    tags = " / ".join(t for t in (muscle, part) if t)

    # ---- 整组跳过:红卡,不列表格 ----
    if ex["skipped"] or ex["top_set"] is None:
        reason = esc(ex.get("skip_reason") or "整组跳过")
        meta = " · ".join(t for t in (tags, f"⚠️ {reason}") if t)
        return (
            f'<div class="card-skip">'
            f'<div class="ex-head">'
            f'<div><div class="ex-name">{n}. {name}</div>'
            f'<div class="ex-sub" style="color:{C_RED}">{meta}</div></div>'
            f'<div class="ex-vol" style="color:{C_RED}">0 kg</div>'
            f'</div></div>'
        )

    top = ex["top_set"]
    kind, color, note = _progress(ex)
    sets = ex["sets"]

    # 格式 B 的旧日志只记了顶组,没有组明细 —— 用顶组撑起一行,并在卡片底部说明,
    # 免得看起来像「这个动作只做了一组」。
    legacy = not sets
    rows_src = sets or [top]

    badges = ""
    if kind == "new":
        badges = (f'<span class="badge" style="background:rgba(0,122,255,0.14);'
                  f'color:{C_BLUE}">NEW</span>'
                  f'<span class="badge-txt" style="color:{C_BLUE}">首练</span>')
    elif kind == "unknown":
        badges = (f'<span class="badge" style="background:rgba(255,149,0,0.16);'
                  f'color:{C_ORANGE}">?</span>'
                  f'<span class="badge-txt" style="color:{C_ORANGE}">无基线</span>')

    # 旧格式没有组明细,加不出这个动作的容量。压暗显示,别用蓝色 —— 蓝色是有数据的样子。
    volume = (f'<span style="color:{INK_30}">—</span>' if legacy
              else f"{int(sum(s['weight'] * s['reps'] for s in sets)):,} kg")
    n_sets = "顶组" if legacy else f"{len(sets)}组"
    meta = " · ".join(t for t in (tags, n_sets) if t)

    # ---- 组明细 ----
    skips = dict(ex.get("skipped_sets") or [])
    body = []
    if 0 in skips:
        body.append(f'<div class="row-note" style="color:{INK_60}">跳过 {skips[0]}</div>')

    top_i = _top_index(rows_src, top)
    for i, s in enumerate(rows_src):
        is_top = i == top_i
        if "热身" in s.get("kind", ""):
            label = f'<span class="label" style="color:{INK_60}">热身</span>'
        elif is_top and kind == "new":
            label = (f'<span class="label" style="background:rgba(0,122,255,0.12);'
                     f'color:{C_BLUE}">🆕 首练基线</span>')
        elif is_top and kind == "up":
            label = (f'<span class="label" style="background:rgba(52,199,89,0.12);'
                     f'color:{C_GREEN}">🏆 顶组</span>')
        elif is_top:
            label = f'<span class="label" style="color:{INK_60}">顶组</span>'
        else:
            label = f'<span class="label" style="color:{INK_60}">Working</span>'

        row = _set_row(i + 1, s, label)
        extra = ""
        if (i + 1) in skips:
            extra = f'<div class="row-note" style="color:{INK_60}">跳过 {skips[i + 1]}</div>'

        if is_top:
            hl = ("hl-blue" if kind == "new" else
                  "hl-green" if kind == "up" else "")
            body.append(f'<div class="{hl}">{row}'
                        f'<div class="row-note" style="color:{color}">{note}</div>'
                        f'{extra}</div>')
        elif extra:
            body.append(f"<div>{row}{extra}</div>")
        else:
            body.append(row)

    # ---- 卡片底部的数据校验提示 ----
    warns = []
    if top.get("approx_top"):
        warns.append("日志没标顶组,取最重的已完成组作为基线")
    heavier = [s for s in sets if "热身" not in s.get("kind", "") and s["weight"] > top["weight"]]
    if heavier:
        mx = max(s["weight"] for s in heavier)
        warns.append(f"有正式组 {fmt_num(mx)}kg 重于顶组 {fmt_num(top['weight'])}kg，"
                     f"核对日志的顶组标记")
    warn_html = "".join(
        f'<div class="card-note" style="color:{C_ORANGE}">⚠️ {esc(w)}</div>' for w in warns)

    return (
        f'<div class="card">'
        f'<div class="ex-head">'
        f'<div><div class="ex-title"><span class="ex-name">{n}. {name}</span>{badges}</div>'
        f'<div class="ex-sub">{meta}</div></div>'
        f'<div class="ex-vol">{volume}</div>'
        f'</div>'
        f'<div class="thead"><div>#</div><div>类型</div><div>重量×次数</div>'
        f'<div>RPE</div><div class="c-vol">容量</div></div>'
        f'{"".join(body)}'
        f'{warn_html}'
        f'</div>'
    )


CSS = """
body{margin:0;background:#f2f2f7}
.page{width:430px;margin:0 auto;background:#f2f2f7;padding:32px 20px 40px;
  font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Noto Sans SC',sans-serif;
  color:rgba(0,0,0,0.9);font-variant-numeric:tabular-nums;box-sizing:border-box}

.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;
  letter-spacing:2px;padding:6px 14px;border-radius:999px}
.title{margin-top:16px;font-size:25px;font-weight:800;letter-spacing:-0.3px}
.sub{margin-top:7px;font-size:13px;color:rgba(60,60,67,0.6)}
.mode{margin-top:14px;display:inline-flex;align-items:center;gap:6px;
  background:rgba(52,199,89,0.12);border:1px solid rgba(52,199,89,0.3);color:#34c759;
  font-size:12px;font-weight:600;padding:6px 12px;border-radius:999px;letter-spacing:0}
.rule{height:1px;margin:22px 0;
  background:linear-gradient(90deg,transparent,rgba(60,60,67,0.2),transparent)}

.stack{display:flex;flex-direction:column;gap:22px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.tile{background:#fff;border:1px solid rgba(60,60,67,0.1);border-radius:14px;
  padding:16px;text-align:center}
.tile .v{font-size:25px;font-weight:800}
.tile .k{margin-top:4px;font-size:12px;color:rgba(60,60,67,0.6)}
.tile-hi{background:rgba(255,149,0,0.12);border:1px solid rgba(255,149,0,0.3)}
.tile-hi .v{color:#ff9500}
.tile-hi .k{color:rgba(255,149,0,0.9)}
.plan{margin-top:-8px;text-align:center;font-size:12px;color:rgba(60,60,67,0.6)}

.sec{display:flex;flex-direction:column;gap:14px}
.sec-h{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;
  color:rgba(60,60,67,0.6)}

.card{background:#fff;border:1px solid rgba(60,60,67,0.1);border-radius:16px;
  padding:16px 16px 6px}
.card-skip{background:rgba(255,59,48,0.08);border:1px solid rgba(255,59,48,0.3);
  border-radius:16px;padding:16px}
.ex-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.ex-title{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.ex-name{font-size:15px;font-weight:700}
.ex-sub{margin-top:3px;font-size:12px;color:rgba(60,60,67,0.6)}
.ex-vol{font-size:17px;font-weight:800;color:#007aff;white-space:nowrap}
.badge{font-size:10px;font-weight:800;padding:2px 6px;border-radius:5px}
.badge-txt{font-size:13px;font-weight:700}

.thead{display:grid;grid-template-columns:20px 82px 1fr 40px 66px;gap:8px;margin-top:14px;
  padding-bottom:6px;border-bottom:1px solid rgba(60,60,67,0.1);font-size:10px;
  color:rgba(60,60,67,0.3);letter-spacing:0.3px}
.row{display:grid;grid-template-columns:20px 82px 1fr 40px 66px;gap:8px;align-items:center;
  padding:9px 2px}
.c-idx{font-size:11px;color:rgba(60,60,67,0.3)}
.c-set{font-size:13px;color:rgba(0,0,0,0.75)}
.c-rpe{font-size:13px;font-weight:700}
.c-vol{font-size:13px;font-weight:600;color:rgba(60,60,67,0.7);text-align:right}
.label{font-size:11px;font-weight:700;padding:2px 6px;border-radius:6px;white-space:nowrap}
.row-note{padding:0 2px 8px 110px;font-size:11px}
.hl-green{background:rgba(52,199,89,0.1);border-radius:8px}
.hl-blue{background:rgba(0,122,255,0.08);border-radius:8px}
.card-note{padding:2px 2px 10px;font-size:11px;line-height:1.5}

.hero{background:rgba(255,149,0,0.1);border:1px solid rgba(255,149,0,0.3);
  border-radius:18px;padding:28px 20px;text-align:center}
.hero .ico{font-size:30px}
.hero .big{margin-top:10px;font-size:23px;font-weight:800}
.hero .det{margin-top:8px;font-size:12px;color:rgba(60,60,67,0.6);line-height:1.6}

.list{background:#fff;border:1px solid rgba(60,60,67,0.1);border-radius:16px;padding:4px 16px}
.li{padding:13px 0;border-bottom:1px solid rgba(60,60,67,0.1)}
.li:last-child{border-bottom:none}
.li-name{font-size:13px;font-weight:700}
.li-cmp{margin-top:5px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px}
.li-row{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:12px 0;border-bottom:1px solid rgba(60,60,67,0.1)}
.li-row:last-child{border-bottom:none}
.li-k{font-size:13px;color:rgba(60,60,67,0.7)}
.li-v{font-size:13px;font-weight:700;text-align:right}
.foot{display:flex;align-items:center;gap:6px;font-size:12px}
.stamp{margin-top:4px;text-align:center;font-size:11px;color:rgba(60,60,67,0.3)}
"""


def _header_html(data: dict) -> str:
    d = data["date"]
    try:
        dt = date.fromisoformat(d)
        day_txt = f"{dt.month}月{dt.day}日"
        weekday = WEEKDAYS[dt.weekday()]
    except ValueError:
        day_txt, weekday = d, ""

    part = topic_part(data.get("topic", ""))
    emoji = TOPIC_EMOJI.get(part, "🏋️")
    topic = data.get("topic") or "训练"
    day_n = data.get("training_day_n", "—")

    # 有什么写什么:收工没填时长就不写时长,不要显示成 0 分钟
    bits = [weekday]
    if data.get("start_time"):
        bits.append(f"{data['start_time']} 起")
    if data.get("duration_min"):
        bits.append(f"{int(data['duration_min'])}分钟")
    bits.append("用户")
    sub = " · ".join(b for b in bits if b)

    mode = data.get("mode_tag")
    mode_html = f'<div class="mode">🛡️ {esc(mode)}</div>' if mode else ""

    return (
        f'<div style="text-align:center">'
        f'<div class="pill" style="background:rgba(0,122,255,0.12);'
        f'border:1px solid rgba(0,122,255,0.3);color:{C_BLUE}">TRAINING LOG</div>'
        f'<div class="title">{emoji} {esc(topic)} #{esc(day_n)} · {day_txt}</div>'
        f'<div class="sub">{esc(sub)}</div>'
        f'{mode_html}'
        f'</div><div class="rule"></div>'
    )


def _stats_html(data: dict, n_pr: int) -> str:
    tv = data.get("total_volume")
    ts = data.get("total_sets")
    dur = data.get("duration_min")
    planned = data.get("planned_volume")

    pr_cls = "tile tile-hi" if n_pr else "tile"
    pr_val = f"{n_pr} 🏆" if n_pr else "0"

    # 缺数据时的破折号要压暗:25px/800 的「—」渲染出来是一根很重的横杠,
    # 看着像个数值,而它表示的恰恰是「没这个数」。
    def v(x, fmt=lambda n: f"{int(n):,}"):
        return fmt(x) if x else f'<span style="color:{INK_30}">—</span>'

    plan_html = ""
    if planned:
        plan_html = (f'<div class="plan">计划 {int(planned):,} kg · '
                     f'{esc(data.get("deviation_pct", "—"))}</div>')

    return (
        f'<div class="grid2">'
        f'<div class="tile"><div class="v">{v(tv)}</div>'
        f'<div class="k">总容量 kg</div></div>'
        f'<div class="tile"><div class="v">{v(ts, lambda n: str(int(n)))}</div>'
        f'<div class="k">总组数</div></div>'
        f'<div class="tile"><div class="v">{v(dur, lambda n: str(int(n)))}</div>'
        f'<div class="k">分钟</div></div>'
        f'<div class="{pr_cls}"><div class="v">{pr_val}</div>'
        f'<div class="k">新顶组</div></div>'
        f'</div>{plan_html}'
    )


def _hero_html(prs: list[tuple[str, float]]) -> str:
    """突破汇总。一个突破都没有就整块不渲染 —— 空着的奖杯比没有奖杯更难看。"""
    if not prs:
        return ""
    total = sum(d for _, d in prs)
    det = " · ".join(f"{esc(short_name(n))} +{fmt_num(d)}" for n, d in prs)
    return (
        f'<div class="hero"><div class="ico">🏆</div>'
        f'<div class="big"><span style="color:{C_ORANGE}">+{fmt_num(total)} kg</span> '
        f'<span>新顶组突破</span></div>'
        f'<div class="det">{det} · 累计 {len(prs)} 个动作刷新顶组</div></div>'
    )


def _compare_html(data: dict, done: list[dict]) -> str:
    """今日顶组 vs 上次顶组。没有历史基线的动作也要列出来并说明原因。"""
    if not done:
        return ""
    today = short_date(data["date"])
    items = []
    for ex in done:
        top = ex["top_set"]
        kind, color, note = _progress(ex)
        now = (f'{fmt_num(top["weight"])}×{top["reps"]} '
               f'RPE{fmt_num(top["rpe"])}')
        last = ex["last_top"]
        if last:
            was = (f'{short_date(last["date"])}: {fmt_num(last["weight"])}×{last["reps"]}'
                   + (f' RPE{fmt_num(last["rpe"])}' if last.get("rpe") is not None else ""))
            line = (f'<span style="color:{INK_60}">{was}</span>'
                    f'<span style="color:rgba(60,60,67,0.25)">→</span>'
                    f'<span style="color:{C_BLUE};font-weight:700">{today}: {now}</span>'
                    f'<span style="color:{color};font-weight:700">({esc(note)})</span>')
        else:
            line = (f'<span style="color:{color};font-weight:700">{esc(note)}</span>'
                    f'<span style="color:rgba(60,60,67,0.25)">·</span>'
                    f'<span style="color:{C_BLUE};font-weight:700">{today}: {now}</span>')
        items.append(f'<div class="li"><div class="li-name">{esc(short_name(ex["name"]))}</div>'
                     f'<div class="li-cmp">{line}</div></div>')

    return (
        f'<div class="sec"><div class="sec-h">📊 顶组对比</div>'
        f'<div class="list">{"".join(items)}</div></div>'
    )


def _next_html(data: dict, done: list[dict], skipped: list[dict]) -> str:
    """下次的基线 = 今天的顶组。跳过的动作单独提醒补回计划。"""
    if not done:
        return ""
    part = topic_part(data.get("topic", "")) or "训练"
    rows = []
    for ex in done:
        top = ex["top_set"]
        tail = "（首练）" if ex.get("first_time") else ""
        rows.append(
            f'<div class="li-row"><div class="li-k">{esc(short_name(ex["name"]))} 基线</div>'
            f'<div class="li-v">{fmt_num(top["weight"])} kg × {top["reps"]} '
            f'RPE{fmt_num(top["rpe"])}{tail}</div></div>')

    warn = ""
    if skipped:
        names = "、".join(short_name(e["name"]) for e in skipped)
        warn = (f'<div class="foot" style="color:{C_ORANGE}">'
                f'⚠️ {esc(names)} 待重新加入下次{esc(part)}日计划</div>')

    return (
        f'<div class="sec"><div class="sec-h">🗓️ 下次{esc(part)}日预告</div>'
        f'<div class="list">{"".join(rows)}</div>{warn}</div>'
    )


def render_html(data: dict) -> str:
    part = topic_part(data.get("topic", ""))
    exercises = data["exercises"]
    done = [e for e in exercises if not (e["skipped"] or e["top_set"] is None)]
    skipped = [e for e in exercises if e["skipped"] or e["top_set"] is None]

    prs = []
    for ex in done:
        dw = weight_delta(ex)
        if dw and dw > 0:
            prs.append((ex["name"], dw))

    cards = "".join(_exercise_card(i + 1, ex, part) for i, ex in enumerate(exercises))

    # 旧格式日志每个动作都只有顶组一行。这话说一次就够了,挂在每张卡上是纯噪音。
    legacy_note = ""
    if data.get("format") == "summary-table":
        legacy_note = (f'<div class="foot" style="color:{C_ORANGE}">'
                       f'⚠️ 旧格式日志，只记录了顶组，无组明细与单动作容量</div>')

    body = (
        f'{_header_html(data)}'
        f'<div class="stack">'
        f'{_stats_html(data, len(prs))}'
        f'<div class="sec"><div class="sec-h">📋 动作记录</div>'
        f'{legacy_note}'
        f'<div class="sec" style="gap:14px">{cards}</div></div>'
        f'{_hero_html(prs)}'
        f'{_compare_html(data, done)}'
        f'{_next_html(data, done, skipped)}'
        f'<div class="stamp">fitness-training-workflow · 生成于 {date.today().isoformat()}</div>'
        f'</div>'
    )

    # 这段脚本只为第一遍量高度服务(把 scrollHeight 写进 title),截图时不显示。
    return (
        '<!DOCTYPE html>\n<html lang="zh"><head><meta charset="UTF-8">'
        f'<title>{esc(data["date"])} 训练战绩卡</title><style>{CSS}</style></head>'
        f'<body><div class="page">{body}</div>'
        "<script>document.title='H='+document.documentElement.scrollHeight;</script>"
        "</body></html>"
    )


def _run_chrome(args: list[str]) -> subprocess.CompletedProcess:
    if not Path(CHROME_PATH).exists():
        raise RuntimeError(f"找不到 Chrome:{CHROME_PATH}(截图依赖 Chrome headless)")
    return subprocess.run([CHROME_PATH, "--headless=new", *args],
                          capture_output=True, text=True)


_HEIGHT_RE = re.compile(r"<title>H=(\d+)</title>")


def screenshot_html(html: str, output_path: str):
    """两遍 Chrome:先量内容高度,再按这个高度截图。

    单遍做不到 —— 卡片行数不定、文字会换行,高度事先算不准;算矮了 Chrome 直接
    把后面的内容裁掉,而且裁得悄无声息。
    """
    # 用唯一临时文件,避免并发跑两次互相覆盖
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        tmp_html = Path(f.name)

    try:
        url = f"file://{tmp_html}"
        probe = _run_chrome([f"--window-size={VIEW_W},800", "--dump-dom", url])
        m = _HEIGHT_RE.search(probe.stdout or "")
        if not m:
            raise RuntimeError(f"量不到页面高度(Chrome 输出异常):{probe.stderr[-400:]}")
        height = int(m.group(1))

        shot = _run_chrome([
            f"--window-size={VIEW_W},{height}",
            f"--force-device-scale-factor={SCALE}",
            "--hide-scrollbars",
            f"--screenshot={output_path}",
            url,
        ])
        if shot.returncode != 0:
            raise RuntimeError(f"Chrome screenshot failed: {shot.stderr}")
    finally:
        tmp_html.unlink(missing_ok=True)

    print(f"✅ Screenshot saved: {output_path} "
          f"({int(VIEW_W * SCALE)}×{int(height * SCALE)})")


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="训练报告生成")
    parser.add_argument("--date", required=True, help="训练日期 YYYY-MM-DD")
    parser.add_argument("--output", help="PNG 输出路径")
    parser.add_argument("--html", action="store_true", help="只输出 HTML,不截图")
    parser.add_argument("--dump", action="store_true", help="只输出解析结果 JSON,便于排查")
    args = parser.parse_args()

    workspace = Path.home() / ".openclaw" / "workspace"
    session_path = workspace / "fitness" / "sessions" / f"{args.date}.md"
    plan_path = workspace / "fitness" / "plans" / f"{args.date}.md"

    if not session_path.exists():
        print(f"❌ session 文件不存在: {session_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = parse_session_md(session_path, plan_path)
    except SessionParseError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    if args.dump:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        sys.exit(0)

    html = render_html(data)

    if args.html:
        print(html)
        sys.exit(0)

    output = args.output or f"/tmp/Workout-Summary-{args.date}.png"
    screenshot_html(html, output)
