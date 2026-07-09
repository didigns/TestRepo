// Interactive Novel Maker — T2 WASM plugin.
// Runs entirely inside the host WASM sandbox. The ONLY host functions it can
// call are the mediated capabilities imported below (ui/generate/storage/
// export/preview). There is NO network import, so this plugin — like any T2
// plugin — cannot exfiltrate data. Its UI is host-rendered component trees;
// its logic lives here in WASM.
import { JSON } from "assemblyscript-json/assembly";

// ---- host imports (mediated capabilities) ----
declare function host_ui_render(ptr: usize, len: i32): void;
declare function host_generate(reqId: i32, ptr: usize, len: i32): void;
declare function host_storage_save(ptr: usize, len: i32): void;
declare function host_storage_delete(ptr: usize, len: i32): void;
declare function host_export(ptr: usize, len: i32): void;
declare function host_preview(ptr: usize, len: i32): void;
declare function host_toast(ptr: usize, len: i32): void;
declare function host_log(ptr: usize, len: i32): void;

// ---- string marshaling (UTF-8 over linear memory) ----
export function alloc(size: i32): usize { return heap.alloc(size); }
export function dealloc(ptr: usize, size: i32): void { heap.free(ptr); }
function readStr(ptr: usize, len: i32): string { return String.UTF8.decodeUnsafe(ptr, len, false); }
function send(fn: i32, s: string): void {
  const buf = String.UTF8.encode(s, false);
  const ptr = changetype<usize>(buf);
  const len = buf.byteLength;
  if (fn == 0) host_ui_render(ptr, len);
  else if (fn == 2) host_storage_save(ptr, len);
  else if (fn == 3) host_export(ptr, len);
  else if (fn == 4) host_preview(ptr, len);
  else if (fn == 5) host_toast(ptr, len);
  else if (fn == 6) host_log(ptr, len);
  else if (fn == 7) host_storage_delete(ptr, len);
}
function sendGen(reqId: i32, s: string): void {
  const buf = String.UTF8.encode(s, false);
  host_generate(reqId, changetype<usize>(buf), buf.byteLength);
}
function toast(s: string): void { send(5, s); }
function log(s: string): void { send(6, s); }

// ---- global state ----
let projects: JSON.Obj = new JSON.Obj();   // id -> project Obj
let cur: JSON.Obj | null = null;
let curId: string = "";
let tab: string = "story";
let draftReqId: i32 = 1;
let pendingDraftStats: string = "";        // stats JSON captured for a draft run
let dTone: string = "";
let dPremise: string = "";
let dStats: string = "";

// ---- JSON helpers ----
function esc(s: string): string {
  let o = "";
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c == 0x22) o += "\\\"";
    else if (c == 0x5c) o += "\\\\";
    else if (c == 0x0a) o += "\\n";
    else if (c == 0x0d) o += "\\r";
    else if (c == 0x09) o += "\\t";
    else o += String.fromCharCode(c);
  }
  return o;
}
function q(s: string): string { return "\"" + esc(s) + "\""; }
function gs(o: JSON.Obj, k: string, d: string): string {
  const v = o.getString(k); if (v != null) return v.valueOf();
  const n = o.getNum(k); if (n != null) return fmtNum(n.valueOf());
  const i = o.getInteger(k); if (i != null) return i.valueOf().toString();
  return d;
}
function fmtNum(f: f64): string {
  if (f == Math.floor(f) && Math.abs(f) < 1e15) return (i64(f)).toString();
  return f.toString();
}
function num(o: JSON.Obj, k: string, d: f64): f64 {
  const n = o.getNum(k); if (n != null) return n.valueOf();
  const i = o.getInteger(k); if (i != null) return f64(i.valueOf());
  const s = o.getString(k); if (s != null) { const p = parseFloat(s.valueOf()); if (!isNaN(p)) return p; }
  return d;
}

// ---- lifecycle ----
export function on_init(ptr: usize, len: i32): void {
  const raw = readStr(ptr, len);
  const root = <JSON.Obj>JSON.parse(raw);
  const store = root.getObj("storage");
  projects = new JSON.Obj();
  if (store != null) {
    const keys = store.keys;
    for (let i = 0; i < keys.length; i++) {
      const v = store.getObj(keys[i]);
      if (v != null) projects.set(keys[i], v);
    }
  }
  cur = null; curId = "";
  render();
}

// ---- event dispatch ----
export function on_event(hp: usize, hl: i32, pp: usize, pl: i32): void {
  const h = readStr(hp, hl);
  const payload = pl > 0 ? readStr(pp, pl) : "";
  const parts = h.split(":");
  const cmd = parts[0];

  if (cmd == "new") { newProject(); render(); return; }
  if (cmd == "open") { openProject(parts[1]); render(); return; }
  if (cmd == "del") { delProject(parts[1]); render(); return; }
  if (cmd == "tab") { tab = parts[1]; render(); return; }
  if (cmd == "save") { save(); toast("저장됨"); render(); return; }
  if (cmd == "preview") { preview(); return; }
  if (cmd == "exportPlugin") { exportPlugin(); return; }
  if (cmd == "draftOpen") { dTone = ""; dPremise = ""; dStats = ""; tab = "draft"; render(); return; }
  if (cmd == "dr") { if (parts[1] == "tone") dTone = payload; else if (parts[1] == "premise") dPremise = payload; else if (parts[1] == "stats") dStats = payload; return; }
  if (cmd == "draftGo") { draftRunFromFields(); return; }

  if (cur == null) return;
  const c = <JSON.Obj>cur;

  if (cmd == "meta") { c.set(parts[1], JSON.Value.String(payload)); return; }
  if (cmd == "addScene") { addScene(); render(); return; }
  if (cmd == "delScene") { delScene(parts[1]); render(); return; }
  if (cmd == "setStart") { c.set("start", JSON.Value.String(parts[1])); render(); return; }
  if (cmd == "setType") { setType(parts[1], payload); render(); return; }
  if (cmd == "field") { setField(parts[1], parts[2], payload); return; }
  if (cmd == "cond") { setCond(parts[1], payload); return; }
  if (cmd == "addOpt") { addOpt(parts[1]); render(); return; }
  if (cmd == "delOpt") { delOpt(parts[1], parseI(parts[2])); render(); return; }
  if (cmd == "optField") { optField(parts[1], parseI(parts[2]), parts[3], payload); return; }
  if (cmd == "addEff") { addEff(parts[1], parseI(parts[2])); render(); return; }
  if (cmd == "delEff") { delEff(parts[1], parseI(parts[2]), parts[3]); render(); return; }
  if (cmd == "effStat") { effStat(parts[1], parseI(parts[2]), parts[3], payload); render(); return; }
  if (cmd == "effDelta") { effDelta(parts[1], parseI(parts[2]), parts[3], payload); return; }
  if (cmd == "addStat") { addStat(); render(); return; }
  if (cmd == "delStat") { delStat(parseI(parts[1])); render(); return; }
  if (cmd == "statField") { statField(parseI(parts[1]), parts[2], payload); return; }
}
function parseI(s: string): i32 { return i32(parseInt(s, 10)); }

