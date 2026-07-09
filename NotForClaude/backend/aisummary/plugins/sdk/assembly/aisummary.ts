// ===================================================================
//  AISummary Plugin SDK for AssemblyScript
//  T2(WASM) 플러그인을 선언적으로 작성하기 위한 헬퍼.
//  - 문자열 마샬링 / 호스트 능력 호출 / UI 컴포넌트 빌더를 감싼다.
//  - import는 모두 "env" 모듈로 고정(@external)되어 호스트 바인딩이 단순.
// ===================================================================
import { JSON } from "assemblyscript-json/assembly";

// ---- 호스트 능력 (import) — 이게 전부. 네트워크 없음 ----
@external("env", "host_ui_render")     declare function _ui(p: usize, l: i32): void;
@external("env", "host_generate")      declare function _gen(r: i32, p: usize, l: i32): void;
@external("env", "host_storage_save")  declare function _save(p: usize, l: i32): void;
@external("env", "host_storage_delete")declare function _sdel(p: usize, l: i32): void;
@external("env", "host_export")        declare function _exp(p: usize, l: i32): void;
@external("env", "host_preview")       declare function _prev(p: usize, l: i32): void;
@external("env", "host_toast")         declare function _toast(p: usize, l: i32): void;
@external("env", "host_log")           declare function _log(p: usize, l: i32): void;

// ---- 문자열 마샬링 (플러그인이 alloc/dealloc을 re-export 해야 함) ----
export function alloc(size: i32): usize { return heap.alloc(size); }
export function dealloc(ptr: usize, size: i32): void { heap.free(ptr); }
export function readStr(ptr: usize, len: i32): string { return String.UTF8.decodeUnsafe(ptr, len, false); }
function emit(fn: i32, s: string): void {
  const b = String.UTF8.encode(s, false); const p = changetype<usize>(b); const n = b.byteLength;
  if (fn == 0) _ui(p, n); else if (fn == 2) _save(p, n); else if (fn == 3) _exp(p, n);
  else if (fn == 4) _prev(p, n); else if (fn == 5) _toast(p, n); else if (fn == 6) _log(p, n);
  else if (fn == 7) _sdel(p, n);
}

// ---- JSON escape ----
export function esc(s: string): string {
  let o = "\"";
  for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i);
    if (c == 0x22) o += "\\\""; else if (c == 0x5c) o += "\\\\";
    else if (c == 0x0a) o += "\\n"; else if (c == 0x0d) o += "\\r"; else if (c == 0x09) o += "\\t";
    else if (c < 0x20) { const h = "0123456789abcdef"; o += "\\u00" + h.charAt((c >> 4) & 0xf) + h.charAt(c & 0xf); }
    else o += String.fromCharCode(c); }
  return o + "\"";
}

// ===================================================================
//  UI 컴포넌트 빌더 — Node 를 조립해서 render() 로 보낸다.
// ===================================================================
export class Node { constructor(public json: string) {} }
export class Opt { constructor(public value: string, public label: string) {} }
export function opt(value: string, label: string): Opt { return new Opt(value, label); }

function kids(c: Node[]): string { const p = new Array<string>(c.length); for (let i = 0; i < c.length; i++) p[i] = c[i].json; return "[" + p.join(",") + "]"; }

export function Text(text: string, muted: bool = false): Node {
  return new Node("{\"t\":\"Text\",\"text\":" + esc(text) + ",\"muted\":" + (muted ? "true" : "false") + "}"); }
export function Markdown(text: string): Node { return new Node("{\"t\":\"MD\",\"text\":" + esc(text) + "}"); }
export function Divider(): Node { return new Node("{\"t\":\"Hr\"}"); }
export function Input(value: string, placeholder: string, ev: string, num: bool = false): Node {
  return new Node("{\"t\":\"In\",\"val\":" + esc(value) + ",\"ph\":" + esc(placeholder) + ",\"ev\":" + esc(ev) + ",\"num\":" + (num ? "true" : "false") + "}"); }
export function Textarea(value: string, placeholder: string, ev: string): Node {
  return new Node("{\"t\":\"TA\",\"val\":" + esc(value) + ",\"ph\":" + esc(placeholder) + ",\"ev\":" + esc(ev) + "}"); }
