# AISummary — build a self-extracting setup.exe (custom installer).
#
# Stages the app payload (Tauri exe + frontend + backend + icons + launcher +
# private Python runtime + Python install scripts), zips it, and wraps it in a
# self-extracting .exe compiled with the in-box .NET Framework csc.exe (app.zip
# is embedded as a resource). Running that .exe shows a splash, expands the
# payload to a temp dir, and launches the Qt installer on the bundled runtime
# (runtime\pythonw.exe installer\install.py). No IExpress / external tooling.
#
# Prerequisite: build the runtime first with installer\prepare-runtime.ps1.
#
# Typical use after `npm run build` in desktop/:
#   powershell -ExecutionPolicy Bypass -File installer\build-installer.ps1 `
#       -Version 0.2.0
#
# Output: dist\AISummary_<version>_x64-setup.exe

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Version,
    # Built Tauri executable. Default = Cargo release output.
    [string]$AppExe   = (Join-Path $PSScriptRoot '..\src-tauri\target\release\aisummary.exe'),
    [string]$Frontend = (Join-Path $PSScriptRoot '..\..\frontend'),
    [string]$Backend  = (Join-Path $PSScriptRoot '..\..\backend'),
    [string]$Icons    = (Join-Path $PSScriptRoot '..\src-tauri\icons'),
    [string]$Launcher = (Join-Path $PSScriptRoot '..\launcher'),
    # Private Python runtime (built by prepare-runtime.ps1).
    [string]$Runtime  = (Join-Path $PSScriptRoot '..\runtime'),
    [string]$OutDir   = (Join-Path $PSScriptRoot '..\dist')
)

$ErrorActionPreference = 'Stop'
# Colored console output can throw IndexOutOfRangeException when the host has no
# real console buffer (redirected output / some terminals). Write-Say degrades
# gracefully to plain text instead of crashing the build.
function Write-Say([string]$Message, [string]$Color = $null) {
    try { if ($Color) { Write-Host $Message -ForegroundColor $Color } else { Write-Host $Message } }
    catch { try { [Console]::WriteLine($Message) } catch {} }
}
function Log($m) { Write-Say "[build] $m" 'Cyan' }

# Normalize OutDir to a clean absolute path (no '..').
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = [System.IO.Path]::GetFullPath((Resolve-Path $OutDir).Path)

