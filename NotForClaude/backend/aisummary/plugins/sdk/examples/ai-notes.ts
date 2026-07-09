import { JSON } from "assemblyscript-json/assembly";
// AI 메모 — SDK로 작성한 T2 플러그인 (직관적 버전)
import * as ui from "../assembly/aisummary";
import { Node, opt } from "../assembly/aisummary";

// 마샬링 함수는 플러그인이 re-export 해야 한다 (한 줄)
export function alloc(size: i32): usize { return ui.alloc(size); }
export function dealloc(ptr: usize, size: i32): void { ui.dealloc(ptr, size); }

// ---- 상태 ----
let notes: string[] = [];
let draft: string = "";
let output: string = "";

// ---- 렌더 (선언적!) ----
function render(): void {
  const main: Node[] = [
    ui.Text("💡 AI 메모"),
    ui.Textarea(draft, "메모를 입력…", "draft"),
    ui.Row([
      ui.Button("＋ 저장", "add", "primary"),
      ui.Button("✨ AI로 요약", "summarize"),
    ]),
  ];
  if (output.length > 0) main.push(ui.Markdown(output));
  ui.renderMain(main);

  const side: Node[] = [ ui.Text("메모 " + notes.length.toString() + "개", true) ];
  for (let i = 0; i < notes.length; i++) {
    const preview = notes[i].length > 40 ? notes[i].substring(0, 40) : notes[i];
    side.push(ui.Card("note-item", [
      ui.Text(preview),
      ui.Button("삭제", "del:" + i.toString(), "mini danger"),
    ]));
  }
  ui.renderSidebar(side);
}

// ---- 라이프사이클 ----
export function on_init(ptr: usize, len: i32): void {
  const store = ui.initStorage(ptr, len);
  notes = [];
  const arr = store.getArr("notes");
  if (arr != null) { const a = arr.valueOf(); for (let i = 0; i < a.length; i++) notes.push((<JSON.Str>a[i]).valueOf()); }
  render();
}

export function on_event(hp: usize, hl: i32, pp: usize, pl: i32): void {
  const e = ui.event(hp, hl, pp, pl);
  if (e.cmd == "draft") { draft = e.value; return; }
  if (e.cmd == "add") {
    if (draft.length == 0) { ui.toast("내용을 입력하세요"); return; }
    notes.push(draft); draft = ""; ui.storage.saveStrings("notes", notes); ui.toast("저장됨"); render(); return;
  }
  if (e.cmd == "del") {
    const idx = e.argInt(0); const n: string[] = [];
    for (let k = 0; k < notes.length; k++) if (k != idx) n.push(notes[k]);
    notes = n; ui.storage.saveStrings("notes", notes); render(); return;
  }
  if (e.cmd == "summarize") {
    if (notes.length == 0) { ui.toast("메모가 없습니다"); return; }
    output = "…"; render();
    ui.generate(1, "다음 메모들을 3줄로 요약해줘:\n- " + notes.join("\n- "), "너는 간결한 메모 정리 도우미야.");
    return;
  }
}

export function on_generate_done(reqId: i32, ptr: usize, len: i32): void {
  output = ui.readStr(ptr, len); render();
}
