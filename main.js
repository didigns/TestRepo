const { app, BrowserWindow, ipcMain, screen, net, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

let win;
let settingsWin = null;

// ---- 설정 저장/로드 --------------------------------------------------
const DEFAULT_CONFIG = {
  model: "llama3.2",
  endpoint: "http://127.0.0.1:11434",
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

function startDaemon() {
  const cfg = loadConfig();
  const script = daemonScriptPath();
  if (!fs.existsSync(script)) {
    daemonError = "folder_observer.py 를 찾을 수 없습니다: " + script;
    return;
  }
  const py = cfg.pythonPath || "python";
  try {
    daemon = spawn(py, [script, "--daemon"], {
      cwd: __dirname,
      // Windows에서 Python 표준입출력을 UTF-8로 강제해 한글 깨짐 방지
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    });
  } catch (e) {
    daemonError = "Python 실행 실패: " + e.message;
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
    // 파일 생성/수정 시 캐싱 큐잉
    if (cfg.enableCache &&
        (msg.eventType === "created" || msg.eventType === "modified") &&
        !msg.isDirectory) {
      const fp = msg.path || msg.destPath;
      if (fp && CACHE_EXTS.has(extOf(fp))) enqueueCache(fp);
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
  ".docx", ".xlsx", ".pptx", ".hwp", ".rtf", ".py", ".js", ".ts",
]);
function extOf(p) {
  const i = p.lastIndexOf(".");
  return i >= 0 ? p.slice(i).toLowerCase() : "";
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
  const cfg = loadConfig();
  if (!cfg.enableCache) return;
  const py = cfg.pythonPath || "python";
  const args = [
    "-m", "LevACache.worker", "--daemon",
    "--config", cacheConfigPath(),
    "--db", cacheDbPath(),
  ];
  try {
    cacheProc = spawn(py, args, {
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
    backfillCache();
    return;
  }
  if (msg.type === "cache-event") {
    if (win && !win.isDestroyed()) win.webContents.send("cache-event", msg);
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
  if (cacheDebounce.has(fp)) clearTimeout(cacheDebounce.get(fp));
  cacheDebounce.set(fp, setTimeout(() => {
    cacheDebounce.delete(fp);
    sendCacheCommand("cache", { path: fp }, { expectResult: false }).catch(() => {});
  }, 1500));
}

// 시작 시 감시 폴더 백필(해시 다른 파일만 캐싱)
async function backfillCache() {
  const cfg = loadConfig();
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
    width: 440,
    height: 640,
    resizable: false,
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
ipcMain.handle("cache-list", async () => {
  const r = await sendCacheCommand("list");
  return r.entries || [];
});
ipcMain.handle("cache-file", async (_e, filePath) => {
  return sendCacheCommand("cache", { path: filePath, force: true });
});
ipcMain.handle("cache-rescan", async () => {
  const cfg = loadConfig();
  const paths = (cfg.watchedFolders || []).map((w) => w.path);
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

// ---- 온디바이스 LLM(Ollama) 호출 --------------------------------------
ipcMain.handle("ask-llm", async (_e, prompt) => {
  const cfg = loadConfig();
  const body = JSON.stringify({
    model: cfg.model || "llama3.2",
    prompt,
    stream: false,
  });

  return new Promise((resolve) => {
    const request = net.request({
      method: "POST",
      url: `${cfg.endpoint || "http://127.0.0.1:11434"}/api/generate`,
    });
    let data = "";
    let done = false;
    const finish = (text) => {
      if (done) return;
      done = true;
      resolve(text);
    };

    const timer = setTimeout(() => {
      request.abort();
      finish(fallback(prompt));
    }, 60000);

    request.on("response", (response) => {
      response.on("data", (chunk) => (data += chunk));
      response.on("end", () => {
        clearTimeout(timer);
        try {
          const json = JSON.parse(data);
          finish((json.response || "").trim() || fallback(prompt));
        } catch (e) {
          finish(fallback(prompt));
        }
      });
    });
    request.on("error", () => {
      clearTimeout(timer);
      finish(
        "로컬 LLM(Ollama)에 연결하지 못했어요. Ollama가 실행 중인지 확인해 주세요.\n(예: `ollama run llama3.2`)"
      );
    });
    request.setHeader("Content-Type", "application/json");
    request.write(body);
    request.end();
  });
});

function fallback(prompt) {
  return `지금은 데모 응답이에요. Ollama를 켜면 실제로 답할게요.\n방금 이렇게 물어봤죠: "${prompt}"`;
}

app.whenReady().then(() => {
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

app.on("window-all-closed", () => app.quit());
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
