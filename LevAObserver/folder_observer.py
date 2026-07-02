"""
folder_observer.py
등록한 폴더의 파일 생성/삭제/수정/이동(File Stream IO)을 실시간 감지하는 스크립트.

사용법:
    pip install watchdog
    python folder_observer.py "C:\\감시할\\폴더경로"
    python folder_observer.py "C:\\감시할\\폴더경로" --recursive --log events.log
    python folder_observer.py --daemon

옵션:
    path              감시할 폴더 경로 (생략 시 현재 폴더, --daemon 시 무시)
    --daemon          부모 프로세스 제어 모드 (stdin/stdout NDJSON 프로토콜)
    --recursive, -r   하위 폴더까지 감시
    --log FILE        이벤트를 파일에도 기록
    --ignore PATTERN  무시할 파일 패턴 (여러 번 지정 가능, 예: --ignore "*.tmp")
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from fnmatch import fnmatch

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("watchdog 라이브러리가 필요합니다. 다음 명령으로 설치하세요:")
    print("    pip install watchdog")
    sys.exit(1)

PROTOCOL_VERSION = "1.0"


class LevAEventHandler(FileSystemEventHandler):
    EVENT_LABELS = {
        "created": "created",
        "deleted": "deleted",
        "modified": "modified",
        "moved": "moved",
    }

    def __init__(self, logger=None, ignore_patterns=None, on_event=None):
        super().__init__()
        self.logger = logger
        self.ignore_patterns = ignore_patterns or []
        self.on_event = on_event

    def set_ignore_patterns(self, ignore_patterns):
        if ignore_patterns is None:
            self.ignore_patterns = []
            return
        self.ignore_patterns = list(ignore_patterns)

    def _ignored(self, path):
        if path is None:
            return False
        name = os.path.basename(path)
        return any(fnmatch(name, pat) for pat in self.ignore_patterns)

    def _build_event_payload(self, event):
        if event is None:
            return None

        payload = {
            "eventType": event.event_type,
            "isDirectory": event.is_directory,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        if event.event_type == "moved":
            payload["srcPath"] = event.src_path
            payload["destPath"] = event.dest_path
        else:
            payload["path"] = event.src_path

        return payload

    def _log_to_logger(self, event):
        if self.logger is None or event is None:
            return

        kind = "directory" if event.is_directory else "file"
        label = self.EVENT_LABELS.get(event.event_type, event.event_type)

        if event.event_type == "moved":
            self.logger.info(f"[{label}] {kind}: {event.src_path} -> {event.dest_path}")
        else:
            self.logger.info(f"[{label}] {kind}: {event.src_path}")

    def _handle_event(self, event):
        if event is None:
            return

        if event.event_type == "moved":
            if self._ignored(event.src_path) and self._ignored(event.dest_path):
                return
        elif self._ignored(event.src_path):
            return

        payload = self._build_event_payload(event)
        if payload is None:
            return

        if self.on_event is not None:
            self.on_event(payload)

        self._log_to_logger(event)

    def on_created(self, event):
        self._handle_event(event)

    def on_deleted(self, event):
        self._handle_event(event)

    def on_modified(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        self._handle_event(event)


class LevAObserverService:
    def __init__(self, logger):
        self._logger = logger
        self._observer = Observer()
        self._ignore_patterns = []
        self._watches = {}
        self._watch_counter = 0

    def start(self):
        if not self._observer.is_alive():
            self._observer.start()
            if self._logger is not None:
                self._logger.info("LevAObserver daemon started")

    def stop(self):
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            if self._logger is not None:
                self._logger.info("LevAObserver daemon stopped")

    def emit_message(self, message):
        if message is None:
            return
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def emit_result(self, request_id, ok, **fields):
        response = {"id": request_id, "type": "result", "ok": ok}
        response.update(fields)
        self.emit_message(response)

    def emit_error(self, request_id, error_code, message):
        self.emit_result(
            request_id,
            False,
            error=error_code,
            message=message,
        )

    def _next_watch_id(self):
        self._watch_counter += 1
        return f"w-{self._watch_counter}"

    def _find_watch_id_by_path(self, path):
        if path is None:
            return None
        for watch_id, entry in self._watches.items():
            if entry is not None and entry.get("path") == path:
                return watch_id
        return None

    def _serialize_watches(self):
        watches = []
        for watch_id, entry in self._watches.items():
            if entry is None:
                continue
            watches.append({
                "watchId": watch_id,
                "path": entry.get("path"),
                "recursive": entry.get("recursive", False),
            })
        return watches

    def _create_handler(self, watch_id):
        def on_event(payload):
            if payload is None:
                return
            event_message = {"type": "event", "watchId": watch_id}
            event_message.update(payload)
            self.emit_message(event_message)

        return LevAEventHandler(
            logger=self._logger,
            ignore_patterns=self._ignore_patterns,
            on_event=on_event,
        )

    def handle_command(self, message):
        if message is None or not isinstance(message, dict):
            return None

        request_id = message.get("id")
        command_type = message.get("type")

        if request_id is None:
            self.emit_error(None, "MISSING_FIELD", "Field 'id' is required")
            return None

        if command_type is None:
            self.emit_error(request_id, "INVALID_REQUEST", "Field 'type' is required")
            return None

        if command_type == "list":
            self.emit_result(request_id, True, watches=self._serialize_watches())
            return None

        if command_type == "add":
            self._handle_add(request_id, message)
            return None

        if command_type == "remove":
            self._handle_remove(request_id, message)
            return None

        if command_type == "set_ignore":
            self._handle_set_ignore(request_id, message)
            return None

        if command_type == "shutdown":
            self.emit_result(request_id, True)
            return "shutdown"

        self.emit_error(request_id, "INVALID_REQUEST", f"Unknown command type: {command_type}")
        return None

    def _handle_add(self, request_id, message):
        raw_path = message.get("path")
        if raw_path is None or str(raw_path).strip() == "":
            self.emit_error(request_id, "MISSING_FIELD", "Field 'path' is required")
            return

        watch_path = os.path.abspath(str(raw_path))
        if not os.path.isdir(watch_path):
            self.emit_error(request_id, "PATH_NOT_FOUND", f"Directory not found: {watch_path}")
            return

        existing_watch_id = self._find_watch_id_by_path(watch_path)
        if existing_watch_id is not None:
            self.emit_error(
                request_id,
                "ALREADY_WATCHING",
                f"Path is already watched: {watch_path} ({existing_watch_id})",
            )
            return

        recursive = bool(message.get("recursive", False))
        watch_id = self._next_watch_id()
        handler = self._create_handler(watch_id)

        try:
            observed_watch = self._observer.schedule(handler, watch_path, recursive=recursive)
        except Exception as exc:
            self.emit_error(request_id, "INVALID_PATH", str(exc))
            return

        self._watches[watch_id] = {
            "path": watch_path,
            "recursive": recursive,
            "observed_watch": observed_watch,
            "handler": handler,
        }

        if self._logger is not None:
            self._logger.info(f"Watch added: {watch_id} -> {watch_path} (recursive={recursive})")

        self.emit_result(request_id, True, watchId=watch_id)

    def _handle_remove(self, request_id, message):
        watch_id = message.get("watchId")
        if watch_id is None or str(watch_id).strip() == "":
            self.emit_error(request_id, "MISSING_FIELD", "Field 'watchId' is required")
            return

        entry = self._watches.get(watch_id)
        if entry is None:
            self.emit_error(request_id, "WATCH_NOT_FOUND", f"watchId not found: {watch_id}")
            return

        observed_watch = entry.get("observed_watch")
        if observed_watch is not None:
            try:
                self._observer.unschedule(observed_watch)
            except Exception as exc:
                self.emit_error(request_id, "INVALID_REQUEST", str(exc))
                return

        del self._watches[watch_id]

        if self._logger is not None:
            self._logger.info(f"Watch removed: {watch_id}")

        self.emit_result(request_id, True)

    def _handle_set_ignore(self, request_id, message):
        patterns = message.get("patterns")
        if patterns is None:
            self.emit_error(request_id, "MISSING_FIELD", "Field 'patterns' is required")
            return

        if not isinstance(patterns, list):
            self.emit_error(request_id, "INVALID_REQUEST", "Field 'patterns' must be a list")
            return

        self._ignore_patterns = [str(pattern) for pattern in patterns if pattern is not None]

        for entry in self._watches.values():
            if entry is None:
                continue
            handler = entry.get("handler")
            if handler is not None:
                handler.set_ignore_patterns(self._ignore_patterns)

        if self._logger is not None:
            self._logger.info(f"Ignore patterns updated: {self._ignore_patterns}")

        self.emit_result(request_id, True, patterns=self._ignore_patterns)

    def run_daemon_loop(self):
        self.start()
        self.emit_message({"type": "ready", "version": PROTOCOL_VERSION})

        try:
            for line in sys.stdin:
                if line is None:
                    continue

                stripped_line = line.strip()
                if stripped_line == "":
                    continue

                try:
                    message = json.loads(stripped_line)
                except json.JSONDecodeError:
                    self.emit_message({
                        "type": "result",
                        "ok": False,
                        "error": "INVALID_REQUEST",
                        "message": "Invalid JSON",
                    })
                    continue

                action = self.handle_command(message)
                if action == "shutdown":
                    break
        finally:
            self.stop()


def build_logger(log_file=None, stream=None):
    logger = logging.getLogger("LevAObserver")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    output_stream = stream if stream is not None else sys.stderr
    console = logging.StreamHandler(output_stream)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


def run_cli(watch_path, recursive, log_file, ignore_patterns):
    if watch_path is None or not os.path.isdir(watch_path):
        print(f"오류: 폴더가 존재하지 않습니다 -> {watch_path}")
        sys.exit(1)

    logger = build_logger(log_file, stream=sys.stderr)

    logger.info("=" * 60)
    logger.info(f"LevAObserver started: {watch_path}")
    logger.info(f"Recursive watch: {'yes' if recursive else 'no'}")
    if ignore_patterns:
        logger.info(f"Ignore patterns: {', '.join(ignore_patterns)}")
    logger.info("Press Ctrl+C to stop.")
    logger.info("=" * 60)

    handler = LevAEventHandler(logger=logger, ignore_patterns=ignore_patterns)
    observer = Observer()
    observer.schedule(handler, watch_path, recursive=recursive)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested. Stopping observer.")
        observer.stop()

    observer.join()
    logger.info("LevAObserver stopped.")


def _force_utf8_io():
    # Windows 기본 콘솔 인코딩(cp949 등)으로 인한 한글 깨짐 방지.
    # stdin/stdout/stderr를 UTF-8로 재설정한다 (Python 3.7+).
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def run_daemon(ignore_patterns):
    _force_utf8_io()
    logger = build_logger(stream=sys.stderr)
    service = LevAObserverService(logger)

    if ignore_patterns:
        service._ignore_patterns = list(ignore_patterns)

    service.run_daemon_loop()


def main():
    parser = argparse.ArgumentParser(
        description="등록한 폴더의 파일 생성/삭제/수정/이동을 실시간 감지합니다."
    )
    parser.add_argument("path", nargs="?", default=".", help="감시할 폴더 경로")
    parser.add_argument("--daemon", action="store_true", help="부모 프로세스 제어 모드")
    parser.add_argument("--recursive", "-r", action="store_true", help="하위 폴더까지 감시")
    parser.add_argument("--log", metavar="FILE", help="이벤트를 기록할 로그 파일")
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="PATTERN",
        help="무시할 파일 패턴 (예: --ignore \"*.tmp\")",
    )
    args = parser.parse_args()

    if args.daemon:
        run_daemon(args.ignore)
        return

    watch_path = os.path.abspath(args.path)
    run_cli(watch_path, args.recursive, args.log, args.ignore)


if __name__ == "__main__":
    main()
