from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import csv
import bisect
import json
import math
import re
import zipfile
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
DATA = Path(tempfile.gettempdir()) / "mp4-mask-cleaner"
DATA.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = 700 * 1024 * 1024
DATA_TTL_SECONDS = 6 * 60 * 60
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
jobs: dict[str, dict] = {}
lock = threading.Lock()
JOB_STATE_FIELDS = ("status", "progress", "detail", "info", "name", "error", "output", "scan_status",
                    "scan_progress", "scan_error", "scan_results", "scan_zip",
                    "scan_interval", "scan_total", "scan_detected")


def persist_job(job_id: str) -> None:
    with lock:
        state = jobs.get(job_id)
        if not state:
            return
        snapshot = {key: state.get(key) for key in JOB_STATE_FIELDS if key in state}
    folder = DATA / job_id
    if not folder.is_dir():
        return
    destination = folder / "job_state.json"
    temporary = folder / "job_state.json.tmp"
    try:
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        temporary.unlink(missing_ok=True)


def restore_job(job_id: str, folder: Path) -> None:
    with lock:
        if job_id in jobs:
            return
    state_file = folder / "job_state.json"
    if not state_file.is_file():
        return
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    interrupted = False
    if state.get("status") == "processing":
        state.update(status="error", progress=0,
                     error="O servidor reiniciou durante o processamento. Reenvie o vídeo e tente novamente.")
        interrupted = True
    if state.get("scan_status") == "processing":
        state.update(scan_status="error", scan_progress=0,
                     scan_error="O servidor reiniciou durante a análise. Inicie a análise novamente.")
        interrupted = True
    with lock:
        jobs.setdefault(job_id, state)
    if interrupted:
        persist_job(job_id)


@app.errorhandler(HTTPException)
def api_http_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description, code=error.code), error.code
    return error.get_response()


@app.errorhandler(Exception)
def api_unexpected_error(error):
    if request.path.startswith("/api/"):
        app.logger.exception("Falha não tratada na API %s", request.path)
        return jsonify(error="Erro interno no servidor. Recarregue a página ou tente novamente."), 500
    app.logger.exception("Falha não tratada na página %s", request.path)
    return "Erro interno no servidor.", 500


def media_binary(name: str) -> str:
    bundled = RUNTIME_ROOT / "ffmpeg_bin" / f"{name}.exe"
    if bundled.is_file():
        return str(bundled)
    return name


def run_checked(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Programa não encontrado: {cmd[0]}. Instale FFmpeg e confirme que ffmpeg e ffprobe estão no PATH."
        ) from exc


def tool_works(name: str) -> bool:
    try:
        return run_checked([media_binary(name), "-version"], timeout=8).returncode == 0
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return False


def command_error(result: subprocess.CompletedProcess) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    return detail[-900:] or f"O comando encerrou com código {result.returncode}."


MASK_NAME = re.compile(r"mask_\d{9}\.png")


def mask_file(folder: Path, name: str) -> Path | None:
    if not MASK_NAME.fullmatch(name or ""):
        return None
    path = (folder / "masks" / name).resolve()
    if path.parent != (folder / "masks").resolve():
        return None
    return path


def job_path(job_id: str) -> Path:
    if not job_id or any(c not in "0123456789abcdef-" for c in job_id):
        abort(404)
    path = DATA / job_id
    if not path.is_dir():
        abort(404)
    restore_job(job_id, path)
    return path


def probe(path: Path) -> dict:
    result = run_checked(
        [media_binary("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
         "-of", "json", str(path)],
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(command_error(result) or "ffprobe não conseguiu ler esse MP4.")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    fps_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
    try:
        numerator, denominator = map(float, fps_text.split("/"))
        fps = numerator / denominator if denominator else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            "fps": min(max(fps, 1.0), 120.0), "duration": duration}


def set_job(job_id: str, **values) -> None:
    with lock:
        state = jobs.get(job_id)
        if state:
            state.update(values)


def cleanup_expired_jobs() -> None:
    while True:
        cutoff = time.time() - DATA_TTL_SECONDS
        for folder in DATA.iterdir():
            if not folder.is_dir():
                continue
            with lock:
                state = jobs.get(folder.name)
                if state and (state.get("status") == "processing" or state.get("scan_status") == "processing"):
                    continue
            try:
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
                    with lock:
                        jobs.pop(folder.name, None)
            except FileNotFoundError:
                pass
        time.sleep(300)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    tesseract_ok = False
    tesseract_error = None
    try:
        import pytesseract
        bundled = RUNTIME_ROOT / "ocr_bin" / "tesseract.exe"
        if bundled.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(bundled)
        pytesseract.get_tesseract_version()
        tesseract_ok = True
    except Exception as exc:
        tesseract_error = str(exc)
    return jsonify(ffmpeg=tool_works("ffmpeg"), ffprobe=tool_works("ffprobe"),
                   tesseract=tesseract_ok, tesseract_error=tesseract_error)


@app.post("/api/upload")
def upload():
    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify(error="Escolha um arquivo MP4."), 400
    if Path(file.filename).suffix.lower() != ".mp4":
        return jsonify(error="Nesta versão, envie um arquivo .mp4."), 400
    job_id = str(uuid.uuid4())
    folder = DATA / job_id
    folder.mkdir(parents=True, exist_ok=False)
    input_path = folder / "original.mp4"
    file.save(input_path)
    if input_path.stat().st_size > MAX_UPLOAD:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error="O arquivo excede o limite de 700 MB."), 413
    try:
        info = probe(input_path)
        cap = cv2.VideoCapture(str(input_path))
        ok, _ = cap.read()
        cap.release()
        if not ok:
            raise ValueError("O vídeo não pôde ser decodificado.")
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=f"Não foi possível abrir esse vídeo: {exc}"), 400
    with lock:
        jobs[job_id] = {"status": "ready", "progress": 0, "info": info,
                        "name": secure_filename(file.filename) or "video.mp4",
                        "error": None, "output": None, "scan_status": "ready",
                        "scan_progress": 0, "scan_error": None, "scan_results": [],
                        "scan_zip": None, "scan_interval": None, "scan_total": 0}
    persist_job(job_id)
    return jsonify(id=job_id, info=info)