// ---- generate result ----
export function on_generate_done(reqId: i32, ptr: usize, len: i32): void {
  const raw = readStr(ptr, len);
  assembleDraft(raw);
  render();
}

// =================== project operations ===================
function scenesObj(): JSON.Obj { const c = <JSON.Obj>cur; let s = c.getObj("scenes"); if (s == null) { s = new JSON.Obj(); c.set("scenes", s); } return <JSON.Obj>s; }
function orderArr(): JSON.Arr { const c = <JSON.Obj>cur; let a = c.getArr("order"); if (a == null) { a = new JSON.Arr(); c.set("order", a); } return <JSON.Arr>a; }
function statsArr(): JSON.Arr { const c = <JSON.Obj>cur; let a = c.getArr("stats"); if (a == null) { a = new JSON.Arr(); c.set("stats", a); } return <JSON.Arr>a; }
function orderIds(): string[] {
  const a = orderArr(); const sc = scenesObj(); const out = new Array<string>();
  const arr = a.valueOf();
  for (let i = 0; i < arr.length; i++) { const s = (<JSON.Str>arr[i]).valueOf(); if (sc.has(s)) out.push(s); }
  return out;
}
function freeSceneId(): string { const sc = scenesObj(); let n = 1; while (sc.has("s" + n.toString())) n++; return "s" + n.toString(); }

function textScene(txt: string, next: string): JSON.Obj { const o = new JSON.Obj(); o.set("type", JSON.Value.String("text")); o.set("text", JSON.Value.String(txt)); o.set("next", JSON.Value.String(next)); return o; }
function endScene(): JSON.Obj { const o = new JSON.Obj(); o.set("type", JSON.Value.String("end")); return o; }

function newProject(): void {
  let n = 1; while (projects.has("nv" + n.toString())) n++;
  const id = "nv" + n.toString();
  const p = new JSON.Obj();
  p.set("id", JSON.Value.String(id));
  p.set("title", JSON.Value.String("새 인터랙티브 소설"));
  p.set("author", JSON.Value.String(""));
  p.set("description", JSON.Value.String(""));
  p.set("persona", JSON.Value.String("너는 몰입감 있는 인터랙티브 소설의 진행자야. 2인칭 시점, 생생하고 절제된 한국어 묘사. 장면은 짧게 써라."));
  const stats = new JSON.Arr(); p.set("stats", stats);
  const scenes = new JSON.Obj(); scenes.set("s1", textScene("이야기가 여기서 시작됩니다…", "end")); scenes.set("end", endScene());
  p.set("scenes", scenes);
  const order = new JSON.Arr(); order.push(JSON.Value.String("s1")); order.push(JSON.Value.String("end")); p.set("order", order);
  p.set("start", JSON.Value.String("s1"));
  projects.set(id, p);
  cur = p; curId = id; tab = "story";
}
function openProject(id: string): void { const p = projects.getObj(id); if (p != null) { cur = p; curId = id; tab = "story"; } }
function delProject(id: string): void {
  if (projects.has(id)) { const np = new JSON.Obj(); const ks = projects.keys; for (let i = 0; i < ks.length; i++) if (ks[i] != id) np.set(ks[i], <JSON.Value>projects.get(ks[i])); projects = np; }
  // remove from storage
  send(7, "{" + q("key") + ":" + q(id) + "}");
  if (curId == id) { cur = null; curId = ""; }
}

