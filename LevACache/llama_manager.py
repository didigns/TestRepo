"""llama-server 하나를 관리하며 필요한 모델로 스왑(재기동)한다.
- ensure_model(key): 현재 로드된 모델과 다르면 서버를 재기동
- chat(prompt, images): /v1/chat/completions 호출 (OpenAI 호환)
비전 모델(MiniCPM-V)은 --mmproj 로 멀티모달 활성화, 이미지는 base64 data URL 로 전달.
"""
import base64
import json
import subprocess
import time
import urllib.error
import urllib.request


class LlamaManager:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger
        self.proc = None
        self.loaded_key = None

    def _log(self, msg):
        if self.log:
            self.log(msg)

    @property
    def base_url(self):
        return f"http://{self.cfg['host']}:{self.cfg['port']}"

    def _model_args(self, key):
        if key == "vision":
            model = self.cfg.get("visionModel")
            mmproj = self.cfg.get("visionMmproj")
            if not model:
                raise RuntimeError("visionModel(MiniCPM-V gguf) 경로가 설정되지 않았습니다.")
            args = ["-m", model]
            if mmproj:
                args += ["--mmproj", mmproj]
            return args
        else:
            model = self.cfg.get("textModel")
            if not model:
                raise RuntimeError("textModel(Gemma gguf) 경로가 설정되지 않았습니다.")
            return ["-m", model]

    def ensure_model(self, key):
        if self.loaded_key == key and self._alive():
            return
        self.stop()
        exe = self.cfg["llamaServerExe"]
        args = [
            exe, *self._model_args(key),
            "--host", self.cfg["host"], "--port", str(self.cfg["port"]),
            "-c", str(self.cfg.get("nCtx", 8192)),
            "-ngl", str(self.cfg.get("nGpuLayers", 0)),
        ]
        # ---- 레이턴시 최적화 플래그 ----
        n_threads = self.cfg.get("nThreads", 0)
        if n_threads and int(n_threads) > 0:
            args += ["-t", str(int(n_threads)), "-tb", str(int(n_threads))]
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
                "image_url": {"url": "data:image/png;base64," + b64},
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
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
        self.loaded_key = None