@app.get("/api/<job_id>/info")
def job_info(job_id):
    job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404, description="Sessão do vídeo expirou; envie o MP4 novamente.")
        return jsonify(id=job_id, name=state.get("name", "vídeo MP4"), info=state["info"],
                       status=state["status"], progress=state.get("progress", 0),
                       error=state.get("error"), scan_status=state.get("scan_status", "ready"),
                       scan_progress=state.get("scan_progress", 0),
                       scan_error=state.get("scan_error"))


def read_mask_manifest(folder: Path) -> list[dict]:
    path = folder / "masks" / "manifest.json"
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def write_mask_manifest(folder: Path, entries: list[dict]) -> None:
    directory = folder / "masks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


@app.get("/api/<job_id>/masks")
def list_masks(job_id):
    folder = job_path(job_id)
    with lock:
        if not jobs.get(job_id):
            abort(404, description="Sessão de vídeo não encontrada; envie o MP4 novamente.")
    return jsonify(masks=read_mask_manifest(folder))


@app.post("/api/<job_id>/masks")
def save_mask(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404, description="Sessão de vídeo não encontrada; envie o MP4 novamente.")
        if state.get("status") == "processing":
            return jsonify(error="Aguarde o processamento terminar antes de editar máscaras."), 409
        info = state["info"]
    try:
        frame_index = int(request.form.get("frame", "-1"))
        if frame_index < 0 or frame_index > int(info["duration"] * info["fps"]):
            raise ValueError
    except ValueError:
        return jsonify(error="Número de quadro inválido."), 400
    mask_file = request.files.get("mask")
    mask = cv2.imdecode(np.frombuffer(mask_file.read(), np.uint8), cv2.IMREAD_GRAYSCALE) if mask_file else None
    if mask is None or mask.shape != (info["height"], info["width"]):
        return jsonify(error="A máscara está vazia ou tem dimensões diferentes do vídeo."), 400
    mask = cv2.threshold(mask, 1, 255, cv2.THRESH_BINARY)[1]
    mask_path = folder / "masks" / f"mask_{frame_index:09d}.png"
    entries = read_mask_manifest(folder)
    old_entry = next((entry for entry in entries if int(entry.get("frame", -1)) == frame_index), None)
    if cv2.countNonZero(mask) == 0:
        if old_entry:
            entries.remove(old_entry)
            stored = mask_file(folder, old_entry.get("file", ""))
            if stored:
                stored.unlink(missing_ok=True)
            write_mask_manifest(folder, entries)
        return jsonify(saved=False, removed=bool(old_entry), masks=entries)
    if not old_entry and len(entries) >= 100:
        return jsonify(error="Limite de 100 quadros editados por vídeo atingido."), 413
    ok, encoded = cv2.imencode(".png", mask)
    if not ok:
        return jsonify(error="Não foi possível salvar a máscara PNG."), 500
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask_path.write_bytes(encoded.tobytes())
    target_text = " ".join(request.form.get("target_text", "").split())[:160]
    bbox = None
    try:
        candidate = json.loads(request.form.get("bbox", "null"))
        if isinstance(candidate, list) and len(candidate) == 4:
            x, y, bw, bh = map(int, candidate)
            x, y = max(0, x), max(0, y)
            bw, bh = min(info["width"] - x, bw), min(info["height"] - y, bh)
            if bw > 0 and bh > 0:
                bbox = [x, y, bw, bh]
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    entry = {"frame": frame_index, "time": round(frame_index / info["fps"], 4),
             "file": mask_path.name, "pixels": int(cv2.countNonZero(mask)),
             "target_text": target_text, "bbox": bbox}
    entries = [item for item in entries if int(item.get("frame", -1)) != frame_index]
    entries.append(entry)
    entries.sort(key=lambda item: item["frame"])
    write_mask_manifest(folder, entries)
    return jsonify(saved=True, masks=entries, frame=frame_index)


@app.get("/api/<job_id>/masks/<int:frame_index>")
def get_mask(job_id, frame_index):
    folder = job_path(job_id)
    entry = next((item for item in read_mask_manifest(folder)
                  if int(item.get("frame", -1)) == frame_index), None)
    if not entry:
        abort(404, description="Este quadro ainda não tem máscara salva.")
    path = mask_file(folder, entry.get("file", ""))
    if not path or not path.is_file():
        abort(404, description="O arquivo da máscara não foi encontrado.")
    return send_file(path, mimetype="image/png", conditional=True)


