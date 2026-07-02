const { app, BrowserWindow, ipcMain, screen, net, dialog, shell } = require("electron");
const path = require("path");
const url = require("url");
const fs = require("fs");
const { spawn } = require("child_process");

let win;
let settingsWin = null;

// ---- 설정 저장/로드 --------------------------------------------------
const DEFAULT_CONFIG = {
  alwaysOnTop: true,
  pythonPath: "python",
  watchedFolders: [], // [{ path, recursive }]
  ignorePatterns: [], // ["*.tmp", ...]
  notifyOnEvents: true,
  // ---- AI 캐싱 (LevACache) ----
  enableCache: true,
  llamaServerExe: "C:\\Work\\llamaCpp\\llama-server.exe",
  textModel: "",       // Gemma 4b gguf (그 외 파일)
  visionModel: "",     // MiniCPM-V gguf (pdf/이미지)
  visionMmproj: "",    // MiniCPM-V mmproj gguf
  cachePort: 8080,
};

function configPath() {
  return path.join(app.getPath("userData"), "leva-config.json");
}

function loadConfig() {
  try {
    const raw = fs.readFileSync(configPath(), "utf8");
    return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
  } catch (e) {
    return { ...DEFAULT_CONFIG };
  }
}

function saveConfig(cfg) {
  const merged = { ...loadConfig(), ...cfg };
  try {
    fs.writeFileSync(configPath(), JSON.stringify(merged, null, 2), "utf8");
  } catch (e) {
    /* 무시 */
  }
  return merged;
}

// ---- 채팅 로그 저장/조회 ----------------------------------------------
const CHATLOG_MAX = 500;
function chatLogPath() {
  return path.join(app.getPath("userData"), "leva-chatlog.json");
}
function readChatLog() {
  try {
    const arr = JSON.parse(fs.readFileSync(chatLogPath(), "utf8"));
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    return [];
  }
}
function appendChatLog(entry) {
  const log = readChatLog();
  log.push(entry);
  const trimmed = log.slice(-CHATLOG_MAX);
  try {
    fs.writeFileSync(chatLogPath(), JSON.stringify(trimmed, null, 2), "utf8");
  } catch (e) {
    /* 무시 */
  }
}

// ---- 폴더 감시 데몬 (LevAObserver/folder_observer.py) -----------------
let daemon = null;
let daemonReady = false;
let daemonError = null;
let reqId = 0;
const pending = new Map();
let stdoutBuf = "";

function daemonScriptPath() {
  return path.join(__dirname, "LevAObserver", "folder_observer.py");
}

// 개발: 시스템 python + 스크립트 / 배포: PyInstaller로 번들된 exe(pybin/)
// 반환: { cmd, base } — 실제 실행 시 base 뒤에 인자를 붙인다.
function pyDaemonCmd(kind) {
  if (app.isPackaged) {
    const exe = path.join(
      process.resourcesPath, "pybin",
      kind === "observer" ? "levaobserver.exe" : "levacache.exe"
    );
    return { cmd: exe, base: [], bundled: true };
  }
  const py = loadConfig().pythonPath || "python";
  if (kind === "observer") return { cmd: py, base: [daemonScriptPath()], bundled: false };
  return { cmd: py, base: ["-m", "LevACache.worker"], bundled: false };
}

function startDaemon() {
  const { cmd, base, bundled } = pyDaemonCmd("observer");
  if (bundled && !fs.existsSync(cmd)) {
    daemonError = "번들된 감시 프로그램을 찾을 수 없습니다: " + cmd;
    return;
  }
  if (!bundled && !fs.existsSync(daemonScriptPath())) {
    daemonError = "folder_observer.py 를 찾을 수 없습니다: " + daemonScriptPath();
    return;
  }
  try {
    daemon = spawn(cmd, [...base, "--daemon"], {
      cwd: __dirname,
      // Windows에서 Python 표준입출력을 UTF-8로 강제해 한글 깨짐 방지
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    });
  } catch (e) {
    daemonError = "감시 프로그램 실행 실패: " + e.message;
    daemon = null;
    return;
  }
  daemonError = null;
  daemon.on("error", (e) => {
    daemonError = "Python 실행 실패: " + e.message + " (설정에서 python 경로 확인)";
    daemon = null;
    daemonReady = false;
  });
  daemon.stdout.setEncoding("utf8"); // 멀티바이트(한글) 경계 안전 디코딩
  daemon.stdout.on("data", onDaemonStdout);
  daemon.stderr.on("data", () => {}); // 로그는 무시
  daemon.on("exit", () => {
    daemon = null;
    daemonReady = false;
  });
}

function onDaemonStdout(chunk) {
  stdoutBuf += chunk; // setEncoding("utf8")로 이미 문자열
  let idx;
  while ((idx = stdoutBuf.indexOf("\n")) >= 0) {
    const line = stdoutBuf.slice(0, idx).trim();
    stdoutBuf = stdoutBuf.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      continue;
    }
    handleDaemonMessage(msg);
  }
}

function handleDaemonMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "ready") {
    daemonReady = true;
    restoreWatches();
    return;
  }
  if (msg.type === "event") {
    const cfg = loadConfig();
    if (cfg.notifyOnEvents && win && !win.isDestroyed()) {
      win.webContents.send("fs-event", msg);
    }
    // 파일 변경에 따라 캐시를 동기화한다.
    if (msg.eventType === "created" || msg.eventType === "modified") {
      // 생성/수정 → 재캐싱 큐잉
      if (cfg.enableCache && !msg.isDirectory) {
        const fp = msg.path;
        if (fp && CACHE_EXTS.has(extOf(fp))) enqueueCache(fp);
      }
    } else if (msg.eventType === "deleted") {
      // 삭제 → 캐시 엔트리 제거(캐싱 토글과 무관하게 정리)
      if (msg.path) uncacheFile(msg.path, msg.isDirectory);
    } else if (msg.eventType === "moved") {
      // 이름변경/이동 → 이전 경로 캐시 제거 후 새 경로 캐싱
      if (msg.srcPath) uncacheFile(msg.srcPath, msg.isDirectory);
      if (cfg.enableCache && !msg.isDirectory &&
          msg.destPath && CACHE_EXTS.has(extOf(msg.destPath))) {
        enqueueCache(msg.destPath);
      }
    }
    return;
  }
  if (msg.type === "result" && msg.id != null && pending.has(msg.id)) {
    const entry = pending.get(msg.id);
    pending.delete(msg.id);
    entry.resolve(msg);
  }
}