function addScene(): void { const id = freeSceneId(); const sc = scenesObj(); sc.set(id, textScene("", "")); orderArr().push(JSON.Value.String(id)); const c = <JSON.Obj>cur; if (gs(c, "start", "") == "") c.set("start", JSON.Value.String(id)); }
function delScene(id: string): void {
  const c = <JSON.Obj>cur; const sc = scenesObj();
  const nsc = new JSON.Obj(); const ks = sc.keys;
  for (let i = 0; i < ks.length; i++) if (ks[i] != id) nsc.set(ks[i], <JSON.Value>sc.get(ks[i]));
  c.set("scenes", nsc);
  const no = new JSON.Arr(); const oa = orderArr().valueOf();
  for (let i = 0; i < oa.length; i++) { const s = (<JSON.Str>oa[i]).valueOf(); if (s != id) no.push(JSON.Value.String(s)); }
  c.set("order", no);
  // sweep dangling refs
  const ids = nsc.keys;
  for (let i = 0; i < ids.length; i++) {
    const s = <JSON.Obj>nsc.getObj(ids[i]);
    if (s == null) continue;
    if (gs(s, "next", "") == id) s.set("next", JSON.Value.String(""));
    if (gs(s, "then", "") == id) s.set("then", JSON.Value.String(""));
    if (gs(s, "else", "") == id) s.set("else", JSON.Value.String(""));
    const opts = s.getArr("options");
    if (opts != null) { const oa2 = opts.valueOf(); for (let k = 0; k < oa2.length; k++) { const op = <JSON.Obj>oa2[k]; if (gs(op, "goto", "") == id) op.set("goto", JSON.Value.String("")); } }
  }
  if (gs(c, "start", "") == id) { const oi = orderIds(); c.set("start", JSON.Value.String(oi.length > 0 ? oi[0] : "")); }
}
function setType(id: string, t: string): void {
  const sc = scenesObj(); const old = sc.getObj(id); const next = old != null ? gs(<JSON.Obj>old, "next", "") : "";
  const o = new JSON.Obj(); o.set("type", JSON.Value.String(t));
  if (t == "text") { o.set("text", JSON.Value.String("")); o.set("next", JSON.Value.String(next)); }
  else if (t == "generate") { o.set("prompt", JSON.Value.String("")); o.set("grounded", JSON.Value.Bool(false)); o.set("into", JSON.Value.String("scene")); o.set("next", JSON.Value.String(next)); }
  else if (t == "choice") { o.set("prompt", JSON.Value.String("어떻게 할까?")); o.set("options", new JSON.Arr()); }
  else if (t == "input") { o.set("label", JSON.Value.String("")); o.set("into", JSON.Value.String("name")); o.set("next", JSON.Value.String(next)); }
  else if (t == "if") { const cn = new JSON.Obj(); cn.set("expr", JSON.Value.String("")); o.set("cond", cn); o.set("then", JSON.Value.String("")); o.set("else", JSON.Value.String("")); }
  sc.set(id, o);
}
function setField(id: string, f: string, v: string): void { const s = scenesObj().getObj(id); if (s != null) (<JSON.Obj>s).set(f, JSON.Value.String(v)); }
function setCond(id: string, v: string): void { const s = scenesObj().getObj(id); if (s != null) { const cn = new JSON.Obj(); cn.set("expr", JSON.Value.String(v)); (<JSON.Obj>s).set("cond", cn); } }
function optsOf(id: string): JSON.Arr { const s = <JSON.Obj>scenesObj().getObj(id); let a = s.getArr("options"); if (a == null) { a = new JSON.Arr(); s.set("options", a); } return <JSON.Arr>a; }
function addOpt(id: string): void { const o = new JSON.Obj(); o.set("label", JSON.Value.String("")); o.set("goto", JSON.Value.String("")); optsOf(id).push(o); }
function delOpt(id: string, i: i32): void { const s = <JSON.Obj>scenesObj().getObj(id); const oa = optsOf(id).valueOf(); const na = new JSON.Arr(); for (let k = 0; k < oa.length; k++) if (k != i) na.push(oa[k]); s.set("options", na); }
function optField(id: string, i: i32, f: string, v: string): void { const oa = optsOf(id).valueOf(); if (i >= 0 && i < oa.length) (<JSON.Obj>oa[i]).set(f, JSON.Value.String(v)); }
function optSet(id: string, i: i32): JSON.Obj { const oa = optsOf(id).valueOf(); const op = <JSON.Obj>oa[i]; let s = op.getObj("set"); if (s == null) { s = new JSON.Obj(); op.set("set", s); } return <JSON.Obj>s; }
function buildExpr(key: string, delta: f64): string { const sign = delta >= 0 ? "+" : "-"; return key + sign + fmtNum(Math.abs(delta)); }
function firstFreeStat(setObj: JSON.Obj): string { const sa = statsArr().valueOf(); for (let i = 0; i < sa.length; i++) { const k = gs(<JSON.Obj>sa[i], "key", ""); if (k != "" && !setObj.has(k)) return k; } return sa.length > 0 ? gs(<JSON.Obj>sa[0], "key", "") : ""; }
function addEff(id: string, i: i32): void { const set = optSet(id, i); const k = firstFreeStat(set); if (k == "") { toast("스탯 탭에서 먼저 스탯을 추가하세요"); return; } const e = new JSON.Obj(); e.set("expr", JSON.Value.String(buildExpr(k, 0))); set.set(k, e); }
function delEff(id: string, i: i32, k: string): void { const set = optSet(id, i); const ns = new JSON.Obj(); const ks = set.keys; for (let j = 0; j < ks.length; j++) if (ks[j] != k) ns.set(ks[j], <JSON.Value>set.get(ks[j])); const oa = optsOf(id).valueOf(); (<JSON.Obj>oa[i]).set("set", ns); }
function exprDelta(set: JSON.Obj, k: string): f64 { const e = set.getObj(k); if (e == null) return 0; const ex = gs(<JSON.Obj>e, "expr", ""); let n = ""; let started = false; for (let i = ex.length - 1; i >= 0; i--) { const ch = ex.charCodeAt(i); if (ch >= 0x30 && ch <= 0x39) { n = String.fromCharCode(ch) + n; started = true; } else if (started && (ch == 0x2b || ch == 0x2d)) { n = String.fromCharCode(ch) + n; break; } else if (started) break; } const v = parseFloat(n); return isNaN(v) ? 0 : v; }
function effStat(id: string, i: i32, oldk: string, newk: string): void { const set = optSet(id, i); const d = exprDelta(set, oldk); const ns = new JSON.Obj(); const ks = set.keys; for (let j = 0; j < ks.length; j++) if (ks[j] != oldk) ns.set(ks[j], <JSON.Value>set.get(ks[j])); const e = new JSON.Obj(); e.set("expr", JSON.Value.String(buildExpr(newk, d))); ns.set(newk, e); const oa = optsOf(id).valueOf(); (<JSON.Obj>oa[i]).set("set", ns); }
function effDelta(id: string, i: i32, k: string, v: string): void { const set = optSet(id, i); const e = new JSON.Obj(); e.set("expr", JSON.Value.String(buildExpr(k, parseFloat(v)))); set.set(k, e); }

