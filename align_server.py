from __future__ import annotations

import cgi
import json
import os
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent


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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        data = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path != "/align":
            self._json(404, {"error": "Bilinmeyen endpoint"})
            return
        try:
            content_type = self.headers.get("Content-Type", "")
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
            script = form.getfirst("script", "")
            if isinstance(script, bytes):
                script = script.decode("utf-8", errors="replace")
            audio = form["audio"]
            if not getattr(audio, "file", None):
                raise ValueError("Ses dosyası eksik")
            suffix = Path(audio.filename or "audio.mp3").suffix or ".mp3"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                audio.file.seek(0)
                tmp.write(audio.file.read())
                audio_path = tmp.name
            try:
                from faster_whisper import WhisperModel
                model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
                import numpy as np
                pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_path, "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"], check=True, capture_output=True).stdout
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                segments, _ = model.transcribe(samples, language=None, word_timestamps=True, vad_filter=True)
                recognized = []
                for segment in segments:
                    for word in segment.words or []:
                        recognized.append({"word": word.word.strip(), "start": word.start, "end": word.end})
                transcript = " ".join(item["word"] for item in recognized)
                replaced = False
                try:
                    timings = align(script, recognized)
                except ValueError:
                    script = transcript
                    timings = align(script, recognized)
                    replaced = True
                self._json(200, {"words": timings, "transcript": script, "replaced": replaced, "recognized": len(recognized)})
            finally:
                os.unlink(audio_path)
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"PromptFlow alignment server: http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
