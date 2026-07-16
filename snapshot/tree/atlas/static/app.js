/* Atlas mobile UI. Session-cookie auth (HttpOnly; JS never sees the token). */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = { view: "chat", selected: null, mode: "open", timer: null, busy: false };

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) { show("login"); throw new Error("Login required"); }
  let data = null;
  try { data = await response.json(); } catch (e) { /* non-JSON */ }
  if (!response.ok) throw new Error((data && data.detail) || `Request failed (${response.status})`);
  return data;
}

function show(view) {
  state.view = view;
  for (const name of ["login", "chat", "settings"]) {
    $(`view-${name}`).classList.toggle("hidden", name !== view);
  }
  $("btnLogout").classList.toggle("hidden", view === "login" || state.mode === "open");
  if (view !== "chat" && state.timer) { clearInterval(state.timer); state.timer = null; }
  if (view === "chat" && !state.timer) state.timer = setInterval(refresh, 2500);
}

/* ---------------------------------------------------------------- auth */
async function boot() {
  try {
    const status = await api("/auth/status");
    state.mode = status.mode;
    $("mode").textContent = status.mode === "open" ? "open dev mode" : "";
    if (!status.authenticated) { show("login"); return; }
    await loadAgents();
    show("chat");
    refresh();
  } catch (e) { /* show() already switched to login on 401 */ }
}
async function login() {
  $("loginError").textContent = "";
  try {
    await api("/auth/login", { method: "POST", body: JSON.stringify({ secret: $("loginSecret").value }) });
    $("loginSecret").value = "";
    await loadAgents();
    show("chat");
    refresh();
  } catch (e) { $("loginError").textContent = e.message; }
}
async function logout() {
  try { await api("/auth/logout", { method: "POST" }); } catch (e) {}
  show("login");
}

/* ---------------------------------------------------------------- chat */
async function loadAgents() {
  const agents = await api("/api/agents");
  $("target").innerHTML = `<option value="auto">Auto</option>` + agents
    .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
}
async function send() {
  if (state.busy) return;
  $("sendError").textContent = "";
  state.busy = true; $("btnSend").textContent = "Sending…";
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ target_id: $("target").value, message: $("message").value }),
    });
    $("message").value = "";
    state.selected = run.id;
    render(run);
  } catch (e) { $("sendError").textContent = e.message; }
  state.busy = false; $("btnSend").textContent = "Send";
}
async function answer(interactionId, value) {
  try {
    const run = await api(`/api/runs/${state.selected}/answer`, {
      method: "POST",
      body: JSON.stringify({ interaction_id: interactionId, answer: value }),
    });
    render(run);
  } catch (e) { alert(e.message); }
}
async function refresh() {
  if (state.view !== "chat") return;
  try {
    const runs = await api("/api/runs");
    $("runList").innerHTML = runs.map((r) => `
      <div class="run" data-id="${esc(r.id)}">
        <span class="badge ${esc(r.state)}">${esc(r.state)}</span>
        <div class="t"><div>${esc(r.target_id)}</div>
        <div class="muted">${esc(r.input && r.input.message)}</div></div>
      </div>`).join("") || `<div class="run muted">No runs yet.</div>`;
    if (state.selected) render(await api(`/api/runs/${state.selected}`));
  } catch (e) { /* login view already shown on 401 */ }
}

const CHIP_EVENTS = {
  "tool.started": (p) => `tool ${p.tool_id || ""}`,
  "tool.completed": (p) => `✓ ${p.tool_id || "tool"}`,
  "tool.denied": (p) => `denied ${p.tool_id || ""}`,
  "tool.failed": (p) => `failed ${p.tool_id || ""}`,
  "agent.model_called": (p) => `model ${p.provider || ""}${p.cost_usd != null ? ` $${p.cost_usd}` : ""}`,
  "agent.fallback_used": (p) => `fallback → ${p.to_provider || ""}`,
  "run.repaired": () => "repaired after restart",
  "run.recovered_orphan": () => "recovered orphan",
};