export function Select(value: string, options: Opt[], ev: string): Node {
  const p = new Array<string>(options.length);
  for (let i = 0; i < options.length; i++) p[i] = "{\"v\":" + esc(options[i].value) + ",\"l\":" + esc(options[i].label) + "}";
  return new Node("{\"t\":\"Sel\",\"val\":" + esc(value) + ",\"opts\":[" + p.join(",") + "],\"ev\":" + esc(ev) + "}"); }
export function Checkbox(checked: bool, label: string, ev: string): Node {
  return new Node("{\"t\":\"Chk\",\"checked\":" + (checked ? "true" : "false") + ",\"label\":" + esc(label) + ",\"ev\":" + esc(ev) + "}"); }
export function Button(label: string, ev: string, variant: string = ""): Node {
  return new Node("{\"t\":\"Btn\",\"label\":" + esc(label) + ",\"ev\":" + esc(ev) + ",\"variant\":" + esc(variant) + "}"); }
export function Row(c: Node[]): Node { return new Node("{\"t\":\"Row\",\"c\":" + kids(c) + "}"); }
export function Col(c: Node[]): Node { return new Node("{\"t\":\"Col\",\"c\":" + kids(c) + "}"); }
export function Card(cls: string, c: Node[]): Node { return new Node("{\"t\":\"Card\",\"cls\":" + esc(cls) + ",\"c\":" + kids(c) + "}"); }
export function Item(cls: string, ev: string, c: Node[]): Node { return new Node("{\"t\":\"Item\",\"cls\":" + esc(cls) + ",\"ev\":" + esc(ev) + ",\"c\":" + kids(c) + "}"); }

export function render(surface: string, body: Node[]): void {
  emit(0, "{\"surface\":" + esc(surface) + ",\"body\":" + kids(body) + "}"); }
export function renderMain(body: Node[]): void { render("main", body); }
export function renderSidebar(body: Node[]): void { render("sidebar", body); }

// ===================================================================
//  호스트 능력 래퍼
// ===================================================================
export function toast(msg: string): void { emit(5, msg); }
export function log(msg: string): void { emit(6, msg); }
export function generate(reqId: i32, prompt: string, persona: string): void {
  const b = String.UTF8.encode("{\"prompt\":" + esc(prompt) + ",\"persona\":" + esc(persona) + "}", false);
  _gen(reqId, changetype<usize>(b), b.byteLength); }

export namespace storage {
  // value 는 이미 직렬화된 JSON 문자열(예: "\"text\"", "[...]", "{...}")
  export function saveJson(key: string, valueJson: string): void { emit(2, "{\"key\":" + esc(key) + ",\"value\":" + valueJson + "}"); }
  export function saveString(key: string, value: string): void { saveJson(key, esc(value)); }
  export function saveStrings(key: string, values: string[]): void {
    const p = new Array<string>(values.length); for (let i = 0; i < values.length; i++) p[i] = esc(values[i]);
    saveJson(key, "[" + p.join(",") + "]"); }
  export function remove(key: string): void { emit(7, "{\"key\":" + esc(key) + "}"); }
}

export function exportPlugin(manifestJson: string): void { emit(3, manifestJson); }
export function preview(specJson: string): void { emit(4, specJson); }

// ===================================================================
//  라이프사이클 헬퍼
// ===================================================================
// on_init(ptr,len) 에서 저장소 스냅샷을 꺼낸다.
export function initStorage(ptr: usize, len: i32): JSON.Obj {
  const root = <JSON.Obj>JSON.parse(readStr(ptr, len));
  const s = root.getObj("storage");
  return s != null ? <JSON.Obj>s : new JSON.Obj();
}
// on_event(...) 핸들러/값을 파싱. cmd = 첫 토큰, arg(i) = 콜론 구분 인자.
export class Ev {
  parts: string[]; value: string;
  constructor(handler: string, value: string) { this.parts = handler.split(":"); this.value = value; }
  get cmd(): string { return this.parts.length > 0 ? this.parts[0] : ""; }
  arg(i: i32): string { return (i + 1) < this.parts.length ? this.parts[i + 1] : ""; }
  argInt(i: i32): i32 { const s = this.arg(i); return s.length > 0 ? i32(parseInt(s, 10)) : 0; }
}
export function event(hp: usize, hl: i32, pp: usize, pl: i32): Ev {
  return new Ev(readStr(hp, hl), pl > 0 ? readStr(pp, pl) : "");
}