function addStat(): void { const a = statsArr(); const n = a.valueOf().length + 1; const s = new JSON.Obj(); s.set("key", JSON.Value.String("stat" + n.toString())); s.set("label", JSON.Value.String("스탯" + n.toString())); s.set("initial", JSON.Value.Integer(100)); s.set("max", JSON.Value.Integer(100)); s.set("bar", JSON.Value.Bool(true)); s.set("show", JSON.Value.Bool(true)); a.push(s); }
function delStat(i: i32): void { const oa = statsArr().valueOf(); const na = new JSON.Arr(); for (let k = 0; k < oa.length; k++) if (k != i) na.push(oa[k]); (<JSON.Obj>cur).set("stats", na); }
function statField(i: i32, f: string, v: string): void {
  const oa = statsArr().valueOf(); if (i < 0 || i >= oa.length) return; const s = <JSON.Obj>oa[i];
  if (f == "initial") { const n = parseFloat(v); s.set("initial", isNaN(n) ? JSON.Value.String(v) : JSON.Value.Float(n)); }
  else if (f == "max") { if (v == "") s.set("max", JSON.Value.Null()); else s.set("max", JSON.Value.Float(parseFloat(v))); }
  else if (f == "bar") { s.set("bar", JSON.Value.Bool(v == "1")); }
  else if (f == "show") { s.set("show", JSON.Value.Bool(v == "1")); }
  else { s.set(f, JSON.Value.String(v)); }
}

// ---- save / export / preview ----
function save(): void {
  if (cur == null) return; const c = <JSON.Obj>cur;
  const id = gs(c, "id", "");
  send(2, "{" + q("key") + ":" + q(id) + "," + q("value") + ":" + c.stringify() + "}");
  projects.set(id, c);
}
function statusFromStats(): string {
  const sa = statsArr().valueOf(); const rows = new Array<string>();
  for (let i = 0; i < sa.length; i++) { const s = <JSON.Obj>sa[i]; const sh = s.getBool("show"); if (sh != null && !sh.valueOf()) continue;
    let r = "{" + q("label") + ":" + q(gs(s, "label", gs(s, "key", ""))) + "," + q("value") + ":" + q("{{" + gs(s, "key", "") + "}}");
    const bar = s.getBool("bar"); const mx = s.get("max");
    if (bar != null && bar.valueOf() && mx != null && !mx.isNull) r += "," + q("bar") + ":{" + q("max") + ":" + fmtNum(num(s, "max", 100)) + "}";
    r += "}"; rows.push(r); }
  if (rows.length == 0) return "null";
  return "{" + q("title") + ":" + q(gs(<JSON.Obj>cur, "title", "상태")) + "," + q("rows") + ":[" + rows.join(",") + "]}";
}
function preview(): void {
  if (cur == null) return; const c = <JSON.Obj>cur;
  const flow = "{" + q("start") + ":" + q(gs(c, "start", "")) + "," + q("steps") + ":" + scenesObj().stringify() + "}";
  const stats = statsArr().stringify();
  const msg = "{" + q("title") + ":" + q(gs(c, "title", "")) + "," + q("persona") + ":" + q(gs(c, "persona", "")) + "," + q("stats") + ":" + stats + "," + q("flow") + ":" + flow + "}";
  send(4, msg);
}
function exportPlugin(): void {
  if (cur == null) return; save(); const c = <JSON.Obj>cur;
  const title = gs(c, "title", "소설");
  const mode = "{" + q("label") + ":" + q(title) + "," + q("icon") + ":" + q("📖") + "," +
    q("persona") + ":" + q(gs(c, "persona", "")) + "," +
    q("stats") + ":" + statsArr().stringify() + "," +
    q("flow") + ":{" + q("start") + ":" + q(gs(c, "start", "")) + "," + q("steps") + ":" + scenesObj().stringify() + "}";
  let modeEnd = mode;
  const status = statusFromStats();
  if (status != "null") modeEnd += "," + q("status") + ":" + status;
  modeEnd += "}";
  const manifest = "{" + q("manifestVersion") + ":1," + q("id") + ":" + q(gs(c, "id", "novel")) + "," +
    q("name") + ":" + q(title) + "," + q("version") + ":" + q("1.0.0") + "," +
    q("author") + ":" + q(gs(c, "author", "")) + "," + q("description") + ":" + q(gs(c, "description", "")) + "," +
    q("permissions") + ":{" + q("folders") + ":[]," + q("network") + ":false," + q("capabilities") + ":[" + q("model:generate") + "," + q("ui:render") + "]}," +
    q("contributes") + ":{" + q("mode") + ":" + modeEnd + "}}";
  send(3, manifest);
}

