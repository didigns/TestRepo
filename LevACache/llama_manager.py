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
        self._log(f"llama-server 기동: model={key}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
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

    def chat(self, prompt, images=None, *, max_tokens=768, temperature=0.2, timeout=300):
        content = [{"type": "text", "text": prompt}]
        for img in images or []:
            with open(img, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + b64},
            })
        payload = {
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
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
