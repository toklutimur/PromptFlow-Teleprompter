from __future__ import annotations

import cgi
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "library"

MODEL_NAME = os.environ.get("PROMPTFLOW_MODEL", "large-v3-turbo")
try:
    BEAM_SIZE = max(1, int(os.environ.get("PROMPTFLOW_BEAM", "1")))
except ValueError:
    BEAM_SIZE = 1

AUDIO_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "webm": "audio/webm",
}
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Only the app's own address may talk to the server: blocks DNS rebinding (Host) and cross-site writes (Origin).
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
TR_MAP = str.maketrans({"ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i", "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u"})


# --- Whisper model: loaded once, kept warm, one transcription at a time ---

_model = None
_ready = False
_model_error: str | None = None
_model_lock = threading.Lock()
_transcribe_lock = threading.Lock()
_dll_handles: list = []  # keep os.add_dll_directory handles alive for the process lifetime


def register_nvidia_dlls() -> list[str]:
    """Expose pip-installed CUDA libraries (requirements-gpu.txt) to ctranslate2 on Windows.

    The nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels put their DLLs in site-packages/nvidia/*/bin,
    which is not on the Windows DLL search path; without this ctranslate2 fails with
    "cublas64_12.dll not found" and the server drops to the slow cpu/int8 path.
    Must run before ctranslate2 / faster_whisper is imported. No-op when the dirs are absent.
    """
    if os.name != "nt":
        return []
    found: list[str] = []
    for base in dict.fromkeys(p for p in sys.path if p):
        root = Path(base) / "nvidia"
        try:
            if not root.is_dir():
                continue
            bins = sorted(b for b in root.glob("*/bin") if any(b.glob("*.dll")))
        except OSError:
            continue
        for bin_dir in bins:
            path = str(bin_dir.resolve())
            if path not in found:
                found.append(path)
    for path in found:
        try:
            _dll_handles.append(os.add_dll_directory(path))
        except (OSError, AttributeError):
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])
    return found


_nvidia_dll_dirs = register_nvidia_dlls()


def detect_device() -> tuple[str, str]:
    forced = os.environ.get("PROMPTFLOW_DEVICE", "auto").lower()
    if forced == "cpu":
        return "cpu", "int8"
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            # int8_float16: ~4x faster than float16 on cards without fp16 tensor cores (GTX 16xx,
            # measured 1.7s vs 6.5s for a 20s clip) and half the VRAM; PROMPTFLOW_COMPUTE overrides.
            compute = os.environ.get("PROMPTFLOW_COMPUTE", "int8_float16")
            supported = ctranslate2.get_supported_compute_types("cuda")
            if compute not in supported:
                compute = "float16" if "float16" in supported else "float32"
            return "cuda", compute
    except Exception:
        pass
    return "cpu", "int8"


_device, _compute = detect_device()


def get_model():
    """Return the shared WhisperModel, building it on first use."""
    global _model, _model_error, _device, _compute
    with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        print(f"[model] loading {MODEL_NAME} on {_device}/{_compute}", flush=True)
        kwargs = {"device": _device, "compute_type": _compute}
        if _device == "cpu":
            kwargs["cpu_threads"] = os.cpu_count() or 4
        try:
            _model = WhisperModel(MODEL_NAME, **kwargs)
        except Exception as exc:
            if _device != "cuda":
                _model_error = str(exc)
                raise
            print(f"[model] CUDA load failed ({exc}); falling back to cpu/int8", flush=True)
            _device, _compute = "cpu", "int8"
            try:
                _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
            except Exception as cpu_exc:
                _model_error = str(cpu_exc)
                raise
        _model_error = None
        print(f"[model] ready in {time.perf_counter() - started:.1f}s", flush=True)
        return _model


def _fall_back_to_cpu(exc: Exception):
    global _model, _device, _compute
    with _model_lock:
        print(f"[model] CUDA transcription failed ({exc}); reloading on cpu/int8", flush=True)
        _model, _device, _compute = None, "cpu", "int8"


