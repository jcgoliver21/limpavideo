import { dilateMask, inpaintRGBA } from "./inpaint.js";

const $ = (id) => document.getElementById(id);
const clamp = (n, a, b) => Math.min(b, Math.max(a, n));
const frameCanvas = $("frame");
const frameCtx = frameCanvas.getContext("2d", { willReadFrequently: true });
const paint = $("paint");
const paintCtx = paint.getContext("2d");
const video = document.createElement("video");
video.muted = true;
video.playsInline = true;
video.preload = "auto";

let file = null;
let objectUrl = null;
let width = 0;
let height = 0;
let duration = 0;
let currentTime = 0;
let drawMode = "brush";
let drawing = false;
let stroke = null;
let rectDraft = null;
let actions = [];
let redo = [];
let seekToken = 0;
let rangeIn = 0;
let rangeOut = null;
let exporting = false;
let conversion = null;
let ocrWorker = null;

$("boot").textContent = "Pronto. O vídeo fica só neste navegador.";

function formatTime(seconds) {
  const ms = Math.max(0, Math.round(Number(seconds || 0) * 1000));
  const m = Math.floor(ms / 60000);
  const s = Math.floor(ms % 60000 / 1000);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms % 1000).padStart(3, "0")}`;
}

function setTool(mode) {
  drawMode = mode;
  for (const id of ["brush", "eraser", "rectTool"]) $(id).classList.toggle("active", id === (mode === "rect" ? "rectTool" : mode));
}

function updateHistory() {
  $("undo").disabled = !actions.length;
  $("redo").disabled = !redo.length;
}

function drawAction(action) {
  paintCtx.save();
  if (action.type === "rect") {
    paintCtx.fillStyle = "rgba(255,70,95,.55)";
    paintCtx.fillRect(action.x, action.y, action.w, action.h);
  } else {
    paintCtx.lineCap = "round";
    paintCtx.lineJoin = "round";
    paintCtx.lineWidth = action.width;
    paintCtx.globalCompositeOperation = action.mode === "eraser" ? "destination-out" : "source-over";
    paintCtx.strokeStyle = action.mode === "eraser" ? "rgba(0,0,0,1)" : "rgba(255,70,95,.55)";
    paintCtx.fillStyle = paintCtx.strokeStyle;
    const [first, ...rest] = action.points;
    if (!rest.length) {
      paintCtx.beginPath();
      paintCtx.arc(first.x, first.y, action.width / 2, 0, Math.PI * 2);
      paintCtx.fill();
    } else {
      paintCtx.beginPath();
      paintCtx.moveTo(first.x, first.y);
      for (const point of rest) paintCtx.lineTo(point.x, point.y);
      paintCtx.stroke();
    }
  }
  paintCtx.restore();
}

function redraw() {
  paintCtx.clearRect(0, 0, paint.width, paint.height);
  for (const action of actions) drawAction(action);
  updateHistory();
}

function addAction(action) {
  actions.push(action);
  redo = [];
  drawAction(action);
  updateHistory();
  $("compare").style.display = "none";
}

function pointFrom(event) {
  const rect = paint.getBoundingClientRect();
  const sx = rect.width ? paint.width / rect.width : 1;
  const sy = rect.height ? paint.height / rect.height : 1;
  return {
    x: clamp((event.clientX - rect.left) * sx, 0, paint.width),
    y: clamp((event.clientY - rect.top) * sy, 0, paint.height),
  };
}

paint.addEventListener("pointerdown", (event) => {
  if (!file || exporting) return;
  drawing = true;
  paint.setPointerCapture(event.pointerId);
  const p = pointFrom(event);
  if (drawMode === "rect") {
    rectDraft = { x: p.x, y: p.y, w: 0, h: 0 };
    return;
  }
  const rect = paint.getBoundingClientRect();
  stroke = {
    type: "stroke",
    mode: drawMode,
    width: Math.max(1, Number($("size").value) * (rect.width ? paint.width / rect.width : 1)),
    points: [p],
  };
  drawAction({ ...stroke, points: [p] });
});

paint.addEventListener("pointermove", (event) => {
  if (!drawing) return;
  const p = pointFrom(event);
  if (rectDraft) {
    rectDraft.w = p.x - rectDraft.x;
    rectDraft.h = p.y - rectDraft.y;
    redraw();
    const x = Math.min(rectDraft.x, p.x);
    const y = Math.min(rectDraft.y, p.y);
    paintCtx.save();
    paintCtx.fillStyle = "rgba(255,70,95,.35)";
    paintCtx.strokeStyle = "rgba(255,210,120,.95)";
    paintCtx.lineWidth = 2;
    paintCtx.fillRect(x, y, Math.abs(rectDraft.w), Math.abs(rectDraft.h));
    paintCtx.strokeRect(x, y, Math.abs(rectDraft.w), Math.abs(rectDraft.h));
    paintCtx.restore();
    return;
  }
  if (!stroke) return;
  const previous = stroke.points[stroke.points.length - 1];
  stroke.points.push(p);
  paintCtx.save();
  paintCtx.lineCap = "round";
  paintCtx.lineWidth = stroke.width;
  paintCtx.globalCompositeOperation = stroke.mode === "eraser" ? "destination-out" : "source-over";
  paintCtx.strokeStyle = stroke.mode === "eraser" ? "#000" : "rgba(255,70,95,.55)";
  paintCtx.beginPath();
  paintCtx.moveTo(previous.x, previous.y);
  paintCtx.lineTo(p.x, p.y);
  paintCtx.stroke();
  paintCtx.restore();
});

function stopDraw(event) {
  if (!drawing) return;
  drawing = false;
  if (rectDraft) {
    const x = Math.min(rectDraft.x, rectDraft.x + rectDraft.w);
    const y = Math.min(rectDraft.y, rectDraft.y + rectDraft.h);
    const w = Math.abs(rectDraft.w);
    const h = Math.abs(rectDraft.h);
    rectDraft = null;
    if (w > 2 && h > 2) addAction({ type: "rect", x, y, w, h });
    else redraw();
  } else if (stroke) {
    actions.push(stroke);
    stroke = null;
    redo = [];
    updateHistory();
    $("compare").style.display = "none";
  }
  if (event && paint.hasPointerCapture(event.pointerId)) paint.releasePointerCapture(event.pointerId);
}

paint.addEventListener("pointerup", stopDraw);
paint.addEventListener("pointercancel", stopDraw);
$("brush").onclick = () => setTool("brush");
$("eraser").onclick = () => setTool("eraser");
$("rectTool").onclick = () => setTool("rect");
$("undo").onclick = () => { if (actions.length) { redo.push(actions.pop()); redraw(); } };
$("redo").onclick = () => { if (redo.length) { actions.push(redo.pop()); redraw(); } };
$("clearMask").onclick = () => { actions = []; redo = []; redraw(); $("compare").style.display = "none"; $("ocrList").replaceChildren(); };
$("expand").oninput = () => { $("expandValue").textContent = `${$("expand").value} px`; $("compare").style.display = "none"; };

function rasterMask() {
  const pixels = paintCtx.getImageData(0, 0, paint.width, paint.height).data;
  const mask = new Uint8Array(paint.width * paint.height);
  let count = 0;
  for (let i = 0; i < mask.length; i++) {
    if (pixels[i * 4 + 3] > 8) {
      mask[i] = 1;
      count++;
    }
  }
  return { mask, count };
}

function scaleMask(mask, sw, sh, dw, dh) {
  if (sw === dw && sh === dh) return mask;
  const source = document.createElement("canvas");
  source.width = sw;
  source.height = sh;
  const image = source.getContext("2d").createImageData(sw, sh);
  for (let i = 0; i < mask.length; i++) image.data[i * 4 + 3] = mask[i] ? 255 : 0;
  source.getContext("2d").putImageData(image, 0, 0);
  const dest = document.createElement("canvas");
  dest.width = dw;
  dest.height = dh;
  const ctx = dest.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(source, 0, 0, dw, dh);
  const outPixels = ctx.getImageData(0, 0, dw, dh).data;
  const out = new Uint8Array(dw * dh);
  for (let i = 0; i < out.length; i++) out[i] = outPixels[i * 4 + 3] > 8 ? 1 : 0;
  return out;
}

async function seekTo(seconds) {
  const token = ++seekToken;
  const target = clamp(seconds, 0, Math.max(0, duration - 0.001));
  if (Math.abs(video.currentTime - target) > 0.001) {
    await new Promise((resolve) => {
      const done = () => { video.removeEventListener("seeked", done); resolve(); };
      video.addEventListener("seeked", done);
      video.currentTime = target;
    });
  }
  if (token !== seekToken) return;
  currentTime = video.currentTime;
  frameCtx.drawImage(video, 0, 0, width, height);
  $("seek").value = String(currentTime);
  $("time").textContent = formatTime(currentTime);
}

function updateRange() {
  $("rangeLabel").textContent = rangeOut == null
    ? "Trecho: vídeo inteiro"
    : `Trecho: ${formatTime(rangeIn)} → ${formatTime(rangeOut)}`;
}

async function openFile(next) {
  if (!next) return;
  if (next.size > 500 * 1024 * 1024) {
    alert("Neste navegador o limite é 500 MB. Para arquivos maiores, use o app local.");
    return;
  }
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  file = next;
  objectUrl = URL.createObjectURL(next);
  video.src = objectUrl;
  $("fileMeta").textContent = `${next.name} · ${(next.size / 1048576).toFixed(1)} MB`;
  await new Promise((resolve, reject) => {
    video.onloadedmetadata = resolve;
    video.onerror = () => reject(new Error("Não foi possível abrir esse vídeo neste navegador."));
  });
  if (!Number.isFinite(video.duration) || !video.videoWidth) {
    throw new Error("Não foi possível ler a duração ou o tamanho desse vídeo.");
  }
  width = video.videoWidth;
  height = video.videoHeight;
  duration = video.duration;
  frameCanvas.width = paint.width = width;
  frameCanvas.height = paint.height = height;
  $("stage").style.aspectRatio = `${width} / ${height}`;
  $("seek").max = String(duration);
  $("seek").step = "0.04";
  actions = [];
  redo = [];
  rangeIn = 0;
  rangeOut = null;
  redraw();
  updateRange();
  $("pills").replaceChildren();
  for (const text of [`${width} × ${height}`, formatTime(duration), `${(next.size / 1048576).toFixed(1)} MB`]) {
    const pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = text;
    $("pills").appendChild(pill);
  }
  $("editor").style.display = "block";
  $("status").style.display = "none";
  $("compare").style.display = "none";
  await seekTo(0);
}

$("file").onchange = async () => {
  try { await openFile($("file").files[0]); }
  catch (error) { alert(error.message); }
};

for (const type of ["dragenter", "dragover"]) {
  document.body.addEventListener(type, (event) => { event.preventDefault(); document.body.classList.add("dropping"); });
}
document.body.addEventListener("dragleave", (event) => { if (!event.relatedTarget) document.body.classList.remove("dropping"); });
document.body.addEventListener("drop", async (event) => {
  event.preventDefault();
  document.body.classList.remove("dropping");
  try { await openFile(event.dataTransfer?.files?.[0]); }
  catch (error) { alert(error.message); }
});

let seekTimer = null;
$("seek").oninput = () => {
  $("time").textContent = formatTime(Number($("seek").value));
  clearTimeout(seekTimer);
  seekTimer = setTimeout(() => seekTo(Number($("seek").value)), 80);
};
$("prev").onclick = () => seekTo(currentTime - 0.1);
$("next").onclick = () => seekTo(currentTime + 0.1);
$("markIn").onclick = () => { rangeIn = currentTime; if (rangeOut != null && rangeOut <= rangeIn) rangeOut = Math.min(duration, rangeIn + 0.2); updateRange(); };
$("markOut").onclick = () => { rangeOut = Math.max(currentTime, rangeIn + 0.1); updateRange(); };
$("clearRange").onclick = () => { rangeIn = 0; rangeOut = null; updateRange(); };

function paintedFrame() {
  const { mask, count } = rasterMask();
  if (!count) throw new Error("Pinte ou selecione a área que deve ser removida.");
  const snapshot = frameCtx.getImageData(0, 0, width, height);
  const copy = new Uint8ClampedArray(snapshot.data);
  const expanded = dilateMask(mask, width, height, Number($("expand").value));
  inpaintRGBA(copy, width, height, expanded, 0);
  const image = new ImageData(copy, width, height);
  return image;
}

function showCompare(image) {
  const before = $("before");
  const after = $("after");
  before.width = after.width = width;
  before.height = after.height = height;
  before.getContext("2d").drawImage(frameCanvas, 0, 0);
  after.getContext("2d").putImageData(image, 0, 0);
  $("compare").style.display = "grid";
}

$("preview").onclick = () => {
  try {
    showCompare(paintedFrame());
    $("hint").textContent = "Prévia pronta. A exportação usa o mesmo preenchimento.";
    $("hint").className = "success";
  } catch (error) {
    $("hint").textContent = error.message;
    $("hint").className = "error";
  }
};

$("saveFrame").onclick = () => {
  try {
    const image = paintedFrame();
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").putImageData(image, 0, 0);
    const link = document.createElement("a");
    link.href = canvas.toDataURL("image/jpeg", 0.94);
    link.download = "quadro_editado.jpg";
    link.click();
  } catch (error) {
    $("hint").textContent = error.message;
    $("hint").className = "error";
  }
};

async function readText() {
  const button = $("ocr");
  button.disabled = true;
  $("hint").textContent = "Baixando o leitor de texto…";
  $("hint").className = "help";
  try {
    if (!ocrWorker) {
      const { createWorker } = await import("https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.esm.min.js");
      ocrWorker = await createWorker("por+eng", 1, {
        workerPath: "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/worker.min.js",
        corePath: "https://cdn.jsdelivr.net/npm/tesseract.js-core@5.1.1/tesseract-core-simd-lstm.wasm.js",
        langPath: "https://tessdata.projectnaptha.com/4.0.0",
        workerBlobURL: true,
      });
    }
    const result = await ocrWorker.recognize(frameCanvas);
    const lines = (result.data.lines || []).filter((line) => line.text.trim() && line.confidence >= 35);
    const list = $("ocrList");
    list.replaceChildren();
    if (!lines.length) {
      $("hint").textContent = "Nenhum texto legível neste quadro. Pinte a área manualmente.";
      return;
    }
    $("hint").textContent = `${lines.length} texto(s) neste quadro.`;
    $("hint").className = "success";
    for (const line of lines) {
      const row = document.createElement("div");
      const label = document.createElement("span");
      label.className = "help";
      label.textContent = `“${line.text.trim()}” · ${Math.round(line.confidence)}%`;
      const use = document.createElement("button");
      use.className = "button ghost";
      use.type = "button";
      use.textContent = "Criar máscara";
      use.onclick = () => {
        const pad = 6;
        const box = line.bbox;
        addAction({
          type: "rect",
          x: clamp(box.x0 - pad, 0, width),
          y: clamp(box.y0 - pad, 0, height),
          w: clamp(box.x1 - box.x0 + pad * 2, 1, width),
          h: clamp(box.y1 - box.y0 + pad * 2, 1, height),
        });
      };
      row.append(label, use);
      list.appendChild(row);
    }
  } catch (error) {
    $("hint").textContent = `OCR indisponível (${error.message}). Pinte a área manualmente.`;
    $("hint").className = "error";
  } finally {
    button.disabled = false;
  }
}

$("ocr").onclick = readText;

function setStatus(text, progress, kind = "") {
  $("status").style.display = "block";
  $("statusText").textContent = text;
  $("statusText").className = kind;
  $("bar").style.width = `${Math.round(progress * 100)}%`;
}

async function exportVideo() {
  if (exporting) return;
  const { mask, count } = rasterMask();
  if (!count) {
    setStatus("Pinte a área antes de exportar.", 0, "error");
    return;
  }
  if (count > width * height * 0.35) {
    const ok = confirm("A área pintada é grande. O resultado pode ficar borrado e a exportação lenta. Continuar?");
    if (!ok) return;
  }
  if (!window.VideoEncoder) {
    setStatus("Este navegador não exporta MP4. Use Chrome ou Edge, ou baixe o quadro corrigido.", 0, "error");
    return;
  }
  let handle = null;
  if (window.showSaveFilePicker) {
    try {
      handle = await showSaveFilePicker({
        suggestedName: "video_editado.mp4",
        types: [{ description: "MP4", accept: { "video/mp4": [".mp4"] } }],
      });
    } catch (error) {
      if (error.name === "AbortError") return;
      if (error.name !== "NotAllowedError" && error.name !== "SecurityError") throw error;
    }
  }
  const mb = await import("https://cdn.jsdelivr.net/npm/mediabunny@1.61.0/+esm");
  let writable = null;
  const target = handle
    ? new mb.StreamTarget(writable = await handle.createWritable(), { chunked: true })
    : new mb.BufferTarget();

  exporting = true;
  $("exportBtn").disabled = true;
  $("cancelBtn").style.display = "inline-flex";
  $("download").replaceChildren();
  setStatus("Preparando a exportação…", 0.02);
  const input = new mb.Input({ formats: mb.ALL_FORMATS, source: new mb.BlobSource(file) });
  const output = new mb.Output({ format: new mb.Mp4OutputFormat(), target });
  const work = typeof OffscreenCanvas === "function"
    ? new OffscreenCanvas(width, height)
    : Object.assign(document.createElement("canvas"), { width, height });
  const ctx = work.getContext("2d", { willReadFrequently: true });
  const prepared = dilateMask(mask, width, height, Number($("expand").value));
  let cachedMask = null;
  const options = {
    input,
    output,
    tracks: "primary",
    video: {
      quality: new mb.Quality("high"),
      process(sample) {
        const dw = sample.displayWidth;
        const dh = sample.displayHeight;
        if (work.width !== dw || work.height !== dh) {
          work.width = dw;
          work.height = dh;
        }
        sample.draw(ctx, 0, 0);
        if (!cachedMask || cachedMask.w !== dw || cachedMask.h !== dh) {
          cachedMask = { w: dw, h: dh, mask: scaleMask(prepared, width, height, dw, dh) };
        }
        const image = ctx.getImageData(0, 0, dw, dh);
        inpaintRGBA(image.data, dw, dh, cachedMask.mask, 0);
        ctx.putImageData(image, 0, 0);
        return work;
      },
    },
  };
  if (rangeOut != null || rangeIn > 0.05) options.trim = { start: rangeIn, end: rangeOut ?? duration };
  conversion = await mb.Conversion.init(options);
  if (!conversion.isValid) {
    await writable?.abort?.();
    const why = conversion.discardedTracks.map((item) => item.reason).join(", ");
    throw new Error(`Não foi possível exportar neste navegador (${why || "codec incompatível"}).`);
  }
  conversion.onProgress = (progress) => setStatus(`Exportando… ${Math.round(progress * 100)}%`, progress);
  try {
    await conversion.execute();
  } catch (error) {
    await writable?.abort?.();
    throw error;
  }
  setStatus(writable ? "MP4 salvo." : "MP4 pronto.", 1, "success");
  if (!writable && output.target.buffer) {
    const blob = new Blob([output.target.buffer], { type: "video/mp4" });
    const link = document.createElement("a");
    link.className = "button";
    link.href = URL.createObjectURL(blob);
    link.download = "video_editado.mp4";
    link.textContent = "Baixar video_editado.mp4";
    $("download").appendChild(link);
  }
}

$("exportBtn").onclick = async () => {
  try {
    await exportVideo();
  } catch (error) {
    if (error?.name === "ConversionCanceledError") setStatus("Exportação cancelada.", 0);
    else setStatus(error.message || "Falha na exportação.", 0, "error");
  } finally {
    exporting = false;
    conversion = null;
    $("exportBtn").disabled = false;
    $("cancelBtn").style.display = "none";
  }
};

$("cancelBtn").onclick = () => conversion?.cancel();

window.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select") || !file) return;
  const key = event.key.toLowerCase();
  if (key === "arrowleft") { event.preventDefault(); seekTo(currentTime - (event.shiftKey ? 1 : 0.1)); }
  else if (key === "arrowright") { event.preventDefault(); seekTo(currentTime + (event.shiftKey ? 1 : 0.1)); }
  else if (key === "b") setTool("brush");
  else if (key === "e") setTool("eraser");
  else if (key === "r") setTool("rect");
  else if (key === "[" || key === "]") $("size").value = String(clamp(Number($("size").value) + (key === "]" ? 2 : -2), 2, 80));
  else if ((event.ctrlKey || event.metaKey) && key === "z") { event.preventDefault(); (event.shiftKey ? $("redo") : $("undo")).click(); }
});
