/**
 * Preenchimento local para máscaras pequenas (texto, marca, logo).
 * Não inventa detalhe fino: puxa a cor da borda para dentro e suaviza.
 * `mask` usa 1 = remover. `data` é RGBA e é alterado no lugar.
 */
export function inpaintRGBA(data, width, height, mask, expand = 0) {
  if (!data || !mask || mask.length !== width * height || data.length !== width * height * 4) {
    throw new Error("Máscara incompatível com o quadro.");
  }
  const hole = expand > 0 ? dilateMask(mask, width, height, expand) : mask;
  let minX = width;
  let minY = height;
  let maxX = -1;
  let maxY = -1;
  let holes = 0;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (!hole[y * width + x]) continue;
      holes++;
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
  }
  if (!holes) return 0;

  const pad = 8;
  minX = Math.max(0, minX - pad);
  minY = Math.max(0, minY - pad);
  maxX = Math.min(width - 1, maxX + pad);
  maxY = Math.min(height - 1, maxY + pad);
  const rw = maxX - minX + 1;
  const rh = maxY - minY + 1;
  const r = new Float32Array(rw * rh);
  const g = new Float32Array(rw * rh);
  const b = new Float32Array(rw * rh);
  const known = new Uint8Array(rw * rh);
  let knownCount = 0;
  let sr = 0;
  let sg = 0;
  let sb = 0;

  for (let y = 0; y < rh; y++) {
    for (let x = 0; x < rw; x++) {
      const sx = minX + x;
      const sy = minY + y;
      const si = (sy * width + sx) * 4;
      const di = y * rw + x;
      r[di] = data[si];
      g[di] = data[si + 1];
      b[di] = data[si + 2];
      if (!hole[sy * width + sx]) {
        known[di] = 1;
        knownCount++;
        sr += data[si];
        sg += data[si + 1];
        sb += data[si + 2];
      }
    }
  }
  if (!knownCount) return holes;

  const meanR = sr / knownCount;
  const meanG = sg / knownCount;
  const meanB = sb / knownCount;
  const pending = [];
  for (let i = 0; i < known.length; i++) {
    if (!known[i]) pending.push(i);
  }
  let guard = Math.max(rw, rh) + 4;
  while (pending.length && guard--) {
    let next = 0;
    for (let p = 0; p < pending.length; p++) {
      const i = pending[p];
      if (known[i]) continue;
      const x = i % rw;
      const y = (i / rw) | 0;
      let ar = 0;
      let ag = 0;
      let ab = 0;
      let n = 0;
      if (x > 0 && known[i - 1]) { ar += r[i - 1]; ag += g[i - 1]; ab += b[i - 1]; n++; }
      if (x + 1 < rw && known[i + 1]) { ar += r[i + 1]; ag += g[i + 1]; ab += b[i + 1]; n++; }
      if (y > 0 && known[i - rw]) { ar += r[i - rw]; ag += g[i - rw]; ab += b[i - rw]; n++; }
      if (y + 1 < rh && known[i + rw]) { ar += r[i + rw]; ag += g[i + rw]; ab += b[i + rw]; n++; }
      if (!n) {
        pending[next++] = i;
        continue;
      }
      r[i] = ar / n;
      g[i] = ag / n;
      b[i] = ab / n;
      known[i] = 2;
    }
    pending.length = next;
    for (let i = 0; i < known.length; i++) if (known[i] === 2) known[i] = 1;
  }
  for (const i of pending) {
    r[i] = meanR;
    g[i] = meanG;
    b[i] = meanB;
  }
  soften(r, g, b, known, rw, rh);
  soften(r, g, b, known, rw, rh);

  for (let y = 0; y < rh; y++) {
    for (let x = 0; x < rw; x++) {
      const sx = minX + x;
      const sy = minY + y;
      if (!hole[sy * width + sx]) continue;
      const di = y * rw + x;
      const si = (sy * width + sx) * 4;
      data[si] = clampByte(r[di]);
      data[si + 1] = clampByte(g[di]);
      data[si + 2] = clampByte(b[di]);
    }
  }
  return holes;
}

export function dilateMask(mask, width, height, amount) {
  const radius = Math.max(0, Math.min(24, amount | 0));
  if (!radius) return mask.slice();
  const out = mask.slice();
  const r2 = radius * radius;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (!mask[y * width + x]) continue;
      const y0 = Math.max(0, y - radius);
      const y1 = Math.min(height - 1, y + radius);
      const x0 = Math.max(0, x - radius);
      const x1 = Math.min(width - 1, x + radius);
      for (let yy = y0; yy <= y1; yy++) {
        const dy = yy - y;
        for (let xx = x0; xx <= x1; xx++) {
          const dx = xx - x;
          if (dx * dx + dy * dy <= r2) out[yy * width + xx] = 1;
        }
      }
    }
  }
  return out;
}

function soften(r, g, b, originalKnown, rw, rh) {
  const nr = r.slice();
  const ng = g.slice();
  const nb = b.slice();
  for (let y = 0; y < rh; y++) {
    for (let x = 0; x < rw; x++) {
      const i = y * rw + x;
      if (originalKnown[i]) continue;
      let ar = 0;
      let ag = 0;
      let ab = 0;
      let n = 0;
      for (let dy = -1; dy <= 1; dy++) {
        const yy = y + dy;
        if (yy < 0 || yy >= rh) continue;
        for (let dx = -1; dx <= 1; dx++) {
          const xx = x + dx;
          if (xx < 0 || xx >= rw) continue;
          const j = yy * rw + xx;
          ar += r[j];
          ag += g[j];
          ab += b[j];
          n++;
        }
      }
      nr[i] = ar / n;
      ng[i] = ag / n;
      nb[i] = ab / n;
    }
  }
  r.set(nr);
  g.set(ng);
  b.set(nb);
}

function clampByte(value) {
  return Math.max(0, Math.min(255, Math.round(value)));
}
