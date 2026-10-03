import { inpaintRGBA } from "../docs/inpaint.js";

function paint(w, h, fill, hole) {
  const data = new Uint8ClampedArray(w * h * 4);
  const mask = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const [r, g, b] = hole(i) ? fill.hole : fill.bg;
    data[i * 4] = r;
    data[i * 4 + 1] = g;
    data[i * 4 + 2] = b;
    data[i * 4 + 3] = 255;
    mask[i] = hole(i) ? 1 : 0;
  }
  return { data, mask };
}

const w = 40;
const h = 30;
const { data, mask } = paint(w, h, { bg: [20, 80, 200], hole: [255, 255, 255] }, (i) => {
  const x = i % w;
  const y = (i / w) | 0;
  return x >= 12 && x < 28 && y >= 10 && y < 18;
});
const holes = inpaintRGBA(data, w, h, mask, 1);
if (holes < 100) throw new Error("máscara não foi detectada");
let bad = 0;
for (let y = 11; y < 17; y++) {
  for (let x = 14; x < 26; x++) {
    const i = (y * w + x) * 4;
    if (data[i] > 80 || data[i + 2] < 120) bad++;
  }
}
if (bad) throw new Error(`preenchimento ruim em ${bad} pixels`);

const untouched = paint(8, 8, { bg: [1, 2, 3], hole: [9, 9, 9] }, () => false);
const before = untouched.data.slice();
if (inpaintRGBA(untouched.data, 8, 8, untouched.mask, 2) !== 0) throw new Error("máscara vazia alterou o quadro");
if (untouched.data.some((v, i) => v !== before[i])) throw new Error("pixels mudaram sem máscara");

console.log("inpaint ok");
