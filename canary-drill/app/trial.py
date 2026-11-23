"""顺序试跑：对同一份已提交快照，按正反两种顺序各跑一次两个动作。

设计要点：
- 只读：取快照用一个只读事务完成（BEGIN 后只 SELECT，最后 rollback），
  整个试跑只改内存副本，绝不写正式库。
- 无共享状态：每次调用都在函数内新建两份独立状态（一种顺序一份），
  不落库、不缓存，因此多个人同时试跑互不影响，也不会干扰正式演练。
- 同规则：每一步都调用 app.rules 里与正式接口相同的受理/判定逻辑
  （回执 gate、确认 gate、置超时 gate），避免规则漂移。
- 固定时钟：取快照时记录 taken_at，两种顺序的每一步都用这个时间，
  因此结果差异只来自动作先后，不来自时间流逝。
"""
from __future__ import annotations

import copy
from datetime import datetime

from . import rules
from .config import SAMPLE_SIZE, WINDOW_SECONDS


class TrialError(ValueError):
    """试跑请求不合法；status 供接口层映射成 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def snapshot_from_db(conn, now: datetime, round_id: int | None = None) -> dict:
    """在只读事务里取轮次 + 档位 + 回执的一致快照。"""
    meta = conn.execute("SELECT rollout_pct, finished FROM meta WHERE id = 1").fetchone()
    if round_id is None:
        row = conn.execute(
            "SELECT * FROM rounds WHERE status = 'open' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise TrialError("当前没有进行中的轮次，无法取快照；请先开一轮采样", 409)
    else:
        row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            raise TrialError("轮次不存在", 404)

    req_rows = conn.execute(
        "SELECT id, idx, label, version FROM requests WHERE round_id = ? ORDER BY idx",
        (row["id"],),
    ).fetchall()
    receipt_rows = conn.execute(
        "SELECT r.request_id, r.success, r.late FROM receipts r "
        "JOIN requests q ON q.id = r.request_id WHERE q.round_id = ?",
        (row["id"],),
    ).fetchall()

    return {
        "taken_at": now.isoformat(timespec="seconds"),
        "round": {
            "id": row["id"],
            "seq": row["seq"],
            "rollout_pct": row["rollout_pct"],
            "new_n": row["new_n"],
            "old_n": row["old_n"],
            "status": row["status"],
            "opened_at": row["opened_at"],
            "deadline_at": row["deadline_at"],
            "closed_at": row["closed_at"],
            "decision_note": row["decision_note"],
        },
        "meta": {
            "rollout_pct": meta["rollout_pct"],
            "finished": bool(meta["finished"]),
        },
        "requests": [
            {
                "request_id": r["id"],
                "idx": r["idx"],
                "label": r["label"],
                "version": r["version"],
            }
            for r in req_rows
        ],
        "receipts": {
            r["request_id"]: {
                "success": bool(r["success"]),
                "late": bool(r["late"]),
            }
            for r in receipt_rows
        },
    }


def _sweep_snapshot(snapshot: dict) -> dict:
    """复现正式流程"任何请求先扫超时"的效果，但只改快照副本。

    正式接口每次调用都会先 sweep；取快照若恰好落在截止之后而库中轮次
    还是 open，试跑也应该从"已超时"的状态起跑，两种顺序才同样公平。
    """
    rnd = snapshot["round"]
    if rnd["status"] == "open" and snapshot["taken_at"] >= rnd["deadline_at"]:
        rnd["status"] = "expired"
        rnd["closed_at"] = snapshot["taken_at"]
        rnd["decision_note"] = rules.sweep_note(WINDOW_SECONDS)
    return snapshot


# ---------------------------------------------------------------------------
# 内存状态与动作
# ---------------------------------------------------------------------------

def _new_state(snapshot: dict) -> dict:
    """由快照克隆出一份完全独立的内存状态。"""
    return {
        "clock": datetime.fromisoformat(snapshot["taken_at"]),
        "round": copy.deepcopy(snapshot["round"]),
        "meta": copy.deepcopy(snapshot["meta"]),
        "requests": {q["request_id"]: dict(q) for q in snapshot["requests"]},
        "receipts": copy.deepcopy(snapshot["receipts"]),
        "steps": [],
    }


def _counts(state: dict) -> dict:
    stats = {
        v: {"total": 0, "received": 0, "fail": 0, "late": 0}
        for v in ("new", "old")
    }
    for req in state["requests"].values():
        stats[req["version"]]["total"] += 1
    for request_id, receipt in state["receipts"].items():
        version = state["requests"][request_id]["version"]
        if receipt["late"]:
            stats[version]["late"] += 1
        else:
            stats[version]["received"] += 1
            if not receipt["success"]:
                stats[version]["fail"] += 1
    for version, s in stats.items():
        s["pending"] = s["total"] - s["received"]
        s["fail_rate"] = (s["fail"] / s["received"]) if s["received"] else None
    return stats


def _summary(state: dict) -> dict:
    stats = _counts(state)
    effective = stats["new"]["received"] + stats["old"]["received"]
    late = stats["new"]["late"] + stats["old"]["late"]
    return {
        "effective_receipts": effective,
        "total_receipts_expected": SAMPLE_SIZE,
        "late_receipts": late,
        "round_status": state["round"]["status"],
        "rollout_pct": state["meta"]["rollout_pct"],
        "finished": state["meta"]["finished"],
        "decision_note": state["round"]["decision_note"],
        "progress": stats,
    }


def _describe_action(action: dict, snapshot: dict) -> str:
    kind = action["type"]
    if kind == "receipt":
        req = next(
            (q for q in snapshot["requests"] if q["request_id"] == action["request_id"]),
            None,
        )
        label = req["label"] if req else f"#{action['request_id']}"
        outcome = "成功" if action["success"] else "失败"
        return f"回填回执 {label}（{outcome}）"
    if kind == "confirm":
        return "确认放量"
    return "置超时"


def _validate_actions(actions, snapshot: dict) -> list[dict]:
    if not isinstance(actions, list) or len(actions) != 2:
        raise TrialError("需要恰好两个动作")
    normalized = []
    request_ids = {q["request_id"] for q in snapshot["requests"]}
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            raise TrialError(f"动作 {i + 1} 格式不正确")
        kind = action.get("type")
        if kind == "receipt":
            request_id = action.get("request_id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                raise TrialError(f"动作 {i + 1} 需要整数 request_id")
            if request_id not in request_ids:
                raise TrialError(f"动作 {i + 1} 的样例不属于该轮次快照")
            if not isinstance(action.get("success"), bool):
                raise TrialError(f"动作 {i + 1} 需要布尔 success")
            normalized.append(
                {"type": "receipt", "request_id": request_id, "success": action["success"]}
            )
        elif kind in ("confirm", "expire"):
            normalized.append({"type": kind})
        else:
            raise TrialError(f"动作 {i + 1} 类型必须是 receipt/confirm/expire")
    return normalized


def _apply(state: dict, action: dict, snapshot: dict, step_no: int) -> dict:
    """在给定内存状态上执行一个动作，并记录逐步结果。"""
    kind = action["type"]
    accepted = False
    reason = ""
    detail = {}

    if kind == "receipt":
        request_id = action["request_id"]
        gate = rules.receipt_gate(
            round_status=state["round"]["status"],
            already_has_receipt=request_id in state["receipts"],
        )
        accepted = gate["accepted"]
        reason = gate["reason"] if accepted else gate["error"]
        if accepted:
            state["receipts"][request_id] = {
                "success": action["success"],
                "late": gate["late"],
                "received_at": state["clock"].isoformat(timespec="seconds"),
            }
            detail["late"] = gate["late"]
        detail["request_id"] = request_id

    elif kind == "confirm":
        stats = _counts(state)
        gate = rules.confirm_gate(
            round_status=state["round"]["status"],
            now=state["clock"],
            deadline_at=datetime.fromisoformat(state["round"]["deadline_at"]),
            rollout_pct=state["meta"]["rollout_pct"],
            new_recv=stats["new"]["received"],
            new_fail=stats["new"]["fail"],
            old_recv=stats["old"]["received"],
            old_fail=stats["old"]["fail"],
            window_seconds=WINDOW_SECONDS,
        )
        accepted = gate["accepted"]
        reason = gate["reason"]
        if gate["close_round"]:
            state["round"]["status"] = gate["round_status"]
            state["round"]["closed_at"] = state["clock"].isoformat(timespec="seconds")
            state["round"]["decision_note"] = gate["decision_note"]
        if accepted:
            state["meta"]["rollout_pct"] = gate["next_pct"]
            state["meta"]["finished"] = bool(gate["finished"])
        detail.update(
            {
                "new_rate": gate["new_rate"],
                "old_rate": gate["old_rate"],
            }
        )

    else:  # expire
        gate = rules.expire_gate(state["round"]["status"], state["meta"]["rollout_pct"])
        accepted = gate["accepted"]
        reason = gate["reason"]
        if gate["accepted"]:
            state["round"]["status"] = gate["round_status"]
            state["round"]["closed_at"] = state["clock"].isoformat(timespec="seconds")
            state["round"]["decision_note"] = gate["decision_note"]

    step = {
        "step": step_no,
        "action_type": kind,
        "action": _describe_action(action, snapshot),
        "accepted": accepted,
        "reason": reason,
        **_summary(state),
    }
    step["detail"] = detail
    state["steps"].append(step)
    return step


def _run_order(snapshot: dict, actions: list[dict]) -> dict:
    """从同一快照出发，按给定顺序执行动作。"""
    state = _new_state(snapshot)
    for i, action in enumerate(actions, start=1):
        _apply(state, action, snapshot, i)
    return {"steps": state["steps"], "result": _summary(state)}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def run_trial(snapshot: dict, raw_actions) -> dict:
    snapshot = _sweep_snapshot(copy.deepcopy(snapshot))
    actions = _validate_actions(raw_actions, snapshot)

    forward = _run_order(snapshot, actions)
    reverse = _run_order(snapshot, list(reversed(actions)))

    baseline = _summary(_new_state(snapshot))
    labels = [_describe_action(a, snapshot) for a in actions]

    fields = ("effective_receipts", "late_receipts", "round_status", "rollout_pct", "finished")
    differing = [
        f for f in fields if forward["result"][f] != reverse["result"][f]
    ]

    return {
        "snapshot": {
            **copy.deepcopy(snapshot["round"]),
            "taken_at": snapshot["taken_at"],
            "tier": snapshot["meta"]["rollout_pct"],
            "finished": snapshot["meta"]["finished"],
            "window_seconds": WINDOW_SECONDS,
            "baseline": baseline,
        },
        "actions": labels,
        "runs": [
            {
                "label": "A→B",
                "order": labels,
                "steps": forward["steps"],
                "result": forward["result"],
            },
            {
                "label": "B→A",
                "order": list(reversed(labels)),
                "steps": reverse["steps"],
                "result": reverse["result"],
            },
        ],
        "comparison": {
            "order_sensitive": bool(differing),
            "differing_fields": differing,
            "baseline": baseline,
            "forward": forward["result"],
            "reverse": reverse["result"],
        },
    }
