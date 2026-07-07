"""whisper.cpp의 whisper-server를 관리하고 오디오를 전사한다.

LlamaManager와 같은 패턴: 별도 포트에 서버를 띄우고 HTTP로 요청한다.
- 서버는 첫 전사 요청 때 기동, 이후 상주(모델 로드 비용 1회).
- wav/mp3/flac/ogg 는 서버가 직접 읽고(miniaudio), m4a/aac/wma/webm 은
  ffmpeg(설정 경로 또는 PATH)로 16kHz mono wav 변환 후 보낸다.
- 결과는 verbose_json 의 세그먼트 타임스탬프를 "[MM:SS] 텍스트" 로 정리해
  회의록·근거 표시에 바로 쓸 수 있는 본문을 만든다.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

# 서버(miniaudio)가 직접 디코딩 가능한 형식 — 그 외는 ffmpeg 변환 필요
SERVER_DECODABLE = {".wav", ".mp3", ".flac", ".ogg"}


class WhisperManager:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger
        self.proc = None
        self._lock = threading.Lock()  # 동시 ensure(실시간+파이프라인) 이중 기동 방지

    def _log(self, msg):
        if self.log:
            try:
                self.log(msg)
            except Exception:
                pass

    # ---- 서버 수명 ---------------------------------------------------
    @property
    def base_url(self):
        return f"http://{self.cfg.get('host', '127.0.0.1')}:{self.port}"

    @property
    def port(self):
        return int(self.cfg.get("whisperPort", 8082) or 8082)

    def configured(self):
        return bool(self.cfg.get("whisperServerExe")) and bool(self.cfg.get("whisperModel"))

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        with self._lock:
            return self._ensure_locked()

    def _ensure_locked(self):
        if not self.configured():
            raise RuntimeError(
                "음성 인식이 설정되지 않았습니다. 설정 → 모델에서 "
                "'Whisper 음성 인식'을 설치해 주세요.")
        if self._alive():
            return
        exe = self.cfg["whisperServerExe"]
        # 스레드: 미지정 시 논리 코어 수(최소 4). CPU 경로에서 속도에 가장 큰 영향을 준다.
        threads = int(self.cfg.get("whisperThreads", 0) or 0)
        if threads <= 0:
            threads = max(4, os.cpu_count() or 4)
        base = [
            exe, "-m", self.cfg["whisperModel"],
            "--host", self.cfg.get("host", "127.0.0.1"),
            "--port", str(self.port),
            "-t", str(threads),
        ]
        lang = str(self.cfg.get("whisperLanguage", "auto") or "auto")
        base += ["-l", lang]
        if self.cfg.get("whisperNoGpu"):
            base += ["-ng"]  # 진단용: GPU 강제 비활성(CPU만)

        # flash-attention 우선 시도(속도↑). 구버전 빌드에서 미지원이면 자동 폴백한다.
        use_fa = self.cfg.get("whisperFlashAttn", True)
        attempts = ([base + ["-fa"]] if use_fa else []) + [base]
        last_err = None
        for args in attempts:
            fa_on = "-fa" in args
            self._log(f"whisper-server 기동: port={self.port}, t={threads}, "
                      f"fa={'on' if fa_on else 'off'}, "
                      f"gpu={'off' if self.cfg.get('whisperNoGpu') else 'auto'}")
            try:
                self.proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except FileNotFoundError:
                raise RuntimeError(f"whisper-server.exe를 찾을 수 없습니다: {exe}")
            except OSError as e:
                raise RuntimeError(f"whisper-server 실행 실패: {e}")
            # stderr 를 앱 로그로 흘려 GPU/CPU 사용 여부·오류를 확인 가능하게 한다.
            threading.Thread(target=self._pump_stderr,
                             args=(self.proc.stderr,), daemon=True).start()
            if self._wait_ready():
                return
            # 이번 시도 실패(예: -fa 미지원으로 조기 종료) → 다음 폴백으로.
            last_err = "whisper-server 기동 실패(모델 경로/빌드 확인)"
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        raise RuntimeError(last_err or "whisper-server 기동 실패")

    def _wait_ready(self):
        """모델 로드까지 대기. 준비되면 True, 조기 종료/타임아웃이면 False.
        (/ 는 데모 페이지 — 응답이 오면 서버가 준비된 것으로 본다)"""
        deadline = time.time() + int(self.cfg.get("startupTimeout", 120) or 120)
        while time.time() < deadline:
            if not self._alive():
                return False  # 조기 종료 → 상위에서 폴백 시도
            try:
                with urllib.request.urlopen(self.base_url + "/", timeout=2) as r:
                    if r.status < 500:
                        return True
            except Exception:
                time.sleep(1)
        return False

    def _pump_stderr(self, pipe):
        """whisper-server stderr 를 앱 로그로. 진단에 유용한 줄만 남겨 로그 스팸을 막는다.
        (예: 'ggml_cuda_init: found 1 CUDA devices' → GPU 사용,
              'whisper_backend_init: ... CPU' → CPU 폴백)"""
        KEEP = ("cuda", "gpu", "error", "fail", "backend", "device",
                "blas", "flash", "whisper_init", "whisper_model_load",
                "system_info", "load time", "total time")
        try:
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line and any(k in line.lower() for k in KEEP):
                    self._log("[whisper] " + line)
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def status(self):
        return {
            "alive": self._alive(),
            "configured": self.configured(),
            "port": self.port,
            "model": os.path.basename(self.cfg.get("whisperModel") or ""),
        }

    # ---- 변환/전사 ---------------------------------------------------
    def _ffmpeg(self):
        exe = (self.cfg.get("ffmpegExe") or "").strip()
        if exe and os.path.isfile(exe):
            return exe
        return shutil.which("ffmpeg")

    def _to_wav(self, path):
        """서버가 못 읽는 형식을 16kHz mono wav로 변환. 반환: (임시wav, 정리필요)"""
        ff = self._ffmpeg()
        if not ff:
            raise RuntimeError(
                os.path.splitext(path)[1] + " 형식은 ffmpeg가 필요합니다. "
                "설정에서 ffmpeg 경로를 지정하거나 PATH에 설치해 주세요.")
        out = os.path.join(tempfile.gettempdir(),
                           "leva_stt_" + uuid.uuid4().hex + ".wav")
        cp = subprocess.run(
            [ff, "-y", "-i", path, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
        if cp.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError("ffmpeg 변환 실패: " + os.path.basename(path))
        return out, True

    @staticmethod
    def _fmt_ts(sec):
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return (f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")

    @staticmethod
    def _seg_time(v):
        """세그먼트 시각: 초(float) 또는 'HH:MM:SS,mmm' 문자열 모두 수용."""
        if isinstance(v, (int, float)):
            return float(v)
        try:
            s = str(v).replace(",", ".")
            parts = s.split(":")
            parts = [float(x) for x in parts]
            while len(parts) < 3:
                parts.insert(0, 0.0)
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except Exception:
            return 0.0

    def transcribe(self, path, *, timeout=7200):
        """오디오 파일 → '[MM:SS] 텍스트' 줄들의 전사 본문."""
        self.ensure()
        ext = os.path.splitext(path)[1].lower()
        wav, cleanup = (path, False)
        if ext not in SERVER_DECODABLE:
            wav, cleanup = self._to_wav(path)
        try:
            with open(wav, "rb") as f:
                audio = f.read()
            boundary = "----LevA" + uuid.uuid4().hex
            fields = {"response_format": "verbose_json", "temperature": "0.0"}
            body = b""
            for k, v in fields.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"file\"; filename=\"audio.wav\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
            body += audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                self.base_url + "/inference", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            raise RuntimeError(f"전사 실패 HTTP {e.code}: {detail or e.reason}")
        finally:
            if cleanup:
                try:
                    os.remove(wav)
                except Exception:
                    pass

        segs = data.get("segments") or data.get("transcription") or []
        lines = []
        for s in segs:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            start = s.get("start", s.get("t0", (s.get("timestamps") or {}).get("from", 0)))
            t = self._seg_time(start)
            # whisper.cpp 일부 포맷은 t0가 센티초 정수
            if isinstance(start, (int, float)) and start > 100000:
                t = float(start) / 100.0
            lines.append(f"[{self._fmt_ts(t)}] {txt}")
        if lines:
            return "\n".join(lines)
        return (data.get("text") or "").strip()
