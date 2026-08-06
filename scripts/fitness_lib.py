#!/usr/bin/env python3
"""
fitness_lib.py — 训练算法实现(fitness-training-workflow)

references/ 里的算法过去只以 Python 伪代码的形式散落在 markdown 里,
`is_large_muscle()` 和 `SUBSTITUTION_TABLE` 被反复调用却从来没有定义过。
结果是每次训练都要现读表格、现推算术 —— 这正是静默算错的地方
(progressive-overload.md 自己的例子就把俯卧腿弯举按小肌群 +1kg 算了,它是腿,该 +2.5kg)。

这个模块是那些算法的**唯一实现**。markdown 只讲「为什么」,数值逻辑一律走这里。

    from fitness_lib import next_top_set, decide_today_topic, substitute_weight

自检:
    python3 scripts/fitness_lib.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

# =====================================================================
# 1. 动作 → 肌群 → 大/小肌群
# =====================================================================

# 大肌群:胸/背/腿(股四/腘绳/臀)  → 递进步长 2.5 kg
# 小肌群:肩/臂/小腿/核心          → 递进步长 1 kg
LARGE_MUSCLES = {"胸", "背", "股四", "腘绳", "臀"}
SMALL_MUSCLES = {"肩", "臂", "小腿", "核心"}

# 动作 → 主练肌群。只收录实际用过或 references 里列过的动作。
# 匹配是「包含」语义:「倒蹬 45°(低脚位)」能命中「倒蹬」。
ACTION_MUSCLE = {
    # --- 腿:股四 ---
    "倒蹬": "股四",
    "腿举": "股四",
    "深蹲": "股四",
    "哈克深蹲": "股四",
    "高脚杯深蹲": "股四",
    "腿屈伸": "股四",
    "伸膝": "股四",
    # --- 腿:腘绳 ---
    "罗马尼亚硬拉": "腘绳",
    "RDL": "腘绳",
    "硬拉": "腘绳",
    "腿弯举": "腘绳",
    # --- 腿:臀 ---
    "臀冲": "臀",
    "髋外展": "臀",
    "髋内收": "臀",
    "弓步": "臀",
    # --- 小腿 ---
    "提踵": "小腿",
    # --- 胸 ---
    "卧推": "胸",
    "推胸": "胸",
    "俯卧撑": "胸",
    "蝴蝶机": "胸",
    "夹胸": "胸",
    # --- 背 ---
    "高位下拉": "背",
    "划船": "背",
    "引体": "背",
    "俯身挺身": "背",     # 竖脊肌,见 exercise-substitutions.md 的 MEMORY 纠正
    "罗马椅": "背",
    # --- 肩 ---
    "肩推": "肩",
    "推举": "肩",
    "侧平举": "肩",
    # --- 臂 ---
    "弯举": "臂",
    "下压": "臂",
    "臂屈伸": "臂",
    # --- 核心 ---
    "卷腹": "核心",
    "举腿": "核心",
    "转体": "核心",
    "平板支撑": "核心",
    "plank": "核心",
}

# 部位主题 → 覆盖的肌群(muscle-rotation.md 第 1 节)
TOPIC_MUSCLES = {
    "推": {"胸", "肩", "臂"},
    "拉": {"背", "臂"},
    "腿": {"股四", "腘绳", "臀", "小腿"},
    "核心": {"核心"},
}

BIG_TOPICS = ("推", "拉", "腿")

LARGE_STEP_KG = 2.5
SMALL_STEP_KG = 1.0


def muscle_of(action: str) -> str | None:
    """动作 → 主练肌群。认不出来返回 None(调用方需要显式处理,不要默默当小肌群)。"""
    if not action:
        return None
    # 长键优先,避免「腿屈伸」被「伸膝」之类的短键抢先
    for key in sorted(ACTION_MUSCLE, key=len, reverse=True):
        if key in action:
            return ACTION_MUSCLE[key]
    return None


# 肌群 → Base「训练组.类别」单选值。
# 「臂」在 TOPIC_MUSCLES 里同属推和拉,但类别选项本身就有「肩」「臂」两项,
# 直接映到自己即可,歧义自然消失。
MUSCLE_CATEGORY = {
    "胸": "推", "背": "拉",
    "股四": "腿", "腘绳": "腿", "臀": "腿", "小腿": "腿",
    "肩": "肩", "臂": "臂", "核心": "核心",
}


def category_of(muscle: str | None) -> str | None:
    """肌群 → 训练组.类别。认不出返回 None。"""
    return MUSCLE_CATEGORY.get(muscle or "")


def is_large_muscle(action: str) -> bool | None:
    """是否大肌群。认不出动作时返回 None。"""
    m = muscle_of(action)
    if m is None:
        return None
    return m in LARGE_MUSCLES


def step_kg(action: str) -> float:
    """该动作的递进步长。

    认不出的动作按小肌群(1 kg)处理 —— 宁可加少了下次再加,
    也不要给一个不认识的动作莽 2.5 kg。
    """
    large = is_large_muscle(action)
    if large is None:
        return SMALL_STEP_KG
    return LARGE_STEP_KG if large else SMALL_STEP_KG


# =====================================================================
# 2. 渐进超负荷:下次顶组
# =====================================================================

# RPE 分档。
#
# --- RPE 的定义(查证来源,不是自拟)---
# 力量训练的 RPE 锚定在 RIR(Reps In Reserve,还能再做几次),
# 源自 Tuchscherer 2008《Reactive Training Systems Manual》,
# 由 Zourdos 等 2016 在 NSCA Strength & Conditioning Journal 形式化:
#
#     RPE 10 = 0 RIR(力竭)   RPE 9 = 1 RIR   RPE 8 = 2 RIR
#     RPE 7  = 3 RIR         RPE 6 = 4-5 RIR  RPE 5 = 5+ RIR
#     RPE ≤4 只表达用力程度,不对应 RIR
#
# (部分文献把 8 记作 2-3 RIR、7 记作 3-4 RIR;本实现取 Zourdos 的整数锚点。)
#
# 所以 RPE 7 **不是「轻松」,是「还剩 3 次」** —— 那是实打实的工作组。
# 早先版本用「轻松/刚好/吃力」这种感觉词描述档位,丢掉了 RIR 锚点,
# 而 RIR 才是让 RPE 可核对的东西(能数的次数 vs 说不清的感觉)。
#
# --- 为什么用区间而不是判等 ---
# 半档(7.5/8.5/9.5)在力量训练圈是标准用法,表达「确定还能做 2 个,也许 3 个」
# 这种介于两档之间的判断。用户目前只记整数(截至 2026-08-03 的 98 条记录
# 全是 5/6/7/8/9),所以区间对他而言主要起两个作用:
#   · 兜住 `==` 判等的失控 else 分支 —— 旧实现 `== 8`/`== 9`/`else`,
#     热身组记的「≤ 5」(真实用过 3 次)会掉进 else 被判力竭 → 下组减重
#   · 手滑输入(8 打成 88)不会静默减重
# 他以后想用半档也直接支持,不用改代码。
#
# 边界靠保守一侧:7.5 归入「2 RIR」档(加 rep 而不是加重量)。
RIR_BY_RPE = {10: 0, 9: 1, 8: 2, 7: 3, 6: 4.5, 5: 5.5}

RPE_NO_RIR_MAX = 4.0    # ≤ 4    → 不对应 RIR,不能据此递进
RPE_EASY_MAX = 7.5      # < 7.5  → ≥3 RIR,还有余量
RPE_OK_MAX = 8.5        # [7.5, 8.5) → 2 RIR,到位
RPE_HARD_MAX = 9.5      # [8.5, 9.5) → 1 RIR,逼近极限;≥9.5 → 0 RIR,力竭

PCT_PER_RPE = 3.5   # 每差 1 个 RPE,调整当前工作重量的百分比
TARGET_RPE = 8.0    # 顶组目标:RPE 8 = 2 RIR(计划模板「留 2-3 RIR,40+ 岁原则」)


def rir_of(rpe: float | None) -> float | None:
    """RPE → 还能再做几次。RPE ≤4 不对应 RIR,返回 None。"""
    if rpe is None or rpe <= 4:
        return None
    if rpe >= 10:
        return 0.0
    lo = int(rpe)
    frac = rpe - lo
    hi_rir = RIR_BY_RPE.get(lo)
    if hi_rir is None:
        return None
    if frac == 0:
        return float(hi_rir)
    nxt = RIR_BY_RPE.get(lo + 1, 0)
    return round(hi_rir + (nxt - hi_rir) * frac, 1)

# 首练时没有基线可递进。给一个按肌群的保守起始区间,并要求人工确认 ——
# 旧实现不管什么动作一律 20 kg × 12,对提踵偏轻、对髋外展偏重,基本没法直接用。
FIRST_TIME_HINT = {
    "股四": "40-60 kg",
    "腘绳": "20-30 kg",
    "臀": "25-40 kg",
    "小腿": "25-35 kg",
    "胸": "30-40 kg",
    "背": "30-45 kg",
    "肩": "10-20 kg",
    "臂": "10-20 kg",
    "核心": "自重",
}


def next_top_set(action: str, last_top: dict | None, plate: float = 2.5) -> dict:
    """算出这个动作下次的顶组目标。

    last_top: {weight, reps, rpe} 或 None(首练)
    返回:    {weight, reps, rpe_target, note, needs_confirmation}

    规则:目标 RPE 8(2 RIR)。负荷按 RPE 偏差的**百分比**调整
    (每差 1 个 RPE 约 3.5% 工作重量,依据见 adjust_load 上方说明),
    不再用固定公斤步长。调整量小于半片配重时改走双重渐进(加/减 1 rep)。
    """
    if last_top is None:
        m = muscle_of(action)
        hint = FIRST_TIME_HINT.get(m, "需用户定")
        return {
            "weight": None,
            "reps": 12,
            "rpe_target": 7,
            "note": f"首练,无基线。建议起始区间 {hint}(肌群:{m or '未识别'}),需用户确认",
            "needs_confirmation": True,
        }

    rpe = last_top.get("rpe")
    weight = last_top["weight"]
    reps = last_top["reps"]

    # RPE 没记 → 不猜,维持原样并要求确认
    if rpe is None:
        return {
            "weight": weight, "reps": reps, "rpe_target": TARGET_RPE,
            "note": "上次未记 RPE,无法判断递进方向,维持基线",
            "needs_confirmation": True,
        }

    # RPE ≤4 落在 RIR 锚定范围之外(Zourdos 2016:≤4 只表达用力程度,不对应 RIR)。
    # 一个记成 RPE ≤4 的「顶组」要么是记错了,要么它根本不是顶组 ——
    # 两种情况下都没有可靠的递进依据,不能像 RPE 5-7 那样自信地加重量。
    if rpe <= RPE_NO_RIR_MAX:
        return {
            "weight": weight, "reps": reps, "rpe_target": TARGET_RPE,
            "note": f"上次 RPE {rpe:g} 低于 RIR 锚定范围(≤4 不对应「还能做几次」),"
                    f"无法据此递进。请确认这一组是否真是顶组,或 RPE 是否记错",
            "needs_confirmation": True,
        }

    # 目标 RPE 8(2 RIR)。按 RPE 偏差百分比调负荷,而不是固定公斤数。
    gap = rpe - TARGET_RPE
    adj = adjust_load(weight, gap, plate)
    rir_txt = f"还剩约 {rir_of(rpe):g} 次"

    # 加不上去(调整量小于半片配重)→ 双重渐进:重量不动,加 1 rep
    if adj["blocked_by_plate"]:
        return {
            "weight": weight, "reps": reps + (1 if gap < 0 else -1),
            "rpe_target": TARGET_RPE,
            "note": f"上次 RPE {rpe:g}({rir_txt})。{adj['note']}",
            "needs_confirmation": False,
        }

    # 已经到位(RPE 8 附近)→ 重量不动,加 1 rep 累积
    if gap == 0:
        return {
            "weight": weight, "reps": reps + 1, "rpe_target": TARGET_RPE,
            "note": f"上次 RPE {rpe:g}({rir_txt}),强度到位,加 1 rep",
            "needs_confirmation": False,
        }

    # 注:力竭(RPE 10)不再额外减 reps。
    # 百分比调整本身就是把负荷降到「同样次数下留 2 RIR」的水平,
    # 再砍 2 个 rep 等于同一件事扣两遍,下次会掉得过多。
    # (旧的固定步长实现是「减 2.5kg + 减 2 reps」,那是重复扣。)

    return {
        "weight": adj["weight"], "reps": reps, "rpe_target": TARGET_RPE,
        "note": f"上次 RPE {rpe:g}({rir_txt})。{adj['note']}",
        "needs_confirmation": False,
    }


def adjust_in_session(action: str, current_weight: float, actual_rpe: float | None,
                      target_rpe: float = TARGET_RPE, plate: float = 2.5) -> dict:
    """训练**中**的下一组微调(progressive-overload.md 第 4 节)。

    和 next_top_set 是两件事,别混:
      next_top_set     决定「下次训练日」这个动作从哪开始(跨天,基于顶组)
      adjust_in_session 决定「下一组」加不加重(组间,基于刚报的这组)
    两者都走 adjust_load() 的百分比逻辑,不再用固定公斤步长。
    """
    if actual_rpe is None:
        return {"delta_kg": 0.0, "weight": current_weight, "note": "未记 RPE,维持"}

    # ≤4 不对应 RIR,不能当作「太轻了」的信号去加重(见 RIR_BY_RPE 上方说明)
    if actual_rpe <= RPE_NO_RIR_MAX:
        return {"delta_kg": 0.0, "weight": current_weight,
                "note": f"RPE {actual_rpe:g} 低于 RIR 锚定范围,维持并跟用户确认"}

    adj = adjust_load(current_weight, actual_rpe - target_rpe, plate)
    return {"delta_kg": adj["delta_kg"], "weight": adj["weight"],
            "blocked_by_plate": adj["blocked_by_plate"], "note": adj["note"]}


def round_to_plate(weight: float, plate: float = 2.5) -> float:
    """按可用配重片粒度取整。

    8/02 踩过:俯卧腿弯举算出 26 kg,但器械没有 1 kg 片,只能停在 25 kg。
    算出来加不上去的重量等于没算。
    """
    if plate <= 0:
        return weight
    return round(weight / plate) * plate


# =====================================================================
# 2b. 按百分比调整负荷(取代固定公斤步长)
# =====================================================================
#
# --- 依据 ---
# 自调节训练(autoregulation)里,负荷调整是**按百分比**的,不是固定公斤数。
# 两条独立来源指向同一个量级:
#
#   1) RTS / RPE 换算表:同一 rep 数下,差 1 次 = 差 2.5 个 1RM 百分点。
#      按 RIR 定义(RPE 9 = 还剩 1 次),差 1 个 RPE 就是差 1 次,
#      所以 1 个 RPE ≈ 2.5 个 1RM 百分点。
#      工作强度在 65-85% 1RM 时,折算成「占当前重量」= 2.9% ~ 3.8%。
#   2) RippedBody 的经验法则:每偏离目标 1 次,调整约 4%。
#
#   两者吻合 → 取 3.5% 作为默认值。
#
# ⚠️ 网上流传的线性 RPE 表说「1 个 RPE = 5 个百分点」,那张表和 RIR 定义不自洽
#   (表上「5 次 @ RPE 9」= 85%,「6 次 @ RPE 10」= 87.5%,按 RIR 这俩该是同一重量)。
#   本实现取自洽的 2.5 个百分点。
#
# --- 为什么这条重要 ---
# 旧实现按大/小肌群给固定步长(2.5kg / 1kg)。对照用户 8/02 的实际重量:
#     倒蹬 90kg  → 2.5kg = 2.8%   ✅ 接近
#     腿屈伸 68kg → 2.5kg = 3.7%   ✅ 接近
#     RDL 40kg   → 2.5kg = 6.2%   ⚠️ 偏大 1.8 倍
#     腿弯举 25kg → 2.5kg = 10.0%  ❌ 偏大 2.9 倍
#     髋内收 27kg → 2.5kg = 9.3%   ❌ 偏大 2.6 倍
# 固定公斤数在大重量动作上碰巧对,在轻重量孤立动作上严重超调。
# 改成百分比后,大重量自然跳得多、轻重量自然跳得少,
# 「大肌群 vs 小肌群」这套分类也就不需要了(俯卧腿弯举到底算大算小的争论随之消失)。



def adjust_load(weight: float, rpe_gap: float, plate: float = 2.5,
                pct_per_rpe: float = PCT_PER_RPE) -> dict:
    """按 RPE 偏差调整负荷。

    rpe_gap = 实际 RPE − 目标 RPE
        正数 = 比预期吃力 → 减重
        负数 = 比预期轻松 → 加重

    返回 {weight, raw_weight, delta_kg, blocked_by_plate, note}

    blocked_by_plate=True 表示:算出来的调整量小于半片配重,
    物理上加不上去 —— 这时应该走「双重渐进」(重量不动,加 1 rep),
    而不是硬凑一个加不上的数字。
    """
    raw = weight * (1 - rpe_gap * pct_per_rpe / 100)
    snapped = round_to_plate(raw, plate)
    delta = snapped - weight

    blocked = (rpe_gap != 0) and (delta == 0)
    if blocked:
        note = (f"按 {abs(rpe_gap):g} 个 RPE 差应调 {abs(raw - weight):.1f}kg,"
                f"小于配重片粒度 {plate:g}kg,加不上去 → 改用加/减 1 rep(双重渐进)")
    elif rpe_gap == 0:
        note = "RPE 符合目标,维持"
    else:
        direction = "减" if delta < 0 else "加"
        note = (f"RPE 差 {rpe_gap:+g} → 按 {pct_per_rpe:g}%/RPE 应{direction} "
                f"{abs(raw - weight):.1f}kg,取整到 {snapped:g}kg({delta:+g}kg)")

    return {"weight": snapped, "raw_weight": round(raw, 2), "delta_kg": delta,
            "blocked_by_plate": blocked, "note": note}


# =====================================================================
# 3. 部位轮换
# =====================================================================

# muscle-rotation.md 第 2 节的教科书值是 48h;第 4 节按用户实际节奏(早/午/晚分时练)
# 调整为 24h。此前 §2 写 48h、§3 写 days_ago≥2、代码写 days>=1,三处不一致。
# 以用户特化为准,统一到这一个常量。
MIN_REST_DAYS = 1


def decide_today_topic(today: date, recent_sessions: list[dict],
                       min_rest_days: int = MIN_REST_DAYS) -> dict:
    """决定今日训练主题。

    recent_sessions: [{"date": date, "topic": str}, ...] 过去 7 天,顺序无所谓(内部会排)

    关键修正:**窗口内从没练过的主题优先级最高**。
    旧实现 `if last:` 会把「7 天内没有记录」的主题整个排除在候选之外 ——
    于是最久没练、最该练的那个部位反而永远选不上,和「选距今最久」正好相反。
    """
    last_seen: dict[str, date] = {}
    for s in recent_sessions:
        d, t = s["date"], s["topic"]
        if t not in last_seen or d > last_seen[t]:
            last_seen[t] = d

    scored = []
    for topic in BIG_TOPICS:
        last = last_seen.get(topic)
        if last is None:
            # 窗口内没练过 → 距今至少超出窗口,排最前
            scored.append((topic, None))
            continue
        days = (today - last).days
        if days >= min_rest_days:
            scored.append((topic, days))

    if scored:
        # 没练过的(None)排最前,其余按距今天数**降序**(最久没练的优先)
        scored.sort(key=lambda x: (x[1] is not None, -(x[1] or 0)))
        top, days = scored[0]
        reason = ("窗口内未练过,优先安排" if days is None
                  else f"距今 {days} 天(候选中最久)")
        return {
            "topic": top,
            "candidates": [t for t, _ in scored[:3]],
            "reason": reason,
        }

    return {
        "topic": "主动恢复",
        "candidates": ["核心", "有氧"],
        "reason": f"所有大肌群距今 < {min_rest_days} 天,推荐主动恢复",
    }


# =====================================================================
# 4. 动作替换
# =====================================================================

# exercise-substitutions.md 的机器可读版。coef = 替代动作重量 / 原动作重量。
# unilateral=True 表示系数是「单边重量」,总负荷要按两边理解。
SUBSTITUTIONS: dict[str, list[dict]] = {
    "倒蹬 45°": [
        {"sub": "哈克深蹲", "coef": 0.7, "note": "轨迹更稳,踝背屈压力低"},
        {"sub": "单腿倒蹬", "coef": 0.5, "unilateral": True, "note": "单侧孤立,踝压力更低"},
        {"sub": "高脚位倒蹬", "coef": 1.0, "note": "强调臀大肌(踝压力大,慎用)"},
    ],
    "杠铃深蹲": [
        {"sub": "哈克深蹲", "coef": 0.8, "note": "稳定性高,颈/肩无压"},
        {"sub": "腿举(挂片)", "coef": 1.2, "note": "力量大,但踝背屈压力大"},
        {"sub": "高脚杯深蹲", "coef": 0.6, "note": "核心参与多"},
    ],
    "罗马尼亚硬拉": [
        {"sub": "哑铃 RDL", "coef": 0.5, "unilateral": True, "note": "行程略短"},
        {"sub": "罗马椅俯身挺身", "coef": 0.0, "bodyweight": True, "note": "竖脊肌 + 臀大肌,无外载"},
        {"sub": "臀冲", "coef": 0.7, "note": "臀大肌孤立,腘绳参与少"},
    ],
    "腿屈伸": [
        {"sub": "单腿腿屈伸", "coef": 0.5, "unilateral": True, "note": "设备被占时"},
        {"sub": "徒手坐姿伸膝", "coef": 0.0, "bodyweight": True, "note": "高 rep,纯股四孤立"},
    ],
    "俯卧腿弯举": [
        {"sub": "坐姿腿弯举", "coef": 0.9, "note": "行程略短"},
        {"sub": "单腿俯卧腿弯举", "coef": 0.5, "unilateral": True, "note": "单侧孤立"},
        {"sub": "瑞士球腿弯举", "coef": 0.0, "bodyweight": True, "note": "腘绳 + 核心"},
    ],
    "站姿提踵": [
        {"sub": "坐姿提踵", "coef": 0.5, "note": "踝压力大时(踝疼)"},
        {"sub": "器械提踵", "coef": 1.2, "note": "标准替代"},
        {"sub": "哑铃提踵", "coef": 0.6, "unilateral": True, "note": "自由重量,行程大"},
    ],
    "杠铃卧推": [
        {"sub": "哑铃卧推", "coef": 0.8, "unilateral": True, "note": "行程大"},
        {"sub": "器械推胸(坐姿)", "coef": 0.7, "note": "稳定,适合等机器"},
        {"sub": "史密斯机卧推", "coef": 0.9, "note": "无需平衡"},
        {"sub": "俯卧撑", "coef": 0.0, "bodyweight": True, "note": "体重足够时"},
    ],
    "坐姿推胸": [
        {"sub": "哑铃推举(平躺)", "coef": 0.8, "unilateral": True, "note": "自由重量版"},
        {"sub": "蝴蝶机", "coef": 0.6, "note": "强调胸中缝"},
        {"sub": "俯卧撑", "coef": 0.0, "bodyweight": True, "note": "临时替代"},
    ],
    "高位下拉": [
        {"sub": "引体向上", "coef": 0.0, "bodyweight": True, "note": "体重足够时"},
        {"sub": "单臂高位下拉", "coef": 0.5, "unilateral": True, "note": "单侧孤立"},
        {"sub": "反握高位下拉", "coef": 1.0, "note": "强调背阔下沿 + 二头"},
    ],
    # 单滑轮坐姿划船是**双臂**动作(MEMORY 2026-07-17 纠正),别写成单臂
    "坐姿划船": [
        {"sub": "单臂哑铃划船", "coef": 0.5, "unilateral": True, "note": "单侧孤立"},
        {"sub": "杠铃划船", "coef": 0.7, "note": "双侧,行程大"},
        {"sub": "T 杆划船", "coef": 0.8, "note": "杠杆版,稳定性高"},
    ],
    "杠铃肩推": [
        {"sub": "哑铃肩推(坐姿)", "coef": 0.7, "unilateral": True, "note": "行程大"},
        {"sub": "器械肩推", "coef": 0.8, "note": "稳定"},
        {"sub": "阿诺德推举", "coef": 0.6, "unilateral": True, "note": "前束 + 中束"},
    ],
    "哑铃侧平举": [
        {"sub": "绳索侧平举", "coef": 0.5, "unilateral": True, "note": "持续张力"},
        {"sub": "侧卧侧平举", "coef": 0.4, "unilateral": True, "note": "行程短"},
    ],
    "杠铃弯举": [
        {"sub": "哑铃弯举", "coef": 0.5, "unilateral": True, "note": "单侧或双侧"},
        {"sub": "锤式弯举", "coef": 0.6, "unilateral": True, "note": "肱肌 + 桡侧腕屈"},
        {"sub": "牧师椅弯举", "coef": 0.7, "note": "行程稳定"},
    ],
    "绳索下压": [
        {"sub": "仰卧臂屈伸", "coef": 0.6, "note": "行程大"},
        {"sub": "单臂绳索下压", "coef": 0.5, "unilateral": True, "note": "单侧"},
        {"sub": "俯卧撑", "coef": 0.0, "bodyweight": True, "note": "体重足够时"},
    ],
}


def find_substitutes(action: str) -> list[dict]:
    """找该动作的替代方案。按动作名包含匹配。"""
    for key, subs in SUBSTITUTIONS.items():
        if key in action or action in key:
            return subs
    return []


def substitute_weight(original_weight: float, action: str, sub_name: str,
                      plate: float = 2.5) -> dict:
    """算替代动作该用多重。

    返回 {weight, unilateral, bodyweight, note};找不到替代关系时 weight 为 None。
    """
    for s in find_substitutes(action):
        if s["sub"] == sub_name or sub_name in s["sub"]:
            if s.get("bodyweight"):
                return {"weight": None, "unilateral": False, "bodyweight": True,
                        "note": f"{s['sub']}:自重动作 —— {s['note']}"}
            w = round_to_plate(original_weight * s["coef"], plate)
            unit = "kg/单边" if s.get("unilateral") else "kg"
            return {"weight": w, "unilateral": s.get("unilateral", False),
                    "bodyweight": False,
                    "note": f"{s['sub']}:{original_weight:g} × {s['coef']} ≈ {w:g} {unit} —— {s['note']}"}

    return {"weight": None, "unilateral": False, "bodyweight": False,
            "note": f"未收录 {action} → {sub_name} 的换算关系,需人工判断"}


# =====================================================================
# 5. 应答表:把训练中要算的东西,在排计划时一次算完
# =====================================================================
#
# 训练中每报一次数,如果现调 next_top_set / adjust_in_session /
# find_substitutes / round_to_plate,就是 4 次工具往返。一次腿日 20+ 组
# = 80+ 次往返,用户在器械上等的全是这个。
#
# 但这四个函数**没有一个真正依赖训练中才知道的信息**:
#   next_top_set / find_substitutes / round_to_plate — 完全不依赖
#   adjust_in_session — 只依赖实际 RPE,而 RPE 的取值只有 6/7/8/9/10 五档
#
# 输入空间小到可以穷举 → 排计划时把五种答案全算出来写进 plan,
# 训练中变成纯查表,零调用。只有"计划外换动作"这种真的没法预知的情况才回落到实时计算。

RPE_BUCKETS = [6, 7, 8, 9, 10]


def build_response_table(action: str, top_weight: float, target_rpe: float = 8.0,
                         plate: float = 2.5) -> dict[str, float]:
    """给定今日顶组目标,穷举「报出各档 RPE 时下一组该用多重」。

    返回 {'6': 97.5, '7': 95, '8': 92.5, '9': 90, '10': 90}
    用户只记整数,五档即全覆盖。万一出现非整数,按 adjust_in_session 的区间落到最近档。
    """
    table = {}
    for rpe in RPE_BUCKETS:
        r = adjust_in_session(action, top_weight, rpe, target_rpe, plate)
        table[str(rpe)] = max(r["weight"], plate)
    return table


def build_exercise_card(action: str, last_top: dict | None,
                        plate: float = 2.5, max_subs: int = 3) -> dict:
    """一个动作的完整训练中所需信息,排计划时算好。"""
    target = next_top_set(action, last_top)
    muscle = muscle_of(action)

    subs = []
    for s in find_substitutes(action)[:max_subs]:
        conv = substitute_weight(target["weight"] or 0, action, s["sub"], plate)
        subs.append({
            "name": s["sub"],
            "weight": conv["weight"],
            "unilateral": conv["unilateral"],
            "bodyweight": conv["bodyweight"],
        })

    card = {
        "action": action,
        "muscle": muscle,
        "step_kg": step_kg(action),
        "last_top": last_top,
        "target": target,
        "substitutes": subs,
        "response_table": None,
    }
    if target["weight"] is not None:
        card["response_table"] = build_response_table(
            action, target["weight"], target["rpe_target"], plate)
    return card


def render_cheatsheet(cards: list[dict]) -> str:
    """把应答表渲染成 markdown,直接贴进 plan 文件。

    训练中 agent 只读这一段就能填满 5 项反馈契约的第 3 项,不调任何函数。
    用户自己在手机上看也是同一张表。
    """
    out = ["## ⚡ 训练中应答表(排计划时算好,训练中只查不算)", ""]

    for c in cards:
        t = c["target"]
        head = f"### {c['action']}"
        if c["muscle"]:
            size = "大" if c["muscle"] in LARGE_MUSCLES else "小"
            head += f"（{c['muscle']} · {size}肌群 · 步长 {c['step_kg']:g}kg）"
        out.append(head)

        if t["weight"] is None:
            out.append(f"⚠️ **{t['note']}** —— 起始重量定下来后再补本表")
            out.append("")
            continue

        lt = c["last_top"]
        lt_txt = (f"{lt['weight']:g}×{lt['reps']} RPE {lt['rpe']:g}"
                  if lt and lt.get("rpe") is not None else "无基线")
        out.append(f"**今日顶组目标:{t['weight']:g}×{t['reps']} RPE {t['rpe_target']:g}**"
                   f"　（上次 {lt_txt} → {t['note']}）")
        out.append("")

        tbl = c["response_table"]
        out.append("| 报出 RPE | ≤6 | 7 | 8（符合） | 9 | ≥10 |")
        out.append("|---|---|---|---|---|---|")
        out.append("| **下组重量** | " + " | ".join(
            f"{tbl[str(r)]:g}" for r in RPE_BUCKETS) + " |")
        out.append("")

        if c["substitutes"]:
            parts = []
            for s in c["substitutes"]:
                if s["bodyweight"]:
                    parts.append(f"{s['name']}（自重）")
                elif s["weight"] is None:
                    parts.append(s["name"])
                else:
                    unit = "kg/单边" if s["unilateral"] else "kg"
                    parts.append(f"{s['name']} {s['weight']:g}{unit}")
            out.append(f"**设备被占备选**:{' / '.join(parts)}")
            out.append("")

    out.append("> 五档已覆盖全部实际取值。热身组记「≤5」查 ≤6 那一列。")
    out.append("> 表里没有的情况(计划外换动作、临时加项)才需要实时算:")
    out.append("> `python3 scripts/fitness_lib.py next --action <动作> --weight W --reps R --rpe N`")
    return "\n".join(out)


# =====================================================================
# 自检
# =====================================================================

def self_test() -> int:
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} {detail}")
            failures.append(name)

    print("肌群识别:")
    check("倒蹬 45°(低脚位) → 股四/大肌群", is_large_muscle("倒蹬 45°(低脚位)") is True)
    check("俯卧腿弯举 → 腘绳/大肌群(旧文档误按小肌群 +1kg)",
          is_large_muscle("俯卧腿弯举") is True and step_kg("俯卧腿弯举") == 2.5)
    check("坐姿提踵 → 小腿/小肌群", is_large_muscle("坐姿提踵") is False and step_kg("坐姿提踵") == 1.0)
    check("坐姿髋内收 → 臀/大肌群", is_large_muscle("坐姿髋内收") is True)
    check("罗马椅俯身挺身 → 背(竖脊肌,不是背阔)", muscle_of("罗马椅俯身挺身") == "背")
    check("认不出的动作返回 None", is_large_muscle("玄学动作") is None)
    check("认不出时按小肌群保守取步长", step_kg("玄学动作") == 1.0)

    print("RPE → RIR(Zourdos 2016 锚点):")
    check("RPE 10 = 0 RIR(力竭)", rir_of(10) == 0)
    check("RPE 9 = 1 RIR", rir_of(9) == 1)
    check("RPE 8 = 2 RIR", rir_of(8) == 2)
    check("RPE 7 = 3 RIR", rir_of(7) == 3)
    check("RPE 8.5 插值 = 1.5 RIR", rir_of(8.5) == 1.5, rir_of(8.5))
    check("RPE ≤4 不对应 RIR", rir_of(4) is None and rir_of(3) is None)
    check("RPE 未记录 → None", rir_of(None) is None)

    print("RPE ≤4:超出 RIR 锚定范围,不能据此递进:")
    r = next_top_set("倒蹬 45°", {"weight": 80, "reps": 10, "rpe": 4})
    check("RPE 4 不加重,要求确认",
          r["weight"] == 80 and r["needs_confirmation"], r["note"])
    r = next_top_set("倒蹬 45°", {"weight": 80, "reps": 10, "rpe": 2})
    check("RPE 2 同样不递进(不会崩)", r["needs_confirmation"], r["note"])
    check("RPE 5 仍正常递进(边界不误伤)",
          next_top_set("倒蹬 45°", {"weight": 80, "reps": 10, "rpe": 5})["weight"] > 80)
    check("训练中 RPE 4 不当作『太轻』加重",
          adjust_in_session("倒蹬 45°", 90, 4, 8)["delta_kg"] == 0.0)
    check("训练中 RPE 5 仍加重", adjust_in_session("倒蹬 45°", 90, 5, 8)["delta_kg"] > 0)

    print("渐进超负荷 —— RPE 边界值(旧实现 else 分支的致命伤):")
    base = {"weight": 40.0, "reps": 8, "rpe": 8.5}
    r = next_top_set("罗马尼亚硬拉", base)
    check("RPE 8.5(略超目标)→ 0.7kg 加不上,改减 1 rep,不是当力竭砍重量",
          r["weight"] == 40.0 and r["reps"] == 7, r["note"])
    r = next_top_set("罗马尼亚硬拉", {"weight": 40.0, "reps": 8, "rpe": 7.5})
    check("RPE 7.5 判为『刚好→加 1 rep』", r["weight"] == 40.0 and r["reps"] == 9, r["note"])
    r = next_top_set("罗马尼亚硬拉", {"weight": 40.0, "reps": 8, "rpe": 10})
    check("RPE 10 减重但不重复扣 reps", r["weight"] == 37.5 and r["reps"] == 8, r["note"])

    print("渐进超负荷 —— 按百分比调负荷(取代固定公斤步长):")
    r = next_top_set("倒蹬 45°", {"weight": 90.0, "reps": 10, "rpe": 9})
    check("倒蹬 90kg RPE9 → 减 3.5% ≈ 3.2kg,取整 87.5", r["weight"] == 87.5, r["note"])
    r = next_top_set("罗马尼亚硬拉", {"weight": 40.0, "reps": 8, "rpe": 9})
    check("RDL 40kg RPE9 → 减 1.4kg,取整 37.5", r["weight"] == 37.5, r["note"])
    r = next_top_set("腿屈伸", {"weight": 64.0, "reps": 12, "rpe": 8})
    check("RPE 8(到位)→ 加 1 rep 不加重", r["weight"] == 64.0 and r["reps"] == 13, r["note"])
    r = next_top_set("倒蹬 45°", {"weight": 90.0, "reps": 10, "rpe": 6})
    check("RPE 6(差 2 档)→ 加约 7%", r["weight"] == 97.5, r["note"])

    print("同一偏差,重的动作跳得多、轻的动作跳得少(固定步长做不到):")
    heavy = adjust_load(90, +1)["delta_kg"]
    light = adjust_load(25, +1)["delta_kg"]
    check("倒蹬 90kg 减 2.5kg / 腿弯举 25kg 减 0kg(卡片)",
          heavy == -2.5 and light == 0.0, (heavy, light))
    check("腿弯举按比例只该减 0.9kg,不是 2.5kg",
          abs(adjust_load(25, +1)["raw_weight"] - 24.125) < 0.01)

    print("配重片加不上去 → 双重渐进(加/减 rep):")
    r = adjust_load(25, +1, plate=2.5)
    check("25kg 减 1 个 RPE 被配重片挡住", r["blocked_by_plate"], r["note"])
    r = next_top_set("俯卧腿弯举", {"weight": 25.0, "reps": 10, "rpe": 9})
    check("挡住时改为减 1 rep,重量不动", r["weight"] == 25.0 and r["reps"] == 9, r["note"])
    r = adjust_load(25, +1, plate=1.0)
    check("有 1kg 片时就能真减(24kg)", r["weight"] == 24.0 and not r["blocked_by_plate"], r["note"])
    r = next_top_set("坐姿髋外展", None)
    check("首练不再一律 20kg,要求确认", r["needs_confirmation"] and r["weight"] is None, r["note"])
    r = next_top_set("倒蹬", {"weight": 80.0, "reps": 10, "rpe": None})
    check("RPE 缺失 → 维持并要求确认", r["needs_confirmation"] and r["weight"] == 80.0)

    print("训练中微调(同样走百分比):")
    check("倒蹬 90kg 报 RPE 9 → 下组 87.5",
          adjust_in_session("倒蹬", 90, 9, 8)["weight"] == 87.5)
    check("倒蹬 90kg 报 RPE 7 → 下组 92.5",
          adjust_in_session("倒蹬", 90, 7, 8)["weight"] == 92.5)
    check("倒蹬 90kg 报 RPE 8 → 维持",
          adjust_in_session("倒蹬", 90, 8, 8)["delta_kg"] == 0.0)
    check("提踵 30kg 报 RPE 7 → 只加 1kg(不是 2.5)",
          adjust_in_session("坐姿提踵", 30, 7, 8, plate=1)["weight"] == 31.0)

    print("配重片粒度:")
    check("26kg 按 2.5 片取整 → 25kg(8/02 腿弯举实况)", round_to_plate(26, 2.5) == 25.0)
    check("有 1kg 片时保留 26kg", round_to_plate(26, 1) == 26.0)

    print("部位轮换 —— 未练过的主题优先(旧实现的逻辑倒置):")
    today = date(2026, 8, 3)
    recent = [
        {"date": today - timedelta(days=1), "topic": "腿"},
        {"date": today - timedelta(days=3), "topic": "拉"},
    ]
    r = decide_today_topic(today, recent)
    check("推 7 天内没练过 → 选推", r["topic"] == "推", r)
    r2 = decide_today_topic(today, recent + [{"date": today - timedelta(days=2), "topic": "推"}])
    check("三个都练过 → 选距今最久的拉", r2["topic"] == "拉", r2)
    r3 = decide_today_topic(today, [{"date": today, "topic": t} for t in BIG_TOPICS])
    check("全都是今天练的 → 主动恢复", r3["topic"] == "主动恢复", r3)

    print("应答表(训练中零调用的前提):")
    tbl = build_response_table("倒蹬 45°", 90.0, target_rpe=8)
    check("五档 RPE 全覆盖", set(tbl) == {"6", "7", "8", "9", "10"}, tbl)
    check("报 8(符合目标)→ 维持 90", tbl["8"] == 90.0, tbl)
    check("报 9 → 降一档 87.5", tbl["9"] == 87.5, tbl)
    check("报 6(差 2 档)→ 加约 7% = 97.5", tbl["6"] == 97.5, tbl)
    check("每档都落在配重片粒度上",
          all(v % 2.5 == 0 for v in tbl.values()), tbl)
    tbl_s = build_response_table("坐姿提踵", 30.0, target_rpe=8, plate=1)
    check("小肌群按 1kg 档且用 1kg 片取整", tbl_s["7"] == 31.0, tbl_s)

    card = build_exercise_card("倒蹬 45°", {"weight": 90, "reps": 10, "rpe": 9})
    check("卡片含备选,按今日目标重量换算", card["substitutes"][0]["weight"] == 60.0,
          card["substitutes"][:1])
    card0 = build_exercise_card("坐姿提踵", None)
    check("首练无应答表,不编重量", card0["response_table"] is None
          and card0["target"]["needs_confirmation"])
    check("渲染不炸", "训练中应答表" in render_cheatsheet([card, card0]))

    print("动作替换:")
    r = substitute_weight(80, "倒蹬 45°", "哈克深蹲")
    check("倒蹬 80kg → 哈克深蹲 56kg(×0.7,按 2.5 取整 55)", r["weight"] == 55.0, r["note"])
    r = substitute_weight(35, "罗马尼亚硬拉", "罗马椅俯身挺身")
    check("自重动作不给数字", r["bodyweight"] and r["weight"] is None)
    r = substitute_weight(80, "倒蹬 45°", "不存在的动作")
    check("未收录的换算关系明确报未知", r["weight"] is None and "未收录" in r["note"])

    print()
    if failures:
        print(f"❌ {len(failures)} 项未通过: {failures}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description="训练算法库")
    sub = parser.add_subparsers(dest="cmd")

    p_sheet = sub.add_parser(
        "cheatsheet",
        help="【排计划时用】一次算完所有动作的应答表,输出 markdown 贴进 plan")
    p_sheet.add_argument(
        "--exercises", required=True,
        help='JSON 数组或 @文件:[{"action":"倒蹬 45°","last_top":{"weight":90,"reps":10,"rpe":9}}]')
    p_sheet.add_argument("--plate", type=float, default=2.5, help="可用配重片粒度 kg")
    p_sheet.add_argument("--json", action="store_true", help="输出 JSON 而非 markdown")

    p_next = sub.add_parser(
        "next",
        help="【计划外情况才用】单个动作实时算,一次调用返回全部字段")
    p_next.add_argument("--action", required=True)
    p_next.add_argument("--weight", type=float, help="上次顶组重量")
    p_next.add_argument("--reps", type=int, help="上次顶组次数")
    p_next.add_argument("--rpe", type=float, help="上次顶组 RPE")
    p_next.add_argument("--plate", type=float, default=2.5)

    parser.add_argument("--self-test", action="store_true", help="跑自检")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if args.cmd == "cheatsheet":
        spec = args.exercises
        if spec.startswith("@"):
            spec = open(spec[1:], encoding="utf-8").read()
        cards = [build_exercise_card(e["action"], e.get("last_top"), args.plate)
                 for e in json.loads(spec)]
        print(json.dumps(cards, ensure_ascii=False, indent=2) if args.json
              else render_cheatsheet(cards))
        sys.exit(0)

    if args.cmd == "next":
        last = None
        if args.weight is not None:
            last = {"weight": args.weight, "reps": args.reps or 10, "rpe": args.rpe}
        print(json.dumps(build_exercise_card(args.action, last, args.plate),
                         ensure_ascii=False, indent=2))
        sys.exit(0)

    parser.print_help()