// ---- AI draft ----
function draftRunFromFields(): void {
  const premise = dPremise;
  const tone = dTone;
  // build stats array from the comma list
  const keys = new Array<string>();
  const raw = dStats.split(",");
  const statsParts = new Array<string>();
  for (let i = 0; i < raw.length; i++) {
    let k = raw[i].trim(); let clean = "";
    for (let j = 0; j < k.length; j++) { const ch = k.charCodeAt(j); if ((ch >= 0x30 && ch <= 0x39) || (ch >= 0x41 && ch <= 0x5a) || (ch >= 0x61 && ch <= 0x7a) || ch == 0x5f) clean += String.fromCharCode(ch); }
    if (clean.length > 0) { keys.push(clean); statsParts.push("{" + q("key") + ":" + q(clean) + "," + q("label") + ":" + q(clean) + "," + q("initial") + ":100," + q("max") + ":100," + q("bar") + ":true," + q("show") + ":true}"); }
  }
  pendingDraftStats = "[" + statsParts.join(",") + "]";
  let statHint = "";
  if (keys.length > 0) statHint = "\n스탯 변수(effects에서 증감): " + keys.join(", ");
  const prompt =
    "너는 인터랙티브 소설 설계자야. 아래 설정으로 분기형 소설 '구조'를 설계해.\n" +
    "반드시 이 JSON 스키마 하나만 출력(설명·코드펜스 금지):\n" +
    "{\"scenes\":[{\"id\":\"s1\",\"narration\":\"지문\",\"choices\":[{\"label\":\"선택\",\"goto\":\"s2\",\"effects\":[{\"stat\":\"hp\",\"delta\":-10}]}]},{\"id\":\"s2\",\"narration\":\"...\",\"ending\":true}]}\n" +
    "규칙: 장면 6~9개, 첫 id는 s1, 각 장면 narration 필수, 분기 장면은 choices 2~3개, choices[].goto는 존재하는 id, 결말은 choices 없이 ending:true." + statHint +
    "\n\n[설정]\n분위기: " + tone + "\n전제: " + premise + "\n\nJSON:";
  const c = new JSON.Obj();
  c.set("prompt", JSON.Value.String(prompt));
  c.set("persona", JSON.Value.String("Return ONLY valid JSON."));
  draftReqId++;
  toast("AI가 이야기 구조를 설계 중…");
  sendGen(draftReqId, c.stringify());
}
function extractJson(raw: string): string {
  let t = raw;
  const a = t.indexOf("{"); const b = t.lastIndexOf("}");
  if (a >= 0 && b > a) t = t.substring(a, b + 1);
  return t;
}
function looksJson(t: string): bool {
  // cheap guard: must start '{' end '}' with balanced braces (the strict AS
  // parser aborts the whole instance on malformed input, so pre-screen).
  if (t.length < 2) return false;
  if (t.charCodeAt(0) != 0x7b || t.charCodeAt(t.length - 1) != 0x7d) return false;
  let depth = 0; let inStr = false; let escd = false;
  for (let i = 0; i < t.length; i++) { const c = t.charCodeAt(i);
    if (inStr) { if (escd) escd = false; else if (c == 0x5c) escd = true; else if (c == 0x22) inStr = false; continue; }
    if (c == 0x22) inStr = true; else if (c == 0x7b || c == 0x5b) depth++; else if (c == 0x7d || c == 0x5d) depth--;
    if (depth < 0) return false; }
  return depth == 0 && !inStr;
}
function assembleDraft(raw: string): void {
  let parsedOk = true;
  const txt = extractJson(raw);
  const scenes = new JSON.Obj(); const order = new JSON.Arr();
  let arr: JSON.Arr | null = null;
  if (looksJson(txt)) { const root = JSON.parse(txt); if (root instanceof JSON.Obj) arr = (<JSON.Obj>root).getArr("scenes"); }
  const validIds = new Array<string>();
  if (arr != null) { const a = arr.valueOf(); for (let i = 0; i < a.length; i++) { const s = <JSON.Obj>a[i]; validIds.push(gs(s, "id", "s" + (i + 1).toString())); } }
  if (arr != null && arr.valueOf().length > 0) {
    const a = arr.valueOf();
    for (let i = 0; i < a.length; i++) {
      const s = <JSON.Obj>a[i];
      const sid = gs(s, "id", "s" + (i + 1).toString());
      const narr = gs(s, "narration", gs(s, "text", ""));
      const choices = s.getArr("choices");
      const hasChoices = choices != null && choices.valueOf().length > 0;
      const cid = sid + "_c"; const eid = sid + "_end";
      const nxt = hasChoices ? cid : eid;
      scenes.set(sid, textScene(narr.length > 0 ? narr : "…", nxt)); order.push(JSON.Value.String(sid));
      if (hasChoices) {
        const co = new JSON.Obj(); co.set("type", JSON.Value.String("choice")); co.set("prompt", JSON.Value.String(gs(s, "prompt", "어떻게 할까?")));
        const opts = new JSON.Arr(); const ca = (<JSON.Arr>choices).valueOf();
        for (let k = 0; k < ca.length; k++) {
          const ch = <JSON.Obj>ca[k];
          let goto = gs(ch, "goto", "");
          let okId = false; for (let z = 0; z < validIds.length; z++) if (validIds[z] == goto) okId = true;
          if (!okId) goto = "";
          const op = new JSON.Obj(); op.set("label", JSON.Value.String(gs(ch, "label", "선택"))); op.set("goto", JSON.Value.String(goto));
          const effs = ch.getArr("effects");
          if (effs != null) { const ea = effs.valueOf(); const setO = new JSON.Obj(); let any = false;
            for (let e = 0; e < ea.length; e++) { const ef = <JSON.Obj>ea[e]; const st = gs(ef, "stat", ""); const dv = num(ef, "delta", 0); if (st != "" && dv != 0) { const ev = new JSON.Obj(); ev.set("expr", JSON.Value.String(buildExpr(st, dv))); setO.set(st, ev); any = true; } }
            if (any) op.set("set", setO); }
          opts.push(op);
        }
        co.set("options", opts); scenes.set(cid, co); order.push(JSON.Value.String(cid));
      } else { scenes.set(eid, endScene()); order.push(JSON.Value.String(eid)); }
    }
  } else { parsedOk = false; scenes.set("s1", textScene("(AI 초안 생성 실패 — 여기서 직접 편집하세요.)", "end")); scenes.set("end", endScene()); order.push(JSON.Value.String("s1")); order.push(JSON.Value.String("end")); }

  let n = 1; while (projects.has("nv" + n.toString())) n++;
  const id = "nv" + n.toString();
  const p = new JSON.Obj();
  p.set("id", JSON.Value.String(id));
  p.set("title", JSON.Value.String("AI 초안 소설"));
  p.set("author", JSON.Value.String(""));
  p.set("description", JSON.Value.String(""));
  p.set("persona", JSON.Value.String("너는 몰입감 있는 인터랙티브 소설의 진행자야. 2인칭 시점, 생생하고 절제된 한국어 묘사. 장면은 짧게 써라."));
  const st = <JSON.Arr>JSON.parse(pendingDraftStats); p.set("stats", st);
  p.set("scenes", scenes); p.set("order", order);
  const oi = order.valueOf(); p.set("start", JSON.Value.String(oi.length > 0 ? (<JSON.Str>oi[0]).valueOf() : ""));
  projects.set(id, p); cur = p; curId = id; tab = "story";
  toast(parsedOk ? "초안 생성 완료" : "초안 파싱 실패 — 스켈레톤 생성");
}

