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

  syncTrialControls();
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

/* ------------------------------------------------------------------ *
 * 顺序试跑
 * ------------------------------------------------------------------ */

let trialBusy = false;
let trialControlsSig = "";

function trialRound() {
  const id = $("trial-round").value;
  if (id === "") return state.current;
  return state.rounds.find((r) => String(r.id) === id) || state.current;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function syncTrialControls() {
  if (!state || trialBusy) return;
  // 轮次下拉：当前轮次置顶，其余已提交轮次按序号倒序
  const roundSel = $("trial-round");
  const rounds = [...state.rounds].reverse();
  const sig = rounds.map((r) => `${r.id}:${r.seq}:${r.requests.length}:` +
    r.requests.map((q) => q.receipt ? (q.receipt.late ? "L" : q.receipt.success ? "S" : "F") : "P").join("")
  ).join(",") + `|${state.current ? state.current.id : "none"}`;
  if (sig !== trialControlsSig) {
    trialControlsSig = sig;
    const keep = roundSel.value;
    roundSel.innerHTML = `<option value="">当前${state.current ? "进行中" : "无"}轮次</option>` +
      rounds.map((r) =>
        `<option value="${r.id}">#${r.seq}（${r.rollout_pct}% 档，${statusText(r.status)}）</option>`
      ).join("");
    if ([...roundSel.options].some((o) => o.value === keep)) roundSel.value = keep;

    // 两个动作的样例下拉
    ["a", "b"].forEach((side) => {
      const sel = $(`trial-${side}-request`);
      const prev = sel.value;
      const round = roundSel.value === "" ? state.current : rounds.find((r) => String(r.id) === roundSel.value);
      const reqs = round ? round.requests : [];
      sel.innerHTML = reqs.map((q) => {
        const stateText = q.receipt
          ? q.receipt.late ? "晚到回执，仅留档" : `已有回执（${q.receipt.success ? "成功" : "失败"}）`
          : "待回执";
        return `<option value="${q.request_id}">${q.label} · ${stateText}</option>`;
      }).join("");
      if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
    });
  }
}

function syncActionVisibility(side) {
  const type = $(`trial-${side}-type`).value;
  $(`trial-${side}-type`).closest(".trial-action").dataset.type = type;
}

["a", "b"].forEach((side) => {
  $(`trial-${side}-type`).addEventListener("change", () => syncActionVisibility(side));
  syncActionVisibility(side);
});

$("trial-round").addEventListener("change", () => {
  trialControlsSig = "";
  syncTrialControls();
});

function buildTrialAction(side) {
  const type = $(`trial-${side}-type`).value;
  if (type === "receipt") {
    return {
      type: "receipt",
      request_id: Number($(`trial-${side}-request`).value),
      success: $(`trial-${side}-success`).value === "1",
    };
  }
  return { type };
}

function pctOrDash(rate) {
  return rate === null || rate === undefined ? "–" : `${(rate * 100).toFixed(0)}%`;
}

function renderStep(step) {
  const tag = step.accepted
    ? `<span class="accepted">已受理</span>`
    : `<span class="rejected">未受理</span>`;
  const late = step.detail && step.detail.late ? ` <span class="late">（晚到，仅留档）</span>` : "";
  return `
    <div class="trial-step">
      <div><b>第 ${step.step} 步</b> ${esc(step.action)} — ${tag}${late}</div>
      <div class="muted">${esc(step.reason)}</div>
      <div>有效回执 ${step.effective_receipts}/${step.total_receipts_expected}，晚到 ${step.late_receipts}；
        轮次 <b>${statusText(step.round_status)}</b>；档位 <b>${step.rollout_pct}%</b>
        ${step.finished ? "（已到 100%）" : ""}</div>
    </div>`;
}

function renderResult(trial) {
  const s = trial.snapshot;
  $("trial-snapshot").innerHTML = `
    快照轮次 <b>#${s.seq}</b>（发出档位 ${s.rollout_pct}%，新版 ${s.new_n} 笔 / 旧版 ${s.old_n} 笔，${statusText(s.status)}）<br>
    快照档位 <b>${s.tier}%</b>${s.finished ? "（已到 100%，停止）" : ""}；取快照时刻 <b>${esc(s.taken_at.replace("T", " ").replace("+00:00", " UTC"))}</b>（时钟固定在此刻）<br>
    快照时已有有效回执 ${s.baseline.effective_receipts}/10，晚到 ${s.baseline.late_receipts}<br>
    动作 A：${esc(trial.actions[0])}；动作 B：${esc(trial.actions[1])}
  `;

  $("trial-runs").innerHTML = trial.runs.map((run) => `
    <div class="trial-run">
      <h4>${run.label}：${run.order.map(esc).join(" → ")}</h4>
      ${run.steps.map(renderStep).join("")}
      <div class="trial-result-line">
        最终：有效回执 <b>${run.result.effective_receipts}/10</b>，
        轮次 <b>${statusText(run.result.round_status)}</b>，
        档位 <b>${run.result.rollout_pct}%</b>${run.result.finished ? "（已到 100%）" : ""}
        ${run.result.decision_note ? `<div class="muted">判定记录：${esc(run.result.decision_note)}</div>` : ""}
      </div>
    </div>`).join("");

  const c = trial.comparison;
  const f = c.forward, r = c.reverse;
  const rows = [
    ["有效回执数", `${f.effective_receipts}/10`, `${r.effective_receipts}/10`, "effective_receipts"],
    ["晚到回执数", f.late_receipts, r.late_receipts, "late_receipts"],
    ["轮次结论", statusText(f.round_status), statusText(r.round_status), "round_status"],
    ["最终档位", `${f.rollout_pct}%`, `${r.rollout_pct}%`, "rollout_pct"],
    ["是否到 100%", f.finished ? "是" : "否", r.finished ? "是" : "否", "finished"],
  ];
  const diffSet = new Set(c.differing_fields);
  $("trial-compare").innerHTML = `
    <table>
      <thead><tr><th>对照项</th><th>A→B</th><th>B→A</th><th>是否随顺序变化</th></tr></thead>
      <tbody>
        ${rows.map(([name, fa, ra, key]) => `<tr><td>${name}</td><td>${esc(fa)}</td><td>${esc(ra)}</td>
          <td class="${diffSet.has(key) ? "diff" : "same"}">${diffSet.has(key) ? "不同" : "相同"}</td></tr>`).join("")}
      </tbody>
    </table>
    <div class="verdict ${c.order_sensitive ? "diff" : "same"}">
      ${c.order_sensitive
        ? `两种顺序结果不同：${diffSet.size} 个对照项随动作先后变化（${diffSet.size} 项）。`
        : "两种顺序结果一致：在当前快照下，这两个动作谁先谁后不影响有效回执数、轮次结论和档位。"}
    </div>`;

  $("trial-result").classList.remove("hidden");
}

$("btn-trial").onclick = async () => {
  if (!state) return;
  const round = trialRound();
  if (!round) {
    $("trial-msg").className = "msg err";
    $("trial-msg").textContent = "当前没有可试跑的轮次，请先开一轮采样";
    return;
  }
  trialBusy = true;
  $("btn-trial").disabled = true;
  $("trial-msg").className = "msg";
  $("trial-msg").textContent = "";
  try {
    const body = {
      actions: [buildTrialAction("a"), buildTrialAction("b")],
    };
    if ($("trial-round").value !== "") body.round_id = round.id;
    const d = await api("/api/trial", "POST", body);
    renderResult(d.trial);
  } catch (e) {
    $("trial-msg").className = "msg err";
    $("trial-msg").textContent = e.message;
  } finally {
    trialBusy = false;
    $("btn-trial").disabled = false;
    syncTrialControls();
    refresh();
  }
};

