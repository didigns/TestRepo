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

    def _model_args(self, key):
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
        if self.cfg.get("useMlock", True):
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
        args = [
            exe, "-m", model, "--embedding",
            "--host", self.cfg["host"], "--port", str(port),
            "-c", str(self.cfg.get("embedNCtx", 2048)),
            "-ngl", str(self.cfg.get("nGpuLayers", 0)),
            "--pooling", str(self.cfg.get("embedPooling", "mean")),
        ]
        t = self._threads()
        if t > 0:
            args += ["-t", str(t)]
        if self.cfg.get("useMlock", True):
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(it["embedding"] for it in items)
            if on_progress:
                try:
                    on_progress(min(start + batch, len(texts)), len(texts))
                except Exception:
                    pass
        return out

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