// =================== UI rendering ===================
function nText(t: string, muted: bool): string { return "{" + q("t") + ":" + q("Text") + "," + q("text") + ":" + q(t) + (muted ? "," + q("muted") + ":true" : "") + "}"; }
function nMD(t: string): string { return "{" + q("t") + ":" + q("MD") + "," + q("text") + ":" + q(t) + "}"; }
function nHr(): string { return "{" + q("t") + ":" + q("Hr") + "}"; }
function nBtn(label: string, ev: string, variant: string): string { return "{" + q("t") + ":" + q("Btn") + "," + q("label") + ":" + q(label) + "," + q("ev") + ":" + q(ev) + (variant != "" ? "," + q("variant") + ":" + q(variant) : "") + "}"; }
function nIn(val: string, ph: string, ev: string, w: string, isNum: bool): string { return "{" + q("t") + ":" + q("In") + "," + q("val") + ":" + q(val) + "," + q("ph") + ":" + q(ph) + "," + q("ev") + ":" + q(ev) + (w != "" ? "," + q("w") + ":" + q(w) : "") + (isNum ? "," + q("num") + ":true" : "") + "}"; }
function nTA(val: string, ph: string, ev: string): string { return "{" + q("t") + ":" + q("TA") + "," + q("val") + ":" + q(val) + "," + q("ph") + ":" + q(ph) + "," + q("ev") + ":" + q(ev) + "}"; }
function nChk(checked: bool, label: string, ev: string): string { return "{" + q("t") + ":" + q("Chk") + "," + q("checked") + ":" + (checked ? "true" : "false") + "," + q("label") + ":" + q(label) + "," + q("ev") + ":" + q(ev) + "}"; }
function nRow(c: string): string { return "{" + q("t") + ":" + q("Row") + "," + q("c") + ":[" + c + "]}"; }
function nCol(c: string): string { return "{" + q("t") + ":" + q("Col") + "," + q("c") + ":[" + c + "]}"; }
function nCard(cls: string, c: string): string { return "{" + q("t") + ":" + q("Card") + "," + q("cls") + ":" + q(cls) + "," + q("c") + ":[" + c + "]}"; }
function nItem(cls: string, ev: string, c: string): string { return "{" + q("t") + ":" + q("Item") + "," + q("cls") + ":" + q(cls) + "," + q("ev") + ":" + q(ev) + "," + q("c") + ":[" + c + "]}"; }
function nSel(val: string, opts: string, ev: string, w: string): string { return "{" + q("t") + ":" + q("Sel") + "," + q("val") + ":" + q(val) + "," + q("opts") + ":[" + opts + "]," + q("ev") + ":" + q(ev) + (w != "" ? "," + q("w") + ":" + q(w) : "") + "}"; }
function opt(v: string, l: string): string { return "{" + q("v") + ":" + q(v) + "," + q("l") + ":" + q(l) + "}"; }

function targetOpts(curVal: string): string {
  const ids = orderIds(); const parts = new Array<string>();
  parts.push(opt("", "(없음 · 끝)"));
  let missing = curVal != "" ? scenesObj().has(curVal) == false : false;
  for (let i = 0; i < ids.length; i++) parts.push(opt(ids[i], ids[i] + " · " + sceneLabel(ids[i])));
  if (missing) parts.push(opt(curVal, "⚠ " + curVal + " (없음)"));
  return parts.join(",");
}
function sceneLabel(id: string): string {
  const s = scenesObj().getObj(id); if (s == null) return "?"; const t = gs(<JSON.Obj>s, "type", "");
  if (t == "text") { const x = gs(<JSON.Obj>s, "text", ""); return "지문" + (x.length > 0 ? " · " + trunc(x, 14) : ""); }
  if (t == "generate") { const x = gs(<JSON.Obj>s, "prompt", ""); return "AI지문" + (x.length > 0 ? " · " + trunc(x, 14) : ""); }
  if (t == "choice") { const o = (<JSON.Obj>s).getArr("options"); return "선택지 · " + (o != null ? o.valueOf().length.toString() : "0") + "개"; }
  if (t == "input") return "입력"; if (t == "if") return "분기"; if (t == "end") return "끝"; return t;
}
function trunc(s: string, n: i32): string { return s.length > n ? s.substring(0, n) : s; }

function typeOpts(cur: string): string {
  return opt("text", "📄 지문(고정)") + "," + opt("generate", "✨ AI 생성 지문") + "," + opt("choice", "🔀 선택지") + "," + opt("input", "⌨ 입력") + "," + opt("if", "⚖ 조건 분기") + "," + opt("end", "🏁 끝");
}

function render(): void { renderSidebar(); renderMain(); }

