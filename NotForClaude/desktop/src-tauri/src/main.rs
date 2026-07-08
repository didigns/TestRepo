// AISummary desktop shell.
//
// Spawns the local Python backend (FastAPI on 127.0.0.1:8756) as a child
// process on startup, waits for the port to come up, then the window loads
// that URL. When the app exits, the backend child is killed.
//
// Assistant behaviour:
//   * Closing the main window HIDES it to the system tray (keeps running in
//     the background). Re-open from the tray menu; "종료" fully quits.
//   * Global hotkey Alt+Space toggles a small "quick ask" window at the
//     bottom-center of the screen. Submitting a question runs it on the
//     backend, which pops a native Windows alert with the answer when done.
//
// Backend discovery works both in dev (repo tree) and in the installed layout
// produced by installer/install.ps1:
//   <InstallDir>\AISummary.exe
//   <InstallDir>\backend\aisummary\...
//   <InstallDir>\runtime\Scripts\python.exe   (venv created by the installer)

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod updater;

use std::io::Write;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::Manager;

const BACKEND_ADDR: &str = "127.0.0.1:8756";

struct Backend(Mutex<Option<Child>>);

/// Append a line to the desktop-shell log (~/.aisummary/desktop.log) so
/// backend-launch failures are diagnosable from a released build.
fn log_line(msg: &str) {
    if let Some(mut dir) = home_dir() {
        dir.push(".aisummary");
        let _ = std::fs::create_dir_all(&dir);
        dir.push("desktop.log");
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&dir) {
            let _ = writeln!(f, "[shell] {msg}");
        }
    }
    eprintln!("[shell] {msg}");
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

/// Find the directory that contains the `aisummary` Python package, checking
/// exe-relative locations first (installed app) then cwd-relative (dev).
fn find_backend_dir() -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            candidates.push(exe_dir.join("backend"));
            candidates.push(exe_dir.join("..").join("backend"));
        }
    }
    for rel in ["../../backend", "../backend", "backend"] {
        candidates.push(PathBuf::from(rel));
    }
    candidates
        .into_iter()
        .find(|d| d.join("aisummary").is_dir())
}

/// Prefer the installer-created venv python; fall back to system interpreters.
fn find_python(backend_dir: &Path) -> Vec<PathBuf> {
    let mut pys: Vec<PathBuf> = Vec::new();
    // venv candidates relative to the backend dir's parent (= install dir)
    if let Some(base) = backend_dir.parent() {
        pys.push(base.join("runtime").join("Scripts").join("python.exe")); // Windows
        pys.push(base.join("runtime").join("bin").join("python")); // Unix
    }
    // system fallbacks (resolved via PATH)
    for name in ["python", "py", "python3"] {
        pys.push(PathBuf::from(name));
    }
    pys.into_iter()
        .filter(|p| {
            // keep absolute paths only if they exist; keep bare names as-is
            p.is_absolute() && p.exists() || !p.is_absolute()
        })
        .collect()
}

/// Poll the backend TCP port until it accepts connections or times out.
fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(
            &BACKEND_ADDR.parse().unwrap(),
            Duration::from_millis(500),
        )
        .is_ok()
        {
            return true;
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    false
}

fn spawn_backend() -> Option<Child> {
    let backend_dir = match find_backend_dir() {
        Some(d) => d,
        None => {
            log_line("backend 디렉터리를 찾지 못함 (aisummary 패키지 없음)");
            return None;
        }
    };
    log_line(&format!("backend dir: {}", backend_dir.display()));

    // Log destination for the backend's own stdout/stderr.
    let mut log_path = home_dir().unwrap_or_else(|| PathBuf::from("."));
    log_path.push(".aisummary");
    let _ = std::fs::create_dir_all(&log_path);
    log_path.push("server.log");

    for py in find_python(&backend_dir) {
        log_line(&format!("python 시도: {}", py.display()));
        let (out, err) = match std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
        {
            Ok(f) => {
                let f2 = f.try_clone().ok();
                (Stdio::from(f), f2.map(Stdio::from).unwrap_or_else(Stdio::null))
            }
            Err(_) => (Stdio::null(), Stdio::null()),
        };
        let mut cmd = Command::new(&py);
        cmd.args(["-m", "aisummary.api"])
            .current_dir(&backend_dir)
            .stdout(out)
            .stderr(err);
        // Don't pop a console window for the Python child on Windows.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        match cmd.spawn() {
            Ok(child) => {
                log_line(&format!("backend 시작됨 (python={})", py.display()));
                return Some(child);
            }
            Err(e) => log_line(&format!("실행 실패 {}: {e}", py.display())),
        }
    }
    log_line("사용 가능한 python으로 backend를 시작하지 못함");
    None
}

