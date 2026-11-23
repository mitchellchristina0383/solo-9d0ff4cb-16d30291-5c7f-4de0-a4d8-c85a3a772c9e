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
"""
from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

from .db import db_cursor, get_conn, init_db

WINDOW_SECONDS = int(os.environ.get("CANARY_WINDOW_SECONDS", "30"))
SAMPLE_SIZE = 10
STEP_PCT = 10

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
    new_n = math.floor(rollout_pct / 100 * SAMPLE_SIZE + 1e-9)
    return new_n, SAMPLE_SIZE - new_n


def sweep_expired(conn: sqlite3.Connection, now: datetime) -> None:
    """把超过截止时间仍 open 的轮次置为 expired（留在原档，可重开采样）。"""
    conn.execute(
        """
        UPDATE rounds
           SET status = 'expired',
               closed_at = ?,
               decision_note = '超过 ' || ? || ' 秒未收齐回执，留在原档，可重开采样'
         WHERE status = 'open' AND deadline_at < ?
        """,
        (iso(now), WINDOW_SECONDS, iso(now)),
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
        if existed:
            return jsonify({"ok": False, "error": "该笔已有回执，以首个回执为准，重复回填被忽略"}), 409

        late = 1 if req_row["round_status"] != "open" else 0
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
        if row["status"] != "open":
            return jsonify(
                {"ok": False, "error": f"轮次已结束（{row['status']}），确认只生效一次，旧确认不能再次推进放量"}
            ), 409

        if now >= datetime.fromisoformat(row["deadline_at"]):
            conn.execute(
                "UPDATE rounds SET status = 'expired', closed_at = ?, "
                "decision_note = ? WHERE id = ?",
                (
                    iso(now),
                    f"超过 {WINDOW_SECONDS} 秒未收齐回执，留在 {row['rollout_pct']}% 档，可重开采样",
                    round_id,
                ),
            )
            return jsonify({"ok": False, "error": "已超时，本轮留在原档，可重开采样"}), 409

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
        total_recv = new_recv + old_recv

        if total_recv < SAMPLE_SIZE:
            return jsonify(
                {"ok": False, "error": f"回执未收齐（{total_recv}/{SAMPLE_SIZE}），值班员需等本档结果齐全再放量"}
            ), 409

        new_rate = new_fail / new_recv if new_recv else 0.0
        old_rate = old_fail / old_recv if old_recv else 0.0
        note = (
            f"{WINDOW_SECONDS}s 内收齐 10 笔；新版失败 {new_fail}/{new_recv}"
            f"（{new_rate:.0%}），旧版失败 {old_fail}/{old_recv}（{old_rate:.0%}）"
        )

        if new_rate > old_rate:
            conn.execute(
                "UPDATE rounds SET status = 'rejected', closed_at = ?, decision_note = ? WHERE id = ?",
                (iso(now), note + "；新版失败率高于旧版，留在原档，可重开采样", round_id),
            )
            return jsonify(
                {"ok": False, "error": f"新版失败率 {new_rate:.0%} 高于旧版 {old_rate:.0%}，留在原档"}
            ), 409

        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        next_pct = min(100, meta["rollout_pct"] + STEP_PCT)
        finished = 1 if next_pct >= 100 else 0
        conn.execute(
            "UPDATE rounds SET status = 'confirmed', closed_at = ?, decision_note = ? WHERE id = ?",
            (iso(now), note + f"；满足条件，放量 {meta['rollout_pct']}% -> {next_pct}%", round_id),
        )
        conn.execute(
            "UPDATE meta SET rollout_pct = ?, finished = ? WHERE id = 1",
            (next_pct, finished),
        )

    return jsonify({"ok": True, "next_pct": next_pct, "state": build_state()})


# 测试/演练辅助：立即把进行中的轮次按超时关闭（正常情况下由 30 秒截止自动触发）
@app.post("/api/rounds/<int:round_id>/expire")
def api_expire(round_id: int):
    now = now_dt()
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "轮次不存在"}), 404
        if row["status"] != "open":
            return jsonify({"ok": False, "error": f"轮次已结束（{row['status']}）"}), 409
        conn.execute(
            "UPDATE rounds SET status = 'expired', closed_at = ?, "
            "decision_note = ? WHERE id = ?",
            (
                iso(now),
                f"手动置超时（演练辅助）：留在 {row['rollout_pct']}% 档，可重开采样",
                round_id,
            ),
        )
    return jsonify({"ok": True, "state": build_state()})


init_db()
