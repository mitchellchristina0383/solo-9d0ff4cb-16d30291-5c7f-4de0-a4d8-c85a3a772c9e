"""顺序试跑单元测试：直接构造快照，验证规则复用、顺序敏感性与隔离。

运行：PYTHONPATH=. python3 -I tests/trial_unit_test.py
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone

from app import trial
from app.rules import confirm_gate, receipt_gate

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def snap(rollout=10, status="open", deadline="2026-11-23T12:00:30+00:00",
         taken="2026-11-23T12:00:00+00:00", receipts=None, new_n=None, old_n=None):
    new_n = rollout // 10 if new_n is None else new_n
    old_n = 10 - new_n if old_n is None else old_n
    reqs = []
    rid = 100
    for i in range(10):
        version = "new" if i < new_n else "old"
        reqs.append({"request_id": rid + i, "idx": i,
                     "label": f"R1-{i + 1:02d}-{version}", "version": version})
    return {
        "taken_at": taken,
        "round": {"id": 1, "seq": 1, "rollout_pct": rollout, "new_n": new_n,
                  "old_n": old_n, "status": status, "opened_at": "2026-11-23T12:00:00+00:00",
                  "deadline_at": deadline, "closed_at": None, "decision_note": ""},
        "meta": {"rollout_pct": rollout, "finished": False},
        "requests": reqs,
        "receipts": receipts or {},
    }


def run(s, actions):
    return trial.run_trial(copy.deepcopy(s), actions)


print("1. 规则一致性：trial 复用 rules（同输入同结论）")
g = confirm_gate(round_status="open", now=datetime(2026, 11, 23, 12, 0, 10, tzinfo=timezone.utc),
                 deadline_at=datetime(2026, 11, 23, 12, 0, 30, tzinfo=timezone.utc),
                 rollout_pct=10, new_recv=1, new_fail=0, old_recv=9, old_fail=0, window_seconds=30)
check("收齐且不劣于旧版 -> 受理 +10", g["accepted"] and g["next_pct"] == 20)
check("回执 gate：重复拒绝", receipt_gate("open", True)["accepted"] is False)

print("2. 同一动作顺序：确认 vs 回填（顺序敏感）")
s = snap(rollout=50, new_n=5, old_n=5)
# 9/10 已回填，剩最后一笔待回；A=确认，B=回填最后一笔
filled = {}
rids = [q["request_id"] for q in s["requests"]]
for rid in rids[:9]:
    filled[rid] = {"success": True, "late": False}
s["receipts"] = filled
last = rids[9]
t = run(s, [{"type": "confirm"}, {"type": "receipt", "request_id": last, "success": True}])
check("A→B：先确认未收齐 -> 未受理，轮次仍 open",
      t["runs"][0]["steps"][0]["accepted"] is False and t["runs"][0]["result"]["round_status"] == "open")
check("A→B：再回填 -> 受理，档位仍 50%", t["runs"][0]["result"]["rollout_pct"] == 50)
check("B→A：先回填 -> 受理", t["runs"][1]["steps"][0]["accepted"] is True)
check("B→A：再确认 -> 收齐受理，档位 60%", t["runs"][1]["result"]["rollout_pct"] == 60)
check("两顺序档位不同 -> order_sensitive", t["comparison"]["order_sensitive"] is True)
check("差异字段含 rollout_pct", "rollout_pct" in t["comparison"]["differing_fields"])

print("3. 顺序不敏感：两个独立回填")
s2 = snap(rollout=50, new_n=5, old_n=5)
rids2 = [q["request_id"] for q in s2["requests"]]
t2 = run(s2, [
    {"type": "receipt", "request_id": rids2[0], "success": True},
    {"type": "receipt", "request_id": rids2[1], "success": False},
])
check("两个回填正反顺序结果一致", t2["comparison"]["order_sensitive"] is False)
check("有效回执均为 2", t2["runs"][0]["result"]["effective_receipts"] == 2
      and t2["runs"][1]["result"]["effective_receipts"] == 2)
check("档位均 50%", t2["runs"][0]["result"]["rollout_pct"] == 50
      and t2["runs"][1]["result"]["rollout_pct"] == 50)

print("4. 回填 vs 置超时（顺序敏感）")
s3 = snap(rollout=40, new_n=4, old_n=6)
rid3 = s3["requests"][0]["request_id"]
t3 = run(s3, [{"type": "expire"}, {"type": "receipt", "request_id": rid3, "success": False}])
check("A→B：置超时受理", t3["runs"][0]["steps"][0]["accepted"] is True)
check("A→B：超时后回填晚到仅留档",
      t3["runs"][0]["steps"][1]["accepted"] is True
      and t3["runs"][0]["steps"][1]["detail"]["late"] is True)
check("A→B：晚到不计有效，结论 expired", t3["runs"][0]["result"]["effective_receipts"] == 0
      and t3["runs"][0]["result"]["round_status"] == "expired")
check("B→A：先回填计入有效 1", t3["runs"][1]["result"]["effective_receipts"] == 1)
check("B→A：再置超时", t3["runs"][1]["result"]["round_status"] == "expired")
check("顺序敏感", t3["comparison"]["order_sensitive"] is True)

print("5. 确认 vs 置超时（顺序敏感）")
s4 = snap(rollout=10, new_n=1, old_n=9)
filled4 = {q["request_id"]: {"success": True, "late": False} for q in s4["requests"]}
s4["receipts"] = filled4
t4 = run(s4, [{"type": "confirm"}, {"type": "expire"}])
check("A→B：确认先行 -> 放量 20%", t4["runs"][0]["result"]["rollout_pct"] == 20)
check("A→B：置超时随后被拒（轮次已结束）", t4["runs"][0]["steps"][1]["accepted"] is False)
t4b = run(s4, [{"type": "expire"}, {"type": "confirm"}])
check("A→B（先置超时）：档位仍 10%", t4b["runs"][0]["result"]["rollout_pct"] == 10)
check("B→A（先确认）：放量 20%", t4b["runs"][1]["result"]["rollout_pct"] == 20)
check("两顺序档位不同", t4["comparison"]["order_sensitive"] is True)

print("5b. 确认失败与置超时先后（均不推进，结果一致）")
t4c = run(s4, [{"type": "confirm"}, {"type": "confirm"}])
check("重复确认：首步受理", t4c["runs"][0]["steps"][0]["accepted"] is True)
check("重复确认：次步被拒", t4c["runs"][0]["steps"][1]["accepted"] is False)
t4d = run(s4, [{"type": "expire"}, {"type": "expire"}])
check("重复置超时：次步被拒", t4d["runs"][0]["steps"][1]["accepted"] is False)

print("6. 固定时钟：取快照时已过截止，两种顺序都被判超时")
s5 = snap(rollout=10, new_n=1, old_n=9,
          taken="2026-11-23T12:01:00+00:00", deadline="2026-11-23T12:00:30+00:00")
rid5 = s5["requests"][0]["request_id"]
t5 = run(s5, [{"type": "receipt", "request_id": rid5, "success": True}, {"type": "confirm"}])
check("A→B：快照起跑即已超时，回填晚到",
      t5["runs"][0]["steps"][0]["detail"]["late"] is True
      and t5["snapshot"]["status"] == "expired")
check("A→B：确认不会推进档位",
      t5["runs"][0]["steps"][1]["accepted"] is False and t5["runs"][0]["result"]["rollout_pct"] == 10)
check("B→A：确认先判超时 expired", t5["runs"][1]["result"]["round_status"] == "expired")
check("两顺序结论一致（都不推进）", t5["comparison"]["order_sensitive"] is False
      and t5["runs"][0]["result"]["rollout_pct"] == 10
      and t5["runs"][1]["result"]["rollout_pct"] == 10)
check("快照副本被扫描、原始输入未被改动",
      s5["round"]["status"] == "open" and t5["snapshot"]["status"] == "expired")

print("7. 隔离：两次试跑之间、试跑与输入快照之间无共享状态")
s6 = snap(rollout=50, new_n=5, old_n=5)
rid6 = [q["request_id"] for q in s6["requests"]]
s6_copy = copy.deepcopy(s6)
t6 = run(s6, [{"type": "receipt", "request_id": rid6[0], "success": True},
              {"type": "receipt", "request_id": rid6[1], "success": True}])
check("输入快照未被修改", s6 == s6_copy)
t7 = run(s6, [{"type": "receipt", "request_id": rid6[0], "success": False},
              {"type": "receipt", "request_id": rid6[1], "success": False}])
check("第二次试跑不受第一次影响（有效回执各自独立）",
      t6["runs"][0]["result"]["effective_receipts"] == 2
      and t7["runs"][0]["result"]["effective_receipts"] == 2)
check("两次试跑回执内容独立（不受 success 相互污染）",
      t6["runs"][0]["result"]["progress"]["new"]["fail"] == 0
      and t7["runs"][0]["result"]["progress"]["new"]["fail"] == 2)
check("两次试跑结果对象无共享引用",
      t6["runs"][0]["result"] is not t7["runs"][0]["result"])

print("8. 参数校验")
for bad, label in [
    ([], "少动作"),
    ([{"type": "receipt"}], "缺 success"),
    ([{"type": "receipt", "request_id": 999, "success": True}, {"type": "confirm"}], "样例不属于轮次"),
    ([{"type": "nope"}, {"type": "confirm"}], "未知类型"),
]:
    try:
        trial.run_trial(copy.deepcopy(s6), bad)
        check(f"拒绝非法输入：{label}", False, "未抛异常")
    except trial.TrialError:
        check(f"拒绝非法输入：{label}", True)

print("9. 已结束轮次：确认与回填都按正式规则拒绝/晚到")
s7 = snap(rollout=10, status="confirmed", receipts={})
rid7 = s7["requests"][0]["request_id"]
t8 = run(s7, [{"type": "confirm"}, {"type": "receipt", "request_id": rid7, "success": True}])
check("已结束轮次确认被拒", t8["runs"][0]["steps"][0]["accepted"] is False)
check("已结束轮次回填晚到留档", t8["runs"][0]["steps"][1]["detail"]["late"] is True)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
