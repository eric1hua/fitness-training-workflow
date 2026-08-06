#!/usr/bin/env python3
"""base_audit — 按 references/data-entry-spec.md 审计飞书 Base,可选自动修复。

为什么要有「写入后审计」,而不是只做写入前校验:

  这个 Base 有**多个独立实现**在写(本仓库,以及另一台机器上的 fitness-coach)。
  前置校验必须在每个实现里各做一遍,漏一个就白做;而审计查的是 Base 本身,
  在任意一台机器上跑一次,就覆盖了所有写入方 —— 包括历史脏数据。

用法:
  python3 scripts/base_audit.py                      # 报告全部违规
  python3 scripts/base_audit.py --since 2026-07-01   # 只看这天起
  python3 scripts/base_audit.py --table 有氧          # 只看一张表
  python3 scripts/base_audit.py --fix --dry-run      # 看要改什么
  python3 scripts/base_audit.py --fix                # 真改

--fix 只动**有唯一正确答案**的三类:

  配速带单位       `11:41 /km` → `11:41`
  时间非 24 小时制  `上午11:30` → `11:30`
  总次数缺失       按当日训练组 `次数` 求和回填

缺链接、缺「录入agent」、「容量kg」为空一律只报告 —— 那些要么需要判断归属,
要么该由写入方补,自动猜一个填进去比空着更糟。
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from base_writer import (  # noqa: E402
    APP_TOKEN, TABLE_IDS, _extract_records, calc_volume, fmt_datetime,
    require_config, run_lark_cli, validate_record,
)

AUDITED_TABLES = ("训练日", "训练组", "有氧", "体测")

# 配速里出现过的单位后缀。剥掉它们,剩下的应该是纯 MM:SS。
_PACE_UNIT_RE = re.compile(r"\s*(/\s*km|每公里|min/km|分/公里)\s*$", re.I)

# 12 小时制:上午/下午/AM/PM + H:MM
_AMPM_RE = re.compile(
    r"^\s*(上午|下午|凌晨|晚上|AM|PM|am|pm)?\s*(\d{1,2}):([0-5]\d)\s*"
    r"(上午|下午|凌晨|晚上|AM|PM|am|pm)?\s*$"
)
_PM_WORDS = {"下午", "晚上", "PM", "pm"}


# === 拉数据 ===

def fetch_all(table_key: str, since: str | None = None) -> list[dict]:
    """取一张表的全部记录,返回 [{record_id, fields}, ...]。

    lark-cli 的 record-list 不自动翻页,靠 --offset 推进。翻不动就停,
    并在返回条数正好撞上页大小时提醒 —— 静默漏记录比报错更难发现。
    """
    out, offset, page = [], 0, 200
    while True:
        args = [
            "base", "+record-list",
            "--base-token", APP_TOKEN,
            "--table-id", TABLE_IDS[table_key],
            "--format", "json",
            "--limit", str(page),
            "--offset", str(offset),
        ]
        if since:
            args += ["--filter-json", json.dumps(
                {"logic": "and",
                 "conditions": [["日期", ">=", f"ExactDate({since})"]]},
                ensure_ascii=False)]
        batch = _extract_records(run_lark_cli(args))
        if not batch:
            break
        out += [{"record_id": r.get("record_id", ""), "fields": r.get("fields", r)}
                for r in batch]
        if len(batch) < page:
            break
        offset += page
    return out


def _text(v) -> str:
    """富文本字段有时是 [{'text': ...}],统一取成字符串。"""
    if isinstance(v, list):
        return "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
    return "" if v is None else str(v)


# === 可自动修复的三类 ===

def fix_pace(value) -> str | None:
    """配速去单位。已经合规则返回 None(表示无需改)。"""
    s = _text(value).strip()
    if not s:
        return None
    stripped = _PACE_UNIT_RE.sub("", s).strip()
    return stripped if stripped != s else None


def fix_time(value) -> str | None:
    """时间归一为 HH:MM。认不出来返回 None,交给人处理。"""
    s = _text(value).strip()
    if not s:
        return None
    m = _AMPM_RE.match(s)
    if not m:
        return None
    hour, minute = int(m.group(2)), m.group(3)
    marker = m.group(1) or m.group(4) or ""
    if marker in _PM_WORDS and hour < 12:
        hour += 12
    elif marker in ("上午", "凌晨", "AM", "am") and hour == 12:
        hour = 0
    normalized = f"{hour:02d}:{minute}"
    return normalized if normalized != s else None


def infer_time_form(records: list[dict]) -> str:
    """看现有合规值长什么样,决定回写用哪种形状。

    `开始`/`结束` 是 DateTime 还是文本字段尚未实测(见 data-entry-spec.md
    「待验证」)。与其猜,不如照着这张表里已经存在且能被接受的值的样子写 ——
    它们是活证据。都认不出时保守用 datetime,那是本仓库当前的写法。
    """
    hhmm = dt = 0
    for r in records:
        for name in ("开始", "结束"):
            s = _text(r["fields"].get(name)).strip()
            if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", s):
                hhmm += 1
            elif re.match(r"^\d{4}-\d{2}-\d{2}[ T]", s):
                dt += 1
    return "hhmm" if hhmm > dt else "datetime"


def reps_by_date(set_records: list[dict]) -> dict[str, int]:
    """按日期汇总训练组次数,用于回填训练日的「总次数」。"""
    totals: dict[str, int] = {}
    for r in set_records:
        d = _text(r["fields"].get("日期"))[:10]
        reps = r["fields"].get("次数")
        if d and isinstance(reps, (int, float)):
            totals[d] = totals.get(d, 0) + int(reps)
    return totals


# === 审计 ===

def audit_table(table_key: str, records: list[dict], reps_lookup: dict) -> list[dict]:
    """返回 [{record_id, 记录ID, issues, fixes}]。fixes 是 {字段: 新值}。"""
    findings = []
    time_form = infer_time_form(records) if table_key == "训练日" else "hhmm"

    for r in records:
        f = dict(r["fields"])
        # 校验前把富文本摊平,不然「记录ID」这类会被当成 list 判成缺失
        for k in ("记录ID", "录入agent", "备注", "配速", "动作", "开始", "结束"):
            if k in f:
                f[k] = _text(f[k])

        issues = validate_record(table_key, f)
        fixes: dict[str, object] = {}

        if table_key == "有氧":
            new = fix_pace(f.get("配速"))
            if new is not None:
                fixes["配速"] = new

        if table_key == "训练日":
            for name in ("开始", "结束"):
                new = fix_time(f.get(name))
                if new is not None:
                    if time_form == "datetime":
                        d = _text(f.get("日期"))[:10]
                        if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                            y, mo, dd = (int(x) for x in d.split("-"))
                            fixes[name] = fmt_datetime(date(y, mo, dd), new)
                    else:
                        fixes[name] = new
            if f.get("总次数") in (None, "", 0):
                d = _text(f.get("日期"))[:10]
                if d in reps_lookup:
                    fixes["总次数"] = reps_lookup[d]

        if issues or fixes:
            findings.append({
                "record_id": r["record_id"],
                "记录ID": _text(f.get("记录ID")) or "<无记录ID>",
                "issues": issues,
                "fixes": fixes,
            })
    return findings


def apply_fixes(table_key: str, findings: list[dict], dry_run: bool) -> int:
    updates = {x["record_id"]: x["fixes"] for x in findings
               if x["fixes"] and x["record_id"]}
    if not updates:
        return 0
    run_lark_cli([
        "base", "+record-batch-update",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS[table_key],
        "--json", json.dumps({"update_records": updates}, ensure_ascii=False),
    ], dry_run=dry_run)
    return len(updates)


# === 自检(纯函数,不碰网络)===

def self_test() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f" {detail}"))
        if not cond:
            failures.append(name)

    print("配速去单位:")
    check("'11:41 /km' → '11:41'", fix_pace("11:41 /km") == "11:41", fix_pace("11:41 /km"))
    check("'6:30/km' → '6:30'", fix_pace("6:30/km") == "6:30", fix_pace("6:30/km"))
    check("已合规的不动", fix_pace("11:41") is None, fix_pace("11:41"))
    check("空值不动", fix_pace("") is None)

    print("时间归一:")
    check("'上午11:30' → '11:30'", fix_time("上午11:30") == "11:30", fix_time("上午11:30"))
    check("'下午1:45' → '13:45'", fix_time("下午1:45") == "13:45", fix_time("下午1:45"))
    check("'7:05 PM' → '19:05'", fix_time("7:05 PM") == "19:05", fix_time("7:05 PM"))
    check("'凌晨12:20' → '00:20'", fix_time("凌晨12:20") == "00:20", fix_time("凌晨12:20"))
    check("'下午12:10' 不误加 12", fix_time("下午12:10") == "12:10", fix_time("下午12:10"))
    check("'9:05' 补零", fix_time("9:05") == "09:05", fix_time("9:05"))
    check("已合规的不动", fix_time("11:30") is None, fix_time("11:30"))
    check("认不出的不猜", fix_time("练完就走") is None, fix_time("练完就走"))

    print("时间形状推断:")
    hhmm_recs = [{"fields": {"开始": "11:30", "结束": "12:45"}}]
    dt_recs = [{"fields": {"开始": "2026-08-02 11:30:00", "结束": "2026-08-02 12:45:00"}}]
    check("现有值是 HH:MM → 回写 HH:MM", infer_time_form(hhmm_recs) == "hhmm")
    check("现有值是 datetime → 回写 datetime", infer_time_form(dt_recs) == "datetime")
    check("无证据时保守用 datetime", infer_time_form([]) == "datetime")

    print("总次数回填:")
    sets = [
        {"fields": {"日期": "2026-08-02", "次数": 10}},
        {"fields": {"日期": "2026-08-02", "次数": 12}},
        {"fields": {"日期": "2026-08-04", "次数": 8}},
    ]
    lookup = reps_by_date(sets)
    check("按日期求和", lookup == {"2026-08-02": 22, "2026-08-04": 8}, lookup)

    print("端到端(用已知的四条脏数据):")
    cardio = [{"record_id": "rec1", "fields": {
        "日期": "2026-07-29", "方式": "椭圆机", "距离km": 3.0, "时长min": 35,
        "配速": "11:41 /km", "记录ID": "cardio-2026-07-29-001",
        "录入agent": "", "备注": ""}}]
    got = audit_table("有氧", cardio, {})
    check("配速违规被认出且给出修法",
          got and got[0]["fixes"].get("配速") == "11:41", got)
    check("缺 录入agent 只报不修",
          any("录入agent" in i for i in got[0]["issues"])
          and "录入agent" not in got[0]["fixes"], got[0])
    check("椭圆机没记阻力被报出",
          any("阻力" in i for i in got[0]["issues"]), got[0]["issues"])

    session = [{"record_id": "rec2", "fields": {
        "日期": "2026-08-02", "主题": "腿", "开始": "11:30", "结束": "12:45",
        "时长min": 67, "总组数": 22, "总次数": None, "总容量kg": 11278,
        "记录ID": "session-2026-08-02", "录入agent": "kepano@imac",
        "组数明细": [{"id": "recX"}]}}]
    got = audit_table("训练日", session, {"2026-08-02": 240})
    check("总次数缺失被回填", got and got[0]["fixes"].get("总次数") == 240, got)

    session2 = [{"record_id": "rec3", "fields": {
        "日期": "2026-08-04", "主题": "腿", "开始": "上午11:30", "结束": "下午1:45",
        "时长min": 60, "总组数": 12, "总次数": 100, "总容量kg": 5000,
        "记录ID": "session-2026-08-04", "录入agent": "kepano@imac",
        "组数明细": [{"id": "recY"}]}}]
    got = audit_table("训练日", session2, {})
    check("12 小时制被归一(表内无证据 → datetime 形状)",
          got[0]["fixes"].get("开始") == "2026-08-04 11:30:00"
          and got[0]["fixes"].get("结束") == "2026-08-04 13:45:00", got[0]["fixes"])

    print()
    if failures:
        print(f"❌ {len(failures)} 项未通过: {failures}")
        return 1
    print("✅ 全部通过")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="按录入规范审计飞书 Base")
    p.add_argument("--self-test", action="store_true", help="跑纯函数自检")
    p.add_argument("--since", help="只看这个日期起的记录 YYYY-MM-DD")
    p.add_argument("--table", choices=AUDITED_TABLES, help="只看一张表")
    p.add_argument("--fix", action="store_true", help="修可确定的三类违规")
    p.add_argument("--dry-run", action="store_true", help="配合 --fix,只预览")
    args = p.parse_args()

    if args.self_test:
        return self_test()

    require_config()
    tables = [args.table] if args.table else list(AUDITED_TABLES)

    # 训练日的「总次数」要靠训练组求和,所以先把训练组拉下来
    reps_lookup = {}
    if "训练日" in tables:
        reps_lookup = reps_by_date(fetch_all("训练组", args.since))

    total_issues = total_fixed = 0
    for t in tables:
        records = fetch_all(t, args.since)
        findings = audit_table(t, records, reps_lookup)
        print(f"\n=== {t} ({len(records)} 条记录,{len(findings)} 条有问题) ===")

        for x in findings:
            print(f"  {x['记录ID']}")
            for issue in x["issues"]:
                total_issues += 1
                mark = "🔧" if any(k in issue for k in x["fixes"]) else "  "
                print(f"    {mark} {issue}")
            for field, new in x["fixes"].items():
                print(f"    🔧 「{field}」→ {new!r}")

        if args.fix:
            n = apply_fixes(t, findings, args.dry_run)
            total_fixed += n
            if n:
                print(f"  {'(dry-run) 将' if args.dry_run else '已'}更新 {n} 条")

    print(f"\n共 {total_issues} 项违规。")
    if args.fix:
        print(f"{'(dry-run) 将' if args.dry_run else '已'}修复 {total_fixed} 条记录。")
    else:
        print("带 🔧 的可以 --fix 自动修;其余需要写入方补。")
    return 1 if total_issues else 0


if __name__ == "__main__":
    sys.exit(main())
