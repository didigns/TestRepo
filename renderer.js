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
const interactive = [orbRow, inputWrap, bubbles, menu];
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
const BUBBLE_TTL = 2000; // 말풍선 유지 시간(ms)
function addBubble(text, who, autoDismiss = true) {
  const b = document.createElement("div");
  b.className = `bubble ${who}`;
  b.textContent = text;
  bubbles.appendChild(b);
  while (bubbles.children.length > MAX_BUBBLES) {
    bubbles.removeChild(bubbles.firstChild);
  }
  if (autoDismiss) {
    setTimeout(() => {
      b.classList.add("fade");
      setTimeout(() => { if (b.parentNode) b.remove(); }, 350);
    }, BUBBLE_TTL);
  }
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

async function send() {
  const text = promptInput.value.trim();
  if (!text) return;
  promptInput.value = "";
  addBubble(text, "user");
  orb.classList.add("thinking");
  showTyping();
  try {
    const answer = await window.leva.ask(text);
    clearTyping();
    addBubble(answer, "ai");
  } catch (e) {
    clearTyping();
    addBubble("오류가 발생했어요: " + e.message, "ai");
  } finally {
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
      addBubble("⚠️ 캐싱 실패: " + name, "ai");
    }
  });
}
