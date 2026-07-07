# 复盘：PDF highlight 在非首页画到错页（off-by-one）

## 症状

用户报告：打开带 `highlight` 参数的 PDF 预览链接，"页面不停滚动"，"高亮位置不对"。

实际查证后是**三个独立的 bug**：

### Bug A — off-by-one（`PdfViewer.tsx`）

scrollTop 在 6 秒采样窗口内完全稳定（不是无限滚动）；高亮黄色矩形画在相邻的**前**一页 canvas 上，scroll 跳到的高亮页看不到黄色。

两个症状是**同一个 bug 的不同侧面**：scroll 正确跳到目标页（1-based page N），但 highlight 被画到了前一个 canvas（1-based page N-1）。用户看不到 highlight 就误以为页面"停在错的位置"，继续手动滚找 highlight。

### Bug B — LCS 兜底过度宽松（`layoutMatch.ts`）

修复 A 后又发现：第 14 页（1-based）的内容里也有 highlight 候选 block 被命中——段落是 "公司消防、医疗救护相关设备物资发生报废...造成机场应急救援**保障能力降低**的，消防急救保障部应**及时向公司运行**指挥中心报告。"。"应急救援" 和 "指挥中心" 中间隔了 25+ 个字符。

LCS（Longest Common Subsequence）按序数得 8 个字符全部匹配，old ratio = `LCS / min(content, highlight)` = `8 / 8` = `1.0`，超过 0.85 阈值，被当成真命中。实际是 **highlight 字符散落 content 的"长巧合"**——includes 检不到（不连续）、LCS 又太宽容。

### Bug C — fillRect 用 PDF 点单位但 canvas 是 scale=0.5（`PdfViewer.tsx`）

修复 A+B 后用户测试新 highlight `"日常管理"`（PDF 第 4 页顶部一段文字含这 4 字），发现黄色仍画错位置。匹配分析：layout 上 0-based page=3 顶部 block（y_pdf 163-261）匹配，但 `fillRect(174, 163, 838, 98)` 把矩形画到 canvas y=163-261。

根因：layoutMatch 用 `pageW × bbox_norm[0]` 算 fillRect 的 x/y/w/h，pageW 来自 layout 数据（PDF 点单位，1191×1684）。但 react-pdf 的 canvas 在默认 viewport 下是 scale=0.5（595×842 像素）。`fillRect` 在 canvas 上用的是**像素**坐标——所以 `fillRect(y=163)` 画到 canvas y=163，而该 block 的实际视觉位置是 canvas y=82（=163/2）。

修复前多个 block 时因巧合视觉位置仍落入对应文字范围（fillRect y=283-741 跨越了 3 个 block 的视觉范围 141-370），单 block 时就明显错位——highlight "日常管理" 唯一匹配 block 1，画在了中段，盖在完全无关的 "应急救援指挥中心是..." 段落上。

## 根因

### Bug A 根因 — off-by-one

`PdfViewer.tsx` 的 `normalizedHitsByPage` Map 用 `page.page`（**0-based**，来自 layout 后端契约）作 key，但 `pageCanvasRefs` / `handlePageRenderSuccess` 的 `pageNumber` 是 **1-based**（来自 Virtuoso `index + 1`）。`allHits.set` 和 `allHits.get` 之间错了一格。

```ts
// 写入侧（错）                              // 读出侧（错）
allHits.set(page.page, hits)                 // 0-based key
hits = normalizedHitsByPage.current.get(pageNumber)  // 1-based lookup → 永远查不到 N，只查到 N-1
```

只有 `firstHitPage` 是同步走 `page.page + 1` 转 1-based 再 `scrollToIndex`，所以 scroll 跳到对的页。highlight 绘制完全错位。

## 为什么没被现有 e2e 测试抓到

`frontend/e2e/pdf-viewer.spec.ts` 现有的 highlight 测试只 mock 了 `page: 0` 的 layout，第一个匹配块一定在 1-based page=1 上。`page.page` (0) 跟 `pageNumber` (1) 错开，但都映射到同一个 canvas——off-by-one 在"第一页"是不可见的。

## 修复

### Bug A 修复

`frontend/src/pages/PdfViewer.tsx:152` 把 map key 从 `page.page` 改成 `page.page + 1`（保持 1-based 与 `pageCanvasRefs` 对齐）：

```ts
allHits.set(page.page + 1, hits)
```

