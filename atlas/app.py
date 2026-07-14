from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from atlas.db import Database
from atlas.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.getenv("ATLAS_DB_PATH", str(ROOT / "atlas.db"))
PACKAGES_PATH = Path(os.getenv("ATLAS_PACKAGES_PATH", str(ROOT / "packages")))

db = Database(DB_PATH)
runtime = Runtime(db, PACKAGES_PATH)
app = FastAPI(title="Atlas Engine", version="0.1.0")


class RunCreate(BaseModel):
    target_id: str = Field(default="auto")
    message: str = Field(min_length=1, max_length=10_000)


class InteractionAnswer(BaseModel):
    interaction_id: str
    answer: Any


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "agents": sorted(runtime.registry.agents)}


@app.post("/api/runs")
def create_run(request: RunCreate) -> dict[str, Any]:
    try:
        run_id = runtime.create_run(request.target_id, request.message)
        return run_detail(run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    try:
        run = db.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run["events"] = db.get_events(run_id)
    run["interaction"] = db.pending_interaction(run_id)
    return run


@app.post("/api/runs/{run_id}/answer")
def answer_interaction(run_id: str, request: InteractionAnswer) -> dict[str, Any]:
    try:
        runtime.answer(run_id, request.interaction_id, request.answer)
        return run_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101114">
<title>Atlas Engine</title>
<style>
:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:#101114;color:#f5f5f7}
main{max-width:780px;margin:auto;padding:calc(env(safe-area-inset-top) + 18px) 16px calc(env(safe-area-inset-bottom) + 30px)}
h1{font-size:26px;margin:0 0 4px}.muted{color:#9da2ac}.card{background:#1a1c21;border:1px solid #2b2f37;border-radius:18px;padding:15px;margin:14px 0;box-shadow:0 8px 28px #0004}
textarea,input,select,button{font:inherit;border-radius:12px;border:1px solid #343944;background:#111318;color:#fff;padding:12px;width:100%}
textarea{min-height:92px;resize:vertical}button{background:#5b68ff;border:0;font-weight:700;cursor:pointer}button.secondary{background:#2a2e37}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stack>*+*{margin-top:10px}
.badge{display:inline-block;border-radius:999px;padding:5px 9px;background:#292d36;font-size:12px}.RUNNING{background:#294165}.COMPLETED{background:#1c5b42}.FAILED,.CANCELLED{background:#6a2930}.WAITING_FOR_USER,.WAITING_FOR_APPROVAL{background:#6a4e20}
.event{padding:9px 0;border-bottom:1px solid #292c33;font-size:14px}.event:last-child{border:0}.run{cursor:pointer}.run h3{margin:5px 0;font-size:17px}.answer{margin-top:12px;padding-top:12px;border-top:1px solid #343944}
pre{white-space:pre-wrap;word-break:break-word;background:#111318;padding:12px;border-radius:12px;font-size:12px}.hidden{display:none}
</style>
</head>
<body><main>
<h1>Atlas Engine</h1><div class="muted">Durable agents, tools, questions and approvals.</div>
<section class="card stack">
<select id="target"><option value="auto">Auto / Concierge</option><option value="car-maintenance-agent">Car Maintenance Agent</option></select>
<textarea id="message" placeholder="Example: Log an oil change"></textarea>
<button onclick="startRun()">Start</button>
<div id="error" class="muted"></div>
</section>
<section><h2>Runs</h2><div id="runs"></div></section>
<section id="detail" class="card hidden"></section>
</main>
<script>
let selected=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options});const j=await r.json();if(!r.ok)throw new Error(j.detail||'Request failed');return j}
async function startRun(){error.textContent='';try{const r=await api('/api/runs',{method:'POST',body:JSON.stringify({target_id:target.value,message:message.value})});message.value='';selected=r.id;await refresh()}catch(e){error.textContent=e.message}}
async function answer(id,value){try{await api('/api/runs/'+selected+'/answer',{method:'POST',body:JSON.stringify({interaction_id:id,answer:value})});await refresh()}catch(e){alert(e.message)}}
function runCard(r){return `<div class="card run" onclick="selectRun('${r.id}')"><span class="badge ${r.state}">${esc(r.state)}</span><h3>${esc(r.target_id)}</h3><div class="muted">${esc(r.input.message)}</div></div>`}
async function selectRun(id){selected=id;await refresh()}
function interactionHtml(i){if(!i)return'';if(i.kind==='approval')return `<div class="answer stack"><strong>${esc(i.prompt)}</strong><div class="row"><button onclick="answer('${i.id}',true)">Approve</button><button class="secondary" onclick="answer('${i.id}',false)">Reject</button></div></div>`;return `<div class="answer stack"><strong>${esc(i.prompt)}</strong><input id="answerInput" inputmode="numeric" placeholder="142300"><button onclick="answer('${i.id}',answerInput.value)">Submit</button></div>`}
async function refresh(){const list=await api('/api/runs');runs.innerHTML=list.map(runCard).join('')||'<div class="muted">No runs yet.</div>';if(selected){const r=await api('/api/runs/'+selected);detail.classList.remove('hidden');detail.innerHTML=`<span class="badge ${r.state}">${esc(r.state)}</span><h2>${esc(r.target_id)}</h2><div>${esc(r.output?.message||r.input.message)}</div>${interactionHtml(r.interaction)}<h3>Activity</h3>${r.events.slice().reverse().map(e=>`<div class="event"><b>${esc(e.type)}</b><br><span class="muted">${esc(e.occurred_at)}</span></div>`).join('')}<details><summary>Raw state</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details>`}}
refresh();setInterval(refresh,2000);
</script></body></html>
"""