def _probe(model):
    """Run the encoder once on silence; surfaces missing CUDA libraries (cuBLAS/cuDNN) at warm-up."""
    import numpy as np

    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", vad_filter=False, beam_size=1)
    for _segment in segments:
        pass


def warm_model():
    global _model_error, _ready
    try:
        with _transcribe_lock:
            try:
                _probe(get_model())
            except Exception as exc:
                if _device != "cuda":
                    raise
                _fall_back_to_cpu(exc)
                _probe(get_model())
        _ready = True
    except Exception as exc:
        _model_error = str(exc)
        print(f"[model] warm-up failed: {exc}", flush=True)


def _run_whisper(model, samples) -> list[dict]:
    segments, _ = model.transcribe(samples, language=None, word_timestamps=True, vad_filter=True, beam_size=BEAM_SIZE)
    recognized = []
    for segment in segments:
        for word in segment.words or []:
            recognized.append({"word": word.word.strip(), "start": word.start, "end": word.end})
    return recognized


def transcribe(audio_path: str) -> list[dict]:
    global _ready
    import numpy as np

    pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_path, "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"], check=True, capture_output=True).stdout
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with _transcribe_lock:
        model = get_model()
        try:
            recognized = _run_whisper(model, samples)
        except Exception as exc:
            if _device != "cuda":
                raise
            _fall_back_to_cpu(exc)
            recognized = _run_whisper(get_model(), samples)
        _ready = True
        return recognized


# --- Script/transcript alignment ---

def norm(word: str) -> str:
    return re.sub(r"[^\wİıŞşĞğÇçÖöÜü]", "", word, flags=re.UNICODE).casefold()


def align(script: str, recognized: list[dict]) -> list[dict]:
    clean_script = re.sub(r"\[(?:HOOK|PAUSE|CAMERA|ASIDE)\]", " ", script)
    script_words = re.findall(r"[\wİıŞşĞğÇçÖöÜü]+(?:['’][\wİıŞşĞğÇçÖöÜü]+)?", clean_script, flags=re.UNICODE)
    wanted = [norm(w) for w in script_words]
    heard = [norm(w["word"]) for w in recognized]
    matcher = SequenceMatcher(None, wanted, heard, autojunk=False)
    result = [{"word": word, "start": None, "end": None} for word in script_words]
    matched = sum(block.size for block in matcher.get_matching_blocks())
    if matched / max(1, max(len(wanted), len(heard))) < .65:
        raise ValueError("Ses ile metin eşleşmedi. Metin sesten oluşturulacak.")
    for block in matcher.get_matching_blocks():
        for i in range(block.size):
            a = block.a + i
            b = block.b + i
            result[a]["start"] = recognized[b]["start"]
            result[a]["end"] = recognized[b]["end"]
    known = [i for i, item in enumerate(result) if item["start"] is not None]
    if not known:
        raise ValueError("Ses ile metin eşleşmedi. Metni kontrol edin.")
    for i, item in enumerate(result):
        if item["start"] is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is not None and after is not None:
            ratio = (i - before) / (after - before)
            start = result[before]["end"] + (result[after]["start"] - result[before]["end"]) * ratio
            end = start + max(0.08, (result[after]["start"] - result[before]["end"]) / max(1, after - before) * .8)
        elif before is not None:
            start = result[before]["end"] + .08 * (i - before)
            end = start + .18
        else:
            start = max(0, result[after]["start"] - .08 * (after - i))
            end = start + .18
        item["start"], item["end"] = round(start, 3), round(end, 3)
    return result


# --- Local library: library/<slug>/audio.<ext> + scene.json ---

def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", (title or "").translate(TR_MAP))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = text[:78].strip("-") or "sahne"
    return f"{text}-1" if text in WINDOWS_RESERVED else text


def entry_dir(slug: str) -> Path | None:
    """Return the library folder for a slug, or None if the slug is unsafe."""
    if not slug or len(slug) > 80 or not SLUG_RE.fullmatch(slug) or slug in WINDOWS_RESERVED:
        return None
    base = LIBRARY.resolve()
    path = (base / slug).resolve()
    if path.parent != base:
        return None
    return path


