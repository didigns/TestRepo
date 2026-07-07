const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leva", {
  // 마우스 클릭 통과 토글 (위젯 위 = false, 빈 영역 = true)
  setIgnoreMouse: (ignore) => ipcRenderer.send("set-ignore-mouse", ignore),
  quit: () => ipcRenderer.send("quit-app"),
  ask: (prompt) => ipcRenderer.invoke("ask-llm", prompt),
  onChatChunk: (cb) => ipcRenderer.on("chat-chunk", (_e, payload) => cb(payload)),

  // 설정 창
  openSettings: () => ipcRenderer.send("open-settings"),
  closeSettings: () => ipcRenderer.send("close-settings"),

  // 대시보드 창
  openDashboard: () => ipcRenderer.send("open-dashboard"),
  closeDashboard: () => ipcRenderer.send("close-dashboard"),

  // 회의록 창 — 녹음 저장·PDF 내보내기
  openMeeting: () => ipcRenderer.send("open-meeting"),
  saveRecording: (buf, title) => ipcRenderer.invoke("save-recording", buf, title),
  listRecordings: () => ipcRenderer.invoke("list-recordings"),
  liveTranscribe: (wavBuf) => ipcRenderer.invoke("live-transcribe", wavBuf),
  // 스트리밍 라이브 자막(faster-whisper)
  liveStart: () => ipcRenderer.invoke("live-start"),
  liveStop: () => ipcRenderer.invoke("live-stop"),
  liveAudio: (buf) => ipcRenderer.send("live-audio", buf),
  onLiveText: (cb) => ipcRenderer.on("live-text", (_e, payload) => cb(payload)),
  onLiveMinutes: (cb) => ipcRenderer.on("live-minutes", (_e, payload) => cb(payload)),
  exportMeetingPdf: (payload) => ipcRenderer.invoke("export-meeting-pdf", payload),

  // llama.cpp 준비(자동 설치)
  onLlamaSetupProgress: (cb) => ipcRenderer.on("llama-setup-progress", (_e, p) => cb(p)),
  retryLlamaSetup: () => ipcRenderer.invoke("llama-setup-retry"),

  // 채팅 로그
  getChatLog: () => ipcRenderer.invoke("chat-log"),
  clearChatLog: () => ipcRenderer.invoke("clear-chat-log"),
  onChatLogged: (cb) => ipcRenderer.on("chat-logged", () => cb()),

  // 파일 뷰어
  openViewer: (p, highlight) => ipcRenderer.send("open-viewer", p, highlight || ""),
  getViewerFile: () => ipcRenderer.invoke("viewer-file"),
  fileInfo: (p) => ipcRenderer.invoke("file-info", p),
  readFile: (p) => ipcRenderer.invoke("read-file", p),
  readXlsx: (p) => ipcRenderer.invoke("read-xlsx", p),
  readDocx: (p) => ipcRenderer.invoke("read-docx", p),
  openExternalFile: (p) => ipcRenderer.invoke("open-external-file", p),
  onViewerFile: (cb) => ipcRenderer.on("viewer-file", (_e, info) => cb(info)),
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
  cacheRetry: (path) => ipcRenderer.invoke("cache-retry", path),
  cachePause: () => ipcRenderer.invoke("cache-pause"),
  cacheResume: () => ipcRenderer.invoke("cache-resume"),
  cacheRescan: () => ipcRenderer.invoke("cache-rescan"),
  clearCacheDb: () => ipcRenderer.invoke("cache-clear-db"),

  // 엔진(llama-server) 상태·재시작 / 검색 테스트
  engineStatus: () => ipcRenderer.invoke("engine-status"),
  engineRestart: (target) => ipcRenderer.invoke("engine-restart", target),
  searchTest: (query) => ipcRenderer.invoke("search-test", query),
  extractText: (p) => ipcRenderer.invoke("extract-text", p), // 뷰어 하이라이트용 본문
  readBin: (p) => ipcRenderer.invoke("read-bin", p), // PDF 바이너리(pdf.js 렌더링용)

  // 대시보드 통계 / 키워드 탐색기
  cacheStats: () => ipcRenderer.invoke("cache-stats"),
  usageStats: () => ipcRenderer.invoke("usage-stats"),
  trashFiles: (paths) => ipcRenderer.invoke("trash-files", paths),
  uncacheFiles: (paths) => ipcRenderer.invoke("uncache-files", paths),
  onConsoleNav: (cb) => ipcRenderer.on("console-nav", (_e, section) => cb(section)),

  whisperSetup: () => ipcRenderer.invoke("whisper-setup"), // whisper.cpp(STT) 설치

  // Hugging Face 모델 다운로드 (모델 카탈로그 기반)
  hfListModels: () => ipcRenderer.invoke("hf-list-models"),
  whisperSetActive: (key) => ipcRenderer.invoke("whisper-set-active", key), // 전사 활성 모델 전환
  hfDownloadModel: (key) => ipcRenderer.invoke("hf-download-model", key),
  hfDownloadCancel: () => ipcRenderer.invoke("hf-download-cancel"),
  onHfProgress: (cb) => ipcRenderer.on("hf-download-progress", (_e, payload) => cb(payload)),

  // 이벤트 수신 (HUD 말풍선용)
  onFsEvent: (cb) => ipcRenderer.on("fs-event", (_e, payload) => cb(payload)),
  onCacheEvent: (cb) => ipcRenderer.on("cache-event", (_e, payload) => cb(payload)),
  onFocusInput: (cb) => ipcRenderer.on("focus-input", () => cb()), // 글로벌 단축키 → HUD 입력
});
