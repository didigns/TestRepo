// OwnYourPC desktop shell.
//
// Spawns the local Python backend (FastAPI on 127.0.0.1:8756) as a child
// process on startup, then the window (configured in tauri.conf.json) loads
// that URL. When the app exits, the backend child is killed.
//
// For a fully bundled installer, replace the `Command::new("python")` spawn
// with a PyInstaller-built sidecar declared under bundle.externalBin and
// launched via tauri-plugin-shell.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct Backend(Mutex<Option<Child>>);

fn spawn_backend() -> Option<Child> {
    // Try the repo-relative backend dir from a few plausible working dirs
    // (tauri dev vs a bundled exe differ). Best-effort: if none work, the
    // user can start the backend manually (python -m ownyourpc.api).
    for dir in ["../../backend", "../backend", "backend"] {
        if std::path::Path::new(dir).join("ownyourpc").is_dir() {
            if let Ok(child) = Command::new("python")
                .args(["-m", "ownyourpc.api"])
                .current_dir(dir)
                .spawn()
            {
                return Some(child);
            }
        }
    }
    None
}

fn main() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(spawn_backend())))
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.app_handle().try_state::<Backend>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running OwnYourPC");
}