function sendCommand(type, extra = {}) {
  return new Promise((resolve, reject) => {
    if (!daemon || !daemon.stdin.writable) {
      reject(new Error("폴더 감시 데몬이 실행되고 있지 않습니다."));
      return;
    }
    const id = "r" + ++reqId;
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error("데몬 응답 시간 초과"));
      }
    }, 8000);
    pending.set(id, {
      resolve: (m) => {
        clearTimeout(timer);
        resolve(m);
      },
    });
    daemon.stdin.write(JSON.stringify({ id, type, ...extra }) + "\n");
  });
}

async function restoreWatches() {
  const cfg = loadConfig();
  const patterns = cfg.ignorePatterns || [];
  if (patterns.length) {
    try {
      await sendCommand("set_ignore", { patterns });
    } catch (e) {}
  }
  for (const w of cfg.watchedFolders || []) {
    try {
      await sendCommand("add", { path: w.path, recursive: !!w.recursive });
    } catch (e) {}
  }
}

// ---- AI 캐싱 워커 (LevACache 파이썬 데몬) ------------------------------
const CACHE_EXTS = new Set([
  ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff",
  ".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm",
  ".docx", ".xlsx", ".pptx", ".hwp", ".rtf",
  ".py", ".js", ".ts", ".jsx", ".tsx", ".vue",
  ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cs",
  ".java", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
  ".sh", ".bat", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini",
  ".lua", ".pl", ".r",
]);
function extOf(p) {
  const i = p.lastIndexOf(".");
  return i >= 0 ? p.slice(i).toLowerCase() : "";
}

// 항상 캐싱하지 않는 시스템/임시 파일(하드코딩 차단)
const BLOCKED_NAMES = new Set([
  "desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store", "ntuser.dat", "iconcache.db",
]);
function isBlockedFile(fp) {
  const b = path.basename(fp).toLowerCase();
  return BLOCKED_NAMES.has(b) || b.startsWith("~$");
}

let cacheProc = null;
let cacheReady = false;
let cacheError = null;
let cacheReqId = 0;
const cachePending = new Map();
let cacheBuf = "";
const cacheDebounce = new Map(); // path -> timer

function cacheConfigPath() {
  return configPath();
}
function cacheDbPath() {
  return path.join(app.getPath("userData"), "leva-cache.db");
}

function startCacheWorker() {
  // llama.cpp 서버 매니저(대화 + 캐싱 공용)이므로 항상 기동한다.
  const { cmd, base, bundled } = pyDaemonCmd("cache");
  if (bundled && !fs.existsSync(cmd)) {
    cacheError = "번들된 캐시 워커를 찾을 수 없습니다: " + cmd;
    return;
  }
  const args = [
    ...base, "--daemon",
    "--config", cacheConfigPath(),
    "--db", cacheDbPath(),
  ];
  try {
    cacheProc = spawn(cmd, args, {
      cwd: __dirname,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    });
  } catch (e) {
    cacheError = "캐시 워커 실행 실패: " + e.message;
    cacheProc = null;
    return;
  }
  cacheError = null;
  cacheProc.on("error", (e) => {
    cacheError = "캐시 워커 실행 실패: " + e.message;
    cacheProc = null;
    cacheReady = false;
  });
  cacheProc.stdout.setEncoding("utf8");
  cacheProc.stdout.on("data", onCacheStdout);
  cacheProc.stderr.on("data", () => {});
  cacheProc.on("exit", () => {
    cacheProc = null;
    cacheReady = false;
  });
}

function onCacheStdout(chunk) {
  cacheBuf += chunk;
  let idx;
  while ((idx = cacheBuf.indexOf("\n")) >= 0) {
    const line = cacheBuf.slice(0, idx).trim();
    cacheBuf = cacheBuf.slice(idx + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { continue; }
    handleCacheMessage(msg);
  }
}

function handleCacheMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "ready") {
    cacheReady = true;
    pruneCache();     // 지워진 파일의 캐시 정리
    backfillCache();
    return;
  }
  if (msg.type === "cache-event") {
    if (win && !win.isDestroyed()) win.webContents.send("cache-event", msg);
    // 설정창·대시보드가 열려 있으면 진행 상황을 함께 표시
    if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send("cache-event", msg);
    if (dashboardWin && !dashboardWin.isDestroyed()) dashboardWin.webContents.send("cache-event", msg);
    return;
  }
  if (msg.type === "chat-chunk") {
    if (win && !win.isDestroyed()) win.webContents.send("chat-chunk", msg);
    return;
  }
  if (msg.type === "result" && msg.id != null && cachePending.has(msg.id)) {
    const entry = cachePending.get(msg.id);
    cachePending.delete(msg.id);
    entry.resolve(msg);
  }
}