function render(run) {
  const parts = [];
  const agentLabel = run.effective_agent_id && run.effective_agent_id !== run.target_id
    ? `${esc(run.target_id)} \u2192 ${esc(run.effective_agent_id)}` : esc(run.target_id);
  parts.push(`<div class="bubble system"><span class="badge ${esc(run.state)}">${esc(run.state)}</span> ${agentLabel}</div>`);
  parts.push(`<div class="bubble user">${esc(run.input && run.input.message)}</div>`);
  const chips = (run.events || [])
    .filter((e) => CHIP_EVENTS[e.type])
    .map((e) => {
      const cls = e.type.includes("denied") ? "denied" : e.type.includes("failed") ? "failed" : "";
      return `<span class="chip ${cls}">${esc(CHIP_EVENTS[e.type](e.payload || {}))}</span>`;
    });
  if (chips.length) parts.push(`<div class="chips">${chips.join("")}</div>`);
  const interaction = run.interaction;
  if (interaction && interaction.kind === "approval") {
    const proposed = interaction.payload && interaction.payload.proposed_action;
    parts.push(`<div class="interact">
      <strong>${esc(interaction.prompt)}</strong>
      <pre>${esc(JSON.stringify(proposed, null, 2))}</pre>
      <div class="approve-row">
        <button data-approve="${esc(interaction.id)}">Approve</button>
        <button class="secondary" data-reject="${esc(interaction.id)}">Reject</button>
      </div></div>`);
  } else if (interaction) {
    parts.push(`<div class="interact">
      <strong>${esc(interaction.prompt)}</strong>
      <div class="row" style="margin-top:10px">
        <input id="answerInput" placeholder="Your answer" aria-label="Answer">
        <button data-answer="${esc(interaction.id)}">Send</button>
      </div></div>`);
  }
  if (run.state === "COMPLETED" && run.output && run.output.message != null) {
    parts.push(`<div class="bubble assistant">${esc(run.output.message)}</div>`);
  }
  if (run.state === "FAILED") {
    parts.push(`<div class="bubble error">Run failed: ${esc(run.output && run.output.error)}</div>`);
  }
  if (run.state === "CANCELLED") parts.push(`<div class="bubble system">Cancelled.</div>`);
  parts.push(`<details><summary>Raw run state</summary><pre>${esc(JSON.stringify(run, null, 2))}</pre></details>`);
  $("thread").innerHTML = parts.join("");
}

/* ------------------------------------------------------------- settings */
async function openSettings() {
  show("settings");
  $("settingsError").textContent = "";
  try {
    const providers = await api("/api/settings/providers");
    const a = providers.anthropic || {};
    $("anthropicStatus").textContent = providers.secrets_enabled
      ? (a.configured ? `configured (…${a.last4})` : "not configured")
      : "set ATLAS_SECRET_KEY to enable encrypted settings";
    const oc = providers.openai_compat || {};
    $("openaiStatus").textContent = oc.configured
      ? `configured (…${oc.last4}) — ${oc.base_url || ""}` : (oc.base_url ? `${oc.base_url} (no key)` : "not configured");
    if (oc.base_url) $("openaiBase").value = oc.base_url;
    if (oc.model) $("openaiModel").value = oc.model;
    const profiles = await api("/api/settings/profiles");
    $("profileList").innerHTML = profiles.profiles.map((p) => `
      <div class="profile-row"><span>${esc(p.id)} → ${esc(p.provider)} ${esc(p.model)}</span>
      <button data-delprofile="${esc(p.id)}">Delete</button></div>`).join("");
    const active = await api("/api/settings/active-profile");
    $("activeProfile").innerHTML = profiles.profiles.map((p) =>
      `<option value="${esc(p.id)}"${p.id === active.active_profile ? " selected" : ""}>${esc(p.id)} (${esc(p.provider)})</option>`
    ).join("");
    $("activeProfileStatus").textContent = `active: ${active.active_profile}`;
    const ready = await fetch("/ready", { credentials: "same-origin" });
    $("diag").textContent = JSON.stringify(await ready.json(), null, 2);
  } catch (e) { $("settingsError").textContent = e.message; }
}
async function saveAnthropic() {
  try {
    await api("/api/settings/providers/anthropic", {
      method: "PUT", body: JSON.stringify({ api_key: $("anthropicKey").value }),
    });
    $("anthropicKey").value = "";
    openSettings();
  } catch (e) { $("settingsError").textContent = e.message; }
}
async function saveOpenai() {
  try {
    await api("/api/settings/providers/openai_compat", {
      method: "PUT",
      body: JSON.stringify({
        base_url: $("openaiBase").value,
        model: $("openaiModel").value,
        api_key: $("openaiKey").value || null,
      }),
    });
    $("openaiKey").value = "";
    openSettings();
  } catch (e) { $("settingsError").textContent = e.message; }
}
async function saveProfile() {
  const num = (id) => ($(id).value.trim() === "" ? null : Number($(id).value));
  // The profile id lives in the URL PATH only (M1.4 §2): ProfileBody uses
  // extra="forbid", so an `id` key in the JSON body is a 422.
  const profileId = $("pfId").value.trim();
  const profile = {
    provider: $("pfProvider").value.trim(),
    model: $("pfModel").value.trim(),
    max_tokens: num("pfMaxTokens") ?? 1024,
    cost_budget_usd: num("pfBudget"),
    input_cost_per_mtok: num("pfInCost"),
    output_cost_per_mtok: num("pfOutCost"),
    fallback: $("pfFallback").value.trim() || null,
  };
  try {
    await api(`/api/settings/profiles/${encodeURIComponent(profileId)}`, {
      method: "PUT", body: JSON.stringify(profile),
    });
    openSettings();
  } catch (e) { $("settingsError").textContent = e.message; }
}

