// Generates the PWA icons (a red cross on white) without any image tooling: `yarn icons`.
import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";

const RED = [185, 28, 28];
const WHITE = [255, 255, 255];

function crc32(buf) {
  let c;
  const table = [];
  for (let n = 0; n < 256; n++) {
    c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  let crc = 0xffffffff;
  for (const b of buf) crc = table[(crc ^ b) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}

function png(size, { maskable }) {
  const radius = maskable ? 0 : size * 0.18;
  const arm = size * (maskable ? 0.13 : 0.16); // half-width of the cross arms
  const reach = size * (maskable ? 0.3 : 0.36); // half-length of the cross
  const c = size / 2;
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = Buffer.alloc(1 + size * 4);
    for (let x = 0; x < size; x++) {
      const px = x + 0.5;
      const py = y + 0.5;
      // rounded-square background (transparent outside for the normal icon)
      let inside = true;
      if (radius) {
        const dx = Math.max(radius - px, 0, px - (size - radius));
        const dy = Math.max(radius - py, 0, py - (size - radius));
        inside = dx * dx + dy * dy <= radius * radius;
      }
      const inCross =
        (Math.abs(px - c) <= arm && Math.abs(py - c) <= reach) || (Math.abs(py - c) <= arm && Math.abs(px - c) <= reach);
      const [r, g, b] = inCross ? RED : WHITE;
      const o = 1 + x * 4;
      row[o] = r;
      row[o + 1] = g;
      row[o + 2] = b;
      row[o + 3] = inside ? 255 : 0;
    }
    rows.push(row);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(Buffer.concat(rows), { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="18" fill="#fff" stroke="#e2e8f0" stroke-width="2"/>
  <path fill="#b91c1c" d="M34 14h32v20h20v32H66v20H34V66H14V34h20z"/>
</svg>
`;

const out = new URL("../public/icons/", import.meta.url);
mkdirSync(out, { recursive: true });
writeFileSync(new URL("icon-192.png", out), png(192, { maskable: false }));
writeFileSync(new URL("icon-512.png", out), png(512, { maskable: false }));
writeFileSync(new URL("icon-maskable-512.png", out), png(512, { maskable: true }));
writeFileSync(new URL("apple-touch-icon.png", out), png(180, { maskable: true }));
writeFileSync(new URL("icon.svg", out), svg);
console.log("icons written to public/icons/");