function sendCacheCommand(type, extra = {}, { expectResult = true, timeout = 600000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!cacheProc || !cacheProc.stdin.writable) {
      reject(new Error("캐시 워커가 실행되고 있지 않습니다."));
      return;
    }
    if (!expectResult) {
      cacheProc.stdin.write(JSON.stringify({ type, ...extra }) + "\n");
      resolve(null);
      return;
    }
    const id = "c" + ++cacheReqId;
    const timer = setTimeout(() => {
      if (cachePending.has(id)) {
        cachePending.delete(id);
        reject(new Error("캐시 워커 응답 시간 초과"));
      }
    }, timeout);
    cachePending.set(id, { resolve: (m) => { clearTimeout(timer); resolve(m); } });
    cacheProc.stdin.write(JSON.stringify({ id, type, ...extra }) + "\n");
  });
}

// 동일 파일 연속 이벤트 디바운스 후 캐싱 요청(fire-and-forget)
function enqueueCache(fp) {
  if (!cacheProc) return;
  if (isBlockedFile(fp)) return; // 시스템/임시 파일은 캐싱 안 함
  if (cacheDebounce.has(fp)) clearTimeout(cacheDebounce.get(fp));
  cacheDebounce.set(fp, setTimeout(() => {
    cacheDebounce.delete(fp);
    sendCacheCommand("cache", { path: fp }, { expectResult: false }).catch(() => {});
  }, 1500));
}

// 삭제/이동된 경로의 캐시 엔트리 제거(폴더면 하위 전부). fire-and-forget.
function uncacheFile(fp, isDir) {
  if (!fp) return;
  // 대기 중이던 캐싱 예약이 있으면 취소
  if (cacheDebounce.has(fp)) {
    clearTimeout(cacheDebounce.get(fp));
    cacheDebounce.delete(fp);
  }
  if (!cacheProc) return;
  sendCacheCommand("uncache", { path: fp, prefix: !!isDir }, { expectResult: false }).catch(() => {});
}

// 디스크에 없는 파일의 캐시 정리(앱 꺼진 사이 삭제된 파일 등). fire-and-forget.
function pruneCache() {
  if (!cacheProc) return;
  sendCacheCommand("prune", {}, { expectResult: false }).catch(() => {});
}

// 시작 시 감시 폴더 백필(해시 다른 파일만 캐싱)
async function backfillCache() {
  const cfg = loadConfig();
  if (!cfg.enableCache) return;
  const paths = (cfg.watchedFolders || []).map((w) => w.path);
  if (!paths.length) return;
  try { await sendCacheCommand("scan", { paths }); } catch (e) {}
}

