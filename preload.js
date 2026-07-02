const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leva", {
  // 마우스 클릭 통과 토글 (위젯 위 = false, 빈 영역 = true)
  setIgnoreMouse: (ignore) => ipcRenderer.send("set-ignore-mouse", ignore),
  quit: () => ipcRenderer.send("quit-app"),
  ask: (prompt) => ipcRenderer.invoke("ask-llm", prompt),

  // 설정 창
  openSettings: () => ipcRenderer.send("open-settings"),
  closeSettings: () => ipcRenderer.send("close-settings"),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  saveSettings: (cfg) => ipcRenderer.invoke("save-settings", cfg),

  // 폴더 관리 (LevAObserver 데몬)
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
  pickFile: (kind) => ipcRenderer.invoke("pick-file", kind),
  observerStatus: () => ipcRenderer.invoke("observer-status"),
  observerList: () => ipcRenderer.invoke("observer-list"),
  observerAdd: (path, recursive) => ipcRenderer.invoke("observer-add", { path, recursive }),
  observerRemove: (watchId, path) => ipcRenderer.invoke("observer-remove", { watchId, path }),
  observerSetIgnore: (patterns) => ipcRenderer.invoke("observer-set-ignore", patterns),

  // AI 캐싱 (LevACache 워커)
  cacheStatus: () => ipcRenderer.invoke("cache-status"),
  cacheList: () => ipcRenderer.invoke("cache-list"),
  cacheFile: (path) => ipcRenderer.invoke("cache-file", path),
  cacheRescan: () => ipcRenderer.invoke("cache-rescan"),

  // 이벤트 수신 (HUD 말풍선용)
  onFsEvent: (cb) => ipcRenderer.on("fs-event", (_e, payload) => cb(payload)),
  onCacheEvent: (cb) => ipcRenderer.on("cache-event", (_e, payload) => cb(payload)),
});
