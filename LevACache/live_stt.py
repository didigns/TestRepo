"""faster-whisper 기반 실시간 스트리밍 전사(라이브 자막).

whisper.cpp(HTTP 배치)와 별개의 선택적 백엔드다:
- faster-whisper가 설치돼 있으면 회의 녹음 중 '타이핑되는 자막'을 제공한다.
- 없으면 ImportError를 던지고, 프런트는 VAD+whisper-server 방식으로 폴백한다.

동작(경량 2단 스트리밍):
- 부분(partial): 말하는 동안 최근 창(최대 8초)을 주기적으로 전사해 갱신 표시.
- 확정(final): 말꼬리 무음(0.6초) 또는 15초 상한에서 버퍼 전체를 전사해
  타임스탬프와 함께 확정하고 버퍼를 비운다.
지연 체감: GPU ≈ 1초 안팎, CPU(int8) ≈ 2~4초.
"""
import os
import threading
import time


def _add_cuda_dll_dirs(log=None):
    """pip로 설치한 nvidia-cublas-cu12 / nvidia-cudnn-cu12 의 DLL 폴더를
    Windows DLL 검색 경로에 등록한다.

    Windows는 site-packages\\nvidia\\*\\bin 을 자동으로 찾지 않기 때문에,
    이 처리를 하지 않으면 CTranslate2가 'cublas64_12.dll is not found' 오류를 낸다.
    비Windows/미설치 환경에서는 조용히 무시(CPU 폴백은 그대로 동작)."""
    if os.name != "nt":
        return
    added = []
    try:
        import importlib.util
        for pkg in ("nvidia.cublas", "nvidia.cudnn"):
            try:
                spec = importlib.util.find_spec(pkg)
            except Exception:
                spec = None
            if not spec or not spec.submodule_search_locations:
                continue
            for base in spec.submodule_search_locations:
                bin_dir = os.path.join(base, "bin")
                if os.path.isdir(bin_dir):
                    try:
                        os.add_dll_directory(bin_dir)  # Python 3.8+
                    except Exception:
                        pass
                    # PATH 폴백(일부 로더는 add_dll_directory를 못 탐)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                    added.append(bin_dir)
    except Exception as e:
        if log:
            log("CUDA DLL 경로 등록 실패(무시): " + str(e))
    if added and log:
        log("CUDA DLL 경로 등록: " + ", ".join(added))


class LiveSTT:
    SR = 16000

    def __init__(self, cfg, emit, log):
        # 모델 로드 전에 cuBLAS/cuDNN DLL 폴더를 검색 경로에 등록
        _add_cuda_dll_dirs(log)
        # 의존성 없으면 여기서 ImportError → 호출부가 폴백 처리
        from faster_whisper import WhisperModel

        device, compute = "cpu", "int8"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device, compute = "cuda", "float16"
        except Exception:
            pass
        name = cfg.get("liveSttModel") or "large-v3-turbo"
        log(f"라이브 STT 로드: {name} ({device}/{compute}) — 첫 실행은 모델 다운로드로 오래 걸릴 수 있음")
        self.model = WhisperModel(name, device=device, compute_type=compute)
        self.device = device
        self.emit = emit
        self.log = log
        lang = (cfg.get("whisperLanguage") or "auto").strip().lower()
        self.lang = None if lang in ("", "auto") else lang
        self._lock = threading.Lock()
        self._buf = bytearray()      # int16 mono 16k PCM
        self._base = 0               # 확정돼 비운 앞부분(샘플 수)
        self._running = True
        self._th = threading.Thread(target=self._loop, daemon=True, name="live-stt")
        self._th.start()

    # ---- 입력 ----------------------------------------------------------
    # 버퍼 상한(초). 전사 루프가 밀려도(CPU 과부하 등) 오디오가 무한정
    # 쌓여 메모리가 커지지 않게 앞부분을 버린다. 15초 확정 상한보다 넉넉히.
    MAX_BUF_SEC = 30

    def feed(self, pcm_bytes):
        if not pcm_bytes:
            return
        with self._lock:
            self._buf += pcm_bytes
            cap = self.SR * 2 * self.MAX_BUF_SEC  # int16 mono = 2 bytes/sample
            if len(self._buf) > cap:
                drop = len(self._buf) - cap
                del self._buf[:drop]
                self._base += drop // 2  # 타임라인 유지(버린 샘플만큼 전진)

    # ---- 내부 ----------------------------------------------------------
    def _snapshot(self):
        import numpy as np
        with self._lock:
            raw = bytes(self._buf)
        a = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
        return a

    def _take_all(self):
        import numpy as np
        with self._lock:
            raw = bytes(self._buf)
            self._buf = bytearray()
        a = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
        return a

    def _transcribe(self, audio):
        kw = {"language": self.lang} if self.lang else {}
        segs, _info = self.model.transcribe(
            audio, beam_size=1, vad_filter=False,
            condition_on_previous_text=False, **kw)
        return " ".join(s.text.strip() for s in segs).strip()

    def _loop(self):
        import numpy as np
        last_partial = ""
        while self._running:
            time.sleep(0.6)
            try:
                a = self._snapshot()
                n = len(a)
                if n < self.SR:  # 1초 미만이면 대기
                    continue
                tail = a[-int(0.6 * self.SR):]
                rms_tail = float(np.sqrt(float((tail * tail).mean()) + 1e-9))
                rms_all = float(np.sqrt(float((a * a).mean()) + 1e-9))
                spoke = rms_all > 0.008
                if spoke and (rms_tail < 0.006 or n > self.SR * 15):
                    # 확정: 버퍼 전체를 최종 전사
                    audio = self._take_all()
                    start = self._base / self.SR
                    self._base += len(audio)
                    text = self._transcribe(audio)
                    last_partial = ""
                    if text:
                        self.emit({"type": "live-text", "final": text,
                                   "start": round(start, 2)})
                    else:
                        self.emit({"type": "live-text", "partial": ""})
                elif spoke:
                    # 부분: 최근 8초 창만 — CPU에서도 주기를 지킬 수 있게
                    win = a[-int(8 * self.SR):]
                    text = self._transcribe(win)
                    if text and text != last_partial:
                        last_partial = text
                        self.emit({"type": "live-text", "partial": text})
                elif n > self.SR * 5:
                    # 무음만 5초+ — 소비(타임라인만 전진)
                    audio = self._take_all()
                    self._base += len(audio)
            except Exception as e:
                self.log("라이브 STT 루프 오류: " + str(e))
                time.sleep(1)

    # ---- 종료 ----------------------------------------------------------
    def stop(self):
        self._running = False
        try:
            self._th.join(timeout=8)
        except Exception:
            pass
        # 남은 꼬리 확정
        try:
            audio = self._take_all()
            if len(audio) > self.SR * 0.8:
                start = self._base / self.SR
                self._base += len(audio)
                text = self._transcribe(audio)
                if text:
                    self.emit({"type": "live-text", "final": text,
                               "start": round(start, 2)})
        except Exception:
            pass