// ---- HUD 창 -----------------------------------------------------------
function createWindow() {
  const primary = screen.getPrimaryDisplay();
  const { width, height } = primary.workAreaSize;
  const { x: waX, y: waY } = primary.workArea;

  const winW = 460;
  const winH = 620;
  const margin = 16;

  win = new BrowserWindow({
    width: winW,
    height: winH,
    x: waX + width - winW - margin,
    y: waY + height - winH - margin,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    focusable: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  const cfg = loadConfig();
  win.setAlwaysOnTop(cfg.alwaysOnTop, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.loadFile("index.html");
  win.setIgnoreMouseEvents(true, { forward: true });
}

ipcMain.on("set-ignore-mouse", (_e, ignore) => {
  if (!win) return;
  win.setIgnoreMouseEvents(ignore, { forward: true });
});

ipcMain.on("quit-app", () => app.quit());

// ---- 설정 창 ----------------------------------------------------------
function openSettings() {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.focus();
    return;
  }
  settingsWin = new BrowserWindow({
    width: 780,
    height: 580,
    minWidth: 640,
    minHeight: 480,
    resizable: true,
    minimizable: false,
    maximizable: false,
    title: "LevA 설정",
    autoHideMenuBar: true,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  settingsWin.loadFile("settings.html");
  settingsWin.on("closed", () => (settingsWin = null));
}

ipcMain.on("open-settings", openSettings);
ipcMain.on("close-settings", () => {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.close();
});

// ---- 대시보드 창 ------------------------------------------------------
let dashboardWin = null;
function openDashboard() {
  if (dashboardWin && !dashboardWin.isDestroyed()) {
    dashboardWin.focus();
    return;
  }
  dashboardWin = new BrowserWindow({
    width: 960,
    height: 640,
    minWidth: 720,
    minHeight: 480,
    resizable: true,
    minimizable: true,
    maximizable: true,
    title: "LevA 대시보드",
    autoHideMenuBar: true,
    backgroundColor: "#0e0f13",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  dashboardWin.loadFile("dashboard.html");
  dashboardWin.on("closed", () => (dashboardWin = null));
}

ipcMain.on("open-dashboard", openDashboard);
ipcMain.on("close-dashboard", () => {
  if (dashboardWin && !dashboardWin.isDestroyed()) dashboardWin.close();
});

// ---- 파일 뷰어 창 -----------------------------------------------------
let viewerWin = null;
let pendingViewerPath = "";

function viewerFileInfo(p) {
  if (!p) return null;
  let fileUrl = "";
  try { fileUrl = url.pathToFileURL(p).href; } catch (e) {}
  return {
    path: p,
    fileUrl,
    name: path.basename(p),
    ext: path.extname(p).toLowerCase(),
    exists: (() => { try { return fs.existsSync(p); } catch (e) { return false; } })(),
  };
}

function openViewer(filePath) {
  pendingViewerPath = filePath || "";
  if (viewerWin && !viewerWin.isDestroyed()) {
    viewerWin.webContents.send("viewer-file", viewerFileInfo(pendingViewerPath));
    viewerWin.focus();
    return;
  }
  viewerWin = new BrowserWindow({
    width: 940,
    height: 740,
    minWidth: 520,
    minHeight: 420,
    title: "LevA 뷰어",
    backgroundColor: "#0e0f13",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      plugins: true, // 내장 PDF 뷰어
    },
  });
  viewerWin.loadFile("viewer.html");
  viewerWin.on("closed", () => (viewerWin = null));
}

ipcMain.on("open-viewer", (_e, p) => openViewer(p));
ipcMain.handle("viewer-file", () => viewerFileInfo(pendingViewerPath));
// 임의 경로의 파일 정보(인라인 뷰어용)
ipcMain.handle("file-info", (_e, p) => viewerFileInfo(p));

// 텍스트 파일 내용 읽기(뷰어용). 크기 제한.
ipcMain.handle("read-file", (_e, p) => {
  try {
    if (!p || !fs.existsSync(p)) return { ok: false, error: "파일을 찾을 수 없습니다." };
    const st = fs.statSync(p);
    if (st.size > 5 * 1024 * 1024) return { ok: false, error: "파일이 너무 큽니다(5MB 초과). 외부 앱으로 열어 주세요." };
    return { ok: true, text: fs.readFileSync(p, "utf8") };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
});

// docx → 문단 블록(adm-zip, MIT). 뷰어용.
function decodeXml(s) {
  return String(s)
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCharCode(parseInt(n, 16)));
}
function docxParaText(p) {
  let out = "";
  const re = /<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>|<w:tab\b[^>]*\/>|<w:br\b[^>]*\/>/g;
  let m;
  while ((m = re.exec(p)) !== null) {
    if (m[0].startsWith("<w:tab")) out += "\t";
    else if (m[0].startsWith("<w:br")) out += "\n";
    else out += decodeXml(m[1]);
  }
  return out;
}
function docxToBlocks(xml) {
  const blocks = [];
  const paras = xml.match(/<w:p\b[\s\S]*?<\/w:p>/g) || [];
  for (const p of paras.slice(0, 3000)) {
    const styleM = p.match(/<w:pStyle w:val="([^"]+)"/);
    const style = styleM ? styleM[1] : "";
    const isList = /<w:numPr>/.test(p);
    const text = docxParaText(p);
    if (!text.trim()) { blocks.push({ type: "space" }); continue; }
    let type = "p";
    if (/^(Title|Heading1)$/i.test(style)) type = "h1";
    else if (/^Heading[2-9]$/i.test(style)) type = "h2";
    if (isList) type = "li";
    blocks.push({ type, text });
  }
  return blocks;
}
ipcMain.handle("read-docx", async (_e, p) => {
  try {
    if (!p || !fs.existsSync(p)) return { ok: false, error: "파일을 찾을 수 없습니다." };
    const AdmZip = require("adm-zip");
    const zip = new AdmZip(p);
    const entry = zip.getEntry("word/document.xml");
    if (!entry) return { ok: false, error: "docx 문서 XML을 찾을 수 없습니다." };
    return { ok: true, blocks: docxToBlocks(zip.readAsText(entry)) };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
});

// xlsx → 표 데이터(ExcelJS, MIT). 뷰어용.
function xlsxCellText(v) {
  if (v == null) return "";
  if (typeof v !== "object") return String(v);
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (Array.isArray(v.richText)) return v.richText.map((t) => t.text).join("");
  if (v.text != null) return String(v.text);
  if (v.result != null) return String(v.result);
  if (v.hyperlink) return String(v.hyperlink);
  if (v.error) return String(v.error);
  return "";
}
ipcMain.handle("read-xlsx", async (_e, p) => {
  try {
    if (!p || !fs.existsSync(p)) return { ok: false, error: "파일을 찾을 수 없습니다." };
    const ExcelJS = require("exceljs");
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.readFile(p);
    const MAXR = 500, MAXC = 40;
    const sheets = [];
    wb.eachSheet((ws) => {
      const rowCount = Math.min(ws.rowCount || 0, MAXR);
      const colCount = Math.min(ws.columnCount || 0, MAXC);
      const rows = [];
      for (let r = 1; r <= rowCount; r++) {
        const row = ws.getRow(r);
        const cells = [];
        for (let c = 1; c <= colCount; c++) cells.push(xlsxCellText(row.getCell(c).value));
        rows.push(cells);
      }
      sheets.push({
        name: ws.name,
        rows,
        truncated: (ws.rowCount || 0) > MAXR || (ws.columnCount || 0) > MAXC,
      });
    });
    return { ok: true, sheets };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
});

// OS 기본 앱으로 열기(docx 등 뷰어 미지원 형식)
ipcMain.handle("open-external-file", async (_e, p) => {
  try {
    const r = await shell.openPath(p || "");
    return { ok: !r, error: r || "" };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
});

ipcMain.handle("get-settings", () => loadConfig());
ipcMain.handle("save-settings", (_e, cfg) => {
  const merged = saveConfig(cfg);
  if (win && !win.isDestroyed()) {
    win.setAlwaysOnTop(!!merged.alwaysOnTop, "screen-saver");
  }
  return merged;
});

// ---- 폴더 관리 IPC ----------------------------------------------------
ipcMain.handle("observer-status", () => ({
  running: !!(daemon && daemonReady),
  error: daemonError,
}));

ipcMain.handle("observer-list", async () => {
  const r = await sendCommand("list");
  return r.watches || [];
});

ipcMain.handle("observer-add", async (_e, { path: p, recursive }) => {
  const r = await sendCommand("add", { path: p, recursive: !!recursive });
  if (r.ok) {
    const cfg = loadConfig();
    const list = (cfg.watchedFolders || []).filter((w) => w.path !== p);
    list.push({ path: p, recursive: !!recursive });
    saveConfig({ watchedFolders: list });
  }
  return r;
});

ipcMain.handle("observer-remove", async (_e, { watchId, path: p }) => {
  const r = await sendCommand("remove", { watchId });
  const cfg = loadConfig();
  const list = (cfg.watchedFolders || []).filter((w) => w.path !== p);
  saveConfig({ watchedFolders: list });
  return r;
});

ipcMain.handle("observer-set-ignore", async (_e, patterns) => {
  const clean = Array.isArray(patterns) ? patterns.filter((s) => s && s.trim()) : [];
  const r = await sendCommand("set_ignore", { patterns: clean });
  saveConfig({ ignorePatterns: clean });
  return r;
});

ipcMain.handle("pick-folder", async () => {
  const parent = settingsWin && !settingsWin.isDestroyed() ? settingsWin : win;
  const res = await dialog.showOpenDialog(parent, { properties: ["openDirectory"] });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

// ---- 캐시 IPC ----
ipcMain.handle("cache-status", () => ({
  running: !!(cacheProc && cacheReady),
  error: cacheError,
}));
// 캐시 목록은 DB를 직접 읽어 반환한다(워커가 캐싱 중이라 바빠도 즉시 응답).
function safeParseKw(s) {
  try { const a = JSON.parse(s || "[]"); return Array.isArray(a) ? a : []; }
  catch (e) { return []; }
}
function readCacheDbDirect() {
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(cacheDbPath(), { readOnly: true });
    const rows = db.prepare(
      "SELECT path, filename, ext, model_key, keywords, summary, status, error_msg, cached_at " +
      "FROM file_cache ORDER BY cached_at DESC LIMIT 300"
    ).all();
    db.close();
    return rows.map((r) => ({ ...r, keywords: safeParseKw(r.keywords) }));
  } catch (e) {
    return null; // node:sqlite 미지원/DB 없음/잠금 → 워커 폴백
  }
}
ipcMain.handle("cache-list", async () => {
  const direct = readCacheDbDirect();
  if (direct) return direct;
  try { const r = await sendCacheCommand("list"); return r.entries || []; }
  catch (e) { return []; }
});
ipcMain.handle("cache-file", async (_e, filePath) => {
  return sendCacheCommand("cache", { path: filePath, force: true });
});
ipcMain.handle("cache-clear-db", async () => {
  // DB를 비우고(직접→워커 폴백), 감시 폴더 전체를 백그라운드로 재캐싱한다.
  let cleared = false;
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(cacheDbPath());
    db.exec("DELETE FROM file_cache");
    db.close();
    cleared = true;
  } catch (e) { /* node:sqlite 미지원 → 워커로 폴백 */ }
  if (!cleared) {
    try { const r = await sendCacheCommand("clear"); cleared = !!(r && r.ok); } catch (e) {}
  }
  if (!cleared) return { ok: false, error: "DB 초기화에 실패했습니다." };
  const cfg = loadConfig();
  const paths = (cfg.watchedFolders || []).map((w) => w.path);
  try { sendCacheCommand("scan", { paths }, { expectResult: false }); } catch (e) {}
  return { ok: true };
});
ipcMain.handle("cache-rescan", async () => {
  const cfg = loadConfig();
  const paths = (cfg.watchedFolders || []).map((w) => w.path);
  await sendCacheCommand("prune", {}, { expectResult: false }).catch(() => {});
  return sendCacheCommand("scan", { paths });
});
ipcMain.handle("pick-file", async (_e, kind) => {
  const parent = settingsWin && !settingsWin.isDestroyed() ? settingsWin : win;
  const filters = kind === "gguf"
    ? [{ name: "GGUF 모델", extensions: ["gguf"] }]
    : [{ name: "실행파일", extensions: ["exe"] }];
  const res = await dialog.showOpenDialog(parent, { properties: ["openFile"], filters });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

// ---- Hugging Face 모델 다운로드 ---------------------------------------
let hfDL = null; // { request, fileStream, tmpPath, destPath, cancelled }

// 다운로드 가능한 모델 카탈로그. URL·저장 경로는 내부에서만 관리하고,
// 사용자에게는 모델 이름과 진행 상태만 보여준다.
const MODEL_CATALOG = [
  {
    key: "textModel",
    name: "Gemma 4 E4B Instruct",
    desc: "대화 · 문서 요약/키워드 · Q4_K_M · 약 4.5GB",
    url: "https://huggingface.co/bartowski/google_gemma-4-E4B-it-GGUF/resolve/main/google_gemma-4-E4B-it-Q4_K_M.gguf",
    assignTo: "textModel",
  },
  {
    key: "visionModel",
    name: "MiniCPM-V 4.6",
    desc: "이미지 · 스캔 PDF 글자추출(OCR) · Q4_K_M · 약 3GB",
    url: "https://huggingface.co/openbmb/MiniCPM-V-4.6-gguf/resolve/main/MiniCPM-V-4_6-Q4_K_M.gguf",
    assignTo: "visionModel",
  },
  {
    key: "visionMmproj",
    name: "MiniCPM-V 4.6 비전 투영기",
    desc: "이미지 인식에 필요한 mmproj · f16 · 약 1.0GB",
    url: "https://huggingface.co/openbmb/MiniCPM-V-4.6-gguf/resolve/main/mmproj-model-f16.gguf",
    assignTo: "visionMmproj",
  },
];

function filenameFromUrl(url) {
  try {
    const u = new URL(url);
    const base = decodeURIComponent((u.pathname.split("/").pop() || "").trim());
    return base || "model.gguf";
  } catch (e) {
    return "model.gguf";
  }
}

function sendHfProgress(payload) {
  if (settingsWin && !settingsWin.isDestroyed())
    settingsWin.webContents.send("hf-download-progress", payload);
}

// 설치 폴더(개발 중엔 프로젝트 루트)
function installDir() {
  return app.isPackaged ? path.dirname(app.getPath("exe")) : __dirname;
}

// 다운로드한 모델은 설치 폴더의 .Models 안에 저장한다.
function hfDefaultDir() {
  return path.join(installDir(), ".Models");
}

function modelDestPath(m) {
  return path.join(hfDefaultDir(), filenameFromUrl(m.url));
}

function isModelDownloaded(m) {
  try {
    const p = modelDestPath(m);
    return fs.existsSync(p) && fs.statSync(p).size > 0;
  } catch (e) {
    return false;
  }
}

// 다운로드된 모델을 설정 경로에 자동 반영(앱 시작 시 1회)
function syncDownloadedModels() {
  const cfg = loadConfig();
  const patch = {};
  for (const m of MODEL_CATALOG) {
    if (isModelDownloaded(m) && !cfg[m.assignTo]) patch[m.assignTo] = modelDestPath(m);
  }
  if (Object.keys(patch).length) saveConfig(patch);
}

// llama-server.exe 는 설치폴더의 llamacpp 안에 항상 번들된다.
// 앱 시작 시 그 경로로 무조건 고정한다(설정 UI에서 별도 지정 불필요).
function llamaServerPath() {
  // 배포: 실행파일과 같은 폴더의 llamacpp / 개발: 프로젝트 루트의 llamacpp
  const exeDir = path.dirname(app.getPath("exe"));
  const candidates = [
    path.join(exeDir, "llamacpp", "llama-server.exe"),
    path.join(__dirname, "llamacpp", "llama-server.exe"),
  ];
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, "llamacpp", "llama-server.exe"));
  }
  const found = candidates.find((p) => {
    try { return fs.existsSync(p); } catch (e) { return false; }
  });
  // 못 찾아도 기대 경로를 반환(에러 메시지가 올바른 위치를 가리키도록)
  return found || (app.isPackaged ? candidates[0] : candidates[1]);
}

function resolveLlamaServer() {
  saveConfig({ llamaServerExe: llamaServerPath() });
}

// ---- llama.cpp 자동 설치 (없으면 GitHub 릴리스에서 받아 압축해제) ------
function llamaCppDir() {
  return path.join(installDir(), "llamacpp");
}

function detectNvidia() {
  try {
    require("child_process").execSync("nvidia-smi -L", {
      stdio: ["ignore", "ignore", "ignore"], timeout: 5000,
    });
    return true;
  } catch (e) {
    return false;
  }
}

function pickLlamaBackend() {
  return detectNvidia() ? "cuda" : "vulkan";
}

function httpGetJson(u) {
  return new Promise((resolve, reject) => {
    const req = net.request({ method: "GET", url: u, redirect: "follow" });
    req.setHeader("User-Agent", "LevAAISummary");
    req.setHeader("Accept", "application/vnd.github+json");
    let data = "";
    req.on("response", (res) => {
      res.on("data", (c) => (data += c));
      res.on("end", () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    });
    req.on("error", reject);
    req.end();
  });
}

// 릴리스 자산 목록에서 백엔드에 맞는 zip URL들을 고른다.
function pickLlamaAssets(assets, backend) {
  const items = (assets || []).map((a) => ({ name: a.name, url: a.browser_download_url }));
  const first = (re) => { const m = items.find((x) => re.test(x.name)); return m ? m.url : null; };
  const out = [];
  if (backend === "cuda") {
    const mains = items.filter((x) => /^llama-.*-bin-win-cuda-[\d.]+-x64\.zip$/i.test(x.name));
    if (!mains.length) return [];
    const chosen = mains.find((x) => /cuda-12/i.test(x.name)) || mains[0]; // CUDA 12.x 우선(드라이버 호환 넓음)
    const ver = (chosen.name.match(/cuda-([\d.]+)-x64/i) || [])[1] || "";
    out.push(chosen.url);
    const rt = items.find((x) =>
      new RegExp("^cudart-llama-bin-win-cuda-" + ver.replace(/\./g, "\\.") + "-x64\\.zip$", "i").test(x.name));
    if (rt) out.push(rt.url); // CUDA 런타임 DLL(드라이버에 없을 수 있으니 함께 받음)
  } else if (backend === "vulkan") {
    const m = first(/^llama-.*-bin-win-vulkan-x64\.zip$/i);
    if (m) out.push(m);
  } else {
    const m = first(/^llama-.*-bin-win-cpu-x64\.zip$/i) || first(/^llama-.*-bin-win-x64\.zip$/i);
    if (m) out.push(m);
  }
  return out;
}

function downloadToFile(u, dest, onProgress) {
  return new Promise((resolve, reject) => {
    const req = net.request({ method: "GET", url: u, redirect: "follow" });
    req.setHeader("User-Agent", "LevAAISummary");
    let fileStream;
    try { fileStream = fs.createWriteStream(dest); }
    catch (e) { return reject(e); }
    let received = 0, total = 0, lastT = Date.now(), lastB = 0;
    req.on("response", (res) => {
      if (res.statusCode >= 400) { try { fileStream.destroy(); } catch (e) {} return reject(new Error("HTTP " + res.statusCode)); }
      const len = res.headers["content-length"];
      total = parseInt(Array.isArray(len) ? len[0] : len, 10) || 0;
      res.on("data", (chunk) => {
        received += chunk.length;
        const ok = fileStream.write(chunk);
        if (!ok) { res.pause(); fileStream.once("drain", () => res.resume()); }
        const now = Date.now();
        if (now - lastT >= 300 && onProgress) {
          const speed = (received - lastB) / ((now - lastT) / 1000);
          lastT = now; lastB = received;
          onProgress({ received, total, percent: total ? (received / total) * 100 : 0, speed });
        }
      });
      res.on("end", () => fileStream.end(() => resolve()));
      res.on("error", (e) => { try { fileStream.destroy(); } catch (er) {} reject(e); });
    });
    req.on("error", (e) => { try { fileStream.destroy(); } catch (er) {} reject(e); });
    req.end();
  });
}

// 하위 폴더 포함해서 파일명을 재귀 검색
function findFileRec(dir, name) {
  let ents = [];
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return null; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { const r = findFileRec(p, name); if (r) return r; }
    else if (e.name.toLowerCase() === name.toLowerCase()) return p;
  }
  return null;
}

// zip 내부에 build/bin 등 하위폴더가 있으면 대상 파일이 있는 폴더의 내용을 루트로 끌어올린다.
function flattenExtract(dir, anchorName) {
  if (fs.existsSync(path.join(dir, anchorName))) return;
  const found = findFileRec(dir, anchorName);
  if (!found) return;
  const srcDir = path.dirname(found);
  if (path.resolve(srcDir) === path.resolve(dir)) return;
  for (const f of fs.readdirSync(srcDir)) {
    try { fs.renameSync(path.join(srcDir, f), path.join(dir, f)); } catch (e) {}
  }
}

// 빠른 압축해제: Windows 10+ 내장 tar(bsdtar) 우선, 실패 시 adm-zip 폴백.
function extractZip(zipPath, destDir) {
  return new Promise((resolve) => {
    const fallback = () => {
      try {
        const AdmZip = require("adm-zip");
        new AdmZip(zipPath).extractAllTo(destDir, true);
        resolve({ ok: true, method: "adm-zip" });
      } catch (e) { resolve({ ok: false, error: String(e.message || e) }); }
    };
    let cp;
    try {
      cp = spawn("tar", ["-xf", zipPath, "-C", destDir], { windowsHide: true });
    } catch (e) { return fallback(); }
    let errored = false;
    cp.on("error", () => { errored = true; fallback(); });
    cp.on("exit", (code) => {
      if (errored) return;
      if (code === 0) resolve({ ok: true, method: "tar" });
      else fallback();
    });
  });
}

async function ensureLlamaCpp(onProgress) {
  const serverExe = path.join(llamaCppDir(), "llama-server.exe");
  if (fs.existsSync(serverExe)) return { ok: true, already: true };
  const backend = pickLlamaBackend();
  const emit = (p) => { try { onProgress && onProgress({ backend, ...p }); } catch (e) {} };
  emit({ phase: "info", message: "llama.cpp 릴리스 조회 중…" });
  let release;
  try { release = await httpGetJson("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"); }
  catch (e) { return { ok: false, backend, error: "릴리스 조회 실패: " + (e.message || e) }; }
  let urls = pickLlamaAssets(release.assets, backend);
  let usedBackend = backend;
  if (!urls.length && backend !== "vulkan") { urls = pickLlamaAssets(release.assets, "vulkan"); usedBackend = "vulkan"; }
  if (!urls.length) { urls = pickLlamaAssets(release.assets, "cpu"); usedBackend = "cpu"; }
  if (!urls.length) return { ok: false, backend, error: "Windows용 llama.cpp 자산을 찾지 못했습니다. llamacpp 폴더를 직접 넣어 주세요." };

  try { fs.mkdirSync(llamaCppDir(), { recursive: true }); } catch (e) {}
  for (let i = 0; i < urls.length; i++) {
    const tmp = path.join(llamaCppDir(), "_dl_" + i + ".zip");
    emit({ phase: "download", backend: usedBackend, index: i + 1, count: urls.length });
    try {
      await downloadToFile(urls[i], tmp, (p) => emit({ phase: "download", backend: usedBackend, index: i + 1, count: urls.length, ...p }));
    } catch (e) { return { ok: false, backend: usedBackend, error: "다운로드 실패: " + (e.message || e) }; }
    emit({ phase: "extract", backend: usedBackend, index: i + 1, count: urls.length });
    const ex = await extractZip(tmp, llamaCppDir()); // 네이티브 tar → adm-zip 폴백
    if (!ex.ok) return { ok: false, backend: usedBackend, error: "압축 해제 실패: " + ex.error };
    try { fs.unlinkSync(tmp); } catch (e) {}
  }
  flattenExtract(llamaCppDir(), "llama-server.exe"); // 하위폴더로 풀렸으면 루트로 끌어올림
  const ok = fs.existsSync(serverExe);
  if (ok) saveConfig({ llamaServerExe: serverExe });
  return { ok, backend: usedBackend, error: ok ? "" : "llama-server.exe 를 찾지 못했습니다(압축 구조 확인)." };
}

// 준비 창(진행률) — llama.cpp가 없을 때만 표시
let setupWin = null;
function sendSetup(p) {
  if (setupWin && !setupWin.isDestroyed()) setupWin.webContents.send("llama-setup-progress", p);
}
async function runLlamaSetup() {
  const res = await ensureLlamaCpp(sendSetup);
  if (res.ok) {
    sendSetup({ phase: "done", backend: res.backend });
    setTimeout(() => { if (setupWin && !setupWin.isDestroyed()) setupWin.close(); }, 1200);
  } else {
    sendSetup({ phase: "error", error: res.error, backend: res.backend });
  }
  return res;
}
async function maybeSetupLlamaCpp() {
  if (fs.existsSync(path.join(llamaCppDir(), "llama-server.exe"))) return;
  setupWin = new BrowserWindow({
    width: 460, height: 260, resizable: false, minimizable: false, maximizable: false,
    title: "LevA 준비", backgroundColor: "#0e0f13", autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false },
  });
  setupWin.loadFile("setup.html");
  await new Promise((r) => setupWin.webContents.once("did-finish-load", r));
  await runLlamaSetup();
}
ipcMain.handle("llama-setup-retry", () => runLlamaSetup());

ipcMain.handle("hf-list-models", () =>
  MODEL_CATALOG.map((m) => ({
    key: m.key, name: m.name, desc: m.desc,
    downloaded: isModelDownloaded(m),
  }))
);

ipcMain.handle("hf-download-cancel", () => {
  if (!hfDL) return { ok: false };
  hfDL.cancelled = true;
  try { hfDL.request.abort(); } catch (e) {}
  try { hfDL.fileStream.destroy(); } catch (e) {}
  try { fs.unlinkSync(hfDL.tmpPath); } catch (e) {}
  hfDL = null;
  return { ok: true };
});

ipcMain.handle("hf-download-model", async (_e, key) => {
  const m = MODEL_CATALOG.find((x) => x.key === key);
  if (!m) return { ok: false, message: "알 수 없는 모델입니다." };
  if (hfDL) return { ok: false, key, message: "이미 다운로드가 진행 중입니다." };
  return runDownload(m);
});

function runDownload(m) {
  const dir = hfDefaultDir();
  try { fs.mkdirSync(dir, { recursive: true }); }
  catch (e) { return Promise.resolve({ ok: false, key: m.key, message: "폴더 생성 실패: " + e.message }); }

  const destPath = modelDestPath(m);
  const tmpPath = destPath + ".part";

  return new Promise((resolve) => {
    let settled = false;
    const done = (r) => { if (!settled) { settled = true; resolve(r); } };

    let fileStream;
    try { fileStream = fs.createWriteStream(tmpPath); }
    catch (e) { return done({ ok: false, key: m.key, message: "파일 생성 실패: " + e.message }); }

    const request = net.request({ method: "GET", url: m.url, redirect: "follow" });
    request.setHeader("User-Agent", "LevAAISummary");

    hfDL = { request, fileStream, tmpPath, destPath, cancelled: false };

    let received = 0, total = 0;
    let lastT = Date.now(), lastBytes = 0, speed = 0;

    const cleanup = (removeTmp) => {
      try { fileStream.destroy(); } catch (e) {}
      if (removeTmp) { try { fs.unlinkSync(tmpPath); } catch (e) {} }
      hfDL = null;
    };

    request.on("response", (response) => {
      if (response.statusCode >= 400) {
        cleanup(true);
        return done({ ok: false, key: m.key, message: "다운로드 실패 (HTTP " + response.statusCode + ")" });
      }
      const len = response.headers["content-length"];
      total = parseInt(Array.isArray(len) ? len[0] : len, 10);
      if (isNaN(total)) total = 0;

      response.on("data", (chunk) => {
        if (!hfDL || hfDL.cancelled) return;
        received += chunk.length;
        const ok = fileStream.write(chunk);
        if (!ok) { response.pause(); fileStream.once("drain", () => response.resume()); }
        const now = Date.now();
        if (now - lastT >= 350) {
          speed = (received - lastBytes) / ((now - lastT) / 1000);
          lastT = now; lastBytes = received;
          sendHfProgress({
            key: m.key, received, total,
            percent: total ? (received / total) * 100 : 0,
            speed,
          });
        }
      });

      response.on("end", () => {
        if (!hfDL || hfDL.cancelled) return;
        fileStream.end(() => {
          try { fs.renameSync(tmpPath, destPath); } catch (e) {}
          sendHfProgress({
            key: m.key, received, total: total || received,
            percent: 100, speed: 0, done: true,
          });
          const patch = {}; patch[m.assignTo] = destPath; saveConfig(patch);
          hfDL = null;
          done({ ok: true, key: m.key, path: destPath });
        });
      });

      response.on("error", (err) => {
        cleanup(true);
        done({ ok: false, key: m.key, message: "스트림 오류: " + err.message });
      });
    });

    request.on("error", (err) => {
      if (hfDL && hfDL.cancelled) { done({ ok: false, key: m.key, message: "취소됨" }); return; }
      cleanup(true);
      done({ ok: false, key: m.key, message: "오류: " + err.message });
    });

    request.end();
  });
}

// ---- 온디바이스 LLM(llama.cpp) 호출 -----------------------------------
// 캐시 워커가 관리하는 llama-server 를 그대로 사용(포트 충돌 방지).
async function computeAnswer(prompt) {
  try {
    const r = await sendCacheCommand("chat", { prompt }, { timeout: 300000 });
    if (r && r.ok && (r.answer || "").trim()) return r.answer.trim();
    if (r && r.error) {
      if (/textModel|gguf|경로/i.test(r.error))
        return "대화 모델이 아직 없어요. 설정 → 모델에서 'Gemma 4 E4B'를 받아 주세요.";
      return "llama.cpp 응답 오류: " + r.error;
    }
    return "llama.cpp 서버에서 답을 받지 못했어요.";
  } catch (e) {
    return "로컬 llama.cpp 서버에 연결하지 못했어요. 설정에서 llama-server 경로와 모델(gguf)이 지정됐는지 확인해 주세요.";
  }
}

ipcMain.handle("ask-llm", async (_e, prompt) => {
  const answer = await computeAnswer(prompt);
  try {
    appendChatLog({ ts: Date.now(), question: String(prompt || ""), answer });
  } catch (e) {}
  if (dashboardWin && !dashboardWin.isDestroyed())
    dashboardWin.webContents.send("chat-logged");
  return answer;
});

ipcMain.handle("chat-log", () => readChatLog());
ipcMain.handle("clear-chat-log", () => {
  try { fs.writeFileSync(chatLogPath(), "[]", "utf8"); } catch (e) {}
  return true;
});

app.whenReady().then(async () => {
  syncDownloadedModels();
  await maybeSetupLlamaCpp(); // llamacpp 없으면 GitHub 릴리스에서 받아 압축해제
  resolveLlamaServer();
  createWindow();
  startDaemon();
  startCacheWorker();
});

app.on("before-quit", () => {
  if (daemon) {
    try { sendCommand("shutdown").catch(() => {}); } catch (e) {}
    setTimeout(() => { if (daemon) daemon.kill(); }, 300);
  }
  if (cacheProc) {
    try { sendCacheCommand("shutdown", {}, { expectResult: false }); } catch (e) {}
    setTimeout(() => { if (cacheProc) cacheProc.kill(); }, 500);
  }
});

// (HF 다운로드 기능 포함)
app.on("window-all-closed", () => app.quit());
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
