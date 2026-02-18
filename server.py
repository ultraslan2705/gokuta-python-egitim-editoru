from __future__ import annotations

import ast
from collections import defaultdict, deque
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


HOST = os.getenv("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)
EXEC_TIMEOUT_SECONDS = 3
MAX_OUTPUT_CHARS = 12_000
RATE_LIMIT_WINDOW_SECONDS = _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
RATE_LIMIT_MAX_REQUESTS = _env_int("RATE_LIMIT_MAX_REQUESTS", 120)
DEFAULT_INPUT_LINE = os.getenv("DEFAULT_INPUT_LINE", "0")
DEFAULT_INPUT_LINES_COUNT = _env_int("DEFAULT_INPUT_LINES_COUNT", 40)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

ERROR_HINTS_TR = {
    "SyntaxError": "Yazım hatası var. Komutun yapısını tekrar kontrol et.",
    "IndentationError": "Girinti hatası var. Aynı bloktaki satırlar eşit boşlukla başlamalı.",
    "TabError": "Sekme ve boşluk karışmış. Girintide tek bir yöntem kullan.",
    "NameError": "Tanımlanmamış bir isim kullandın.",
    "TypeError": "Veri türleri bu işlem için uyumlu değil.",
    "ValueError": "Fonksiyona uygun olmayan bir değer gönderildi.",
    "ZeroDivisionError": "Sıfıra bölme yapılamaz.",
    "IndexError": "Listenin olmayan bir indeksine erişilmeye çalışıldı.",
    "KeyError": "Sözlükte olmayan bir anahtar kullanıldı.",
    "AttributeError": "Bu nesnede istenen özellik veya metot yok.",
    "ModuleNotFoundError": "İstenen modül bulunamadı.",
    "EOFError": "input() verisi eksik görünüyor. Girdi kutusuna satır ekleyip tekrar dene.",
}

DETAIL_PHRASES_TR = (
    ("invalid syntax", "geçersiz söz dizimi"),
    ("unexpected EOF while parsing", "kod beklenmeden bitti (parantez/tırnak eksik olabilir)"),
    ("unterminated string literal", "tırnak kapanmadan metin bitti"),
    ("expected ':'", "':' bekleniyor"),
    ("expected an indented block", "girintili bir blok bekleniyor"),
    ("unexpected indent", "beklenmeyen girinti"),
    (
        "unindent does not match any outer indentation level",
        "girinti seviyesi dış bloklarla eşleşmiyor",
    ),
    ("division by zero", "sıfıra bölme"),
    ("list index out of range", "liste indeksi aralık dışında"),
    ("object is not callable", "nesne fonksiyon gibi çağrılamaz"),
    ("unsupported operand type(s)", "desteklenmeyen işlem türü"),
    ("No module named", "modül bulunamadı"),
    ("EOF when reading a line", "satır okunurken giriş verisi bitti"),
)


def _trim(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n\n... [çıktı kısaltıldı]"


def _normalized_stdin(code: str, stdin_data: str) -> str:
    if stdin_data != "":
        return stdin_data

    if re.search(r"\binput\s*\(", code):
        return (DEFAULT_INPUT_LINE + "\n") * max(1, DEFAULT_INPUT_LINES_COUNT)

    return ""


def _extract_literal_input_prompts(code: str) -> list[str]:
    prompts: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return prompts

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "input":
            continue
        if not node.args:
            continue

        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            if first_arg.value:
                prompts.append(first_arg.value)

    return prompts


def _normalize_inline_input_prompts(code: str, stdout: str) -> str:
    prompts = _extract_literal_input_prompts(code)
    if not prompts or not stdout:
        return stdout

    normalized = stdout
    search_from = 0
    for prompt in prompts:
        idx = normalized.find(prompt, search_from)
        if idx == -1:
            continue
        after = idx + len(prompt)
        if after < len(normalized) and normalized[after] not in ("\n", "\r"):
            normalized = normalized[:after] + "\n" + normalized[after:]
            search_from = after + 1
        else:
            search_from = after
    return normalized


def _strip_input_prompts_from_stdout(code: str, stdout: str) -> str:
    cleaned = stdout
    for prompt in _extract_literal_input_prompts(code):
        cleaned = cleaned.replace(prompt, "")
    return cleaned


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1, window_seconds)
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
                return False, retry_after

            bucket.append(now)
            return True, 0


RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)


def _extract_exception_info(stderr: str) -> tuple[str | None, str]:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?::\s*(.*))?$", line)
        if match:
            return match.group(1), (match.group(2) or "").strip()
    return None, ""


def _extract_line_no(stderr: str) -> int | None:
    matches = re.findall(r'File "<string>", line (\d+)', stderr)
    if not matches:
        return None
    return int(matches[-1])


