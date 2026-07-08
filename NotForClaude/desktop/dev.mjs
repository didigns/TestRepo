// AISummary dev launcher: starts the Python backend AND the Tauri app together
// with `npm run dev`, so you never forget the server (or leave a stale one).
//
// Flow: free port 8756 (kill orphaned backends from a previous run) -> start
// the backend -> wait until it's listening -> launch `tauri dev` telling the
// shell the backend is already managed (AISUMMARY_NO_BACKEND=1). On exit both
// are stopped; if the backend crashes it is auto-restarted.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import net from 'node:net';

const here = dirname(fileURLToPath(import.meta.url));
const backendDir = join(here, '..', 'backend');
const PORT = 8756;
const isWin = process.platform === 'win32';
const PY = process.env.PYTHON || (isWin ? 'python' : 'python3');

let shuttingDown = false;
let backend = null;
let tauri = null;

function log(m) { console.log(`[dev] ${m}`); }

// Kill whatever is holding the backend port (a leftover backend from a prior run).
function freePort(port) {
  return new Promise((resolve) => {
    const cmd = isWin
      ? `for /f "tokens=5" %a in ('netstat -aon ^| findstr :${port} ^| findstr LISTENING') do @taskkill /F /PID %a >nul 2>&1`
      : `lsof -ti tcp:${port} | xargs -r kill -9 2>/dev/null; true`;
    const p = spawn(cmd, { stdio: 'ignore', shell: true });
    p.on('close', () => resolve());
    p.on('error', () => resolve());
  });
}

// Resolve once the backend accepts TCP connections (or timeout).
function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const tick = () => {
      const s = net.connect(port, '127.0.0.1');
      s.on('connect', () => { s.destroy(); resolve(true); });
      s.on('error', () => {
        s.destroy();
        if (Date.now() > deadline) resolve(false);
        else setTimeout(tick, 400);
      });
    };
    tick();
  });
}

function startBackend() {
  backend = spawn(PY, ['-m', 'aisummary.api'], { cwd: backendDir, stdio: 'inherit' });
  backend.on('error', (e) => log(`백엔드 실행 실패: ${e.message} (python이 PATH에 있는지 확인)`));
  backend.on('close', (code) => {
    if (shuttingDown) return;
    log(`백엔드가 종료됨 (code=${code}) — 2초 후 재시작`);
    setTimeout(startBackend, 2000);
  });
}

function stop(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  try { if (tauri) tauri.kill(); } catch {}
  try { if (backend) backend.kill(); } catch {}
  setTimeout(() => process.exit(code), 300);
}

async function main() {
  log('포트 8756 정리 중…');
  await freePort(PORT);
  log(`백엔드 시작: ${PY} -m aisummary.api  (cwd=${backendDir})`);
  startBackend();
  log('백엔드 준비 대기…');
  const up = await waitForPort(PORT, 60000);
  log(up ? '백엔드 준비 완료 (127.0.0.1:8756)' : '백엔드 대기 시간 초과 — 앱은 계속 진행(재연결 대기)');

  log('앱 실행: tauri dev');
  tauri = spawn('tauri', ['dev'], {
    cwd: here,
    stdio: 'inherit',
    shell: isWin,   // resolve the tauri CLI from node_modules/.bin on Windows
    env: { ...process.env, AISUMMARY_NO_BACKEND: '1', AISUMMARY_NO_UPDATE: '1' },
  });
  tauri.on('error', (e) => { log(`tauri 실행 실패: ${e.message}`); stop(1); });
  tauri.on('close', (code) => stop(code || 0));

  process.on('SIGINT', () => stop(0));
  process.on('SIGTERM', () => stop(0));
}

main();
