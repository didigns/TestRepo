// OwnYourPC auto-updater.
//
// On app startup we fetch a small `.meta` JSON hosted on Google Drive. It
// carries the latest version string and a Drive link to the full installer
// (.exe). If the remote version is newer than the running app, we ask the
// user, download the installer (verifying its SHA-256 when provided), launch
// it, and exit so it can replace the installed files.
//
// The `.meta` file id and (optionally) the meta URL are configurable:
//   - OYPC_META_URL   full URL to the .meta (overrides everything)
//   - OYPC_META_ID    Drive file id of the .meta
// Default is the file id baked in below.

use std::io::Read;
use std::path::PathBuf;
use std::time::Duration;

use serde::Deserialize;

/// Drive file id of the `.meta` document (the link the user shared).
const DEFAULT_META_ID: &str = "1hsD3y7bAJYCDdE1TR31gE6odYVWEy2T_";

#[derive(Debug, Deserialize)]
pub struct Meta {
    pub version: String,
    #[serde(default)]
    pub notes: String,
    #[serde(default)]
    #[allow(dead_code)] // parsed for completeness; update flow now owned by the launcher
    pub mandatory: bool,
    pub installer: Installer,
}

#[derive(Debug, Deserialize)]
pub struct Installer {
    #[serde(default = "default_filename")]
    pub filename: String,
    pub url: String,
    #[serde(default)]
    #[allow(dead_code)] // parsed from .meta but not used on the Rust side
    pub size: u64,
    #[serde(default)]
    pub sha256: String,
}

fn default_filename() -> String {
    "OwnYourPC-setup.exe".to_string()
}

/// Build a direct-download URL for a Google Drive file id. Uses the
/// usercontent endpoint with `confirm=t`, which bypasses the virus-scan
/// interstitial for large files and returns the bytes directly.
fn drive_download_url(id: &str) -> String {
    format!(
        "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"
    )
}

/// Extract a Drive file id from the common URL shapes:
///   https://drive.google.com/file/d/<ID>/view?...
///   https://drive.google.com/uc?export=download&id=<ID>
///   https://drive.usercontent.google.com/download?id=<ID>&...
/// If the input has no recognizable id, it is returned unchanged (assumed
/// to already be a plain id).
fn extract_drive_id(url: &str) -> String {
    if let Some(rest) = url.split("/d/").nth(1) {
        if let Some(id) = rest.split('/').next() {
            return id.to_string();
        }
    }
    if let Some(rest) = url.split("id=").nth(1) {
        let id: String = rest.chars().take_while(|c| *c != '&').collect();
        if !id.is_empty() {
            return id;
        }
    }
    url.to_string()
}

fn meta_url() -> String {
    if let Ok(u) = std::env::var("OYPC_META_URL") {
        if !u.trim().is_empty() {
            return u;
        }
    }
    let id = std::env::var("OYPC_META_ID").unwrap_or_else(|_| DEFAULT_META_ID.to_string());
    drive_download_url(&id)
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(600))
        .redirects(10)
        .build()
}

/// Fetch and parse the remote `.meta`.
pub fn fetch_meta() -> Result<Meta, String> {
    let url = meta_url();
    let resp = agent()
        .get(&url)
        .call()
        .map_err(|e| format!("메타 요청 실패: {e}"))?;
    let body = resp
        .into_string()
        .map_err(|e| format!("메타 본문 읽기 실패: {e}"))?;
    // Strip a UTF-8 BOM if present (some editors/PowerShell add one).
    let body = body.strip_prefix('\u{feff}').unwrap_or(&body);
    serde_json::from_str::<Meta>(body)
        .map_err(|e| format!("메타 JSON 파싱 실패: {e} — 본문 앞부분: {:.200}", body))
}

/// Semver-ish comparison. Returns true when `remote` is strictly newer than
/// `current`. Falls back to a lenient numeric/lexical compare if either side
/// is not valid semver.
pub fn is_newer(remote: &str, current: &str) -> bool {
    let r = remote.trim().trim_start_matches('v');
    let c = current.trim().trim_start_matches('v');
    match (semver::Version::parse(r), semver::Version::parse(c)) {
        (Ok(rv), Ok(cv)) => rv > cv,
        _ => cmp_loose(r, c),
    }
}

fn cmp_loose(a: &str, b: &str) -> bool {
    let pa: Vec<u64> = a.split(['.', '-']).filter_map(|p| p.parse().ok()).collect();
    let pb: Vec<u64> = b.split(['.', '-']).filter_map(|p| p.parse().ok()).collect();
    for i in 0..pa.len().max(pb.len()) {
        let x = pa.get(i).copied().unwrap_or(0);
        let y = pb.get(i).copied().unwrap_or(0);
        if x != y {
            return x > y;
        }
    }
    false
}

