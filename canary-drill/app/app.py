"""灰度放量演练 Flask 应用。

业务规则（与需求逐条对应）：
1. 新版流量从 10% 起，每轮按当前比例把 10 笔样例请求分给新/旧版。
2. 回执按 request_id 唯一约束，以首个回执为准；重复回填被拒绝。
3. 轮次发出后 30 秒截止；截止后到达的回执标记 late=1，仅留档不参与判定。
4. 截止时间内收齐 10 笔且新版失败率 <= 旧版，确认后档位 +10 个百分点；
   超时或超标则本轮留在原档（expired/rejected），可重新开采样。
5. 放量到 100% 停止，不再开新轮。
6. 确认走 BEGIN IMMEDIATE 事务 + 状态机校验，并发确认只推进一次。
7. 回执始终挂在"请求发出时的轮次和版本"上（requests 行不可变），
   换轮后的旧回执天然无法影响新轮判定。
8. 受理/判定规则集中在 app/rules.py，正式接口与顺序试跑（app/trial.py）共用；
   试跑只读快照并在内存副本上推演，不写正式库。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

from .config import SAMPLE_SIZE, WINDOW_SECONDS
from .db import db_cursor, get_conn, init_db, read_snapshot_cursor
from . import rules, trial

app = Flask(__name__)


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def split_counts(rollout_pct: int) -> tuple[int, int]:
    """按当前比例拆分 10 笔样例：返回 (新版笔数, 旧版笔数)。"""
    return rules.split_counts(rollout_pct)


def sweep_expired(conn: sqlite3.Connection, now: datetime) -> None:
    """把超过截止时间仍 open 的轮次置为 expired（留在原档，可重开采样）。"""
    conn.execute(
        """
        UPDATE rounds
           SET status = 'expired',
               closed_at = ?,
               decision_note = ?
         WHERE status = 'open' AND deadline_at < ?
        """,
        (iso(now), rules.sweep_note(WINDOW_SECONDS), iso(now)),
    )


def round_progress(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict:
    stats = {}
    for version in ("new", "old"):
        total = row["new_n"] if version == "new" else row["old_n"]
        received = conn.execute(
            "SELECT COUNT(*) c FROM receipts r JOIN requests q ON q.id = r.request_id "
            "WHERE r.round_id = ? AND r.late = 0 AND q.version = ?",
            (row["id"], version),
        ).fetchone()["c"]
        fail = conn.execute(
            "SELECT COUNT(*) c FROM receipts r JOIN requests q ON q.id = r.request_id "
            "WHERE r.round_id = ? AND r.late = 0 AND q.version = ? AND r.success = 0",
            (row["id"], version),
        ).fetchone()["c"]
        late = conn.execute(
            "SELECT COUNT(*) c FROM receipts r JOIN requests q ON q.id = r.request_id "
            "WHERE r.round_id = ? AND r.late = 1 AND q.version = ?",
            (row["id"], version),
        ).fetchone()["c"]
        stats[version] = {
            "total": total,
            "received": received,
            "pending": total - received,
            "success": received - fail,
            "fail": fail,
            "late": late,
            "fail_rate": (fail / received) if received else None,
        }

    req_rows = conn.execute(
        """
        SELECT q.id, q.idx, q.label, q.version,
               r.success AS r_success, r.late AS r_late, r.received_at AS r_at
          FROM requests q
          LEFT JOIN receipts r ON r.request_id = q.id
         WHERE q.round_id = ?
         ORDER BY q.idx
        """,
        (row["id"],),
    ).fetchall()
    requests_out = [
        {
            "request_id": q["id"],
            "idx": q["idx"],
            "label": q["label"],
            "version": q["version"],
            "receipt": None
            if q["r_at"] is None
            else {
                "success": bool(q["r_success"]),
                "late": bool(q["r_late"]),
                "received_at": q["r_at"],
            },
        }
        for q in req_rows
    ]

    deadline = datetime.fromisoformat(row["deadline_at"])
    seconds_left = max(0, int((deadline - now).total_seconds()))
    all_received = (
        stats["new"]["received"] == stats["new"]["total"]
        and stats["old"]["received"] == stats["old"]["total"]
    )
    return {
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
        "seconds_left": seconds_left,
        "all_received": all_received,
        "progress": stats,
        "requests": requests_out,
    }


def build_state() -> dict:
    now = now_dt()
    conn = get_conn()
    try:
        sweep_expired(conn, now)
        conn.commit()
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        round_rows = conn.execute("SELECT * FROM rounds ORDER BY seq").fetchall()
        rounds_out = [round_progress(conn, r, now) for r in round_rows]
        current = next((r for r in rounds_out if r["status"] == "open"), None)
        return {
            "server_time": iso(now),
            "window_seconds": WINDOW_SECONDS,
            "rollout_pct": meta["rollout_pct"],
            "finished": bool(meta["finished"]),
            "current": current,
            "rounds": rounds_out,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", window_seconds=WINDOW_SECONDS)


@app.get("/api/state")
def api_state():
    return jsonify(build_state())


# ---------------------------------------------------------------------------
# 开轮：按当前档位拆 10 笔样例
# ---------------------------------------------------------------------------

@app.post("/api/rounds/open")
def api_open_round():
    now = now_dt()
    with db_cursor() as conn:
        sweep_expired(conn, now)
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if meta["finished"]:
            return jsonify({"ok": False, "error": "已放量到 100%，演练结束"}), 409
        open_row = conn.execute(
            "SELECT id FROM rounds WHERE status = 'open'"
        ).fetchone()
        if open_row:
            return jsonify({"ok": False, "error": "已有进行中的轮次，请先收齐并确认或等待超时"}), 409

        pct = meta["rollout_pct"]
        new_n, old_n = split_counts(pct)
        last_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) s FROM rounds").fetchone()["s"]
        seq = last_seq + 1
        deadline = now + timedelta(seconds=WINDOW_SECONDS)
        cur = conn.execute(
            "INSERT INTO rounds(seq, rollout_pct, new_n, old_n, opened_at, deadline_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seq, pct, new_n, old_n, iso(now), iso(deadline)),
        )
        round_id = cur.lastrowid
        for i in range(SAMPLE_SIZE):
            version = "new" if i < new_n else "old"
            conn.execute(
                "INSERT INTO requests(round_id, version, idx, label) VALUES (?, ?, ?, ?)",
                (round_id, version, i, f"R{seq}-{i + 1:02d}-{version}"),
            )
    return jsonify({"ok": True, "state": build_state()})


# ---------------------------------------------------------------------------
# 回填回执：首个为准；轮次关闭后到达的回执标记 late，仅留档
# ---------------------------------------------------------------------------

@app.post("/api/requests/<int:request_id>/receipt")
def api_receipt(request_id: int):
    payload = request.get_json(silent=True) or {}
    if "success" not in payload or not isinstance(payload["success"], bool):
        return jsonify({"ok": False, "error": "请求体需要布尔字段 success"}), 400

    now = now_dt()
    with db_cursor() as conn:
        sweep_expired(conn, now)
        req_row = conn.execute(
            "SELECT q.id, q.round_id, q.version, r.status AS round_status "
            "FROM requests q JOIN rounds r ON r.id = q.round_id WHERE q.id = ?",
            (request_id,),
        ).fetchone()
        if req_row is None:
            return jsonify({"ok": False, "error": "样例请求不存在"}), 404

        existed = conn.execute(
            "SELECT id FROM receipts WHERE request_id = ?", (request_id,)
        ).fetchone()
        gate = rules.receipt_gate(req_row["round_status"], existed is not None)
        if not gate["accepted"]:
            return jsonify({"ok": False, "error": gate["error"]}), 409

        late = 1 if gate["late"] else 0
        try:
            conn.execute(
                "INSERT INTO receipts(request_id, round_id, version, success, late, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    req_row["round_id"],
                    req_row["version"],
                    1 if payload["success"] else 0,
                    late,
                    iso(now),
                ),
            )
        except sqlite3.IntegrityError:
            # 并发回填同一笔：唯一约束兜底，只有第一个成功
            return jsonify({"ok": False, "error": "该笔已有回执，以首个回执为准"}), 409

    return jsonify({"ok": True, "late": bool(late), "state": build_state()})


# ---------------------------------------------------------------------------
# 确认放量：IMMEDIATE 事务串行化，并发确认只推进一次
# ---------------------------------------------------------------------------

@app.post("/api/rounds/<int:round_id>/confirm")
def api_confirm(round_id: int):
    now = now_dt()
    with db_cursor(immediate=True) as conn:
        row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "轮次不存在"}), 404

        counts = conn.execute(
            """
            SELECT q.version,
                   SUM(CASE WHEN r.late = 0 THEN 1 ELSE 0 END) AS received,
                   SUM(CASE WHEN r.late = 0 AND r.success = 0 THEN 1 ELSE 0 END) AS fail
              FROM requests q LEFT JOIN receipts r ON r.request_id = q.id
             WHERE q.round_id = ?
             GROUP BY q.version
            """,
            (round_id,),
        ).fetchall()
        stat = {c["version"]: (c["received"] or 0, c["fail"] or 0) for c in counts}
        new_recv, new_fail = stat.get("new", (0, 0))
        old_recv, old_fail = stat.get("old", (0, 0))

        # 正式流程以 meta 当前档位为准（与轮次发出档位一致；轮次未关闭时不应漂移）
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        verdict = rules.confirm_gate(
            round_status=row["status"],
            now=now,
            deadline_at=datetime.fromisoformat(row["deadline_at"]),
            rollout_pct=meta["rollout_pct"],
            new_recv=new_recv,
            new_fail=new_fail,
            old_recv=old_recv,
            old_fail=old_fail,
            window_seconds=WINDOW_SECONDS,
        )

        if verdict["close_round"]:
            conn.execute(
                "UPDATE rounds SET status = ?, closed_at = ?, decision_note = ? WHERE id = ?",
                (verdict["round_status"], iso(now), verdict["decision_note"], round_id),
            )
        if not verdict["accepted"]:
            return jsonify({"ok": False, "error": verdict["error"]}), 409

        conn.execute(
            "UPDATE meta SET rollout_pct = ?, finished = ? WHERE id = 1",
            (verdict["next_pct"], 1 if verdict["finished"] else 0),
        )

    return jsonify({"ok": True, "next_pct": verdict["next_pct"], "state": build_state()})


# 测试/演练辅助：立即把进行中的轮次按超时关闭（正常情况下由 30 秒截止自动触发）
@app.post("/api/rounds/<int:round_id>/expire")
def api_expire(round_id: int):
    now = now_dt()
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "轮次不存在"}), 404
        verdict = rules.expire_gate(row["status"], row["rollout_pct"])
        if not verdict["accepted"]:
            return jsonify({"ok": False, "error": verdict["error"]}), 409
        conn.execute(
            "UPDATE rounds SET status = ?, closed_at = ?, decision_note = ? WHERE id = ?",
            (verdict["round_status"], iso(now), verdict["decision_note"], round_id),
        )
    return jsonify({"ok": True, "state": build_state()})


# ---------------------------------------------------------------------------
# 顺序试跑：同一份已提交快照，按正反两种顺序各跑两个动作
# ---------------------------------------------------------------------------

@app.post("/api/trial")
def api_trial():
    """试跑不写库：取快照后全程在内存副本上执行，两个顺序各自独立。"""
    payload = request.get_json(silent=True) or {}
    round_id = payload.get("round_id")
    if round_id is not None and (not isinstance(round_id, int) or isinstance(round_id, bool)):
        return jsonify({"ok": False, "error": "round_id 需要整数或留空"}), 400

    now = now_dt()
    try:
        with read_snapshot_cursor() as conn:
            snapshot = trial.snapshot_from_db(conn, now, round_id)
        result = trial.run_trial(snapshot, payload.get("actions"))
    except trial.TrialError as exc:
        return jsonify({"ok": False, "error": exc.message}), exc.status
    return jsonify({"ok": True, "trial": result})


init_db()
