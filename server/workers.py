"""Запуск рабочих процессов моделей и обмен с ними сообщениями."""
import itertools
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENVS = Path(os.environ.get("VENVS_DIR", "/workspace/venvs"))
LOG_DIR = Path(os.environ.get("TTS_HOME", "/workspace/tts")) / "logs"

# Какой скрипт и в каком окружении запускать
WORKER_SPECS = {
    "qwen": ("worker_qwen.py", "qwen"),
    "voxcpm": ("worker_voxcpm.py", "voxcpm"),
    "asr": ("worker_asr.py", "qwen"),   # Whisper работает в окружении Qwen (там transformers 5)
}


class WorkerError(RuntimeError):
    pass


class Worker:
    def __init__(self, kind):
        self.kind = kind
        self.proc = None
        self.lines = None
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def _python(self):
        if os.environ.get("FAKE_ENGINES") == "1":
            return os.environ.get("FAKE_PYTHON", "python3"), HERE / "worker_fake.py"
        script, venv = WORKER_SPECS[self.kind]
        return str(VENVS / venv / "bin" / "python"), HERE / script

    def start(self):
        python, script = self._python()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / f"worker_{self.kind}.log", "a", encoding="utf-8")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=str(HERE))
        self.proc = subprocess.Popen(
            [python, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, encoding="utf-8", bufsize=1, cwd=str(HERE), env=env,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self.lines), daemon=True).start()
        hello = self._read(timeout=600)
        if not hello.get("hello"):
            raise WorkerError(f"{self.kind}: неожиданный ответ при запуске: {hello}")

    @staticmethod
    def _reader(proc, lines):
        for line in proc.stdout:
            line = line.strip()
            if line:
                lines.put(line)
        lines.put(None)  # процесс завершился

    def _read(self, timeout):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.stop()
            raise WorkerError(f"{self.kind}: нет ответа {timeout} с, процесс перезапущен")
        if line is None:
            code = self.proc.poll()
            self.proc = None
            raise WorkerError(f"{self.kind}: процесс упал (код {code}), подробности в logs/worker_{self.kind}.log")
        return json.loads(line)

    def call(self, op, timeout=900, **params):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.start()
            req_id = next(self.ids)
            self.proc.stdin.write(json.dumps({"id": req_id, "op": op, **params}, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            reply = self._read(timeout)
            if not reply.get("ok"):
                raise WorkerError(f"{self.kind}/{op}: {reply.get('error')}")
            return reply

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None


class WorkerPool:
    def __init__(self):
        self.workers = {}

    def get(self, kind):
        if kind not in self.workers:
            self.workers[kind] = Worker(kind)
        return self.workers[kind]

    def stop_all(self):
        for w in self.workers.values():
            w.stop()