/// Download the installer to a temp path, verifying SHA-256 when the meta
/// provides a non-zero hash. Returns the path to the downloaded file.
pub fn download_installer(installer: &Installer) -> Result<PathBuf, String> {
    let id = extract_drive_id(&installer.url);
    let url = drive_download_url(&id);

    let resp = agent()
        .get(&url)
        .call()
        .map_err(|e| format!("인스톨러 다운로드 요청 실패: {e}"))?;

    let ctype = resp.content_type().to_string();
    let mut reader = resp.into_reader();
    let mut bytes = Vec::new();
    reader
        .read_to_end(&mut bytes)
        .map_err(|e| format!("인스톨러 다운로드 실패: {e}"))?;

    // A tiny HTML payload means Drive returned an interstitial page, not the
    // file (private file, wrong id, or scan page we couldn't bypass).
    if ctype.contains("text/html") && bytes.len() < 100_000 {
        return Err(format!(
            "Drive가 파일 대신 HTML을 반환했습니다({} bytes). \
             파일 공유 설정(링크가 있는 모든 사용자)과 파일 ID를 확인하세요.",
            bytes.len()
        ));
    }

    if !installer.sha256.is_empty() && !installer.sha256.chars().all(|c| c == '0') {
        let got = sha256_hex(&bytes);
        if !got.eq_ignore_ascii_case(installer.sha256.trim()) {
            return Err(format!(
                "SHA-256 불일치 — 예상 {}, 실제 {}",
                installer.sha256, got
            ));
        }
    }

    let mut path = std::env::temp_dir();
    path.push(sanitize(&installer.filename));
    std::fs::write(&path, &bytes).map_err(|e| format!("임시 파일 쓰기 실패: {e}"))?;
    Ok(path)
}

fn sanitize(name: &str) -> String {
    let name = name.trim();
    let cleaned: String = name
        .chars()
        .filter(|c| !matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|'))
        .collect();
    if cleaned.is_empty() {
        "OwnYourPC-setup.exe".to_string()
    } else {
        cleaned
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

/// Launch a downloaded installer. On Windows this starts the .exe detached so
/// it can proceed while this app exits.
pub fn launch_installer(path: &PathBuf) -> Result<(), String> {
    std::process::Command::new(path)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("인스톨러 실행 실패: {e}"))
}

/// Full startup flow: check the remote meta, and if a newer version exists,
/// prompt the user, download + verify the installer, launch it, and exit.
/// Intended to run on a background thread so it never blocks app startup.
/// Any error is surfaced as a non-fatal dialog (unless `silent`).
pub fn run_startup_check(current: &str, silent: bool) {
    use rfd::{MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};

    let meta = match fetch_meta() {
        Ok(m) => m,
        Err(e) => {
            // Network/parse issues on startup should never block usage.
            eprintln!("[updater] {e}");
            if !silent {
                MessageDialog::new()
                    .set_level(MessageLevel::Warning)
                    .set_title("OwnYourPC 업데이트")
                    .set_description(format!("업데이트 확인 실패:\n{e}"))
                    .show();
            }
            return;
        }
    };

    if !is_newer(&meta.version, current) {
        if !silent {
            MessageDialog::new()
                .set_title("OwnYourPC 업데이트")
                .set_description(format!("최신 버전입니다 (v{current})."))
                .show();
        }
        return;
    }

    let prompt = format!(
        "새 버전 v{}이(가) 있습니다 (현재 v{current}).\n\n{}\n\n지금 다운로드하고 설치할까요?\n설치를 진행하면 앱이 종료됩니다.",
        meta.version,
        if meta.notes.is_empty() { "(변경 사항 없음)" } else { &meta.notes }
    );
    let answer = MessageDialog::new()
        .set_level(MessageLevel::Info)
        .set_title("OwnYourPC 업데이트")
        .set_description(prompt)
        .set_buttons(MessageButtons::YesNo)
        .show();

    if answer != MessageDialogResult::Yes {
        return;
    }

    match download_installer(&meta.installer) {
        Ok(path) => {
            if let Err(e) = launch_installer(&path) {
                MessageDialog::new()
                    .set_level(MessageLevel::Error)
                    .set_title("OwnYourPC 업데이트")
                    .set_description(e)
                    .show();
                return;
            }
            // Give the installer a moment to spin up, then exit so it can
            // replace the running files.
            std::thread::sleep(Duration::from_millis(800));
            std::process::exit(0);
        }
        Err(e) => {
            MessageDialog::new()
                .set_level(MessageLevel::Error)
                .set_title("OwnYourPC 업데이트")
                .set_description(format!("다운로드 실패:\n{e}"))
                .show();
        }
    }
}
