const orb = document.getElementById("orb");
const orbWrap = document.getElementById("orbWrap");
const orbRow = document.getElementById("orbRow");
const bubbles = document.getElementById("bubbles");
const inputWrap = document.getElementById("inputWrap");
const promptInput = document.getElementById("prompt");
const sendBtn = document.getElementById("sendBtn");
const closeBtn = document.getElementById("closeBtn");
const menu = document.getElementById("menu");

const MAX_BUBBLES = 6;

// ---- 클릭 통과 제어 ----------------------------------------------------
// 위젯(오브/말풍선/입력창/메뉴) 위에 마우스가 있을 때만 클릭을 받고,
// 그 외 빈 영역에서는 뒤쪽 창을 조작할 수 있도록 통과시킨다.
// 주의: #bubbles 컨테이너는 pointer-events:none 이라 여기 넣어도 이벤트가 안 온다.
// 말풍선 각각에 addBubble()에서 직접 리스너를 붙인다.
const interactive = [orbRow, inputWrap, menu];
let overCount = 0;
function refreshMouse() {
  window.leva.setIgnoreMouse(overCount <= 0);
}
interactive.forEach((el) => {
  el.addEventListener("mouseenter", () => { overCount++; refreshMouse(); });
  el.addEventListener("mouseleave", () => { overCount = Math.max(0, overCount - 1); refreshMouse(); });
});
window.leva.setIgnoreMouse(true);

// ---- 말풍선 -----------------------------------------------------------
const BUBBLE_TTL = 6000;   // 기본 유지 시간(ms)
const BUBBLE_GRACE = 3000; // 마우스/휠/클릭 상호작용 후 유지 시간(ms)
let bubblesHovered = false;
let bubbleResumeTimer = null;

function armBubble(b, delay) {
  if (!b || !b._auto || b._pinned) return;
  clearTimeout(b._dismiss);
  b.classList.remove("fade");
  b._dismiss = setTimeout(() => {
    b.classList.add("fade");
    setTimeout(() => { if (b.parentNode) b.remove(); }, 350);
  }, delay);
}

function pauseBubbles() {
  bubbles.querySelectorAll(".bubble").forEach((b) => {
    if (b._auto) { clearTimeout(b._dismiss); b.classList.remove("fade"); }
  });
}

function resumeBubbles(delay) {
  bubbles.querySelectorAll(".bubble").forEach((b) => armBubble(b, delay));
}

// 휠·클릭 등 상호작용이 있으면 유지 시간을 연장한다.
function bumpBubbles() {
  pauseBubbles();
  clearTimeout(bubbleResumeTimer);
  if (!bubblesHovered) bubbleResumeTimer = setTimeout(() => resumeBubbles(BUBBLE_GRACE), 500);
}

// 말풍선 각각에 마우스 이벤트를 붙인다(컨테이너는 pointer-events:none 이라 안 됨).
function wireBubble(b) {
  b.addEventListener("mouseenter", () => {
    overCount++; refreshMouse();           // 클릭 통과 해제 → 휠 스크롤 가능
    bubblesHovered = true;
    clearTimeout(bubbleResumeTimer);
    pauseBubbles();                          // 올라가 있는 동안 사라지지 않음
  });
  b.addEventListener("mouseleave", () => {
    overCount = Math.max(0, overCount - 1); refreshMouse();
    bubblesHovered = false;
    resumeBubbles(BUBBLE_GRACE);             // 벗어나면 잠시 뒤 사라짐
  });
  b.addEventListener("wheel", bumpBubbles, { passive: true });
  b.addEventListener("mousedown", bumpBubbles);
}

function addBubble(text, who, autoDismiss = true) {
  const b = document.createElement("div");
  b.className = `bubble ${who}`;
  b.textContent = text;
  b._auto = autoDismiss;
  bubbles.appendChild(b);
  wireBubble(b);
  while (bubbles.children.length > MAX_BUBBLES) {
    bubbles.removeChild(bubbles.firstChild);
  }
  // 마우스가 올라가 있는 동안 추가되면 벗어날 때까지 유지한다.
  if (autoDismiss && !bubblesHovered) armBubble(b, BUBBLE_TTL);
  return b;
}

let typingEl = null;
function showTyping() {
  typingEl = addBubble("생각 중…", "ai", false);
  typingEl.classList.add("typing");
}
function clearTyping() {
  if (typingEl && typingEl.parentNode) typingEl.remove();
  typingEl = null;
}

