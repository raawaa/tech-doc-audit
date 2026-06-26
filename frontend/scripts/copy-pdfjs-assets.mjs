/**
 * Copy PDF.js runtime assets from node_modules/pdfjs-dist to public/.
 *
 * The pdfjs-dist worker needs these at runtime. They're gitignored and
 * regenerated on every `npm install` / `npm run build`.
 */
import { cpSync, existsSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

const copies = [
  { src: 'node_modules/pdfjs-dist/cmaps', dest: 'public/cmaps' },
  { src: 'node_modules/pdfjs-dist/wasm',  dest: 'public/pdfjs/wasm' },
];

for (const { src, dest } of copies) {
  const srcPath = join(root, src);
  const destPath = join(root, dest);

  if (!existsSync(srcPath)) {
    console.warn(`[pdfjs-assets] ⚠️  source not found, skipping: ${src}`);
    continue;
  }

  mkdirSync(dirname(destPath), { recursive: true });
  cpSync(srcPath, destPath, { recursive: true, force: true });
  console.log(`[pdfjs-assets] ✔  ${src} → ${dest}`);
}
