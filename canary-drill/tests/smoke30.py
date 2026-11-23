"""真实 30 秒窗口烟测：默认窗口配置，验证自动超时与晚到回执留档。"""
import json, os, socket, subprocess, sys, tempfile, time
import urllib.request

PORT = 18085
BASE = f"http://127.0.0.1:{PORT}"
PYLIBS = os.environ.get("CANARY_PYLIBS", "/tmp/pylibs")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    tmp = tempfile.mkdtemp(prefix="canary-smoke30-")
    env = dict(os.environ, CANARY_DB=os.path.join(tmp, "s.db"),
               PYTHONPATH=PYLIBS)  # 不设 CANARY_WINDOW_SECONDS，用默认 30
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "--bind", f"127.0.0.1:{PORT}",
         "--workers", "2", "--threads", "2", "wsgi:app"],
        cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(75):
            try:
                with socket.create_connection(("127.0.0.1", PORT), 0.5):
                    break
            except OSError:
                time.sleep(0.2)
        html = urllib.request.urlopen(BASE + "/", timeout=5).read().decode()
        ok = "灰度放量演练台" in html and "app.js" in html
        print(f"[{'PASS' if ok else 'FAIL'}] 首页渲染正常（含标题与静态资源引用）")
        cur = call("POST", "/api/rounds/open")["state"]["current"]
        print(f"开轮 #{cur['seq']}，窗口 30s，deadline 剩余 {cur['seconds_left']}s")
        first = cur["requests"][0]["request_id"]
        call("POST", f"/api/requests/{first}/receipt", {"success": True})
        print("等待 31 秒，验证真实 30s 窗口自动超时...")
        time.sleep(31)
        st = call("GET", "/api/state")
        r = next(x for x in st["rounds"] if x["id"] == cur["id"])
        ok = r["status"] == "expired" and "超过 30 秒未收齐回执" in r["decision_note"]
        print(f"[{'PASS' if ok else 'FAIL'}] 31s 后轮次自动 expired，note={r['decision_note']}")
        q = next(x for x in r["requests"] if x["receipt"] is None)
        d = call("POST", f"/api/requests/{q['request_id']}/receipt", {"success": False})
        st = call("GET", "/api/state")
        r2 = next(x for x in st["rounds"] if x["id"] == cur["id"])
        fails = r2["progress"]["new"]["fail"] + r2["progress"]["old"]["fail"]
        ok = d["late"] is True and fails == 0 and st["rollout_pct"] == 10
        print(f"[{'PASS' if ok else 'FAIL'}] 晚到失败回执 late=1 仅留档，失败统计仍为 0，档位保持 10%")
        st2 = call("POST", "/api/rounds/open")["state"]
        ok = st2["current"] is not None and st2["current"]["seq"] == cur["seq"] + 1
        print(f"[{'PASS' if ok else 'FAIL'}] 超时后可重开采样（新轮 #{st2['current']['seq'] if st2['current'] else '-'}）")
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