# Stage/package under OutDir (on E:) rather than %TEMP% to keep build paths
# stable and out of the user profile.
$buildRoot = Join-Path $OutDir ("_build_" + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $buildRoot 'stage'
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$script:success = $false

try {
    # --- Stage payload ----------------------------------------------------
    if (-not (Test-Path $AppExe)) { throw "앱 실행 파일을 찾을 수 없음: $AppExe (먼저 tauri build를 실행하세요)" }
    Log "실행 파일: $AppExe"
    Copy-Item $AppExe (Join-Path $stage 'AISummary.exe') -Force

    Log "프론트엔드 복사"
    Copy-Item $Frontend (Join-Path $stage 'frontend') -Recurse -Force

    Log "백엔드 복사 (venv/캐시 제외)"
    $exclude = @('__pycache__', '.pytest_cache', '.venv', 'venv', 'runtime', '*.pyc')
    robocopy $Backend (Join-Path $stage 'backend') /E /XD __pycache__ .pytest_cache .venv venv runtime /XF *.pyc | Out-Null

    Copy-Item $Icons (Join-Path $stage 'icons') -Recurse -Force

    Log "런처 복사"
    robocopy $Launcher (Join-Path $stage 'launcher') /E /XD __pycache__ /XF *.pyc | Out-Null

    Log "설치 스크립트(Python) 복사"
    $instDst = Join-Path $stage 'installer'
    New-Item -ItemType Directory -Force -Path $instDst | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'install.py')   $instDst -Force
    Copy-Item (Join-Path $PSScriptRoot 'uninstall.py') $instDst -Force

    if (-not (Test-Path (Join-Path $Runtime 'python.exe'))) {
        throw "런타임을 찾을 수 없음: $Runtime `n  먼저 실행: installer\prepare-runtime.ps1"
    }
    Log "비공개 Python 런타임 복사 (용량 큼)"
    robocopy $Runtime (Join-Path $stage 'runtime') /E /XD __pycache__ /XF *.pyc | Out-Null

    Set-Content -Path (Join-Path $stage 'version.txt') -Value $Version -Encoding Ascii

    # --- Zip payload ------------------------------------------------------
    $pkgDir = Join-Path $buildRoot 'pkg'
    New-Item -ItemType Directory -Force -Path $pkgDir | Out-Null
    $zip = Join-Path $pkgDir 'app.zip'
    Log "패키지 압축 → app.zip"
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force

    # --- Self-extracting exe via .NET csc.exe -----------------------------
    # A tiny C# bootstrapper embeds app.zip as a resource; on run it extracts
    # to %TEMP%\AISummary_payload and launches install.ps1. Compiled with the
    # in-box .NET Framework csc.exe (present on all Windows) — no IExpress.
    $target = Join-Path $OutDir "AISummary_${Version}_x64-setup.exe"
    if (Test-Path $target) { Remove-Item $target -Force }

    $csSrc = Join-Path $pkgDir 'SelfExtract.cs'
    $csText = @'
using System;
using System.IO;
using System.Reflection;
using System.Diagnostics;
using System.IO.Compression;
using System.Threading;
using System.Drawing;
using System.Windows.Forms;

static class SelfExtract {
    static Form splash;

    [STAThread]
    static int Main() {
        Application.EnableVisualStyles();
        ShowSplash();
        try {
            string dest = Path.Combine(Path.GetTempPath(), "AISummary_setup");
            if (Directory.Exists(dest)) { try { Directory.Delete(dest, true); } catch {} }
            Directory.CreateDirectory(dest);

            var asm = Assembly.GetExecutingAssembly();
            string zipPath = Path.Combine(dest, "app.zip");
            using (var rs = asm.GetManifestResourceStream("app.zip"))
            using (var fs = File.Create(zipPath)) {
                if (rs == null) throw new Exception("embedded app.zip not found");
                rs.CopyTo(fs);
            }
            ZipFile.ExtractToDirectory(zipPath, dest);
            try { File.Delete(zipPath); } catch {}

            CloseSplash();

            string pyw = Path.Combine(dest, "runtime", "pythonw.exe");
            string script = Path.Combine(dest, "installer", "install.py");
            var psi = new ProcessStartInfo {
                FileName = pyw,
                Arguments = "\"" + script + "\" --payload \"" + dest + "\"",
                UseShellExecute = false,
                WorkingDirectory = dest
            };
            var p = Process.Start(psi);
            p.WaitForExit();
            return p.ExitCode;
        } catch (Exception e) {
            CloseSplash();
            MessageBox.Show(e.ToString(), "AISummary 설치 오류",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    static void ShowSplash() {
        var t = new Thread(() => {
            splash = new Form {
                FormBorderStyle = FormBorderStyle.None,
                StartPosition = FormStartPosition.CenterScreen,
                Size = new Size(360, 140),
                BackColor = ColorTranslator.FromHtml("#20242b"),
                TopMost = true, ShowInTaskbar = false
            };
            var title = new Label {
                Text = "AISummary", ForeColor = ColorTranslator.FromHtml("#e8ebf0"),
                Font = new Font("Segoe UI", 15, FontStyle.Bold),
                TextAlign = ContentAlignment.MiddleCenter, Dock = DockStyle.Top, Height = 64
            };
            var lbl = new Label {
                Text = "설치 준비 중…", ForeColor = ColorTranslator.FromHtml("#8b93a1"),
                Font = new Font("Segoe UI", 9),
                TextAlign = ContentAlignment.MiddleCenter, Dock = DockStyle.Fill
            };
            splash.Controls.Add(lbl);
            splash.Controls.Add(title);
            Application.Run(splash);
        });
        t.SetApartmentState(ApartmentState.STA);
        t.IsBackground = true;
        t.Start();
    }

    static void CloseSplash() {
        try { if (splash != null) splash.Invoke(new Action(() => splash.Close())); } catch {}
    }
}
'@
    [System.IO.File]::WriteAllText($csSrc, ($csText -replace "`r?`n", "`r`n"), [System.Text.Encoding]::UTF8)

    # Locate csc.exe (prefer 64-bit v4).
    $csc = $null
    foreach ($base in @("$env:WINDIR\Microsoft.NET\Framework64", "$env:WINDIR\Microsoft.NET\Framework")) {
        if (Test-Path $base) {
            $found = Get-ChildItem $base -Directory -Filter 'v4*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName 'csc.exe' } |
                Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($found) { $csc = $found; break }
        }
    }
    if (-not $csc) { throw ".NET csc.exe를 찾을 수 없음 (Microsoft.NET\Framework64\v4*). .NET Framework가 필요합니다." }
    Log "컴파일러: $csc"

    $ico = Join-Path $Icons 'icon.ico'
    $cscArgs = @(
        '/nologo',
        '/target:winexe',
        "/out:$target",
        "/resource:$zip,app.zip",
        '/reference:System.IO.Compression.FileSystem.dll',
        '/reference:System.Windows.Forms.dll',
        '/reference:System.Drawing.dll'
    )
    if (Test-Path $ico) { $cscArgs += "/win32icon:$ico" }
    $cscArgs += $csSrc

    Log "self-extracting exe 컴파일"
    $cscOut = & $csc @cscArgs 2>&1
    if (-not (Test-Path $target)) {
        throw ("csc 컴파일 실패: $target 생성 안 됨.`n" + ($cscOut -join "`n"))
    }
    $size = (Get-Item $target).Length
    Log "완료: $target ($([math]::Round($size/1MB,1)) MB)"
    $script:success = $true
    Write-Host $target
}
finally {
    # 실패 시에는 진단을 위해 빌드 폴더를 남겨둠.
    if ($script:success) {
        Remove-Item $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Say "[build] 빌드 산출물 보존: $buildRoot" 'DarkYellow'
    }
}