`firstHitPage.current` 仍保持 0-based——它只在 scroll effect 内部 `+ 1` 转 1-based，那条路径一直是对的。

`HighlightRect.page`（layoutMatch.ts 内部 type 的字段）保留 0-based，因为它只用作分组 key，不再被外部读到。

### Bug B 修复

`frontend/src/lib/layoutMatch.ts:124` 把 ratio 公式从 `LCS / min(content, highlight)` 改成 `LCS / max(content, highlight)`：

```ts
// before
return lcsLen(shorter, longer) / shorter.length   // 短串全字符命中 → 1.0
// after
return lcsLen(na, nb) / Math.max(na.length, nb.length)  // 长串含多少短串字符 → 0.12
```

`min` 把分母放到"被检索的一侧"——highlight 短、content 长时，content 里只要包含 highlight 的全部字符（哪怕散落），LCS 就是 1.0。`max` 把分母放到"较长一侧"——散落命中时 ratio 等于 `LCS / content长度`，会非常低；只有真正密集匹配（如 OCR 单字错场景）才能保持高 ratio。

三种典型场景对比（threshold 0.85）：

| 场景 | LCS | max | 旧 ratio (min) | 新 ratio (max) | 期望 |
|------|-----|-----|-----|-----|------|
| OCR 单字错（"对话"→"对讲"在 14 字符里） | 13 | 14 | 0.93 | 0.93 | ✓ 命中 |
| highlight 散落 content（"应急救援"+隔 25 字+"指挥中心"） | 8 | 67 | **1.0** ❌ | 0.12 ✓ | 不应命中 |
| 完全不同 | 0 | max | 0 | 0 | ✓ 不命中 |

旧 ratio 在 OCR 场景和"散落巧合"场景下都给出高 ratio，无法区分。新 ratio 把"散落巧合"压到阈值之下。

### Bug C 修复

`frontend/src/pages/PdfViewer.tsx` 的两处 fillRect（match effect 的 forEach 与 `handlePageRenderSuccess`）按 `canvas.width / layoutPage.width` 缩放：

```ts
const layoutPage = layout.data?.layout.find(p => p.page + 1 === pageNum)
const scaleX = canvas.width / Math.max(layoutPage.width, 1)
const scaleY = canvas.height / Math.max(layoutPage.height, 1)
for (const h of hits) ctx.fillRect(h.x * scaleX, h.y * scaleY, h.w * scaleX, h.h * scaleY)
```

在 paint 时读 canvas 实际尺寸算 scale，而非 match 时假定——后者会因为 viewport 变化、PDF 缩放等不稳定。

**架构反思**：layoutMatch 的 docstring 本来就说"`pageW`/`pageH`（像素）"，但调用方传的是 PDF 点单位的 layout.width/height。要么改 layoutMatch 的语义（要"canvas 像素"就用 canvas 像素），要么改返回值语义（保留 PDF 点单位，paint 时再换算）。后者更灵活——viewport 变化、PDF scale 调整都不用动 layoutMatch。

### Bug C 回归

`frontend/e2e/pdf-viewer.spec.ts` 新增 `单匹配 block 时黄色必须落在 block 视觉位置（fillRect canvas scale 回归）`：

用真实 doc + `highlight=日常管理`（唯一匹配 0-based page=3 顶部 block，y_pdf=163-261）。在 1-based page=4 canvas 上扫黄色像素 y 范围，断言 `maxY < 150`（block 1 视觉位置 y_canvas=82-131）。修复前 maxY=260 落在中段，测试 RED；修复后 maxY=130 落在顶部，测试 GREEN。

## 回归测试

`frontend/e2e/pdf-viewer.spec.ts` 新增测试 `高亮画到匹配 block 所在的 1-based 页（off-by-one 回归）`：

- 用真实 doc `01KW1QXZ5AKBGK34BDJRV1X4JZ`（用户 bug 现场同一份 PDF），其 layout 在 0-based page=3 有"应急救援指挥中心"3 个匹配块。
- 用 `page.addInitScript` patch `HTMLCanvasElement.prototype.getContext`，把每次 `ctx.fillRect` 的 `fillStyle` 含黄色的调用记到 `window.__paintedPages`，并附带调用所在 canvas 的 `data-page-number`。
- 断言 `paintedPages` 含 `"4"`（1-based）且不含 `"3"`。