def entry_audio(path: Path) -> Path | None:
    for candidate in sorted(path.glob("audio.*")):
        if candidate.suffix.lstrip(".").lower() in AUDIO_TYPES:
            return candidate
    return None


def form_text(form, name: str, default: str = "") -> str:
    value = form.getfirst(name, default)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else default


class Handler(BaseHTTPRequestHandler):
    def _parts(self) -> list[str]:
        raw = urlsplit(self.path).path
        return [unquote(part) for part in raw.split("/") if part]

    def _json(self, status: int, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        content_type = self.headers.get("Content-Type", "")
        return cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})

    def _forbidden(self, write: bool) -> bool:
        """403 unless the request targets this server by its own name and, for writes, comes from its own page.

        No CORS headers are sent anywhere: the UI is same-origin, so other sites can neither read responses
        nor pass the Origin check on POST/DELETE, and a rebound hostname fails the Host check.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        origin = self.headers.get("Origin")
        if host not in ALLOWED_HOSTS or (write and origin is not None and origin.strip().lower() not in ALLOWED_ORIGINS):
            self.close_connection = True
            self._json(403, {"error": "Yalnızca yerel PromptFlow sayfasından erişilebilir"})
            return True
        return False

    # GET -------------------------------------------------------------

    def do_GET(self):
        if self._forbidden(write=False):
            return
        parts = self._parts()
        if not parts or parts == ["index.html"]:
            self._index()
        elif parts == ["health"]:
            self._json(200, {"ready": _ready and _model is not None, "device": _device, "computeType": _compute, "model": MODEL_NAME, "beamSize": BEAM_SIZE, "error": _model_error})
        elif parts == ["library"]:
            self._library_list()
        elif len(parts) in (2, 3) and parts[0] == "library":
            path = entry_dir(parts[1])
            if path is None:
                self._json(400, {"error": "Geçersiz kayıt adı"})
            elif not (path / "scene.json").is_file():
                self._json(404, {"error": "Kayıt bulunamadı"})
            elif len(parts) == 2:
                self._json(200, json.loads((path / "scene.json").read_text(encoding="utf-8")))
            elif parts[2] == "audio":
                self._library_audio(path)
            else:
                self._json(404, {"error": "Bilinmeyen endpoint"})
        else:
            self._json(404, {"error": "Bilinmeyen endpoint"})

    def _index(self):
        data = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _library_list(self):
        items = []
        if LIBRARY.is_dir():
            for folder in LIBRARY.iterdir():
                scene_file = folder / "scene.json"
                if not folder.is_dir() or not scene_file.is_file():
                    continue
                try:
                    scene = json.loads(scene_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                items.append({"slug": folder.name, "title": scene.get("title", folder.name), "savedAt": scene.get("savedAt", ""), "duration": scene.get("duration", 0)})
        items.sort(key=lambda item: item["savedAt"], reverse=True)
        self._json(200, items)

    def _library_audio(self, path: Path):
        audio = entry_audio(path)
        if audio is None:
            self._json(404, {"error": "Ses bulunamadı"})
            return
        data = audio.read_bytes()
        mime = AUDIO_TYPES.get(audio.suffix.lstrip(".").lower(), "application/octet-stream")
        start, end, status = 0, len(data) - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and data and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), len(data) - 1) if match.group(2) else len(data) - 1
            else:
                start = max(0, len(data) - int(match.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            status = 206
        body = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # POST ------------------------------------------------------------

    def do_POST(self):
        if self._forbidden(write=True):
            return
        parts = self._parts()
        if parts == ["align"]:
            self._align()
        elif parts == ["library"]:
            self._library_save()
        else:
            self._json(404, {"error": "Bilinmeyen endpoint"})

    def _align(self):
        try:
            form = self._form()
            script = form_text(form, "script")
            audio = form["audio"] if "audio" in form else None
            if audio is None or not getattr(audio, "file", None):
                raise ValueError("Ses dosyası eksik")
            name = audio.filename or "audio.mp3"
            suffix = Path(name).suffix or ".mp3"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                audio.file.seek(0)
                tmp.write(audio.file.read())
                audio_path = tmp.name
            try:
                started = time.perf_counter()
                recognized = transcribe(audio_path)
                transcript = " ".join(item["word"] for item in recognized)
                replaced = False
                try:
                    timings = align(script, recognized)
                except ValueError:
                    script = transcript
                    timings = align(script, recognized)
                    replaced = True
                print(f"[align] {name}: {time.perf_counter() - started:.1f}s, {len(recognized)} words", flush=True)
                self._json(200, {"words": timings, "transcript": script, "replaced": replaced, "recognized": len(recognized)})
            finally:
                os.unlink(audio_path)
        except Exception as exc:
            print(f"[align] error: {exc}", flush=True)
            self._json(500, {"error": str(exc)})

    def _library_save(self):
        try:
            form = self._form()
            audio = form["audio"] if "audio" in form else None
            if audio is None or not getattr(audio, "file", None):
                self._json(400, {"error": "Ses dosyası eksik"})
                return
            title = form_text(form, "title").strip() or "Sahne"
            text = form_text(form, "text")
            try:
                alignment = json.loads(form_text(form, "alignment", "[]") or "[]")
            except ValueError:
                alignment = None
            if not isinstance(alignment, list) or not all(isinstance(item, dict) for item in alignment):
                self._json(400, {"error": "Zamanlama verisi geçersiz"})
                return
            try:
                duration = float(form_text(form, "duration", "0") or 0)
            except ValueError:
                duration = 0.0
            if duration <= 0 and alignment:
                duration = float(alignment[-1].get("end") or 0)
            ext = Path(audio.filename or "audio.mp3").suffix.lstrip(".").lower()
            if ext not in AUDIO_TYPES:
                ext = "mp3"
            slug = slugify(title)
            path = entry_dir(slug)
            if path is None:
                self._json(400, {"error": "Geçersiz kayıt adı"})
                return
            overwritten = (path / "scene.json").is_file()
            path.mkdir(parents=True, exist_ok=True)
            for old in path.glob("audio.*"):
                old.unlink()
            audio.file.seek(0)
            (path / f"audio.{ext}").write_bytes(audio.file.read())
            scene = {
                "title": title,
                "slug": slug,
                "text": text,
                "alignment": alignment,
                "duration": round(duration, 3),
                "audioName": Path(audio.filename or f"audio.{ext}").name,
                "audioExt": ext,
                "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            tmp = path / "scene.json.tmp"
            tmp.write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path / "scene.json")
            print(f"[library] saved {slug}", flush=True)
            self._json(200, {"slug": slug, "title": title, "savedAt": scene["savedAt"], "overwritten": overwritten})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    # DELETE ----------------------------------------------------------

    def do_DELETE(self):
        if self._forbidden(write=True):
            return
        parts = self._parts()
        if len(parts) != 2 or parts[0] != "library":
            self._json(404, {"error": "Bilinmeyen endpoint"})
            return
        path = entry_dir(parts[1])
        if path is None:
            self._json(400, {"error": "Geçersiz kayıt adı"})
        elif not path.is_dir():
            self._json(404, {"error": "Kayıt bulunamadı"})
        else:
            shutil.rmtree(path)
            print(f"[library] deleted {parts[1]}", flush=True)
            self._json(200, {"deleted": parts[1]})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print(f"PromptFlow alignment server: http://{HOST}:{PORT} (model {MODEL_NAME}, {_device}/{_compute}, beam {BEAM_SIZE})", flush=True)
    print(f"[cuda] nvidia DLL dirs: {', '.join(_nvidia_dll_dirs) or 'none (requirements-gpu.txt not installed)'}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=warm_model, name="whisper-warmup", daemon=True).start()
    server.serve_forever()