@app.delete("/api/<job_id>/masks/<int:frame_index>")
def delete_mask(job_id, frame_index):
    folder = job_path(job_id)
    entries = read_mask_manifest(folder)
    entry = next((item for item in entries if int(item.get("frame", -1)) == frame_index), None)
    if not entry:
        return jsonify(error="Não há máscara salva neste quadro."), 404
    entries.remove(entry)
    stored = mask_file(folder, entry.get("file", ""))
    if stored:
        stored.unlink(missing_ok=True)
    write_mask_manifest(folder, entries)
    return jsonify(deleted=True, masks=entries)


@app.delete("/api/<job_id>/masks")
def clear_masks(job_id):
    folder = job_path(job_id)
    shutil.rmtree(folder / "masks", ignore_errors=True)
    return jsonify(cleared=True, masks=[])


def expand_binary_mask(mask: np.ndarray | None, amount: int) -> np.ndarray | None:
    """Expand the removal region to cover antialiased text edges and halos."""
    if mask is None or amount <= 0:
        return mask
    amount = min(max(int(amount), 0), 20)
    if amount == 0:
        return mask
    size = amount * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return cv2.dilate(mask, kernel, iterations=1)


@app.post("/api/<job_id>/preview")
def preview_frame(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
    if not state:
        abort(404, description="Sessão de vídeo não encontrada; envie o MP4 novamente.")
    mask_file = request.files.get("mask")
    mask = cv2.imdecode(np.frombuffer(mask_file.read(), np.uint8), cv2.IMREAD_GRAYSCALE) if mask_file else None
    info = state["info"]
    if mask is None or mask.shape != (info["height"], info["width"]):
        return jsonify(error="A máscara está vazia ou tem dimensões diferentes do vídeo."), 400
    mask = cv2.threshold(mask, 1, 255, cv2.THRESH_BINARY)[1]
    if not cv2.countNonZero(mask):
        return jsonify(error="Pinte uma área ou carregue uma máscara antes da prévia."), 400
    cap = None
    try:
        frame_index = max(0, min(int(request.form.get("frame", "0")), int(info["duration"] * info["fps"])))
        radius = min(max(float(request.form.get("radius", "8")), 1.0), 25.0)
        mask_expand = min(max(int(request.form.get("mask_expand", "4")), 0), 20)
        mask = expand_binary_mask(mask, mask_expand)
        method = request.form.get("method", "temporal")
        if method not in ("telea", "ns", "temporal"):
            raise ValueError("Método de reconstrução inválido.")
        cap = cv2.VideoCapture(str(folder / "original.mp4"))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, image = cap.read()
        previous = next_frame = None
        if frame_index > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index - 1)
            prev_ok, candidate = cap.read()
            previous = candidate if prev_ok else None
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index + 1)
        next_ok, candidate = cap.read()
        next_frame = candidate if next_ok else None
        cap.release()
        cap = None
        if not ok:
            return jsonify(error="Não foi possível decodificar o quadro selecionado."), 400
        algo = cv2.INPAINT_NS if method == "ns" else cv2.INPAINT_TELEA
        output = cv2.inpaint(image, mask, radius, algo)
        if method == "temporal":
            output = _temporal_fill(image, output, mask, [(previous, mask), (next_frame, mask)])
        ok, encoded = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 94])
        if not ok:
            raise RuntimeError("Não foi possível codificar a prévia.")
        from flask import Response
        return Response(encoded.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return jsonify(error=f"Falha ao criar prévia: {exc}"), 500
    finally:
        if cap is not None:
            cap.release()


@app.get("/api/<job_id>/frame")
def frame(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
    if not state:
        abort(404)
    cap = cv2.VideoCapture(str(folder / "original.mp4"))
    try:
        if request.args.get("frame") is not None:
            frame_index = max(0, min(int(request.args.get("frame", 0)),
                                     int(state["info"]["duration"] * state["info"]["fps"])))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        else:
            t = max(0.0, min(float(request.args.get("t", 0)), state["info"]["duration"]))
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    except (TypeError, ValueError):
        cap.release()
        return jsonify(error="Quadro ou tempo inválido."), 400
    ok, image = cap.read()
    cap.release()
    if not ok:
        cap = cv2.VideoCapture(str(folder / "original.mp4"))
        ok, image = cap.read()
        cap.release()
    if not ok:
        abort(404)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        abort(500)
    from flask import Response
    return Response(encoded.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


def _ocr_lines(frame: np.ndarray) -> list[dict]:
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("pytesseract não está instalado. Rode pip install -r requirements.txt.") from exc
    bundled_tesseract = RUNTIME_ROOT / "ocr_bin" / "tesseract.exe"
    if bundled_tesseract.is_file():
        pytesseract.pytesseract.tesseract_cmd = str(bundled_tesseract)
    h, w = frame.shape[:2]
    scale = min(1.0, 1600.0 / max(w, h))
    image = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    config = "--oem 3 --psm 11"
    bundled_data = RUNTIME_ROOT / "tessdata"
    if bundled_data.is_dir():
        config += f' --tessdata-dir "{bundled_data}"'
    try:
        data = pytesseract.image_to_data(
            rgb, lang="por+eng", config=config, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract OCR não encontrado. Instale o Tesseract com os idiomas português e inglês."
        ) from exc
    except pytesseract.TesseractError as exc:
        raise RuntimeError(f"Tesseract falhou ({exc}). Confira se os idiomas por e eng estão instalados.") from exc
    groups: dict[tuple[int, int, int], list[dict]] = {}
    for i, raw_text in enumerate(data["text"]):
        text = " ".join(str(raw_text).split())
        if not text or not re.search(r"[\wÀ-ÿ]", text, re.UNICODE):
            continue
        try:
            confidence = float(data["conf"][i])
        except (ValueError, TypeError):
            continue
        if confidence < 35:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        x = int(round(int(data["left"][i]) / scale))
        y = int(round(int(data["top"][i]) / scale))
        bw = int(round(int(data["width"][i]) / scale))
        bh = int(round(int(data["height"][i]) / scale))
        groups.setdefault(key, []).append({"text": text, "confidence": confidence,
                                            "x": x, "y": y, "w": bw, "h": bh})
    lines = []
    for words in groups.values():
        x0 = min(word["x"] for word in words)
        y0 = min(word["y"] for word in words)
        x1 = max(word["x"] + word["w"] for word in words)
        y1 = max(word["y"] + word["h"] for word in words)
        lines.append({"text": " ".join(word["text"] for word in words),
                      "confidence": round(sum(word["confidence"] for word in words) / len(words), 1),
                      "box": [x0, y0, x1 - x0, y1 - y0], "classification": "Texto"})
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


def _timecode(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


@app.post("/api/<job_id>/scan")
def start_scan(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404)
        if state.get("scan_status") == "processing":
            return jsonify(error="A análise desse vídeo já está em andamento."), 409
        if state.get("status") == "processing":
            return jsonify(error="Aguarde o processamento de remoção terminar antes de analisar."), 409
    try:
        interval = float(request.form.get("interval", "0.5"))
        if not 0.1 <= interval <= 10:
            raise ValueError
    except ValueError:
        return jsonify(error="O intervalo deve ficar entre 0,1 e 10 segundos."), 400
    set_job(job_id, scan_status="processing", scan_progress=0, scan_error=None,
            scan_results=[], scan_zip=None, scan_interval=interval, scan_total=0)
    persist_job(job_id)
    threading.Thread(target=run_scan, args=(job_id, folder, interval), daemon=True).start()
    return jsonify(ok=True, interval=interval)


def run_scan(job_id: str, folder: Path, interval: float) -> None:
    cap = cv2.VideoCapture(str(folder / "original.mp4"))
    released = False
    scan_dir = folder / "scan"
    frames_dir = scan_dir / "frames"
    marked_dir = scan_dir / "marked"
    shutil.rmtree(scan_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    marked_dir.mkdir(parents=True, exist_ok=True)
    try:
        with lock:
            info = (jobs.get(job_id) or {}).get("info")
        if not info:
            raise RuntimeError("A sessão do vídeo foi encerrada durante a análise.")
        fps = info["fps"]
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        step = max(1, int(round(fps * interval)))
        expected_samples = math.ceil(count / step) if count else 0
        if expected_samples > 3000:
            raise ValueError(f"A análise geraria {expected_samples} amostras. Aumente o intervalo para reduzir para 3.000 quadros ou menos.")
        results = []
        frame_index = 0
        sample_index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_index % step == 0:
                timestamp = frame_index / fps
                lines = _ocr_lines(frame)
                if lines:
                    filename = f"quadro_{frame_index:08d}_{int(timestamp * 1000):010d}ms.jpg"
                    marked_name = f"marcado_{filename}"
                    cv2.imwrite(str(frames_dir / filename), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    marked = frame.copy()
                    for line in lines:
                        x, y, bw, bh = line["box"]
                        cv2.rectangle(marked, (x, y), (x + bw, y + bh), (0, 220, 255), 2)
                    cv2.imwrite(str(marked_dir / marked_name), marked, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    results.append({"frame": frame_index, "timestamp": round(timestamp, 3),
                                    "timecode": _timecode(timestamp), "filename": filename,
                                    "marked": marked_name, "lines": lines})
                sample_index += 1
                if sample_index % 5 == 0:
                    progress = int(100 * sample_index / max(expected_samples, 1))
                    set_job(job_id, scan_progress=min(progress, 99), scan_total=sample_index)
            frame_index += 1
        cap.release()
        released = True

        # Repeated text in nearly the same location is a watermark candidate, not a certainty.
        occurrences: dict[str, list[tuple[int, dict]]] = {}
        for result in results:
            for line in result["lines"]:
                normalized = " ".join(re.sub(r"\W+", " ", line["text"].lower(), flags=re.UNICODE).split())
                if normalized:
                    occurrences.setdefault(normalized, []).append((result["frame"], line))
        width = info["width"]
        height = info["height"]
        recurring = set()
        for normalized, found in occurrences.items():
            if len({frame for frame, _ in found}) < 3:
                continue
            centers = [((line["box"][0] + line["box"][2] / 2) / width,
                        (line["box"][1] + line["box"][3] / 2) / height) for _, line in found]
            if max(x for x, _ in centers) - min(x for x, _ in centers) < 0.03 and \
                    max(y for _, y in centers) - min(y for _, y in centers) < 0.03:
                recurring.add(normalized)
        for result in results:
            for line in result["lines"]:
                normalized = " ".join(re.sub(r"\W+", " ", line["text"].lower(), flags=re.UNICODE).split())
                if normalized in recurring:
                    line["classification"] = "Texto recorrente (possível marca d’água)"

        csv_path = scan_dir / "deteccoes.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["quadro", "timecode", "tempo_segundos", "texto", "confianca_ocr",
                             "classificacao", "x", "y", "largura", "altura", "arquivo"])
            for result in results:
                for line in result["lines"]:
                    x, y, bw, bh = line["box"]
                    writer.writerow([result["frame"], result["timecode"], result["timestamp"],
                                     line["text"], line["confidence"], line["classification"],
                                     x, y, bw, bh, result["filename"]])
        zip_path = scan_dir / "quadros_com_texto.zip"
        readme = scan_dir / "LEIA.txt"
        readme.write_text(
            f"LimpaVídeo — quadros amostrados a cada {interval:g} s.\n"
            f"Quadros com texto OCR: {len(results)}.\n"
            "Texto recorrente na mesma posição é apenas um candidato a marca d'água.\n"
            "OCR pode falhar em texto pequeno, estilizado ou com baixo contraste e não detecta logotipos sem texto de modo confiável.\n",
            encoding="utf-8")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
            archive.write(csv_path, "deteccoes.csv")
            archive.write(readme, "LEIA.txt")
            for result in results:
                archive.write(frames_dir / result["filename"], f"quadros_originais/{result['filename']}")
                archive.write(marked_dir / result["marked"], f"quadros_marcados/{result['marked']}")
        set_job(job_id, scan_status="done", scan_progress=100, scan_error=None,
                scan_results=results, scan_zip=str(zip_path), scan_total=sample_index,
                scan_detected=len(results))
        persist_job(job_id)
    except Exception as exc:
        if not released:
            cap.release()
        set_job(job_id, scan_status="error", scan_error=f"Falha na análise OCR: {exc}")
        persist_job(job_id)


@app.get("/api/<job_id>/scan/status")
def scan_status(job_id):
    job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404)
        return jsonify(status=state.get("scan_status", "ready"),
                       progress=state.get("scan_progress", 0),
                       total=state.get("scan_total", 0),
                       detected=state.get("scan_detected", 0),
                       error=state.get("scan_error"))


@app.get("/api/<job_id>/scan/results")
def scan_results(job_id):
    job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state or state.get("scan_status") != "done":
            abort(404)
        return jsonify(interval=state["scan_interval"], total=state["scan_total"],
                       detected=state.get("scan_detected", 0), frames=state["scan_results"])


@app.get("/api/<job_id>/scan/frame/<filename>")
def scan_frame(job_id, filename):
    folder = job_path(job_id)
    if not re.fullmatch(r"(?:marcado_)?quadro_\d{8}_\d{10}ms\.jpg", filename):
        abort(404)
    marked = filename.startswith("marcado_")
    base = filename.removeprefix("marcado_")
    path = folder / "scan" / ("marked" if marked else "frames") / (f"marcado_{base}" if marked else base)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True)


@app.get("/api/<job_id>/scan/download")
def scan_download(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
    if not state or state.get("scan_status") != "done" or not state.get("scan_zip"):
        abort(404)
    return send_file(folder / "scan" / "quadros_com_texto.zip", as_attachment=True,
                     download_name="quadros_com_texto.zip", mimetype="application/zip")


@app.post("/api/<job_id>/auto")
def auto_masks(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404, description="Sessão de vídeo não encontrada; envie o MP4 novamente.")
        if state.get("scan_status") != "done":
            return jsonify(error="Analise o vídeo primeiro para usar a remoção automática."), 400
        if state.get("status") == "processing":
            return jsonify(error="Aguarde o processamento atual terminar."), 409
    try:
        pad = int(request.form.get("padding", "8"))
        pad = min(max(pad, 0), 40)
    except ValueError:
        return jsonify(error="Margem inválida."), 400

    info = state["info"]
    width, height = info["width"], info["height"]
    occurrences: dict[str, list[dict]] = {}
    for result in state.get("scan_results", []):
        for line in result.get("lines", []):
            if "Texto recorrente" in line.get("classification", ""):
                normalized = normalize_text(line["text"])
                if normalized:
                    occurrences.setdefault(normalized, []).append({
                        "frame": int(result.get("frame", 0)),
                        "text": line["text"],
                        "box": line["box"],
                        "confidence": line.get("confidence", 0),
                    })
    if not occurrences:
        return jsonify(error="Nenhum texto recorrente foi detectado. Tente reduzir o intervalo de amostragem ou pintar a máscara manualmente."), 400

    created = 0
    for normalized, found in occurrences.items():
        if not found:
            continue
        best = max(found, key=lambda item: item["confidence"])
        frame_index = best["frame"]
        x, y, bw, bh = best["box"]
        x = max(0, x - pad)
        y = max(0, y - pad)
        bw = min(width - x, bw + pad * 2)
        bh = min(height - y, bh + pad * 2)
        if bw <= 0 or bh <= 0:
            continue
        mask = np.zeros((height, width), dtype=np.uint8)
        mask[y:y + bh, x:x + bw] = 255
        mask_path = folder / "masks" / f"mask_{frame_index:09d}.png"
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".png", mask)
        if not ok:
            continue
        mask_path.write_bytes(encoded.tobytes())
        entry = {
            "frame": frame_index,
            "time": round(frame_index / info["fps"], 4),
            "file": mask_path.name,
            "pixels": int(cv2.countNonZero(mask)),
            "target_text": best["text"][:160],
            "bbox": [x, y, bw, bh],
        }
        entries = [item for item in read_mask_manifest(folder)
                   if int(item.get("frame", -1)) != frame_index]
        entries.append(entry)
        entries.sort(key=lambda item: item["frame"])
        write_mask_manifest(folder, entries)
        created += 1
    if not created:
        return jsonify(error="Não foi possível criar nenhuma máscara automática."), 400
    return jsonify(created=created)


@app.post("/api/<job_id>/process")
def process(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404, description="Sessão de vídeo não encontrada; envie o MP4 novamente.")
        if state["status"] == "processing":
            return jsonify(error="Este vídeo já está sendo processado."), 409
    mask_mode = request.form.get("mask_mode", "fixed")
    method = request.form.get("method", "telea")
    try:
        radius = float(request.form.get("radius", "8"))
        mask_expand = int(request.form.get("mask_expand", "4"))
        info = state["info"]
        start = float(request.form.get("start", "0") or 0)
        end = float(request.form.get("end") or info["duration"])
        if mask_mode not in ("fixed", "keyframes", "similar"):
            return jsonify(error="Modo de aplicação das máscaras inválido."), 400
        if method not in ("telea", "ns", "temporal"):
            return jsonify(error="Método de preenchimento inválido."), 400
        radius = min(max(radius, 1.0), 25.0)
        mask_expand = min(max(mask_expand, 0), 20)
        start = min(max(start, 0.0), info["duration"])
        end = min(max(end, 0.0), info["duration"])
        if end - start < 1 / max(info["fps"], 1):
            return jsonify(error="O trecho selecionado é curto demais."), 400
        fixed_mask = None
        keyframes = []
        if mask_mode == "fixed":
            mask_file = request.files.get("mask")
            fixed_mask = cv2.imdecode(np.frombuffer(mask_file.read(), np.uint8), cv2.IMREAD_GRAYSCALE) if mask_file else None
            if fixed_mask is None or fixed_mask.shape != (info["height"], info["width"]):
                return jsonify(error="A máscara está vazia ou tem dimensões diferentes do vídeo."), 400
            fixed_mask = cv2.threshold(fixed_mask, 1, 255, cv2.THRESH_BINARY)[1]
            if cv2.countNonZero(fixed_mask) == 0:
                return jsonify(error="Pinte uma área ou salve pelo menos uma máscara por quadro."), 400
        else:
            for entry in read_mask_manifest(folder):
                mask_path = mask_file(folder, entry.get("file", ""))
                saved_mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE) if mask_path else None
                if saved_mask is not None and saved_mask.shape == (info["height"], info["width"]):
                    saved_mask = cv2.threshold(saved_mask, 1, 255, cv2.THRESH_BINARY)[1]
                    if cv2.countNonZero(saved_mask):
                        keyframes.append({**entry, "mask": saved_mask})
            if not keyframes:
                return jsonify(error="Salve ao menos uma máscara em um quadro antes de processar."), 400
    except Exception as exc:
        return jsonify(error=f"Máscara inválida: {exc}"), 400

    mask_count = 1 if mask_mode == "fixed" else len(keyframes)
    set_job(job_id, status="processing", progress=0, error=None,
            process_mode=mask_mode, process_masks=mask_count)
    persist_job(job_id)
    thread = threading.Thread(
        target=run_inpaint,
        args=(job_id, folder, fixed_mask, keyframes, mask_mode, method, radius, mask_expand,
              state.get("scan_results", []), state.get("scan_interval") or 0.5, start, end), daemon=True)
    thread.start()
    return jsonify(ok=True, mode=mask_mode, masks=mask_count)


def normalize_text(text: str) -> str:
    return " ".join(re.sub(r"\W+", " ", str(text).lower(), flags=re.UNICODE).split())


def transform_mask(mask: np.ndarray, source_box, target_box,
                   width: int, height: int) -> np.ndarray:
    if not source_box or not target_box or len(source_box) != 4 or len(target_box) != 4:
        return mask
    sx, sy, sw, sh = map(float, source_box)
    tx, ty, tw, th = map(float, target_box)
    if min(sw, sh, tw, th) <= 0:
        return mask
    if max(abs(sx - tx), abs(sy - ty), abs(sw - tw), abs(sh - th)) <= 2:
        return mask
    scale_x, scale_y = tw / sw, th / sh
    matrix = np.array([[scale_x, 0, tx - sx * scale_x],
                       [0, scale_y, ty - sy * scale_y]], dtype=np.float32)
    return cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def build_mask_provider(mode: str, fixed_mask: np.ndarray | None, keyframes: list[dict],
                        scan_results: list[dict], scan_interval: float,
                        fps: float, width: int, height: int):
    if mode == "fixed":
        return lambda frame_index: fixed_mask

    ordered = sorted(keyframes, key=lambda item: int(item["frame"]))
    key_positions = [int(item["frame"]) for item in ordered]

    def nearest_keyframe(frame_index: int) -> np.ndarray:
        position = bisect.bisect_left(key_positions, frame_index)
        candidates = [i for i in (position - 1, position) if 0 <= i < len(ordered)]
        chosen = min(candidates, key=lambda i: abs(key_positions[i] - frame_index))
        return ordered[chosen]["mask"]

    if mode != "similar":
        return nearest_keyframe

    templates: dict[str, list[dict]] = {}
    for keyframe in ordered:
        key = normalize_text(keyframe.get("target_text", ""))
        if key and keyframe.get("bbox"):
            templates.setdefault(key, []).append(keyframe)
    if not templates:
        return nearest_keyframe

    occurrences: dict[str, dict[int, list[list[int]]]] = {}
    for sample in scan_results or []:
        sample_frame = int(sample.get("frame", -1))
        if sample_frame < 0:
            continue
        for line in sample.get("lines", []):
            key = normalize_text(line.get("text", ""))
            box = line.get("box")
            if key in templates and isinstance(box, list) and len(box) == 4:
                occurrences.setdefault(key, {}).setdefault(sample_frame, []).append(box)
    occurrence_frames = {key: sorted(frames) for key, frames in occurrences.items()}
    active_radius = max(1, int(round(max(scan_interval, 0.1) * fps / 2)))

    def similar_mask(frame_index: int) -> np.ndarray:
        output = np.zeros((height, width), dtype=np.uint8)
        for key, frames in occurrence_frames.items():
            position = bisect.bisect_left(frames, frame_index)
            candidates = [i for i in (position - 1, position) if 0 <= i < len(frames)]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda i: abs(frames[i] - frame_index))
            sample_frame = frames[nearest]
            if abs(sample_frame - frame_index) > active_radius:
                continue
            source = min(templates[key], key=lambda item: abs(int(item["frame"]) - sample_frame))
            for target_box in occurrences[key][sample_frame]:
                warped = transform_mask(source["mask"], source.get("bbox"), target_box, width, height)
                cv2.bitwise_or(output, warped, dst=output)
        return output

    return similar_mask


def _flow_gray(frame: np.ndarray, mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(mask, size, interpolation=cv2.INTER_AREA)
    small_mask = np.where(small_mask > 0, 255, 0).astype(np.uint8)
    clean = cv2.inpaint(small, small_mask, 3, cv2.INPAINT_TELEA)
    return cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY)


def _flow_full_resolution(flow: np.ndarray, width: int, height: int) -> np.ndarray:
    small_h, small_w = flow.shape[:2]
    full = cv2.resize(flow, (width, height), interpolation=cv2.INTER_LINEAR)
    full[:, :, 0] *= width / small_w
    full[:, :, 1] *= height / small_h
    return full


def _temporal_fill(frame: np.ndarray, base: np.ndarray, mask: np.ndarray,
                   neighbors: list[tuple[np.ndarray | None, np.ndarray | None]]) -> np.ndarray:
    """Use only motion-consistent pixels outside the painted region; keep Telea elsewhere."""
    height, width = mask.shape
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return base
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    target = mask[y0:y1, x0:x1] > 0
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    best_error = np.full(target.shape, np.inf, dtype=np.float32)
    best_pixels = np.zeros((y1-y0, x1-x0, 3), dtype=np.uint8)
    flow_scale = min(1.0, 960.0 / max(width, height))
    flow_size = (max(16, int(width * flow_scale)), max(16, int(height * flow_scale)))
    current_gray = _flow_gray(frame, mask, flow_size)

    for neighbor, neighbor_mask in neighbors:
        if neighbor is None or neighbor.shape != frame.shape or neighbor_mask is None:
            continue
        try:
            neighbor_gray = _flow_gray(neighbor, neighbor_mask, flow_size)
            f_small = cv2.calcOpticalFlowFarneback(
                current_gray, neighbor_gray, None, 0.5, 3, 21, 3, 7, 1.5, 0)
            b_small = cv2.calcOpticalFlowFarneback(
                neighbor_gray, current_gray, None, 0.5, 3, 21, 3, 7, 1.5, 0)
            forward = _flow_full_resolution(f_small, width, height)
            backward = _flow_full_resolution(b_small, width, height)
            f = forward[y0:y1, x0:x1]
            map_x = xx + f[:, :, 0]
            map_y = yy + f[:, :, 1]
            in_bounds = ((map_x >= 0) & (map_x < width - 1) &
                         (map_y >= 0) & (map_y < height - 1))
            reverse_at_source = cv2.remap(
                backward, map_x, map_y, cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(9999, 9999))
            cycle_error = np.sqrt(np.sum((f + reverse_at_source) ** 2, axis=2))
            source_mask = cv2.remap(
                neighbor_mask, map_x, map_y, cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT, borderValue=255)
            warped = cv2.remap(
                neighbor, map_x, map_y, cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
            valid = target & in_bounds & (source_mask == 0) & (cycle_error <= 1.5)
            better = valid & (cycle_error < best_error)
            best_error[better] = cycle_error[better]
            best_pixels[better] = warped[better]
        except cv2.error:
            continue

    usable = np.isfinite(best_error)
    result = base.copy()
    region = result[y0:y1, x0:x1]
    region[usable] = best_pixels[usable]
    return result


def run_inpaint(job_id: str, folder: Path, fixed_mask: np.ndarray | None,
                keyframes: list[dict], mask_mode: str, method: str, radius: float,
                mask_expand: int,
                scan_results: list[dict], scan_interval: float,
                start: float = 0.0, end: float | None = None) -> None:
    source = folder / "original.mp4"
    silent = folder / "cleaned_lossless.mkv"
    output = folder / "cleaned.mp4"
    cap = cv2.VideoCapture(str(source))
    writer = None
    released = False
    try:
        if not cap.isOpened():
            raise RuntimeError("Não foi possível reabrir o vídeo original para processá-lo.")
        with lock:
            info = (jobs.get(job_id) or {}).get("info")
        if not info:
            raise RuntimeError("A sessão do vídeo foi encerrada durante o processamento.")
        fps = info["fps"]
        width, height = info["width"], info["height"]
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        mask_provider = build_mask_provider(
            mask_mode, fixed_mask, keyframes, scan_results, scan_interval,
            fps, width, height)
        mask_at = lru_cache(maxsize=6)(
            lambda frame_index: expand_binary_mask(mask_provider(frame_index), mask_expand))
        codec = cv2.VideoWriter_fourcc(*"FFV1")
        writer = cv2.VideoWriter(str(silent), codec, fps, (width, height))
        if not writer.isOpened():
            silent = folder / "cleaned_fallback.mp4"
            codec = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(silent), codec, fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError("Não foi possível inicializar a gravação do vídeo.")
        algo = cv2.INPAINT_NS if method == "ns" else cv2.INPAINT_TELEA
        processed = 0
        ok, frame = cap.read()
        next_ok, next_frame = cap.read()
        next_frame = next_frame if next_ok else None
        previous = None
        while ok:
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            if previous is not None and (previous.shape[1] != width or previous.shape[0] != height):
                previous = cv2.resize(previous, (width, height), interpolation=cv2.INTER_AREA)
            if next_frame is not None and (next_frame.shape[1] != width or next_frame.shape[0] != height):
                next_frame = cv2.resize(next_frame, (width, height), interpolation=cv2.INTER_AREA)
            current_mask = mask_at(processed)
            if current_mask is None or current_mask.shape != (height, width):
                current_mask = np.zeros((height, width), dtype=np.uint8)
            timestamp = processed / fps
            in_range = start <= timestamp < (end if end is not None else info["duration"] + 1)
            if in_range and cv2.countNonZero(current_mask):
                fixed = cv2.inpaint(frame, current_mask, radius, algo)
                if method == "temporal":
                    previous_mask = mask_at(processed - 1) if previous is not None else None
                    next_mask = mask_at(processed + 1) if next_frame is not None else None
                    fixed = _temporal_fill(frame, fixed, current_mask,
                                           [(previous, previous_mask), (next_frame, next_mask)])
            else:
                fixed = frame
            writer.write(fixed)
            processed += 1
            if processed % 8 == 0:
                progress = int(96 * processed / max(count, 1))
                set_job(job_id, progress=min(progress, 96), detail=f"Quadro {processed} de {max(count, processed)}")
            previous = frame
            frame = next_frame
            ok = frame is not None
            if ok:
                next_ok, next_frame = cap.read()
                next_frame = next_frame if next_ok else None
        cap.release()
        released = True
        writer.release()
        writer = None
        if processed == 0:
            raise RuntimeError("Nenhum quadro foi processado.")
        set_job(job_id, progress=97, detail="Compactando MP4 e preservando o áudio…")
        audio = run_checked([
            media_binary("ffprobe"), "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
        ])
        has_audio = audio.returncode == 0 and bool(audio.stdout.strip())
        cmd = [media_binary("ffmpeg"), "-y", "-v", "error", "-i", str(silent), "-i", str(source),
               "-map", "0:v:0", "-c:v", "libx264", "-preset", "fast", "-crf", "16",
               "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
        if has_audio:
            cmd += ["-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(output)]
        encoded = run_checked(cmd)
        if encoded.returncode != 0 or not output.is_file():
            raise RuntimeError(command_error(encoded))
        set_job(job_id, status="done", progress=100, output=str(output), detail="Vídeo pronto para download.")
        persist_job(job_id)
    except Exception as exc:
        if not released:
            cap.release()
        if writer is not None:
            writer.release()
        output.unlink(missing_ok=True)
        set_job(job_id, status="error", error=f"Falha no processamento: {exc}", detail=None)
        persist_job(job_id)
    finally:
        (folder / "cleaned_lossless.mkv").unlink(missing_ok=True)
        (folder / "cleaned_fallback.mp4").unlink(missing_ok=True)


@app.get("/api/<job_id>/status")
def status(job_id):
    job_path(job_id)
    with lock:
        state = jobs.get(job_id)
        if not state:
            abort(404)
        return jsonify(status=state["status"], progress=state["progress"], error=state["error"],
                       detail=state.get("detail"))


@app.get("/api/<job_id>/download")
def download(job_id):
    folder = job_path(job_id)
    with lock:
        state = jobs.get(job_id)
    if not state or state["status"] != "done":
        abort(404)
    return send_file(folder / "cleaned.mp4", as_attachment=True, download_name="video_editado.mp4",
                     mimetype="video/mp4")


if __name__ == "__main__":
    threading.Thread(target=cleanup_expired_jobs, daemon=True).start()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7860"))
    if getattr(sys, "frozen", False) and host in ("127.0.0.1", "localhost"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host=host, port=port, debug=False, threaded=True)