不依赖 `%PDF-1.4 dummy` 之类的假 PDF（之前的"合法 doc" 测试就因为这个 dummy 渲染不出来经常 flake）。真实 PDF + 真实 layout 让 fillRect 调用是真的，测试不再受 MutationObserver 时序影响。

负向验证：临时把 `page.page + 1` 改回 `page.page` 后测试报 `["3","3","3"]`（3 个匹配块全部错画到前一页），修复后 `["4","4","4"]`。

### Bug B — unit

`frontend/src/lib/layoutMatch.test.ts` 新增测试 `highlight 字符散落长 content 不应误命中（LCS ratio 用 max 而非 min）`：

用真实 PDF 第 13 段（"公司消防医疗救护...应急救援保障能力降低的...消防急救保障部应及时向公司运行指挥中心报告。"）作为误命中对照——includes 检不到（不连续），但 LCS 能数出 8 个有序匹配字符。旧 ratio `8 / 8 = 1.0` 命中，新 ratio `8 / 67 ≈ 0.12` 不命中。

负向验证：临时把 `Math.max(...)` 改回 `Math.min(...)` 后测试失败（散落命中误判），改回 max 后通过。

## 反馈环脚本

`frontend/scripts/repro-pdf-highlight.mjs`：开 Playwright、采样 6 秒 scrollTop、读 canvas pixel 找黄色矩形实际像素坐标 + 所在 1-based 页。退出码 0=无 bug，1=有 bug。

完整闭环：
```bash
# 拿真实 PDF + 真实 layout 当 fixture，验证修复
node frontend/scripts/repro-pdf-highlight.mjs
```

## 设计反思

为什么会出现 0-based/1-based 混用：

- 后端 `pages_doc` 是 0-based（业界惯例，跟 `array index` 对齐）
- Virtuoso `index` 必然是 0-based（数组下标）
- `react-pdf <Page pageNumber>` 是 1-based（PDF 文档模型用 1-based）
- 前端"显示"层（URL `page=`、跳页输入框）约定俗成用 1-based

正常做法是**单一来源**：要么 layout 端转 1-based 给前端，要么前端全程 0-based。当前代码三个边界各转一次（`firstHitPage + 1`、`pageNumber = index + 1`、URL `parseInt`），加上 `pageCanvasRefs` 用 1-based key，多了一个隐式契约。任何契约违背就立刻报错——但 V46 之前没有非首页的 e2e 覆盖，所以一直没触发。

## 后续

- **架构层面**：考虑让 `pages_doc.page` 后端直接 1-based，或者前端加一个 `page_display` 字段显式区分 0/1-based（避免后人再踩）。本次未做，留作债。
- **测试覆盖**：所有依赖"layout.page 与 pageNumber 同号"的路径都需要跨页 fixture。**Bug C 的教训是 layoutMatch 单测只覆盖了"返回 PDF 点单位坐标"的语义，没覆盖"fillRect 在不同 scale canvas 上的视觉位置"——单测要尽量覆盖"被调用方的真实使用场景"，否则语义偏差不会被发现**。当前 highlight 路径已加 e2e + 单测两层。
- **匹配算法**：LCS 是 subsequence（不要求连续），当 highlight 字符在 content 里"散落分布"时也会算高 ratio。如果未来要支持"忽略顺序"的匹配，可以再叠一层 Jaccard；现在的 ratio 改成 max 已经堵住最常见的"两端短语散落"场景。
- **坐标语义**：layoutMatch 的 `pageW/pageH` 参数含义要从"PDF 点单位"改为"调用方实际需要的坐标单位"。本次 fix 选择保留 PDF 点单位、在 paint 时换算，更灵活但容易再踩。
- **诊断流程**：Bug C 浪费了一轮——一开始试图用 Playwright 像素扫描复现，但 yellow 检测 filter 写错（rgb(255,255,153) 的 B 通道是 153 不是 < 150），始终看到奇怪的"黄色范围"。最后是用户手动截图 + 我保存 canvas PNG 截图肉眼对照，才真正定位到 scale 问题。教训：图像 bug 优先看图，不要只信像素统计。
- **三个 bug 串成一条线**：用户最初报告的"页面不停滚动 + 高亮位置不对"其实是三个独立 bug 的合奏——A 让 highlight 画到错页（→"滚动找高亮"），B 让 highlight 误命中无关段落（→"高亮位置不对"），C 让 highlight 画在错视觉位置（→"高亮位置不对"）。三个症状相似、根因完全不同。修一个就换下一个，要逐步验。