function renderSidebar(): void {
  const parts = new Array<string>();
  parts.push(nBtn("＋ 새 소설", "new", "primary"));
  parts.push(nBtn("✨ AI로 초안 만들기", "draftOpen", "ai"));
  const ks = projects.keys;
  if (ks.length == 0) parts.push(nText("아직 만든 소설이 없습니다. 새 소설 또는 AI 초안으로 시작하세요.", true));
  for (let i = 0; i < ks.length; i++) {
    const p = <JSON.Obj>projects.getObj(ks[i]);
    const sc = p.getObj("scenes"); const scN = sc != null ? sc.keys.length : 0;
    const stA = p.getArr("stats"); const stN = stA != null ? stA.valueOf().length : 0;
    const active = ks[i] == curId ? " active" : "";
    const inner = nText(gs(p, "title", "(제목 없음)"), false) + "," + nText("장면 " + scN.toString() + " · 스탯 " + stN.toString(), true) + "," + nBtn("삭제", "del:" + ks[i], "mini danger");
    parts.push(nItem("nv-item" + active, "open:" + ks[i], inner));
  }
  send(0, "{" + q("surface") + ":" + q("sidebar") + "," + q("body") + ":[" + parts.join(",") + "]}");
}

function renderMain(): void {
  if (tab == "draft") { send(0, "{" + q("surface") + ":" + q("main") + "," + q("body") + ":[" + draftPanel() + "]}"); return; }
  if (cur == null) { send(0, "{" + q("surface") + ":" + q("main") + "," + q("body") + ":[" + nCard("nv-empty-card", nText("📖 인터랙티브 소설 메이커", false) + "," + nText("왼쪽에서 새 소설을 만들거나 AI 초안으로 시작하세요.", true)) + "]}"); return; }
  const c = <JSON.Obj>cur;
  // toolbar
  const toolbar = nRow(
    nIn(gs(c, "title", ""), "소설 제목", "meta:title", "", false) + "," +
    nBtn("📝 장면", "tab:story", tab == "story" ? "tabon" : "tab") + "," +
    nBtn("🎭 스탯", "tab:stats", tab == "stats" ? "tabon" : "tab") + "," +
    nBtn("⚙️ 설정", "tab:meta", tab == "meta" ? "tabon" : "tab") + "," +
    nBtn("▶ 미리보기", "preview", "") + "," +
    nBtn("💾 저장", "save", "") + "," +
    nBtn("🧩 플러그인으로 설치", "exportPlugin", ""));
  let panel = "";
  if (tab == "story") panel = storyPanel();
  else if (tab == "stats") panel = statsPanel();
  else if (tab == "draft") panel = draftPanel();
  else panel = metaPanel();
  send(0, "{" + q("surface") + ":" + q("main") + "," + q("body") + ":[" + toolbar + "," + nHr() + "," + panel + "]}");
}

function storyPanel(): string {
  const ids = orderIds(); const parts = new Array<string>();
  parts.push(nText("각 카드가 하나의 장면(노드)입니다. 유형을 바꾸고 다음 장면을 연결하세요.", true));
  const start = gs(<JSON.Obj>cur, "start", "");
  for (let i = 0; i < ids.length; i++) parts.push(sceneCard(ids[i], ids[i] == start));
  parts.push(nBtn("＋ 장면 추가", "addScene", "primary"));
  return nCol(parts.join(","));
}
function sceneCard(id: string, isStart: bool): string {
  const s = <JSON.Obj>scenesObj().getObj(id); const t = gs(s, "type", "");
  const head = nRow(
    nText(id, false) + "," +
    (isStart ? nText("● 시작", false) + "," : "") +
    nSel(t, typeOpts(t), "setType:" + id, "auto") + "," +
    (isStart ? "" : nBtn("시작으로", "setStart:" + id, "mini") + ",") +
    nBtn("삭제", "delScene:" + id, "mini danger"));
  let body = "";
  if (t == "text") body = nText("지문 (마크다운)", true) + "," + nTA(gs(s, "text", ""), "", "field:" + id + ":text") + "," + nText("다음 장면", true) + "," + nSel(gs(s, "next", ""), targetOpts(gs(s, "next", "")), "field:" + id + ":next", "");
  else if (t == "generate") body = nText("AI 생성 지시문 ({{변수}} 사용 가능)", true) + "," + nTA(gs(s, "prompt", ""), "", "field:" + id + ":prompt") + "," + nText("결과 저장 변수", true) + "," + nIn(gs(s, "into", "scene"), "", "field:" + id + ":into", "", false) + "," + nText("다음 장면", true) + "," + nSel(gs(s, "next", ""), targetOpts(gs(s, "next", "")), "field:" + id + ":next", "");
  else if (t == "choice") body = choiceBody(id, s);
  else if (t == "input") body = nText("안내 문구", true) + "," + nIn(gs(s, "label", ""), "예: 주인공의 이름을 지어주세요.", "field:" + id + ":label", "", false) + "," + nText("저장 변수", true) + "," + nIn(gs(s, "into", "name"), "", "field:" + id + ":into", "", false) + "," + nText("다음 장면", true) + "," + nSel(gs(s, "next", ""), targetOpts(gs(s, "next", "")), "field:" + id + ":next", "");
  else if (t == "if") { const cn = s.getObj("cond"); const expr = cn != null ? gs(<JSON.Obj>cn, "expr", "") : ""; const keys = statKeyList(); body = nText("조건식 (예: hp<=40 || sanity<=40)", true) + "," + nIn(expr, "hp <= 0", "cond:" + id, "", false) + "," + nText("변수: " + keys, true) + "," + nText("참이면 →", true) + "," + nSel(gs(s, "then", ""), targetOpts(gs(s, "then", "")), "field:" + id + ":then", "") + "," + nText("거짓이면 →", true) + "," + nSel(gs(s, "else", ""), targetOpts(gs(s, "else", "")), "field:" + id + ":else", ""); }
  else body = nText("이야기가 여기서 끝납니다.", true);
  return nCard(isStart ? "nv-scene start" : "nv-scene", head + "," + body);
}
function choiceBody(id: string, s: JSON.Obj): string {
  const parts = new Array<string>();
  parts.push(nText("질문/프롬프트", true)); parts.push(nIn(gs(s, "prompt", ""), "", "field:" + id + ":prompt", "", false));
  parts.push(nText("선택지", true));
  const oa = optsOf(id).valueOf();
  for (let i = 0; i < oa.length; i++) parts.push(optEditor(id, i, <JSON.Obj>oa[i]));
  parts.push(nBtn("＋ 선택지 추가", "addOpt:" + id, "mini"));
  return parts.join(",");
}
function optEditor(id: string, i: i32, o: JSON.Obj): string {
  const parts = new Array<string>();
  parts.push(nRow(nIn(gs(o, "label", ""), "선택지 문구", "optField:" + id + ":" + i.toString() + ":label", "", false) + "," + nSel(gs(o, "goto", ""), targetOpts(gs(o, "goto", "")), "optField:" + id + ":" + i.toString() + ":goto", "") + "," + nBtn("삭제", "delOpt:" + id + ":" + i.toString(), "mini danger")));
  const statList = statKeyList();
  parts.push(nText("스탯 효과" + (statList == "(없음)" ? " (스탯 탭에서 먼저 추가)" : ""), true));
  const set = o.getObj("set");
  if (set != null) { const ks = (<JSON.Obj>set).keys; for (let k = 0; k < ks.length; k++) { const d = exprDelta(<JSON.Obj>set, ks[k]);
    parts.push(nRow(nSel(ks[k], statOpts(), "effStat:" + id + ":" + i.toString() + ":" + ks[k], "auto") + "," + nIn(fmtNum(d), "", "effDelta:" + id + ":" + i.toString() + ":" + ks[k], "70px", true) + "," + nBtn("✕", "delEff:" + id + ":" + i.toString() + ":" + ks[k], "mini danger"))); } }
  if (statList != "(없음)") parts.push(nBtn("＋ 효과", "addEff:" + id + ":" + i.toString(), "mini ai"));
  return nCard("nv-opt", parts.join(","));
}
function statKeyList(): string { const a = statsArr().valueOf(); if (a.length == 0) return "(없음)"; const ks = new Array<string>(); for (let i = 0; i < a.length; i++) ks.push(gs(<JSON.Obj>a[i], "key", "")); return ks.join(", "); }
function statOpts(): string { const a = statsArr().valueOf(); const parts = new Array<string>(); for (let i = 0; i < a.length; i++) { const k = gs(<JSON.Obj>a[i], "key", ""); parts.push(opt(k, k)); } return parts.join(","); }

