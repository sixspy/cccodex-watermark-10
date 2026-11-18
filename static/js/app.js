(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  // 核对台状态
  let headers = [];
  let records = [];
  let replayLogs = [];
  let gapCardList = [];
  const snapshotMap = {}; // header_id -> 冻结快照
  const detailCache = {}; // batch_id -> 批次详情（含头栏核对信息）

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t, isErr = false) {
    msgEl.textContent = t || "";
    msgEl.classList.toggle("err", !!isErr);
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
  }

  async function refreshReconcile() {
    headers = (await api("/api/reconcile/headers")).items || [];
    records = (await api("/api/reconcile/records")).items || [];
    replayLogs = (await api("/api/reconcile/replay-logs")).items || [];
    gapCardList = (await api("/api/reconcile/gap-cards")).items || [];
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function driftChip(h) {
    if (!h.drifted) return `<span class="chip chip-ok">已对齐</span>`;
    return `<span class="chip chip-drift">漂移 ${h.delta > 0 ? "+" : ""}${h.delta}</span>`;
  }

  function modeChip(mode) {
    if (mode === "冻结") return `<span class="chip chip-freeze">冻结</span>`;
    if (mode === "重数") return `<span class="chip chip-recount">重数</span>`;
    return "-";
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${t.table_name}</td>
            <td>${t.column_name}</td>
            <td>${t.version_no}</td>
            <td><span class="chip">${t.status}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账。点「详情」同查所属批头栏头栏数与实数。</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.table_name}</td>
            <td>${b.run_code}</td>
            <td>${b.biz_date}</td>
            <td><span class="chip">${b.status}</span></td>
            <td>${b.watermark_value || "-"}</td>
            <td class="cell-actions">
              <button type="button" class="ghost" data-detail="${b.id}">详情</button>
              ${
                b.status === "待盖印"
                  ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                  : ""
              }
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${selected.run_code} · ${selected.table_name}</p>
        <label>水印值<input id="stamp-input" value="${stampValue}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }
      <div id="batch-detail-box"></div>`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${traceValue}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${s.run_code} · ${s.table_name}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${d.run_code} · ${d.status} · ${d.watermark_value || "未盖印"}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账</h2>
      <button type="button" id="btn-gap">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>${r.as_of_date}</td>
            <td>${r.gap_count}</td>
            <td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3 style="margin-top:1.2rem">缺口卡片（头栏漂移则阻断）</h3>
      <table>
        <thead><tr><th>业务日</th><th>表</th><th>头栏申报</th><th>运行码实数</th><th>差量</th><th>状态</th><th>缺口摘要</th></tr></thead>
        <tbody>
          ${gapCardList
            .map(
              (c) => `<tr class="${c.blocked ? "row-drift" : ""}">
            <td>${c.biz_date}</td>
            <td>${c.table_name}</td>
            <td>${c.declared_stamped}</td>
            <td>${c.actual_stamped}</td>
            <td class="${c.delta !== 0 ? "delta-bad" : "delta-ok"}">${c.delta > 0 ? "+" : ""}${c.delta}</td>
            <td>${
              c.blocked
                ? '<span class="chip chip-drift">对账阻断</span>'
                : '<span class="chip chip-ok">已对齐</span>'
            }</td>
            <td>${c.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReconcile() {
    return `
      <h2>水印核对台</h2>
      <p class="muted">头栏申报的已盖印水印数须与运行码行数一致。漂移时缺口摘要跟着漂并阻断对账，纠偏落库后解除。</p>
      <h3>批头栏（头栏数 vs 实数）</h3>
      <table>
        <thead><tr><th>业务日</th><th>表</th><th>头栏水印数</th><th>运行码实数</th><th>差量</th><th>形态</th><th>操作</th></tr></thead>
        <tbody>
          ${headers
            .map(
              (h) => `<tr class="${h.drifted ? "row-drift" : ""}">
            <td>${h.biz_date}</td>
            <td>${h.table_name}</td>
            <td>${h.declared_stamped}</td>
            <td>${h.actual_stamped}</td>
            <td class="${h.delta !== 0 ? "delta-bad" : "delta-ok"}">${h.delta > 0 ? "+" : ""}${h.delta}</td>
            <td>${driftChip(h)}${h.last_mode ? modeChip(h.last_mode) : ""}</td>
            <td class="cell-actions">
              <button type="button" class="ghost" data-snapshot="${h.header_id}">冻结快照</button>
              <button type="button" data-correct-freeze="${h.header_id}" ${snapshotMap[h.header_id] ? "" : "disabled"}>冻结纠偏</button>
              <button type="button" data-correct-recount="${h.header_id}">重数纠偏</button>
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <p class="muted" id="snapshot-hint">${snapshotHintText()}</p>

      <h3 style="margin-top:1.2rem">回放队列（纠偏留证）</h3>
      <table>
        <thead><tr><th>编号</th><th>头栏</th><th>形态</th><th>纠偏前</th><th>纠偏后</th><th>当时实数</th><th>差量</th><th>操作人</th><th>时间</th><th></th></tr></thead>
        <tbody>
          ${records
            .map(
              (r) => {
                const h = headers.find((x) => x.header_id === r.header_id);
                return `<tr>
              <td>${r.id}</td>
              <td>${h ? h.biz_date + " " + h.table_name : r.header_id}</td>
              <td>${modeChip(r.mode)}</td>
              <td>${r.before_declared}</td>
              <td>${r.after_declared}</td>
              <td>${r.actual_count}</td>
              <td class="${r.delta !== 0 ? "delta-bad" : "delta-ok"}">${r.delta > 0 ? "+" : ""}${r.delta}</td>
              <td>${r.operator}</td>
              <td>${r.created_at}</td>
              <td><button type="button" class="ghost" data-replay="${r.id}">两轮回放</button></td>
            </tr>`;
              }
            )
            .join("")}
        </tbody>
      </table>

      <h3 style="margin-top:1.2rem">回放留证</h3>
      <table>
        <thead><tr><th>编号</th><th>纠偏单</th><th>轮次</th><th>结果</th><th>口径数</th><th>现场实数</th><th>头栏现值</th><th>说明</th></tr></thead>
        <tbody>
          ${replayLogs
            .map(
              (l) => `<tr class="${l.consistent ? "" : "row-drift"}">
            <td>${l.id}</td>
            <td>#${l.record_id}</td>
            <td>第${l.round_no}轮</td>
            <td>${l.consistent ? '<span class="chip chip-ok">一致</span>' : '<span class="chip chip-drift">漂移</span>'}</td>
            <td>${l.basis_count}</td>
            <td>${l.live_actual}</td>
            <td>${l.header_declared}</td>
            <td>${l.message}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>

      <h3 style="margin-top:1.2rem">缺口卡片</h3>
      <table>
        <thead><tr><th>业务日</th><th>表</th><th>头栏申报</th><th>实数</th><th>差量</th><th>缺口</th><th>状态</th></tr></thead>
        <tbody>
          ${gapCardList
            .map(
              (c) => `<tr class="${c.blocked ? "row-drift" : ""}">
            <td>${c.biz_date}</td>
            <td>${c.table_name}</td>
            <td>${c.declared_stamped}</td>
            <td>${c.actual_stamped}</td>
            <td class="${c.delta !== 0 ? "delta-bad" : "delta-ok"}">${c.delta > 0 ? "+" : ""}${c.delta}</td>
            <td>${c.gap_count}</td>
            <td>${c.blocked ? '<span class="chip chip-drift">阻断</span>' : '<span class="chip chip-ok">通行</span>'}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function snapshotHintText() {
    const keys = Object.keys(snapshotMap);
    if (!keys.length) return "未冻结任何快照：先点「冻结快照」，再做冻结纠偏；重数纠偏无需快照。";
    return keys
      .map((k) => {
        const s = snapshotMap[k];
        const h = headers.find((x) => String(x.header_id) === String(k));
        return h
          ? `${h.biz_date} ${h.table_name} 冻住头栏 ${s.frozen_declared}/实数 ${s.frozen_actual}`
          : "";
      })
      .filter(Boolean)
      .join("；");
  }

  function renderBatchDetailBox(batchId) {
    const box = document.getElementById("batch-detail-box");
    if (!box) return;
    const d = detailCache[batchId];
    if (!d) {
      box.innerHTML = "";
      return;
    }
    const b = d.batch;
    const h = d.header;
    box.innerHTML = `
      <div class="cardish">
        <h3>批次详情 · ${b.run_code}</h3>
        <p>${b.table_name} · 业务日 ${b.biz_date} · 状态 ${b.status} · 水印 ${b.watermark_value || "-"}</p>
        ${
          h
            ? `<p>所属批头栏 #${h.header_id}：头栏水印数 <strong>${h.declared_stamped}</strong>，
               运行码实数 <strong>${h.actual_stamped}</strong>，
               ${driftChip(h)}（差量 ${h.delta > 0 ? "+" : ""}${h.delta}，版本 v${h.version_no}）</p>`
            : `<p class="muted">该批次暂无所属批头栏</p>`
        }
      </div>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else if (tab === "reconcile") view.innerHTML = renderReconcile();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.detail);
        setMsg("");
        try {
          detailCache[id] = await api("/api/batches/" + id);
          render();
          renderBatchDetailBox(id);
          setMsg("批次详情已载入，头栏与实数同查");
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          if (tab === "gaps") await refreshReconcile();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    }

    // 核对台交互
    view.querySelectorAll("[data-snapshot]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.snapshot);
        setMsg("");
        try {
          const j = await api(`/api/reconcile/headers/${id}/snapshot`, {
            method: "POST",
            body: JSON.stringify({}),
          });
          snapshotMap[id] = j.snapshot;
          setMsg(
            `已冻结：头栏 ${j.snapshot.frozen_declared}，实数 ${j.snapshot.frozen_actual}（快照 #${j.snapshot.snapshot_id}）`
          );
          render();
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    });
    view.querySelectorAll("[data-correct-freeze]").forEach((btn) => {
      btn.onclick = () => runCorrect(Number(btn.dataset.correctFreeze), "冻结");
    });
    view.querySelectorAll("[data-correct-recount]").forEach((btn) => {
      btn.onclick = () => runCorrect(Number(btn.dataset.correctRecount), "重数");
    });
    view.querySelectorAll("[data-replay]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.replay);
        setMsg("");
        try {
          const j = await api(`/api/reconcile/records/${id}/replay`, {
            method: "POST",
            body: JSON.stringify({}),
          });
          await refreshReconcile();
          render();
          setMsg(j.replay.message, !j.replay.consistent);
        } catch (e) {
          setMsg(e.message, true);
        }
      };
    });
  }

  async function runCorrect(headerId, mode) {
    setMsg("");
    const payload = { mode };
    if (mode === "冻结") {
      const snap = snapshotMap[headerId];
      if (!snap) {
        setMsg("请先冻结快照再做冻结纠偏", true);
        return;
      }
      payload.snapshot_id = snap.snapshot_id;
    }
    try {
      const j = await api(`/api/reconcile/headers/${headerId}/correct`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const r = j.result;
      delete snapshotMap[headerId];
      await refreshReconcile();
      render();
      const tail =
        mode === "冻结"
          ? r.aligned_live
            ? "冻结口径与现场实数一致"
            : "按冻住实数对齐（现场行已变化，与最新实数仍有差）"
          : "按提交瞬间重数对齐";
      setMsg(
        `纠偏落库成功：${r.before_declared} → ${r.after_declared}，差量 ${r.delta > 0 ? "+" : ""}${r.delta}，${tail}`
      );
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = async () => {
      tab = btn.dataset.tab;
      syncDock();
      if (tab === "reconcile" || tab === "gaps") {
        try {
          await refreshReconcile();
        } catch (e) {
          setMsg(e.message, true);
        }
      }
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message, true));
})();