def _translate_detail_tr(exc_type: str, detail: str) -> str:
    if not detail:
        return ""

    if exc_type == "NameError":
        match = re.search(r"name '(.+?)' is not defined", detail)
        if match:
            return f"`{match.group(1)}` adı tanımlı değil."

    if exc_type == "ModuleNotFoundError":
        match = re.search(r"No module named '(.+?)'", detail)
        if match:
            return f"`{match.group(1)}` modülü bulunamadı."

    if exc_type == "KeyError":
        match = re.match(r"'(.+?)'", detail)
        if match:
            return f"`{match.group(1)}` anahtarı sözlükte yok."

    translated = detail
    for src, dst in DETAIL_PHRASES_TR:
        translated = translated.replace(src, dst)
    return translated


def _build_error_message_tr(stderr: str) -> str:
    exc_type, detail = _extract_exception_info(stderr)
    if not exc_type:
        return _trim(stderr.strip()) if stderr.strip() else "Kod çalıştırılırken bir hata oluştu."

    line_no = _extract_line_no(stderr)
    hint = ERROR_HINTS_TR.get(exc_type, f"{exc_type} hatası oluştu.")
    translated_detail = _translate_detail_tr(exc_type, detail)
    original_line = f"{exc_type}: {detail}" if detail else exc_type

    lines = [hint]
    if line_no is not None:
        lines.append(f"Hata satırı: {line_no}")
    if translated_detail:
        lines.append(f"Detay: {translated_detail}")
    lines.append(f"Orijinal mesaj: {original_line}")

    return _trim("\n".join(lines))


def run_python(
    code: str, stdin_data: str = "", strip_input_prompts: bool = False
) -> dict[str, str | bool]:
    stdin_value = _normalized_stdin(code, stdin_data)

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            input=stdin_value,
            timeout=EXEC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output": "",
            "error": "Kod zaman aşımına uğradı (3 saniye). Sonsuz döngü olabilir.",
        }
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "output": "",
            "error": f"Beklenmeyen bir hata oluştu: {exc}",
        }

    stdout_raw = completed.stdout.strip()
    if strip_input_prompts:
        stdout_raw = _strip_input_prompts_from_stdout(code, stdout_raw).strip()
    else:
        stdout_raw = _normalize_inline_input_prompts(code, stdout_raw).strip()
    stderr_raw = completed.stderr.strip()
    stdout = _trim(stdout_raw)

    if completed.returncode == 0:
        return {
            "ok": True,
            "output": stdout if stdout else "(Çıktı yok)",
            "error": "",
        }

    return {
        "ok": False,
        "output": stdout,
        "error": _build_error_message_tr(stderr_raw),
    }


class PlaygroundHandler(BaseHTTPRequestHandler):
    def _send_json(
        self,
        status: int,
        payload: dict[str, str | bool],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _get_client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return self.client_address[0]

    def _send_html(self) -> None:
        if not INDEX_FILE.exists():
            self.send_error(500, "index.html bulunamadı.")
            return

        body = INDEX_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html()
            return
        self.send_error(404, "Sayfa bulunamadı.")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_error(404, "Endpoint bulunamadı.")
            return

        allowed, retry_after = RATE_LIMITER.allow(self._get_client_ip())
        if not allowed:
            self._send_json(
                429,
                {
                    "ok": False,
                    "output": "",
                    "error": f"Çok sık istek gönderildi. {retry_after} saniye sonra tekrar dene.",
                },
                extra_headers={"Retry-After": str(retry_after)},
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "output": "", "error": "Geçersiz istek."})
            return

        if length <= 0:
            self._send_json(400, {"ok": False, "output": "", "error": "Boş istek."})
            return

        raw = self.rfile.read(min(length, 100_000))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(
                400, {"ok": False, "output": "", "error": "JSON formatı hatalı."}
            )
            return

        code = payload.get("code", "")
        stdin_data = payload.get("stdin", "")
        strip_input_prompts = payload.get("strip_input_prompts", False)
        if not isinstance(code, str):
            self._send_json(
                400,
                {"ok": False, "output": "", "error": "`code` metin (string) olmalı."},
            )
            return
        if not isinstance(stdin_data, str):
            self._send_json(
                400,
                {"ok": False, "output": "", "error": "`stdin` metin (string) olmalı."},
            )
            return
        if not isinstance(strip_input_prompts, bool):
            self._send_json(
                400,
                {
                    "ok": False,
                    "output": "",
                    "error": "`strip_input_prompts` true/false olmalı.",
                },
            )
            return

        if not code.strip():
            self._send_json(
                400, {"ok": False, "output": "", "error": "Kod alanı boş olamaz."}
            )
            return

        self._send_json(
            200,
            run_python(
                code, stdin_data=stdin_data, strip_input_prompts=strip_input_prompts
            ),
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), PlaygroundHandler)
    display_host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    print(f"Sunucu hazır: http://{display_host}:{PORT}")
    print("Durdurmak için Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nSunucu kapatıldı.")


if __name__ == "__main__":
    main()