/// Manual "check for updates" entry point, callable from the UI via
/// `window.__TAURI__.core.invoke('check_for_updates')`. Shows a dialog even
/// when already up to date.
#[tauri::command]
fn check_for_updates() {
    let current = env!("CARGO_PKG_VERSION").to_string();
    std::thread::spawn(move || updater::run_startup_check(&current, false));
}

/// Hide the quick-ask window (called from its own page on submit / Esc).
#[tauri::command]
fn hide_quick(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("quick") {
        let _ = win.hide();
    }
}

/// Place the quick-ask window at the bottom-center of the monitor it's on.
fn position_quick(win: &tauri::WebviewWindow) {
    if let Ok(Some(monitor)) = win.current_monitor() {
        let scr = monitor.size();
        let origin = monitor.position();
        if let Ok(size) = win.outer_size() {
            let x = origin.x + ((scr.width as i32) - (size.width as i32)) / 2;
            let y = origin.y + (scr.height as i32) - (size.height as i32) - 140;
            let _ = win.set_position(tauri::PhysicalPosition { x, y });
        }
    }
}

/// Alt+Space: show the quick-ask window (positioned + focused) or hide it.
fn quick_toggle(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("quick") {
        if win.is_visible().unwrap_or(false) {
            let _ = win.hide();
        } else {
            position_quick(&win);
            let _ = win.show();
            let _ = win.set_focus();
        }
    }
}

fn main() {
    // Startup update check (non-blocking). Skip with AISUMMARY_NO_UPDATE=1.
    if std::env::var("AISUMMARY_NO_UPDATE").ok().as_deref() != Some("1") {
        let current = env!("CARGO_PKG_VERSION").to_string();
        std::thread::spawn(move || updater::run_startup_check(&current, true));
    }

    // When launched by the Python launcher (AISUMMARY_NO_BACKEND=1) the backend is
    // already running and health-checked, so we skip spawning and waiting —
    // the window loads a ready server. Otherwise (e.g. dev / direct launch)
    // we start it ourselves and wait for the port.
    let managed = std::env::var("AISUMMARY_NO_BACKEND").ok().as_deref() == Some("1");
    let child = if managed { None } else { spawn_backend() };
    if child.is_some() {
        if wait_for_backend(Duration::from_secs(45)) {
            log_line("backend 포트 응답 확인 (127.0.0.1:8756)");
        } else {
            log_line("backend 포트 대기 시간 초과 — 창은 계속 진행");
        }
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(Backend(Mutex::new(child)))
        .invoke_handler(tauri::generate_handler![check_for_updates, hide_quick])
        .setup(|app| {
            // ---- system tray (background assistant) ----
            use tauri::menu::{Menu, MenuItem};
            use tauri::tray::TrayIconBuilder;
            let show_i = MenuItem::with_id(app, "show", "AISummary 열기", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &quit_i])?;
            let mut tray = TrayIconBuilder::with_id("main")
                .tooltip("AISummary — 백그라운드 실행 중 (Alt+Space 빠른 질문)")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.unminimize();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                });
            if let Some(icon) = app.default_window_icon().cloned() {
                tray = tray.icon(icon);
            }
            tray.build(app)?;

            // ---- global hotkey: Alt+Space -> toggle quick ask ----
            use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
            let hotkey = Shortcut::new(Some(Modifiers::ALT), Code::Space);
            let _ = app.global_shortcut().on_shortcut(hotkey, |app, _sc, event| {
                if event.state() == ShortcutState::Pressed {
                    quick_toggle(app);
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| match event {
            // Closing a window never quits the app: the main window hides to the
            // tray (assistant keeps running); the quick window just hides.
            tauri::WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            // Spotlight behaviour: the quick-ask window disappears on blur.
            tauri::WindowEvent::Focused(false) => {
                if window.label() == "quick" {
                    let _ = window.hide();
                }
            }
            // A real exit (tray "종료" -> app.exit) destroys windows: stop backend.
            tauri::WindowEvent::Destroyed => {
                if window.label() == "main" {
                    if let Some(state) = window.app_handle().try_state::<Backend>() {
                        if let Some(mut child) = state.0.lock().unwrap().take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("error while running AISummary");
}