/* --------------------------------------------------------------- wiring */
document.addEventListener("click", (event) => {
  const t = event.target.closest("[data-id],[data-answer],[data-approve],[data-reject],[data-delprofile]");
  if (!t) return;
  if (t.dataset.id) { state.selected = t.dataset.id; $("runList").classList.add("hidden"); refresh(); }
  if (t.dataset.answer) answer(t.dataset.answer, $("answerInput").value);
  if (t.dataset.approve) answer(t.dataset.approve, true);
  if (t.dataset.reject) answer(t.dataset.reject, false);
  if (t.dataset.delprofile) {
    api(`/api/settings/profiles/${encodeURIComponent(t.dataset.delprofile)}`, { method: "DELETE" })
      .then(openSettings).catch((e) => { $("settingsError").textContent = e.message; });
  }
});
$("btnLogin").addEventListener("click", login);
$("loginSecret").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("btnLogout").addEventListener("click", logout);
$("btnSend").addEventListener("click", send);
$("btnRuns").addEventListener("click", () => $("runList").classList.toggle("hidden"));
$("btnSettings").addEventListener("click", openSettings);
$("btnBack").addEventListener("click", () => { show("chat"); refresh(); });
async function testProvider(name, out) {
  $(out).textContent = "testing…";
  try {
    const r = await api(`/api/settings/providers/${name}/test`, { method: "POST" });
    $(out).textContent = r.ok
      ? `ok — ${r.snippet || r.finish_reason} (${r.latency_ms} ms)`
      : `failed [${r.category}] ${r.detail}`;
  } catch (e) { $(out).textContent = e.message; }
}
async function removeProviderKey(name, out) {
  try {
    const r = await api(`/api/settings/providers/${name}/key`, { method: "DELETE" });
    $(out).textContent = r.removed ? "key removed" : "no key was stored";
    openSettings();
  } catch (e) { $(out).textContent = e.message; }
}
async function applyActiveProfile() {
  try {
    const r = await api("/api/settings/active-profile", {
      method: "PUT",
      body: JSON.stringify({ profile_id: $("activeProfile").value }),
    });
    $("activeProfileStatus").textContent = `active: ${r.active_profile}`;
  } catch (e) { $("activeProfileStatus").textContent = e.message; }
}
$("btnTestAnthropic").addEventListener("click", () => testProvider("anthropic", "anthropicTest"));
$("btnRemoveAnthropic").addEventListener("click", () => removeProviderKey("anthropic", "anthropicTest"));
$("btnTestOpenai").addEventListener("click", () => testProvider("openai_compat", "openaiTest"));
$("btnRemoveOpenai").addEventListener("click", () => removeProviderKey("openai_compat", "openaiTest"));
$("btnApplyProfile").addEventListener("click", applyActiveProfile);
$("btnSaveAnthropic").addEventListener("click", saveAnthropic);
$("btnSaveOpenai").addEventListener("click", saveOpenai);
$("btnSaveProfile").addEventListener("click", saveProfile);
boot();
