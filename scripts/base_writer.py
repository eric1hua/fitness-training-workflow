#!/usr/bin/env python3
"""
base_writer.py — 飞书 Base 批量写入工具(fitness-training-workflow)

功能:
1. 创建训练日 record
2. 批量创建训练组 records(linked to 训练日)
3. 创建有氧 record
4. 更新训练日.组数明细 link(双向)

依赖:lark-cli(已绑 bot 身份,token: <YOUR_BASE_TOKEN>)

--- lark-cli 契约(2026-08-03 `lark-cli base +<cmd> --help` 实测)---
* +record-upsert       --json 收**顶层字段 map**,不要包一层 fields。
                       不带 --record-id = 新建;带 = 更新该条。它不会按业务键自动 upsert。
* +record-batch-create --json 收 {"create_records":[{字段map}, ...]},单次上限 200 条。
                       不是 {fields, rows} —— 旧版本写错了,批量写入会整批失败。
* +record-list         过滤/排序参数是 --filter-json / --sort-json,
                       不是 --filter / --sort(references 里的示例是原生 OpenAPI 写法,对不上 CLI)。
* 公式 / lookup / 附件 / 系统字段**不可写**。写入方的署名靠系统字段「创建人」,
  它按调用应用自动填,不进 payload。
  注意「容量kg」**不在此列** —— 它是普通数字字段,必须自己算 重量kg × 次数 并写入。
  旧版本据一处错误记载把它当公式字段过滤,导致整列写空。
* datetime CellValue 是 "YYYY-MM-DD HH:MM:SS",不是 ISO8601 带时区。
* link CellValue 是 [{"id":"rec_xxx"}],不是裸 record_id 字符串。

Usage:
    # Dry-run(打印请求,不入库)
    python3 scripts/base_writer.py --date 2026-07-18 --sets '[{...}]' --dry-run

    # 真写
    python3 scripts/base_writer.py --date 2026-07-18 --sets '[{...}]'

    # 自检(纯函数单测,不碰网络)
    python3 scripts/base_writer.py --self-test
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fitness_lib  # noqa: E402  —— 肌群/类别 的领域知识只此一份,不在这里复制一遍

# 飞书 Base 配置 —— 从 config.json 读(照着 config.example.json 填自己的)。
# 不硬编码:表 token 和 table_id 是每个人自己的,写死了别人 clone 下来就得改源码。
CONFIG_PATH = Path(
    os.environ.get("FITNESS_CONFIG", Path(__file__).resolve().parent.parent / "config.json")
)


def _load_config() -> dict:
    # 缺配置不在 import 期就退出 —— --self-test 是纯逻辑自检,不该要 token。
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


_CFG = _load_config()
BASE_TOKEN = _CFG.get("base_token", "")
APP_TOKEN = BASE_TOKEN

# 写入方的署名靠飞书系统字段「创建人」,它按调用时的应用身份自动填,不用也不能自己写。
# A 和 B 用的是两个不同的飞书应用(cli_aaac… / cli_a96d…),所以这一列区分得开。
# 系统字段一律不进 payload,写了会被 API 拒。
SYSTEM_FIELDS = frozenset({"创建人", "创建时间", "修改人", "最后更新时间"})

# 飞书表 ID(查法:`lark-cli base +base-block-list --type table`)
# 没配置时留占位值,好让 --dry-run / --self-test 在零配置下仍然跑得通;
# 真发请求前由 require_config() 拦下来。
_TABLE_KEYS = ("训练日", "训练组", "有氧", "体测", "训练计划")
TABLE_IDS = _CFG.get("table_ids") or {k: f"<TABLE_ID:{k}>" for k in _TABLE_KEYS}


def require_config() -> None:
    """真要发请求前校验。占位符当成真 token 发出去只会拿到一个费解的 400。"""
    if not BASE_TOKEN or not _CFG.get("table_ids"):
        sys.exit(
            f"缺少配置文件 {CONFIG_PATH}\n"
            "复制 config.example.json 为 config.json,填上你自己的 base_token 和 table_ids。\n"
            "table_id 查法:lark-cli base +base-block-list --base-token <你的token> --type table"
        )

# === 录入规范校验(references/data-entry-spec.md 的可执行形式)===
#
# 规则改动先改文档,再改这里。两处不一致时以文档为准。

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PACE_RE = re.compile(r"^[0-5]?\d:[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 表 → 必填字段。link 字段单列,因为它们的空值形状是 [] 而不是 ""。
_REQUIRED = {
    "训练日": ["日期", "主题", "开始", "结束", "时长min", "总组数", "总次数",
              "总容量kg", "记录ID"],
    "训练组": ["日期", "动作", "肌群", "类别", "组类型", "组序", "重量kg", "次数",
              "容量kg", "记录ID"],
    "有氧":   ["日期", "方式", "距离km", "时长min", "配速", "记录ID"],
    "体测":   ["日期", "体重kg", "记录ID"],
}

_REQUIRED_LINKS = {"训练日": ["组数明细"], "训练组": ["训练日"]}

# 有氧:这些方式的备注里必须出现对应关键词。同样的距离时长,坡度阻力不同强度差很远。
_CARDIO_NOTE_REQUIRED = {"跑步": "坡度", "椭圆机": "阻力"}


def _time_part(value: str) -> str | None:
    """从时间值里取出 HH:MM,取不出返回 None。

    接受两种形式,因为它们是同一个东西的两个层次:
      - `11:30`               —— 规范面向录入方的输入格式
      - `2026-08-02 11:30:00` —— 飞书 DateTime 字段的 CellValue 形状

    `开始`/`结束` 究竟是 DateTime 还是文本字段尚未实测(见 data-entry-spec.md
    「待验证」)。确认是文本后,fmt_datetime() 可以去掉,这里的第二种形式也随之消失。

    `上午11:30`、`11:30 AM`、`9:5` 这类一律取不出 —— 那才是要拦的东西。
    """
    s = value.strip()
    if _HHMM_RE.match(s):
        return s
    m = re.match(r"^\d{4}-\d{2}-\d{2}[ T](([01]\d|2[0-3]):[0-5]\d)(:[0-5]\d)?$", s)
    return m.group(1) if m else None


def validate_record(table_key: str, fields: dict) -> list[str]:
    """按录入规范校验单条记录,返回违规说明列表(空 = 合规)。

    纯函数,不碰网络,所以进得了 --self-test。
    """
    issues = []

    for name in _REQUIRED.get(table_key, []):
        v = fields.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            issues.append(f"缺必填字段「{name}」")

    for name in _REQUIRED_LINKS.get(table_key, []):
        if not fields.get(name):
            issues.append(f"缺必填链接「{name}」—— 只挂一边等于没挂")

    d = fields.get("日期")
    if isinstance(d, str) and d and not _DATE_RE.match(d):
        issues.append(f"「日期」应为 YYYY-MM-DD,得到 {d!r}")

    if table_key == "训练日":
        for name in ("开始", "结束"):
            v = fields.get(name)
            if isinstance(v, str) and v and _time_part(v) is None:
                issues.append(
                    f"「{name}」应为 HH:MM 24 小时制,得到 {v!r}")

    if table_key == "训练组":
        w, r, vol = fields.get("重量kg"), fields.get("次数"), fields.get("容量kg")
        if all(isinstance(x, (int, float)) for x in (w, r, vol)):
            expect = calc_volume(w, r)
            if abs(vol - expect) > 0.05:
                issues.append(f"「容量kg」={vol} 与 重量kg×次数={expect} 不符")

    if table_key == "有氧":
        pace = fields.get("配速")
        if isinstance(pace, str) and pace and not _PACE_RE.match(pace.strip()):
            issues.append(f"「配速」应为 MM:SS 且不带单位,得到 {pace!r}")
        kw = _CARDIO_NOTE_REQUIRED.get(str(fields.get("方式") or ""))
        if kw and kw not in str(fields.get("备注") or ""):
            issues.append(f"方式为「{fields.get('方式')}」时,备注必须记{kw}")

    return issues


def calc_volume(weight_kg: float, reps: int) -> float:
    """训练组容量。Base 里 `容量kg` **不是公式字段**,不写就是整列为空。

    此前 base-schema.md 记载它是 Formula,本脚本据此把它过滤掉了
    (READONLY_FIELDS),结果这台机器写的组记录容量全空。记载是错的。
    """
    return round(float(weight_kg) * int(reps), 1)


class BaseWriteError(RuntimeError):
    """写入前置校验失败(ID 冲突等)。"""


def _arg_value(args: list, flag: str):
    """取 args 里 flag 的下一个值,没有则 None。"""
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def enforce_spec(args: list, dry_run: bool = False) -> None:
    """发请求前按录入规范校验 payload。真写违规则退出;dry-run 只警告。

    只校验**新建**:带 --record-id 的是局部更新(如回填 link),字段本来就不全,
    拿整表的必填清单去卡它只会误报。

    dry-run 不硬退出,是为了保住「零配置也能预览请求体」——违规该被看见,
    但不该妨碍你看 payload 长什么样。
    """
    payload_raw = _arg_value(args, "--json")
    if payload_raw is None or _arg_value(args, "--record-id") is not None:
        return

    table_id = _arg_value(args, "--table-id")
    table_key = {v: k for k, v in TABLE_IDS.items()}.get(table_id)
    if table_key is None:
        return  # 不认识的表,不拦

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return  # 交给 lark-cli 自己报

    if isinstance(payload, dict) and "update_records" in payload:
        return  # 批量更新也是局部的,同 --record-id 的道理

    records = payload.get("create_records") if isinstance(payload, dict) else None
    if records is None:
        records = [payload]

    problems = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for issue in validate_record(table_key, rec):
            problems.append(f"  {rec.get('记录ID', '<无记录ID>')}: {issue}")

    if not problems:
        return
    msg = (
        f"录入规范校验未通过({table_key},{len(problems)} 项):\n"
        + "\n".join(problems)
        + "\n规范见 references/data-entry-spec.md。"
    )
    if dry_run:
        print(f"⚠️  {msg}\n(dry-run,不阻止;真写会被拦下)", file=sys.stderr)
    else:
        sys.exit(msg)


def run_lark_cli(args: list, dry_run: bool = False) -> dict:
    """运行 lark-cli 命令,返回 JSON(dry-run 返回 placeholder 让下游继续)"""
    if not dry_run:
        require_config()
    enforce_spec(args, dry_run)
    cmd = ["lark-cli"] + args
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {result.stderr}")
    parsed = json.loads(result.stdout)
    # Dry-run 返回 data.api(API 预览),不是真实 response
    if dry_run and "api" in parsed.get("data", {}):
        return {"data": {"record": {"record_id": "<DRY-RUN>"}, "records": []}, "dry_run": True}
    return parsed


# === ID 生成器(record-id-conventions.md) ===

def gen_session_id(d: date) -> str:
    return f"session-{d.isoformat()}"


def gen_set_id(d: date, nnn: int) -> str:
    assert 1 <= nnn <= 999, "NNN 必须在 001-999"
    return f"set-{d.isoformat()}-{nnn:03d}"


def gen_cardio_id(d: date, nnn: int) -> str:
    assert 1 <= nnn <= 999, "NNN 必须在 001-999"
    return f"cardio-{d.isoformat()}-{nnn:03d}"


def gen_body_id(d: date, nnn: int) -> str:
    assert 1 <= nnn <= 999, "NNN 必须在 001-999"
    return f"body-{d.isoformat()}-{nnn:03d}"


def fmt_datetime(d: date, hhmm: str) -> str:
    """飞书 datetime CellValue:'YYYY-MM-DD HH:MM:SS'。

    输入必须是 24 小时制 HH:MM(H:MM 会补零)。2026-08-04 那条 12 小时制的记录
    就是从这里漏进去的 —— 之前这个函数对输入不设防,拿到什么就拼什么。
    """
    s = hhmm.strip()
    m = re.match(r"^(\d{1,2}):([0-5]\d)$", s)
    if not m or int(m.group(1)) > 23:
        raise ValueError(
            f"时间应为 24 小时制 HH:MM,得到 {hhmm!r}。"
            "「上午11:30」「11:30 AM」这类先转成 11:30 再传。"
        )
    return f"{d.isoformat()} {int(m.group(1)):02d}:{m.group(2)}:00"


# === 序号续号(record-id-conventions.md 第 5 节)===

_SEQ_RE = re.compile(r"-(\d{3})$")


def max_seq_from_ids(record_ids: list[str], prefix: str, d: date) -> int:
    """从一批记录ID里找出该日已用的最大序号;没有就返回 0。

    record-id-conventions.md 第 5 节要求「取该日 max NNN + 1」。
    旧版本把起始序号写死成 1,同一天只要写第二次(补录 / 上次写到一半),
    就会生成和已有记录相同的 ID。
    """
    head = f"{prefix}-{d.isoformat()}"
    seqs = []
    for rid in record_ids:
        if not rid or not rid.startswith(head):
            continue
        m = _SEQ_RE.search(rid)
        if m:
            seqs.append(int(m.group(1)))
    return max(seqs, default=0)


def _extract_records(payload: dict) -> list[dict]:
    """从 record-list 响应里取出记录数组(容忍几种外层包法)。"""
    data = payload.get("data", payload)
    for key in ("items", "records", "record_list"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def query_day_record_ids(table_key: str, d: date, dry_run: bool = False) -> list[str]:
    """查该日已存在记录的「记录ID」字段值。"""
    if dry_run:
        return []
    payload = run_lark_cli([
        "base", "+record-list",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS[table_key],
        "--filter-json", json.dumps(
            {"logic": "and", "conditions": [["日期", "==", f"ExactDate({d.isoformat()})"]]},
            ensure_ascii=False),
        "--field-id", "记录ID",
        "--format", "json",
        "--limit", "200",
    ])
    out = []
    for rec in _extract_records(payload):
        fields = rec.get("fields", rec)
        val = fields.get("记录ID")
        if isinstance(val, list):  # 富文本字段有时是 [{"text": "..."}]
            val = "".join(seg.get("text", "") for seg in val if isinstance(seg, dict))
        if val:
            out.append(str(val))
    return out


def next_seq(table_key: str, prefix: str, d: date, dry_run: bool = False) -> int:
    """该日下一个可用序号。"""
    return max_seq_from_ids(query_day_record_ids(table_key, d, dry_run), prefix, d) + 1


# === payload 构造(纯函数,可单测)===

def build_session_fields(
    session_date: date, topic: str, total_volume_kg: float, total_sets: int,
    total_reps: int, duration_min: int, start_time: str, end_time: str, notes: str = "",
) -> dict:
    return {
        "日期": session_date.isoformat(),
        "主题": topic,
        "开始": fmt_datetime(session_date, start_time),
        "结束": fmt_datetime(session_date, end_time),
        "时长min": duration_min,
        "总组数": total_sets,
        "总次数": total_reps,
        "总容量kg": total_volume_kg,
        "记录ID": gen_session_id(session_date),
        "备注": notes,
    }


def build_set_records(
    session_date: date, session_record_id: str, sets: list[dict], start_seq: int = 1,
) -> list[dict]:
    """构造 create_records 数组。每条是独立的字段 map。

    组序 = 该动作内部的第几组(1,2,3…),按动作分别计数;
    记录ID 的 NNN 是当日全局流水号。两者用途不同,别混。
    """
    records = []
    per_action_count: dict[str, int] = {}

    for i, s in enumerate(sets):
        action = s["动作"]
        per_action_count[action] = per_action_count.get(action, 0) + 1

        # 肌群/类别 认不出来就报错,不要静默填空串 —— 空肌群会让递进步长按小肌群
        # 算(+1kg 而非 +2.5kg),部位轮换也判不出练过什么。宁可现在停下来问。
        muscle = s.get("肌群") or fitness_lib.muscle_of(action)
        if not muscle:
            raise BaseWriteError(
                f"认不出动作「{action}」的肌群。在 sets 里显式给「肌群」,"
                f"或把它加进 fitness_lib.ACTION_MUSCLE。"
            )
        category = s.get("类别") or fitness_lib.category_of(muscle)
        if not category:
            raise BaseWriteError(f"肌群「{muscle}」没有对应的类别,检查 MUSCLE_CATEGORY。")

        rec = {
            "日期": session_date.isoformat(),
            "训练日": [{"id": session_record_id}],      # link CellValue
            "动作": action,
            "肌群": muscle,
            "类别": category,
            "组类型": s.get("组类型", "正式"),
            "组序": per_action_count[action],
            "重量kg": s["重量kg"],
            "次数": s["次数"],
            "RPE": s.get("RPE", 0),
            "容量kg": calc_volume(s["重量kg"], s["次数"]),
            "记录ID": gen_set_id(session_date, start_seq + i),
                "备注": s.get("备注", ""),
        }
        records.append(rec)

    return records


def build_cardio_fields(
    cardio_date: date, seq: int, method: str, distance_km: float, duration_min: int,
    avg_hr: int, calories: int, pace: str, notes: str = "",
) -> dict:
    return {
        "日期": cardio_date.isoformat(),
        "方式": method,
        "距离km": distance_km,
        "时长min": duration_min,
        "平均心率": avg_hr,
        "卡路里": calories,
        "配速": pace,
        "记录ID": gen_cardio_id(cardio_date, seq),
        "备注": notes,
    }


# === 训练日 ===

def create_training_session(
    session_date: date,
    topic: str,
    total_volume_kg: float,
    total_sets: int,
    total_reps: int,
    duration_min: int,
    start_time: str,
    end_time: str,
    notes: str = "",
    dry_run: bool = False,
    allow_duplicate: bool = False,
) -> str:
    """创建训练日 record,返回飞书 record_id。

    同日已存在训练日时默认报错不写(record-id-conventions.md:v1 默认跳过,避免误覆盖)。
    """
    session_id = gen_session_id(session_date)

    if not dry_run and not allow_duplicate:
        if session_id in query_day_record_ids("训练日", session_date):
            raise BaseWriteError(
                f"训练日 {session_id} 已存在。"
                f"需要覆盖请显式传 allow_duplicate=True,或先在 Base 里删掉旧记录。"
            )

    fields = build_session_fields(
        session_date, topic, total_volume_kg, total_sets, total_reps,
        duration_min, start_time, end_time, notes,
    )

    result = run_lark_cli([
        "base", "+record-upsert",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS["训练日"],
        "--json", json.dumps(fields, ensure_ascii=False),
    ], dry_run=dry_run)

    if dry_run:
        return "<DRY-RUN-SESSION>"
    # lark-cli 1.0.81+ 的 +record-upsert 返回 data.record.record_id_list(数组)
    # 旧版本返回 data.record.record_id(字符串),两种都兼容
    rec = result["data"]["record"]
    if isinstance(rec, dict):
        rid_list = rec.get("record_id_list")
        if rid_list:
            return rid_list[0]
        if rec.get("record_id"):
            return rec["record_id"]
    raise KeyError(f"unexpected upsert response: {result}")


# === 训练组(批量) ===

def batch_create_training_sets(
    session_date: date,
    session_record_id: str,
    sets: list[dict],
    dry_run: bool = False,
    start_seq: int | None = None,
) -> list[str]:
    """批量创建训练组 records,返回飞书 record_id 列表。

    start_seq 不给就查库续号,避免同日二次写入撞 ID。
    """
    if start_seq is None:
        start_seq = next_seq("训练组", "set", session_date, dry_run)

    records = build_set_records(session_date, session_record_id, sets, start_seq)

    result = run_lark_cli([
        "base", "+record-batch-create",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS["训练组"],
        "--json", json.dumps({"create_records": records}, ensure_ascii=False),
    ], dry_run=dry_run)

    if dry_run:
        return [f"<DRY-RUN-SET-{i}>" for i in range(len(sets))]
    # lark-cli 1.0.81+ 的 +record-batch-create 返回 data.record_id_list(数组)
    # 旧版本返回 data.records:[{record_id}] 列表,两种都兼容
    data = result["data"]
    if isinstance(data.get("record_id_list"), list):
        return data["record_id_list"]
    if isinstance(data.get("records"), list):
        return [r["record_id"] for r in data["records"]]
    raise KeyError(f"unexpected batch-create response: {result}")


# === 训练日 link 更新 ===

def update_session_link(
    session_record_id: str,
    set_record_ids: list[str],
    dry_run: bool = False,
) -> dict:
    """更新训练日.组数明细 link 字段(双向 link 闭环)。"""
    fields = {
        # link CellValue 必须是 [{"id": rec_xxx}],不是裸字符串数组
        "组数明细": [{"id": rid} for rid in set_record_ids],
    }

    return run_lark_cli([
        "base", "+record-upsert",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS["训练日"],
        "--record-id", session_record_id,
        "--json", json.dumps(fields, ensure_ascii=False),
    ], dry_run=dry_run)


# === 有氧 ===

def create_cardio(
    cardio_date: date,
    method: str,
    distance_km: float,
    duration_min: int,
    avg_hr: int,
    calories: int,
    pace: str,
    notes: str = "",
    dry_run: bool = False,
    seq: int | None = None,
) -> str:
    """创建有氧 record。

    一天可能有多条有氧(晨跑 + 晚走),序号查库续号 —— 旧版本写死 NNN=1,
    第二条必然和第一条撞 ID。
    """
    if seq is None:
        seq = next_seq("有氧", "cardio", cardio_date, dry_run)

    fields = build_cardio_fields(
        cardio_date, seq, method, distance_km, duration_min, avg_hr, calories, pace, notes,
    )

    result = run_lark_cli([
        "base", "+record-upsert",
        "--base-token", APP_TOKEN,
        "--table-id", TABLE_IDS["有氧"],
        "--json", json.dumps(fields, ensure_ascii=False),
    ], dry_run=dry_run)

    if dry_run:
        return "<DRY-RUN-CARDIO>"
    rec = result["data"]["record"]
    if isinstance(rec, dict):
        rid_list = rec.get("record_id_list")
        if rid_list:
            return rid_list[0]
        if rec.get("record_id"):
            return rec["record_id"]
    raise KeyError(f"unexpected upsert response: {result}")


# === 完整流程 ===

def write_full_session(
    session_date: date,
    topic: str,
    total_volume_kg: float,
    total_sets: int,
    total_reps: int,
    duration_min: int,
    start_time: str,
    end_time: str,
    sets: list[dict],
    cardio: dict | None = None,
    notes: str = "",
    dry_run: bool = False,
) -> dict:
    """完整流程:训练日 + 训练组 + 有氧 + 双向 link"""

    print(f"[1/4] 创建训练日 {gen_session_id(session_date)}...")
    session_rid = create_training_session(
        session_date, topic, total_volume_kg, total_sets, total_reps,
        duration_min, start_time, end_time, notes, dry_run
    )
    print(f"      → 飞书 record_id: {session_rid}")

    print(f"[2/4] 批量创建 {len(sets)} 个训练组...")
    set_rids = batch_create_training_sets(session_date, session_rid, sets, dry_run)
    print(f"      → 飞书 record_ids: {set_rids[:3]}...")

    print("[3/4] 更新训练日.组数明细 link...")
    update_session_link(session_rid, set_rids, dry_run)
    print("      → 双向 link 完成")

    cardio_rid = None
    if cardio:
        print("[4/4] 创建有氧 record...")
        cardio_rid = create_cardio(
            session_date,
            cardio["方式"],
            cardio["距离km"],
            cardio["时长min"],
            cardio["平均心率"],
            cardio["卡路里"],
            cardio["配速"],
            cardio.get("备注", ""),
            dry_run,
        )
        print(f"      → 飞书 record_id: {cardio_rid}")

    return {
        "session_record_id": session_rid,
        "set_record_ids": set_rids,
        "cardio_record_id": cardio_rid,
        "dry_run": dry_run,
    }


# === 自检(纯函数,不碰网络)===

def self_test() -> int:
    d = date(2026, 8, 2)
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} {detail}")
            failures.append(name)

    print("序号续号:")
    check("空表从 001 起", max_seq_from_ids([], "set", d) + 1 == 1)
    check("已有 3 条 → 下一个 004",
          max_seq_from_ids([gen_set_id(d, i) for i in (1, 2, 3)], "set", d) + 1 == 4)
    check("忽略其它日期的记录",
          max_seq_from_ids(["set-2026-08-01-009", gen_set_id(d, 2)], "set", d) + 1 == 3)
    check("有氧独立计数",
          max_seq_from_ids(["cardio-2026-08-02-001"], "cardio", d) + 1 == 2)

    print("训练组 payload:")
    sets = [
        {"动作": "倒蹬 45°", "重量kg": 85, "次数": 10, "RPE": 8, "组类型": "正式"},
        {"动作": "倒蹬 45°", "重量kg": 90, "次数": 10, "RPE": 9, "组类型": "顶组"},
        {"动作": "腿屈伸", "重量kg": 66.5, "次数": 12, "RPE": 8, "组类型": "正式"},
    ]
    recs = build_set_records(d, "recABC", sets, start_seq=4)
    check("容量kg 已写入(不是公式字段,不写就整列空)",
          [r["容量kg"] for r in recs] == [850.0, 900.0, 798.0],
          [r.get("容量kg") for r in recs])
    check("容量小数保留 1 位", calc_volume(37.5, 7) == 262.5, calc_volume(37.5, 7))
    check("记录ID 从 start_seq 续号", [r["记录ID"] for r in recs] ==
          ["set-2026-08-02-004", "set-2026-08-02-005", "set-2026-08-02-006"],
          [r["记录ID"] for r in recs])
    check("组序按动作分别计数", [r["组序"] for r in recs] == [1, 2, 1],
          [r["组序"] for r in recs])
    check("link 是 [{'id':...}] 形状", recs[0]["训练日"] == [{"id": "recABC"}])
    check("小数重量原样保留", recs[2]["重量kg"] == 66.5)

    print("datetime 格式:")
    check("HH:MM → 'YYYY-MM-DD HH:MM:SS'",
          fmt_datetime(d, "17:00") == "2026-08-02 17:00:00", fmt_datetime(d, "17:00"))

    print("训练日 payload:")
    f = build_session_fields(d, "腿", 11278, 22, 240, 67, "11:30", "12:45")
    check("记录ID 正确", f["记录ID"] == "session-2026-08-02")
    check("开始时间格式", f["开始"] == "2026-08-02 11:30:00", f["开始"])

    print("系统字段不进 payload:")
    cardio_f = build_cardio_fields(d, 1, "跑步", 5.0, 30, 140, 300, "6:00")
    for label, payload in (("训练日", f), ("有氧", cardio_f)):
        check(f"{label} payload 不含系统字段",
              not (SYSTEM_FIELDS & set(payload)), SYSTEM_FIELDS & set(payload))
    check("训练组 payload 不含系统字段",
          all(not (SYSTEM_FIELDS & set(r)) for r in recs))

    print("构造出来的 payload 必须过得了自己的校验:")
    # 这条是端到端的:分开测 build_* 和 validate_record 都会绿,但真实链路上
    # build 的产物要经 enforce_spec 再发出去。少了这条,fmt_datetime 输出完整
    # datetime 而校验只认 HH:MM 的矛盾就会一路漏到线上。
    if True:
        s_fields = build_session_fields(d, "腿", 11278, 22, 240, 67, "11:30", "12:45")
        s_fields["组数明细"] = [{"id": "recX"}]
        check("训练日 build → validate 零违规",
              validate_record("训练日", s_fields) == [],
              validate_record("训练日", s_fields))
        s_recs = build_set_records(d, "recABC", sets, start_seq=4)
        set_issues = [i for r in s_recs for i in validate_record("训练组", r)]
        check("训练组 build → validate 零违规", set_issues == [], set_issues)
        c_fields = build_cardio_fields(
            d, 1, "椭圆机", 3.2, 30, 128, 260, "11:41", "阻力 8")
        check("有氧 build → validate 零违规",
              validate_record("有氧", c_fields) == [],
              validate_record("有氧", c_fields))

    print("fmt_datetime 入口设防:")
    for bad in ("上午11:30", "11:30 AM", "9:5", "25:00"):
        try:
            fmt_datetime(d, bad)
            check(f"拒绝 {bad!r}", False, "没抛异常")
        except ValueError:
            check(f"拒绝 {bad!r}", True)
    check("H:MM 补零", fmt_datetime(d, "9:05") == "2026-08-02 09:05:00",
          fmt_datetime(d, "9:05"))

    print("录入规范校验:")
    ok_set = {
        "日期": "2026-08-02", "动作": "倒蹬 45°", "肌群": "股四", "类别": "腿",
        "组类型": "顶组", "组序": 1, "重量kg": 90, "次数": 10, "容量kg": 900.0,
        "记录ID": "set-2026-08-02-001",
        "训练日": [{"id": "recABC"}],
    }
    check("合规训练组零违规", validate_record("训练组", ok_set) == [],
          validate_record("训练组", ok_set))
    check("容量对不上被抓出",
          any("容量kg" in x for x in validate_record("训练组", {**ok_set, "容量kg": 123})))
    check("缺 训练日 link 被抓出",
          any("训练日" in x for x in validate_record("训练组", {**ok_set, "训练日": []})))

    ok_session = {
        "日期": "2026-08-02", "主题": "腿", "开始": "11:30", "结束": "12:45",
        "时长min": 67, "总组数": 22, "总次数": 240, "总容量kg": 11278,
        "记录ID": "session-2026-08-02",
        "组数明细": [{"id": "recX"}],
    }
    check("合规训练日零违规", validate_record("训练日", ok_session) == [],
          validate_record("训练日", ok_session))
    for bad in ("上午11:30", "11:30 AM", "9:5", "11:30/12:45"):
        check(f"开始={bad!r} 被抓出",
              any("开始" in x for x in validate_record("训练日", {**ok_session, "开始": bad})))
    check("DateTime CellValue 形状被接受(字段类型未定,两种都得认)",
          validate_record("训练日", {**ok_session, "开始": "2026-08-02 11:30:00"}) == [])
    check("总次数缺失被抓出",
          any("总次数" in x for x in
              validate_record("训练日", {**ok_session, "总次数": None})))
    check("缺 组数明细 被抓出",
          any("组数明细" in x for x in
              validate_record("训练日", {**ok_session, "组数明细": []})))

    ok_cardio = {
        "日期": "2026-08-02", "方式": "椭圆机", "距离km": 3.2, "时长min": 30,
        "配速": "11:41", "记录ID": "cardio-2026-08-02-001",
        "备注": "阻力 8",
    }
    check("合规有氧零违规", validate_record("有氧", ok_cardio) == [],
          validate_record("有氧", ok_cardio))
    check("配速带单位被抓出",
          any("配速" in x for x in validate_record("有氧", {**ok_cardio, "配速": "11:41 /km"})))
    check("椭圆机备注没记阻力被抓出",
          any("阻力" in x for x in validate_record("有氧", {**ok_cardio, "备注": ""})))
    check("跑步备注没记坡度被抓出",
          any("坡度" in x for x in
              validate_record("有氧", {**ok_cardio, "方式": "跑步", "备注": "跑步机 2 号"})))
    check("步行不强制备注",
          validate_record("有氧", {**ok_cardio, "方式": "步行", "备注": ""}) == [])

    print()
    if failures:
        print(f"❌ {len(failures)} 项未通过: {failures}")
        return 1
    print("✅ 全部通过")
    return 0


# === CLI ===

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="飞书 Base 批量写入")
    parser.add_argument("--date", help="训练日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="预览请求,不入库")
    parser.add_argument("--from-session-md", help="从 session markdown 解析(待实现)")
    parser.add_argument("--sets", help="训练组 JSON 字符串")
    parser.add_argument("--topic", default="腿", help="训练主题")
    parser.add_argument("--duration", type=int, default=60, help="无氧时长 min")
    parser.add_argument("--start", default="17:00", help="开始时间 HH:MM")
    parser.add_argument("--end", default="18:00", help="结束时间 HH:MM")
    parser.add_argument("--self-test", action="store_true", help="跑纯函数自检")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if not args.date:
        parser.error("--date 必填(除非 --self-test)")

    d = date.fromisoformat(args.date)

    if args.from_session_md:
        # TODO: parse session md → 提取 sets / cardio
        # workout_summary.py 已经能解析 session 的组明细,后续把它抽成共用模块再接上
        print("TODO: parse session md → 暂未实现", file=sys.stderr)
        sys.exit(1)
    elif args.sets:
        sets = json.loads(args.sets)
        try:
            result = write_full_session(
                session_date=d,
                topic=args.topic,
                total_volume_kg=sum(s["重量kg"] * s["次数"] for s in sets),
                total_sets=len(sets),
                total_reps=sum(s["次数"] for s in sets),
                duration_min=args.duration,
                start_time=args.start,
                end_time=args.end,
                sets=sets,
                dry_run=args.dry_run,
            )
        except BaseWriteError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("请提供 --sets 或 --from-session-md", file=sys.stderr)
        sys.exit(1)
