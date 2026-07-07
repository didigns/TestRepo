"""llama-server 하나를 관리하며 필요한 모델로 스왑(재기동)한다.
- ensure_model(key): 현재 로드된 모델과 다르면 서버를 재기동
- chat(prompt, images): /v1/chat/completions 호출 (OpenAI 호환)
비전 모델(MiniCPM-V)은 --mmproj 로 멀티모달 활성화, 이미지는 base64 data URL 로 전달.
"""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

# 확장자 → data URL MIME 서브타입 (llama-server 멀티모달 이미지 디코딩용)
_IMG_MIME = {
    "jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif",
    "bmp": "bmp", "webp": "webp", "tif": "tiff", "tiff": "tiff",
}


def _img_mime(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return _IMG_MIME.get(ext, "png")


def _extract_server_error(body):
    """llama-server 오류 응답(JSON 또는 텍스트)에서 사람이 읽을 메시지를 뽑는다."""
    if not body:
        return ""
    try:
        obj = json.loads(body)
        err = obj.get("error", obj)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or obj)[:300]
        return str(err)[:300]
    except Exception:
        return str(body)[:300]


class LlamaManager:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger
        self.proc = None
        self.loaded_key = None
        self.embed_proc = None  # 임베딩 전용 서버(별도 포트 상시 기동)

    def _log(self, msg):
        if self.log:
            self.log(msg)

    @property
    def base_url(self):
        return f"http://{self.cfg['host']}:{self.cfg['port']}"

    def _threads(self):
        """생성 스레드 수. nThreads>0이면 그 값, 0(기본)이면 CPU 코어 수로 자동 설정.
        구하지 못하면 0을 반환(llama.cpp 기본 자동)."""
        n = int(self.cfg.get("nThreads", 0) or 0)
        if n > 0:
            return n
        try:
            return max(1, os.cpu_count() or 0)
        except Exception:
            return 0

    @staticmethod
    def _total_ram_gb():
        """물리 RAM 총량(GB). 구하지 못하면 None."""
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                class _MS(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", wintypes.DWORD),
                        ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64),
                    ]

                ms = _MS()
                ms.dwLength = ctypes.sizeof(_MS)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                    return ms.ullTotalPhys / (1024 ** 3)
            else:
                return (os.sysconf("SC_PAGE_SIZE")
                        * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
        except Exception:
            return None
        return None

    def _use_mlock(self):
        """mlock 사용 여부. 설정이 True라도 물리 RAM이 부족(<24GB)하면 끈다.

        mlock은 가중치를 RAM에 고정해 콜드 페이지폴트를 없애지만, 저사양에서
        여러 서버(메인+임베딩+whisper+faster-whisper)가 동시에 고정하면 OS가
        메모리를 회수하지 못해 하드 OOM(크래시)으로 이어진다. 넉넉한 RAM에서만 켠다."""
        if self.cfg.get("lowMemory"):
            return False  # 저메모리 모드: OS 페이지 회수 허용(가중치 고정 해제)
        if not self.cfg.get("useMlock", True):
            return False
        gb = self._total_ram_gb()
        if gb is not None and gb < 24:
            self._log(f"mlock 비활성(물리 RAM {gb:.0f}GB < 24GB) — 저사양 OOM 방지")
            return False
        return True

    def _unified_vision(self):
        """텍스트 모델(Gemma 4)이 곧 비전 모델인 '통합 멀티모달' 구성인지 판단.

        visionModel 이 textModel 과 같은 파일이고 Gemma용 mmproj(visionMmproj)가
        존재하면, MiniCPM 스왑 없이 하나의 llama-server 가 텍스트와 이미지를 모두
        처리한다. (요청마다 모델을 재기동하던 스왑 비용·프로세스 churn 제거)"""
        tm = self.cfg.get("textModel")
        vm = self.cfg.get("visionModel")
        mm = self.cfg.get("visionMmproj")
        if not (tm and vm and mm):
            return False
        try:
            same = (os.path.normcase(os.path.abspath(tm))
                    == os.path.normcase(os.path.abspath(vm)))
        except Exception:
            same = (tm == vm)
        return same and os.path.isfile(mm)

    def _model_args(self, key):
        # 통합 멀티모달(Gemma 4 = 텍스트+비전): 키와 무관하게 한 서버로 처리.
        if self._unified_vision():
            return ["-m", self.cfg["textModel"], "--mmproj", self.cfg["visionMmproj"]]
        if key == "vision":
            model = self.cfg.get("visionModel")
            mmproj = self.cfg.get("visionMmproj")
            if not model:
                raise RuntimeError("visionModel(MiniCPM-V gguf) 경로가 설정되지 않았습니다.")
            if not mmproj:
                raise RuntimeError(
                    "이미지 OCR에 필요한 비전 투영기(mmproj)가 설정되지 않았습니다. "
                    "설정 → 모델에서 'MiniCPM-V 비전 투영기'를 받아 주세요."
                )
            if not os.path.isfile(mmproj):
                raise RuntimeError(f"비전 투영기(mmproj) 파일을 찾을 수 없습니다: {mmproj}")
            return ["-m", model, "--mmproj", mmproj]
        else:
            model = self.cfg.get("textModel")
            if not model:
                raise RuntimeError("textModel(Gemma gguf) 경로가 설정되지 않았습니다.")
            return ["-m", model]

    def ensure_model(self, key):
        # 통합 멀티모달이면 vision 요청도 하나의 text 서버가 처리 → 재기동(스왑) 없음
        if self._unified_vision():
            key = "text"
        if self.loaded_key == key and self._alive():
            return
        self._stop_main()  # 스왑: 메인 서버만 재기동(임베딩 서버는 유지)
        exe = self.cfg["llamaServerExe"]
        args = [
            exe, *self._model_args(key),
            "--host", self.cfg["host"], "--port", str(self.cfg["port"]),
            "-c", str(self.cfg.get("nCtx", 8192)),
            "-ngl", str(self.cfg.get("nGpuLayers", 0)),
        ]
        # ---- 레이턴시 최적화 플래그 ----
        n_threads = self._threads()
        if n_threads > 0:
            args += ["-t", str(n_threads), "-tb", str(n_threads)]
        fa = self.cfg.get("flashAttn", "auto")
        if fa:
            args += ["-fa", str(fa)]          # 어텐션 가속 + KV 메모리 절감
        if self._use_mlock():
            args += ["--mlock"]                # 가중치를 RAM에 상주(콜드 페이지폴트 제거)
        self._log(f"llama-server 기동: model={key} args={args[1:]}")
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"llama-server.exe를 찾을 수 없습니다: {exe} "
                "(설정 → AI 캐싱에서 경로를 확인하세요.)"
            )
        except OSError as e:
            raise RuntimeError(f"llama-server 실행 실패: {e}")
        self._wait_ready()
        self.loaded_key = key

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _wait_ready(self):
        deadline = time.time() + self.cfg.get("startupTimeout", 120)
        url = self.base_url + "/health"
        while time.time() < deadline:
            if not self._alive():
                raise RuntimeError("llama-server 프로세스가 종료되었습니다(모델/경로 확인).")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(1)
        raise RuntimeError("llama-server 기동 시간 초과")

    def _build_messages(self, prompt, images, system):
        messages = []
        # 고정 system 메시지를 맨 앞에 두면 llama-server 프롬프트 캐싱이
        # 그 프리픽스 KV를 재사용해 TTFT가 줄어든다.
        if system:
            messages.append({"role": "system", "content": system})
        content = [{"type": "text", "text": prompt}]
        for img in images or []:
            with open(img, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{_img_mime(img)};base64," + b64},
            })
        messages.append({"role": "user", "content": content})
        return messages

    def chat(self, prompt, images=None, *, system=None, max_tokens=1024,
             temperature=0.2, timeout=300):
        payload = {
            "messages": self._build_messages(prompt, images, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            # Gemma 4 / Qwen 계열의 사고(thinking) 모드를 꺼서 토큰 낭비·JSON 깨짐 방지
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            detail = _extract_server_error(body)
            has_img = bool(images)
            hint = " (이미지 입력 실패 — 비전 모델/mmproj 설정을 확인하세요)" if has_img else ""
            raise RuntimeError(
                f"llama-server 오류 HTTP {e.code}: {detail or e.reason}{hint}"
            )
        return data["choices"][0]["message"]["content"]

    def chat_stream(self, prompt, on_delta, images=None, *, system=None,
                    max_tokens=2048, temperature=0.3, timeout=300):
        """SSE 스트리밍으로 응답을 받으며 on_delta(조각)을 호출. 전체 문자열 반환."""
        payload = {
            "messages": self._build_messages(prompt, images, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        parts = []
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                except Exception:
                    delta = ""
                if delta:
                    parts.append(delta)
                    try:
                        on_delta(delta)
                    except Exception:
                        pass
        return "".join(parts)

    # ---- 임베딩(의미검색) 전용 서버 --------------------------------
    @property
    def embed_base_url(self):
        port = self.cfg.get("embedPort") or (int(self.cfg["port"]) + 1)
        return f"http://{self.cfg['host']}:{port}"

    def _embed_alive(self):
        return self.embed_proc is not None and self.embed_proc.poll() is None

    def ensure_embed(self):
        """임베딩 모델이 설정돼 있으면 별도 포트에 상시 서버를 띄운다."""
        model = self.cfg.get("embedModel")
        if not model:
            return False
        if self._embed_alive():
            return True
        exe = self.cfg["llamaServerExe"]
        port = self.cfg.get("embedPort") or (int(self.cfg["port"]) + 1)
        n_ctx = int(self.cfg.get("embedNCtx", 2048))
        args = [
            exe, "-m", model, "--embedding",
            "--host", self.cfg["host"], "--port", str(port),
            "-c", str(n_ctx),
            # 임베딩 모드는 입력 1건이 물리 배치(-ub, 기본 512토큰)를 넘으면
            # HTTP 500("input is too large to process")을 반환한다.
            # 한글 청크(embedChunkChars=1000자)는 1000토큰을 훌쩍 넘으므로
            # 논리/물리 배치를 컨텍스트 크기와 같게 올려 준다.
            "-b", str(n_ctx), "-ub", str(n_ctx),
            "-ngl", str(self.cfg.get("nGpuLayers", 0)),
        ]
        # 풀링을 명시하지 않으면 GGUF 메타데이터의 모델 기본값(BGE-M3=CLS)을 쓴다.
        pooling = str(self.cfg.get("embedPooling") or "").strip()
        if pooling:
            args += ["--pooling", pooling]
        t = self._threads()
        if t > 0:
            args += ["-t", str(t)]
        if self._use_mlock():
            args += ["--mlock"]
        self._log(f"임베딩 서버 기동: port={port}")
        try:
            self.embed_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            raise RuntimeError(f"llama-server.exe를 찾을 수 없습니다: {exe}")
        # health 대기
        deadline = time.time() + self.cfg.get("startupTimeout", 120)
        url = self.embed_base_url + "/health"
        while time.time() < deadline:
            if not self._embed_alive():
                raise RuntimeError("임베딩 서버가 종료되었습니다(모델 경로 확인).")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(1)
        raise RuntimeError("임베딩 서버 기동 시간 초과")

    def embed(self, texts, *, timeout=120, on_progress=None):
        """텍스트 리스트 → 임베딩 벡터 리스트. 모델 미설정 시 None.

        청크 개수 제한을 없애면 한 파일이 수백 개 청크가 될 수 있으므로,
        하나의 거대한 요청 대신 embedBatch 개수씩 나눠 보낸다(안정성·진행표시).
        on_progress(done, total) 이 있으면 배치마다 호출한다."""
        if not self.cfg.get("embedModel"):
            return None
        self.ensure_embed()
        texts = list(texts or [])
        if not texts:
            return []
        batch = int(self.cfg.get("embedBatch", 64) or 64)
        if batch <= 0:
            batch = len(texts)
        out = []
        for start in range(0, len(texts), batch):
            part = texts[start:start + batch]
            payload = {"input": part}
            req = urllib.request.Request(
                self.embed_base_url + "/v1/embeddings",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 서버가 알려준 실제 원인(예: input is too large...)을 남긴다
                try:
                    detail = _extract_server_error(e.read().decode("utf-8", "replace"))
                except Exception:
                    detail = ""
                raise RuntimeError(
                    f"임베딩 서버 HTTP {e.code}: {detail or e.reason}"
                ) from None
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(it["embedding"] for it in items)
            if on_progress:
                try:
                    on_progress(min(start + batch, len(texts)), len(texts))
                except Exception:
                    pass
        return out

    # ---- 상태/재시작 (콘솔 UI용) -----------------------------------
    @staticmethod
    def _proc_rss(proc):
        """프로세스 실메모리(WorkingSet, 바이트). 실패/미기동 시 None."""
        if proc is None or proc.poll() is not None:
            return None
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            h = k32.OpenProcess(0x1000, False, proc.pid)  # QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                pmc = _PMC()
                pmc.cb = ctypes.sizeof(_PMC)
                if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    return int(pmc.WorkingSetSize)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
        return None

    def status(self):
        """엔진 상태 스냅샷: 메인(대화/비전 스왑) + 임베딩 서버."""
        def base(key):
            m = self.cfg.get(key) or ""
            return os.path.basename(m) if m else ""
        return {
            "main": {
                "alive": self._alive(),
                "loaded": self.loaded_key,  # 'text' | 'vision' | None
                "port": int(self.cfg.get("port", 8080)),
                "textModel": base("textModel"),
                "visionModel": base("visionModel"),
                "rss": self._proc_rss(self.proc),
            },
            "embed": {
                "alive": self._embed_alive(),
                "configured": bool(self.cfg.get("embedModel")),
                "port": int(self.cfg.get("embedPort")
                            or (int(self.cfg["port"]) + 1)),
                "model": base("embedModel"),
                "rss": self._proc_rss(self.embed_proc),
            },
        }

    def restart(self, target="all"):
        """서버 재시작: 내려 두면 다음 사용 시 자동 재기동된다."""
        if target in ("main", "all"):
            self._stop_main()
        if target in ("embed", "all"):
            self._terminate(self.embed_proc)
            self.embed_proc = None
        return True

    def _terminate(self, proc):
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _stop_main(self):
        """스왑용 메인 서버만 종료(임베딩 서버는 유지)."""
        self._terminate(self.proc)
        self.proc = None
        self.loaded_key = None

    def stop(self):
        """전체 종료: 메인 + 임베딩 서버."""
        self._stop_main()
        self._terminate(self.embed_proc)
        self.embed_proc = None