function statsPanel(): string {
  const parts = new Array<string>();
  parts.push(nText("스탯은 플레이 중 상태창에 표시되고, 선택지 효과·조건 분기에 쓰입니다.", true));
  const a = statsArr().valueOf();
  for (let i = 0; i < a.length; i++) { const s = <JSON.Obj>a[i];
    const barOn = s.getBool("bar"); const showOn = s.getBool("show");
    parts.push(nRow(
      nIn(gs(s, "key", ""), "변수(hp)", "statField:" + i.toString() + ":key", "90px", false) + "," +
      nIn(gs(s, "label", ""), "표시 이름", "statField:" + i.toString() + ":label", "110px", false) + "," +
      nText("초기", true) + "," + nIn(fmtNum(num(s, "initial", 0)), "", "statField:" + i.toString() + ":initial", "70px", true) + "," +
      nText("최대", true) + "," + nIn(hasMax(s) ? fmtNum(num(s, "max", 100)) : "", "없음", "statField:" + i.toString() + ":max", "70px", true) + "," +
      nChk(barOn != null ? barOn.valueOf() : true, "막대", "statField:" + i.toString() + ":bar") + "," +
      nChk(showOn != null ? showOn.valueOf() : true, "표시", "statField:" + i.toString() + ":show") + "," +
      nBtn("✕", "delStat:" + i.toString(), "mini danger")));
  }
  parts.push(nBtn("＋ 스탯 추가", "addStat", "primary"));
  return nCol(parts.join(","));
}
function hasMax(s: JSON.Obj): bool { const m = s.get("max"); return m != null && !m.isNull; }

function metaPanel(): string {
  const c = <JSON.Obj>cur;
  return nCol(
    nText("제목", true) + "," + nIn(gs(c, "title", ""), "", "meta:title", "", false) + "," +
    nText("작가", true) + "," + nIn(gs(c, "author", ""), "", "meta:author", "", false) + "," +
    nText("설명", true) + "," + nTA(gs(c, "description", ""), "", "meta:description") + "," +
    nText("진행자 페르소나 (AI 생성 지문의 시스템 지시)", true) + "," + nTA(gs(c, "persona", ""), "", "meta:persona") + "," +
    nText("이 소설은 100% 로컬에서 생성·저장되며, 내보낸 플러그인도 네트워크 권한이 없어 외부로 나가지 않습니다.", true));
}
function draftPanel(): string {
  return nCol(
    nText("✨ AI로 초안 만들기", false) + "," +
    nText("전제와 분위기를 적으면 로컬 AI가 분기 장면 구조를 만듭니다. 이후 자유롭게 편집하세요.", true) + "," +
    nText("분위기 / 장르", true) + "," + nIn(dTone, "예: 심리 호러, 2인칭", "dr:tone", "", false) + "," +
    nText("전제 / 시놉시스", true) + "," + nTA(dPremise, "예: 폭풍우 치는 밤, 낯선 저택에 발이 묶인 주인공…", "dr:premise") + "," +
    nText("스탯 (쉼표로 구분 — 예: hp,sanity)", true) + "," + nIn(dStats, "hp, sanity", "dr:stats", "", false) + "," +
    nRow(nBtn("취소", "tab:story", "") + "," + nBtn("✨ 생성", "draftGo", "primary")));
}