function greet() {
  addBubble("안녕하세요! LevA 비서예요. 오브를 클릭해 말을 걸어보세요 ✨", "ai");
}

// ---- 인터랙션 ---------------------------------------------------------
let inputOpen = false;
function toggleInput(force) {
  inputOpen = force !== undefined ? force : !inputOpen;
  inputWrap.classList.toggle("show", inputOpen);
  if (inputOpen) setTimeout(() => promptInput.focus(), 30);
}

orb.addEventListener("click", () => toggleInput());

// ---- 마크다운/LaTeX 정리 (작은 말풍선용 일반 텍스트로) ---------------
function sanitizeMd(t) {
  if (!t) return t;
  let s = t;
  // LaTeX 화살표·인라인 수식
  s = s.replace(/\$\s*\\?rightarrow\s*\$/g, "→").replace(/\$\s*\\?leftarrow\s*\$/g, "←");
  s = s.replace(/\\rightarrow/g, "→").replace(/\\leftarrow/g, "←").replace(/\\to\b/g, "→");
  s = s.replace(/\$([^$\n]*)\$/g, "$1"); // 남은 $...$ 는 구분자만 제거
  // 마크다운 강조·코드·헤더·인용·불릿
  s = s.replace(/\*\*(.*?)\*\*/g, "$1").replace(/__(.*?)__/g, "$1");
  s = s.replace(/`([^`]*)`/g, "$1");
  s = s.replace(/^#{1,6}\s+/gm, "");
  s = s.replace(/^\s*>\s?/gm, "");
  s = s.replace(/^\s*[-*]\s+/gm, "• "); // 불릿을 • 로
  return s;
}

// ---- 파일 참조 하이퍼링크 --------------------------------------------
// 뷰어가 지원하는 확장자만 링크로 만든다.
const VIEWER_EXTS = new Set([
  ".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm", ".rtf",
  ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
  ".cs", ".java", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
  ".sh", ".bat", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini", ".lua", ".pl", ".r",
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
  ".pdf", ".xlsx", ".docx",
]);
function extOfName(n) { const i = n.lastIndexOf("."); return i >= 0 ? n.slice(i).toLowerCase() : ""; }

async function buildFileIndex() {
  let entries = [];
  try { entries = await window.leva.cacheList(); } catch (e) {}
  const map = {};
  entries.forEach((e) => {
    if (e.filename && e.path && VIEWER_EXTS.has(extOfName(e.filename))) map[e.filename] = e.path;
  });
  return map;
}

// 말풍선에 텍스트를 넣되, 알려진 파일명은 클릭 가능한 링크로 변환
function renderLinkified(bubble, text, fileMap) {
  bubble.textContent = "";
  const names = Object.keys(fileMap).sort((a, b) => b.length - a.length);
  if (!names.length) { bubble.textContent = text; return; }
  const esc = names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp("(" + esc.join("|") + ")", "g");
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) bubble.appendChild(document.createTextNode(text.slice(last, m.index)));
    const name = m[0];
    const link = document.createElement("span");
    link.className = "filelink"; link.textContent = name; link.title = "열기: " + fileMap[name];
    link.addEventListener("click", (ev) => { ev.stopPropagation(); window.leva.openViewer(fileMap[name]); });
    bubble.appendChild(link);
    last = m.index + name.length;
    if (re.lastIndex === m.index) re.lastIndex++; // 안전장치
  }
  if (last < text.length) bubble.appendChild(document.createTextNode(text.slice(last)));
}

// ---- 스트리밍 응답 말풍선 --------------------------------------------
let streamBubble = null;
let streamRaw = "";
window.leva.onChatChunk((p) => {
  if (!p || !p.delta) return;
  if (!streamBubble) {
    clearTyping();
    streamBubble = addBubble("", "ai", false); // 생성 중엔 자동으로 안 사라짐
    streamRaw = "";
  }
  streamRaw += p.delta;
  streamBubble.textContent = sanitizeMd(streamRaw); // 원본 누적 → 정리해 표시
  streamBubble.scrollTop = streamBubble.scrollHeight; // 새 내용으로 스크롤
});

async function send() {
  const text = promptInput.value.trim();
  if (!text) return;
  promptInput.value = "";
  addBubble(text, "user");
  orb.classList.add("thinking");
  streamBubble = null;
  streamRaw = "";
  showTyping();
  try {
    const answer = await window.leva.ask(text);
    clearTyping();
    const finalText = sanitizeMd((answer && answer.trim()) ? answer : streamRaw);
    const fileMap = await buildFileIndex(); // 참고 파일명 → 경로
    if (streamBubble) {
      // 스트리밍으로 채워진 말풍선을 최종 답변으로 확정 + 파일명 링크화
      renderLinkified(streamBubble, finalText, fileMap);
      streamBubble._auto = true;
      armBubble(streamBubble, BUBBLE_TTL);
    } else {
      // 스트림이 없었으면(폴백) 한 번에 표시
      const b = addBubble("", "ai");
      renderLinkified(b, finalText, fileMap);
    }
  } catch (e) {
    clearTyping();
    if (streamBubble) { streamBubble._auto = true; armBubble(streamBubble, BUBBLE_TTL); }
    else addBubble("오류가 발생했어요: " + e.message, "ai");
  } finally {
    streamBubble = null;
    orb.classList.remove("thinking");
  }
}

sendBtn.addEventListener("click", send);
promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") send();
  if (e.key === "Escape") toggleInput(false);
});

closeBtn.addEventListener("click", () => window.leva.quit());

// ---- 우클릭 컨텍스트 메뉴 ---------------------------------------------
function openMenu(x, y) {
  menu.classList.add("show");
  window.leva.setIgnoreMouse(false); // 메뉴 조작 가능하도록
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  let left = x - mw;
  let top = y - mh;
  left = Math.max(4, Math.min(left, window.innerWidth - mw - 4));
  top = Math.max(4, Math.min(top, window.innerHeight - mh - 4));
  menu.style.left = left + "px";
  menu.style.top = top + "px";
}

function closeMenu() {
  menu.classList.remove("show");
  refreshMouse();
}

// 오브/위젯 영역 우클릭 시 메뉴 표시
orbRow.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  openMenu(e.clientX, e.clientY);
});

menu.addEventListener("click", (e) => {
  const item = e.target.closest(".menu-item");
  if (!item) return;
  const action = item.dataset.action;
  closeMenu();
  if (action === "chat") toggleInput(true);
  else if (action === "dashboard") window.leva.openDashboard();
  else if (action === "settings") window.leva.openSettings();
  else if (action === "quit") window.leva.quit();
});

// 다른 곳 클릭/ESC 시 메뉴 닫기
document.addEventListener("mousedown", (e) => {
  if (menu.classList.contains("show") && !menu.contains(e.target)) closeMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
});

greet();

// ---- 폴더 감시 이벤트 → 말풍선 ---------------------------------------
if (window.leva.onFsEvent) {
  const LABELS = { created: "생성", deleted: "삭제", modified: "수정", moved: "이동" };
  window.leva.onFsEvent((ev) => {
    if (!ev) return;
    // 잦은 modified 이벤트는 생략해 알림 폭주를 막음
    if (ev.eventType === "modified") return;
    const kind = ev.isDirectory ? "폴더" : "파일";
    const label = LABELS[ev.eventType] || ev.eventType;
    const raw = ev.destPath || ev.path || "";
    const name = raw.split(/[\\/]/).pop() || raw;
    if (ev.eventType === "moved") {
      const from = (ev.srcPath || "").split(/[\\/]/).pop();
      // 상위 폴더가 같으면 이름변경(rename), 다르면 이동(move)
      const parentOf = (pth) => (pth || "").replace(/[\\/]+[^\\/]*$/, "");
      const renamed = parentOf(ev.srcPath) === parentOf(ev.destPath);
      const verb = renamed ? "이름변경" : "이동";
      addBubble("📁 " + kind + " " + verb + ": " + from + " → " + name, "ai");
    } else {
      addBubble("📁 " + kind + " " + label + ": " + name, "ai");
    }
  });
}

// ---- AI 캐싱 이벤트 → 말풍선 -----------------------------------------
if (window.leva.onCacheEvent) {
  window.leva.onCacheEvent((ev) => {
    if (!ev) return;
    const name = ev.filename || "";
    if (ev.phase === "start") {
      addBubble("🧠 캐싱 시작: " + name, "ai");
    } else if (ev.phase === "done") {
      const kw = (ev.keywords || []).slice(0, 4).join(", ");
      addBubble("🧠 캐싱 완료: " + name + (kw ? "\n키워드: " + kw : ""), "ai");
    } else if (ev.phase === "error") {
      const why = ev.error ? "\n" + String(ev.error).slice(0, 120) : "";
      addBubble("⚠️ 캐싱 실패: " + name + why, "ai");
    }
  });
}
