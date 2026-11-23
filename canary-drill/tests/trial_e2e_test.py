#!/usr/bin/env python3
"""顺序试跑端到端测试：真实 gunicorn（2 worker），验证只读与隔离。

运行：
  PYTHONPATH=/tmp/pylibs python3 tests/trial_e2e_test.py
覆盖：
  1. 当前轮快照 + 两个动作、正反顺序各跑一次，逐步给出受理结果；
  2. 顺序敏感场景与顺序无关场景的对照结论正确；
  3. 试跑不写正式库：档位、轮次状态、回执数前后完全一致；
  4. 试跑期间正式演练可继续（并发开轮/回填/确认不受阻塞、结果不受污染）；
  5. 两个试跑并发、以及试跑与正式操作交错时互不影响；
  6. 参数错误 400、无进行中轮次 409、指定已提交轮次可试跑。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PORT = int(os.environ.get("CANARY_TRIAL_TEST_PORT", "18081"))
BASE = f"http://127.0.0.1:{PORT}"
PYLIBS = os.environ.get("CANARY_PYLIBS", "/tmp/pylibs")
WINDOW = "60"
PASS, FAIL = 0, 0


def call(method, path, body=None, expect_error=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
            if expect_error:
                raise AssertionError(f"{path} 应失败却成功: {payload}")
            return payload
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        if not expect_error:
            raise AssertionError(f"{path} 意外失败: {e.code} {payload}")
        return e.code, payload


def wait_port(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("gunicorn 未在预期时间内启动")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def state():
    return call("GET", "/api/state")


def open_round():
    return call("POST", "/api/rounds/open")["state"]["current"]


def trial(actions, round_id=None):
    body = {"actions": actions}
    if round_id is not None:
        body["round_id"] = round_id
    return call("POST", "/api/trial", body)["trial"]


def fill(cur, success_of):
    for q in cur["requests"]:
        call("POST", f"/api/requests/{q['request_id']}/receipt",
             {"success": success_of(q["idx"], q["version"])})


def fingerprint():
    """正式状态指纹：档位 + 每轮状态/判定 + 每轮回执内容。"""
    st = state()
    return {
        "rollout_pct": st["rollout_pct"],
        "finished": st["finished"],
        "rounds": [
            {
                "seq": r["seq"],
                "status": r["status"],
                "decision_note": r["decision_note"],
                "receipts": [
                    (q["request_id"], q["receipt"]["success"], q["receipt"]["late"])
                    for q in r["requests"] if q["receipt"]
                ],
                "progress": r["progress"],
            }
            for r in st["rounds"]
        ],
    }


def main():
    tmp = tempfile.mkdtemp(prefix="canary-trial-e2e-")
    db = os.path.join(tmp, "test.db")
    env = dict(os.environ, CANARY_DB=db, CANARY_WINDOW_SECONDS=WINDOW,
               PYTHONPATH=PYLIBS)
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn",
         "--bind", f"127.0.0.1:{PORT}", "--workers", "2", "--threads", "2",
         "wsgi:app"],
        cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_port()
        print(f"gunicorn 已启动（db={db}, 窗口={WINDOW}s）")

        # --- 1. 无进行中轮次时的行为 -------------------------------------
        print("场景1 无进行中轮次 / 参数校验")
        code, _ = call("POST", "/api/trial", {"actions": [
            {"type": "confirm"}, {"type": "expire"}]}, expect_error=True)
        check("无进行中轮次 -> 409", code == 409)
        cur = open_round()
        check("10% 档开轮 1 新 9 旧", cur["new_n"] == 1 and cur["old_n"] == 9)
        code, _ = call("POST", "/api/trial", {"actions": [{"type": "confirm"}]},
                       expect_error=True)
        check("动作不足两个 -> 400", code == 400)
        code, _ = call("POST", "/api/trial", {"actions": [
            {"type": "receipt", "request_id": 999999, "success": True},
            {"type": "confirm"}]}, expect_error=True)
        check("样例不属于快照 -> 400", code == 400)
        code, _ = call("POST", "/api/trial", {"round_id": 999999, "actions": [
            {"type": "confirm"}, {"type": "expire"}]}, expect_error=True)
        check("指定不存在轮次 -> 404", code == 404)

        # --- 2. 顺序敏感：确认 vs 回填最后一笔 ----------------------------
        print("场景2 顺序敏感：确认与回填最后一笔")
        rids = [q["request_id"] for q in cur["requests"]]
        for rid in rids[:9]:
            call("POST", f"/api/requests/{rid}/receipt", {"success": True})
        last = rids[9]
        before = fingerprint()
        actions = [{"type": "confirm"},
                   {"type": "receipt", "request_id": last, "success": True}]
        t = trial(actions)
        check("快照 9/10 有效回执",
              t["snapshot"]["baseline"]["effective_receipts"] == 9)
        check("快照档位 10%", t["snapshot"]["tier"] == 10)
        check("A→B 先确认未收齐被拒",
              t["runs"][0]["steps"][0]["accepted"] is False
              and t["runs"][0]["result"]["round_status"] == "open")
        check("A→B 档位仍 10%", t["runs"][0]["result"]["rollout_pct"] == 10)
        check("B→A 先回填再确认 -> 20%",
              t["runs"][1]["steps"][0]["accepted"] is True
              and t["runs"][1]["result"]["rollout_pct"] == 20
              and t["runs"][1]["result"]["round_status"] == "confirmed")
        check("对照标记为顺序敏感",
              t["comparison"]["order_sensitive"] is True
              and set(t["comparison"]["differing_fields"]) >= {"round_status", "rollout_pct"})
        check("逐步结果含有效回执/结论/档位",
              all({"effective_receipts", "round_status", "rollout_pct"} <= set(s)
                  for s in t["runs"][0]["steps"]))
        check("试跑后正式状态指纹不变", fingerprint() == before)

        # --- 3. 顺序无关：两个互不影响的回填 ------------------------------
        print("场景3 顺序无关：两个独立回填")
        before = fingerprint()
        t = trial([
            {"type": "receipt", "request_id": last, "success": True},
            {"type": "receipt", "request_id": rids[0], "success": True},
        ])
        # rids[0] 已有回执，两个顺序里都会有一个重复被拒；结果应一致
        check("两个顺序有效回执数一致",
              t["runs"][0]["result"]["effective_receipts"]
              == t["runs"][1]["result"]["effective_receipts"])
        check("对照标记为顺序无关", t["comparison"]["order_sensitive"] is False)
        check("试跑后正式状态指纹不变", fingerprint() == before)

        # --- 4. 试跑期间正式演练照常进行 ----------------------------------
        print("场景4 试跑期间正式演练照常进行")
        actions = [{"type": "confirm"},
                   {"type": "receipt", "request_id": last, "success": True}]
        results = []

        def try_trial():
            try:
                results.append(("ok", trial(actions)))
            except AssertionError as exc:
                # 取快照时若正式轮次刚好已确认关闭，返回 409 属正确行为
                results.append(("409" if "409" in str(exc) else "err", None))

        with ThreadPoolExecutor(6) as ex:
            futures = [ex.submit(try_trial) for _ in range(6)]
            # 试跑并发期间，正式回填最后一笔并确认
            call("POST", f"/api/requests/{last}/receipt", {"success": True})
            cur2 = state()["current"]
            d = call("POST", f"/api/rounds/{cur2['id']}/confirm")
            for f in futures:
                f.result(timeout=20)
        ok_trials = [r for kind, r in results if kind == "ok"]
        check("并发试跑均正常返回（成功或快照竞争 409，无 5xx）",
              all(kind in ("ok", "409") for kind, _ in results) and ok_trials)
        check("成功返回的并发试跑结果一致",
              all(x["comparison"] == ok_trials[0]["comparison"] for x in ok_trials))
        check("正式确认成功（试跑未阻塞/未干扰）", d["next_pct"] == 20)
        st = state()
        check("正式档位推进到 20%", st["rollout_pct"] == 20)
        check("正式轮次结论为 confirmed",
              next(r for r in st["rounds"] if r["id"] == cur2["id"])["status"] == "confirmed")
        # 并发试跑都基于同一快照：B→A 都推演到 20%，正式结果也是 20%
        check("并发试跑均基于取快照时刻推演",
              all(x["runs"][1]["result"]["rollout_pct"] == 20 for x in ok_trials))

        # --- 5. 指定已提交轮次试跑 ---------------------------------------
        print("场景5 指定已提交轮次（confirmed）试跑")
        before = fingerprint()
        t = trial([{"type": "confirm"}, {"type": "expire"}], round_id=cur2["id"])
        check("已提交轮次快照状态 confirmed", t["snapshot"]["status"] == "confirmed")
        check("已提交轮次确认被拒",
              t["runs"][0]["steps"][0]["accepted"] is False)
        check("两顺序都不改档位",
              t["runs"][0]["result"]["rollout_pct"] == 20
              and t["runs"][1]["result"]["rollout_pct"] == 20)
        check("指定轮次试跑后正式状态不变", fingerprint() == before)

        # --- 6. 开新一轮：试跑 vs 正式置超时 -----------------------------
        print("场景6 开新一轮：回填与置超时先后")
        cur3 = open_round()
        rid3 = cur3["requests"][0]["request_id"]
        before = fingerprint()
        t = trial([
            {"type": "expire"},
            {"type": "receipt", "request_id": rid3, "success": False},
        ])
        check("A→B 置超时先行，回填晚到仅留档",
              t["runs"][0]["steps"][0]["accepted"] is True
              and t["runs"][0]["steps"][1]["detail"]["late"] is True
              and t["runs"][0]["result"]["effective_receipts"] == 0)
        check("B→A 先回填计入有效 1",
              t["runs"][1]["result"]["effective_receipts"] == 1)
        check("顺序敏感", t["comparison"]["order_sensitive"] is True)
        check("试跑后正式状态不变（轮次仍 open）", fingerprint() == before)
        st = state()
        check("正式轮次仍 open、档位 20%",
              st["current"] is not None and st["rollout_pct"] == 20)
        check("正式轮次回执数仍为 0",
              st["current"]["progress"]["new"]["received"]
              + st["current"]["progress"]["old"]["received"] == 0)

        # --- 7. 试跑不影响其他试跑（不同动作并发） ------------------------
        print("场景7 不同动作的并发试跑互不影响")
        acts_a = [{"type": "confirm"}, {"type": "expire"}]
        acts_b = [{"type": "receipt", "request_id": rid3, "success": True},
                  {"type": "receipt", "request_id": cur3["requests"][1]["request_id"],
                   "success": True}]
        ref_a = trial(acts_a)
        ref_b = trial(acts_b)
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(
                lambda i: trial(acts_a if i % 2 == 0 else acts_b), range(8)))
        check("偶数并发结果等于 A 单独结果",
              all(g["comparison"] == ref_a["comparison"] for g in got[0::2]))
        check("奇数并发结果等于 B 单独结果",
              all(g["comparison"] == ref_b["comparison"] for g in got[1::2]))
        check("A/B 两种动作推演结果不同",
              ref_a["runs"][0]["result"] != ref_b["runs"][0]["result"])

        # --- 8. 收尾：正式流程仍可正常推进 --------------------------------
        print("场景8 试跑后正式演练仍可正常推进")
        fill(state()["current"], lambda i, v: True)
        cur4 = state()["current"]
        d = call("POST", f"/api/rounds/{cur4['id']}/confirm")
        check("正式确认推进到 30%", d["next_pct"] == 30)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        if FAIL:
            print("\n--- gunicorn 输出 ---")
            print(out)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
