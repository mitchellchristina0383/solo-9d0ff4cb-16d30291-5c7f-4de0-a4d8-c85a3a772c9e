/* 灰度演练台前端：1 秒轮询 /api/state，逐笔回填成败 */
let state = null;

const $ = (id) => document.getElementById(id);

async function api(path, method = "GET", body) {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

function pctText(rate) {
  return rate === null ? "–" : `${(rate * 100).toFixed(0)}%`;
}

function statusText(s) {
  return {
    open: "进行中",
    confirmed: "已确认放量",
    rejected: "超标留在原档",
    expired: "超时留在原档",
  }[s] || s;
}

function renderStat(el, stat, label) {
  el.innerHTML = `
    <div class="stat-line">回收 <b>${stat.received}/${stat.total}</b>，待回 ${stat.pending}</div>
    <div class="stat-line">成功 ${stat.success}，失败 ${stat.fail}，失败率 <b>${pctText(stat.fail_rate)}</b></div>
    ${stat.late ? `<div class="late">轮次关闭后晚到回执 ${stat.late} 笔（仅留档，不参与判定）</div>` : ""}
  `;
}

function render() {
  if (!state) return;
  $("rollout-pct").textContent = `${state.rollout_pct}%${state.finished ? "（已到 100%，停止）" : ""}`;
  $("server-time").textContent = `服务器时间 ${state.server_time.replace("T", " ").replace("+00:00", " UTC")}`;

  const cur = state.current;
  $("btn-open").disabled = !!cur || state.finished;
  $("btn-confirm").disabled = !cur;
  $("btn-expire").disabled = !cur;

  const panel = $("current-panel");
  if (!cur) {
    panel.classList.add("hidden");
  } else {
    panel.classList.remove("hidden");
    $("round-title").textContent =
      `#${cur.seq}（发出档位 ${cur.rollout_pct}%，新版 ${cur.new_n} 笔 / 旧版 ${cur.old_n} 笔）`;

    const banner =
      cur.status === "open"
        ? `进行中，剩余 ${cur.seconds_left} 秒（${state.window_seconds}s 窗口）`
        : `已结束：${statusText(cur.status)}`;
    $("round-banner").className = `banner ${cur.status}`;
    $("round-banner").textContent = banner;

    renderStat($("new-stat"), cur.progress.new);
    renderStat($("old-stat"), cur.progress.old);

    const tbody = $("req-tbody");
    tbody.innerHTML = "";
    cur.requests.forEach((q) => {
      const tr = document.createElement("tr");
      let statusCell, actionCell;
      if (!q.receipt) {
        statusCell = `<span class="pending">待回执</span>`;
        actionCell = cur.status === "open"
          ? `<button data-id="${q.request_id}" data-success="1" class="ok">回填成功</button>
             <button data-id="${q.request_id}" data-success="0" class="fail">回填失败</button>`
          : `<span class="muted">轮次已关闭</span>`;
      } else {
        const kind = q.receipt.success ? "ok" : "fail";
        const tag = q.receipt.success ? "成功" : "失败";
        statusCell = `<span class="${kind}">${tag}${q.receipt.late ? "（晚到，仅留档）" : ""}</span>`;
        actionCell = `<span class="muted">首个回执已锁定</span>`;
      }
      tr.innerHTML = `<td>${q.idx + 1}</td><td>${q.label}</td><td>${q.version === "new" ? "新版" : "旧版"}</td><td>${statusCell}</td><td>${actionCell}</td>`;
      tbody.appendChild(tr);
    });

    const basis = $("basis");
    const n = cur.progress.new, o = cur.progress.old;
    basis.innerHTML = `
      <li>窗口 ${state.window_seconds} 秒内回执：${n.received + o.received}/10（新版 ${n.received}/${n.total}，旧版 ${o.received}/${o.total}）${cur.all_received ? "，<b>已收齐</b>" : "，未收齐"}</li>
      <li>新版失败率 ${pctText(n.fail_rate)}，旧版失败率 ${pctText(o.fail_rate)}；要求新版 ≤ 旧版</li>
      <li>满足条件确认后：${state.rollout_pct}% → ${Math.min(100, state.rollout_pct + 10)}%${Math.min(100, state.rollout_pct + 10) === 100 ? "（到 100% 停止）" : ""}</li>
      <li>超时或超标：留在 ${state.rollout_pct}% 原档，可重新开采样；晚到/历史回执不参与本轮判定</li>
      ${cur.decision_note ? `<li><b>判定记录：</b>${cur.decision_note}</li>` : ""}
    `;
  }

  const hist = $("history-tbody");
  hist.innerHTML = "";
  state.rounds.forEach((r) => {
    if (state.current && r.id === state.current.id) return;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>#${r.seq}</td><td>${r.rollout_pct}%</td><td>${r.new_n} / ${r.old_n}</td>
      <td>${statusText(r.status)}</td><td>${r.decision_note || "–"}</td>`;
    hist.appendChild(tr);
  });
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
  } catch (e) {
    $("global-msg").className = "msg err";
    $("global-msg").textContent = e.message;
  }
}

function flash(msg, ok) {
  const el = $("global-msg");
  el.className = `msg ${ok ? "ok" : "err"}`;
  el.textContent = msg;
  setTimeout(() => (el.textContent = ""), 4000);
}

$("btn-open").onclick = async () => {
  try { await api("/api/rounds/open", "POST"); flash("已开新一轮采样", true); }
  catch (e) { flash(e.message, false); }
  refresh();
};

$("btn-confirm").onclick = async () => {
  try {
    const d = await api(`/api/rounds/${state.current.id}/confirm`, "POST");
    flash(`确认成功，新档 ${d.next_pct}%`, true);
  } catch (e) { flash(e.message, false); }
  refresh();
};

$("btn-expire").onclick = async () => {
  try { await api(`/api/rounds/${state.current.id}/expire`, "POST"); flash("轮次已置超时", true); }
  catch (e) { flash(e.message, false); }
  refresh();
};

$("req-tbody").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (!btn) return;
  try {
    const d = await api(`/api/requests/${btn.dataset.id}/receipt`, "POST", {
      success: btn.dataset.success === "1",
    });
    if (d.late) flash("该回执晚到：已留档但不参与本轮判定", false);
  } catch (e) { flash(e.message, false); }
  refresh();
});

refresh();
setInterval(refresh, 1000);
