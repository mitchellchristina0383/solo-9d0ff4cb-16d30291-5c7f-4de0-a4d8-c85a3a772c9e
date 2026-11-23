"""正式演练与顺序试跑共用的受理/判定规则。

这里只放纯函数：输入轮次状态、回执计数、时钟等快照数据，输出"是否受理、
给出什么结论、原因是什么"。正式接口负责把结论落库；试跑只把结论应用到
内存副本，所以两边不会出现规则漂移。
"""
from __future__ import annotations

import math
from datetime import datetime

from .config import SAMPLE_SIZE, STEP_PCT


def split_counts(rollout_pct: int) -> tuple[int, int]:
    """按当前比例拆分 10 笔样例：返回 (新版笔数, 旧版笔数)。"""
    new_n = math.floor(rollout_pct / 100 * SAMPLE_SIZE + 1e-9)
    return new_n, SAMPLE_SIZE - new_n


def receipt_gate(round_status: str, already_has_receipt: bool) -> dict:
    """回填回执的受理规则：首个为准；轮次关闭后晚到仅留档。"""
    if already_has_receipt:
        return {
            "accepted": False,
            "late": False,
            "error": "该笔已有回执，以首个回执为准，重复回填被忽略",
            "reason": "该笔已有首个回执，重复回填不生效",
        }
    if round_status != "open":
        return {
            "accepted": True,
            "late": True,
            "error": None,
            "reason": "轮次已结束，回执晚到：仅留档，不参与本轮判定",
        }
    return {
        "accepted": True,
        "late": False,
        "error": None,
        "reason": "轮次进行中，回执受理并计入本轮判定",
    }


def confirm_gate(
    *,
    round_status: str,
    now: datetime,
    deadline_at: datetime,
    rollout_pct: int,
    new_recv: int,
    new_fail: int,
    old_recv: int,
    old_fail: int,
    window_seconds: int,
) -> dict:
    """确认放量的受理与判定规则。

    返回 dict：
      accepted      是否受理（推进档位）
      round_status  动作执行后的轮次状态；未关闭时保持 open
      close_round   是否需要把轮次状态/关闭时间/判定记录落库
      decision_note 需要落库的判定记录（close_round 为真时）
      next_pct      受理后的新档位
      finished      是否到达 100%
      error         不受理时给调用方的错误文案
      reason        给试跑页展示的人可读原因
    """
    base = {
        "accepted": False,
        "round_status": round_status,
        "close_round": False,
        "decision_note": None,
        "next_pct": None,
        "finished": None,
        "error": None,
        "reason": "",
        "new_rate": None,
        "old_rate": None,
    }

    if round_status != "open":
        base.update(
            error=f"轮次已结束（{round_status}），确认只生效一次，旧确认不能再次推进放量",
            reason="轮次已结束，确认只生效一次；旧确认不能再次推进放量",
        )
        return base

    if now >= deadline_at:
        note = (
            f"超过 {window_seconds} 秒未收齐回执，留在 {rollout_pct}% 档，"
            "可重开采样"
        )
        base.update(
            round_status="expired",
            close_round=True,
            decision_note=note,
            error="已超时，本轮留在原档，可重开采样",
            reason=note,
        )
        return base

    total_recv = new_recv + old_recv
    if total_recv < SAMPLE_SIZE:
        base.update(
            error=(
                f"回执未收齐（{total_recv}/{SAMPLE_SIZE}），"
                "值班员需等本档结果齐全再放量"
            ),
            reason=(
                f"有效回执 {total_recv}/{SAMPLE_SIZE}，未收齐；"
                "轮次保持进行中，档位不变"
            ),
        )
        return base

    new_rate = new_fail / new_recv if new_recv else 0.0
    old_rate = old_fail / old_recv if old_recv else 0.0
    note = (
        f"{window_seconds}s 内收齐 {SAMPLE_SIZE} 笔；新版失败 {new_fail}/{new_recv}"
        f"（{new_rate:.0%}），旧版失败 {old_fail}/{old_recv}（{old_rate:.0%}）"
    )
    base.update(new_rate=new_rate, old_rate=old_rate)

    if new_rate > old_rate:
        base.update(
            round_status="rejected",
            close_round=True,
            decision_note=note + "；新版失败率高于旧版，留在原档，可重开采样",
            error=f"新版失败率 {new_rate:.0%} 高于旧版 {old_rate:.0%}，留在原档",
            reason=(
                f"收齐 {SAMPLE_SIZE} 笔，但新版失败率 {new_rate:.0%} "
                f"高于旧版 {old_rate:.0%}；本轮超标关闭，档位不变"
            ),
        )
        return base

    next_pct = min(100, rollout_pct + STEP_PCT)
    finished = next_pct >= 100
    base.update(
        accepted=True,
        round_status="confirmed",
        close_round=True,
        decision_note=note + f"；满足条件，放量 {rollout_pct}% -> {next_pct}%",
        next_pct=next_pct,
        finished=finished,
        reason=(
            f"收齐 {SAMPLE_SIZE} 笔，且新版失败率 {new_rate:.0%} ≤ "
            f"旧版 {old_rate:.0%}；确认放量 {rollout_pct}% -> {next_pct}%"
        ),
    )
    return base


def sweep_note(window_seconds: int) -> str:
    """超时扫描落库的判定记录，正式流程与试跑共用同一文案。"""
    return f"超过 {window_seconds} 秒未收齐回执，留在原档，可重开采样"


def expire_gate(round_status: str, rollout_pct: int) -> dict:
    """演练辅助"立即置超时"的受理规则。"""
    if round_status != "open":
        return {
            "accepted": False,
            "round_status": round_status,
            "close_round": False,
            "decision_note": None,
            "error": f"轮次已结束（{round_status}）",
            "reason": "轮次已结束，无法再次置超时",
        }
    note = f"手动置超时（演练辅助）：留在 {rollout_pct}% 档，可重开采样"
    return {
        "accepted": True,
        "round_status": "expired",
        "close_round": True,
        "decision_note": note,
        "error": None,
        "reason": note,
    }
