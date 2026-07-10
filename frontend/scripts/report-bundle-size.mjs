/**
 * 报告 frontend/dist/assets/*.js 的原始 / gzip 体积,作为 spike 体积门槛的
 * 复核渠道(详见 issue #66 verification risk #5 + .out-of-scope/dropin-viewer
 * 的体积门槛讨论)。
 *
 * 仅在已经 ``npm run build`` 之后运行 — 只读 dist/ 输出,不重新编译。
 *
 * 用法:
 *   cd frontend
 *   npm run build
 *   node scripts/report-bundle-size.mjs
 *
 * 输出:
 *   - 各 js / wasm 文件:原始 bytes / gzip bytes
 *   - 总计:原始 bytes / gzip bytes
 *   - 与上次 baseline 的差值 (如果 scripts/bundle-size-baseline.json 存在)
 *
 * 写出 scripts/bundle-size-current.json 作为下次比较的快照。
 */
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { gzipSync } from 'node:zlib'
import { dirname, join, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const DIST_ASSETS = join(__dirname, '..', 'dist', 'assets')
const BASELINE = join(__dirname, 'bundle-size-baseline.json')
const CURRENT = join(__dirname, 'bundle-size-current.json')

/** 字节格式化(KB / MB),保留 2 位小数 */
function fmt(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

async function gzipSize(buf) {
  return gzipSync(buf, { level: 9 }).length
}

async function main() {
  const files = await readdir(DIST_ASSETS)
  const entries = []
  for (const f of files) {
    if (!f.endsWith('.js') && !f.endsWith('.wasm') && !f.endsWith('.css')) continue
    const buf = await readFile(join(DIST_ASSETS, f))
    const raw = buf.length
    const gz = await gzipSize(buf)
    entries.push({ name: f, raw, gzip: gz })
  }

  entries.sort((a, b) => b.raw - a.raw)

  let totalRaw = 0
  let totalGz = 0
  for (const e of entries) totalRaw += e.raw, totalGz += e.gzip

  console.log('\n=== bundle assets ===')
  for (const e of entries) {
    console.log(`  ${e.name.padEnd(56)}  raw=${fmt(e.raw).padStart(10)}  gzip=${fmt(e.gzip).padStart(10)}`)
  }
  console.log(`  ${'TOTAL'.padEnd(56)}  raw=${fmt(totalRaw).padStart(10)}  gzip=${fmt(totalGz).padStart(10)}`)

  // 与 baseline 比较(若有)
  let baseline = null
  try {
    baseline = JSON.parse(await readFile(BASELINE, 'utf8'))
  } catch { /* 未设 baseline,忽略 */ }
  if (baseline) {
    const dRaw = totalRaw - baseline.totalRaw
    const dGz = totalGz - baseline.totalGzip
    const sign = (n) => (n >= 0 ? '+' : '')
    console.log(`\n=== vs baseline ===`)
    console.log(`  raw  ${sign(dRaw)}${fmt(Math.abs(dRaw))}`)
    console.log(`  gzip ${sign(dGz)}${fmt(Math.abs(dGz))}`)
  }

  await writeFile(CURRENT, JSON.stringify({
    at: new Date().toISOString(),
    totalRaw,
    totalGzip: totalGz,
    files: entries,
  }, null, 2))
  console.log(`\nwrote ${basename(CURRENT)}`)
}

main().catch((e) => { console.error(e); process.exit(1) })
