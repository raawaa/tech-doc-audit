# PDF 预览体验改进设计

## 背景

当前 `PdfViewer.tsx` 使用 pdfjs-dist 直接渲染 PDF，一次只显示一页，需要手动点击 ← → 按钮翻页，不支持连续滚动。高亮功能（`highlight` URL 参数）已存在，但只搜索当前页，无法跨页定位到高亮区域。

**核心场景：** 用户在审核报告中点击"查看原文"跳转到 PDF 预览页，期望立即看到被标注的原文段落（高亮 + 自动定位）。

## 目标

1. **连续滚动：** PDF 文档所有页在同一个滚动容器内连续显示，无需翻页按钮
2. **高亮自动定位：** 页面加载后自动搜索高亮关键词，找到后画黄色矩形并平滑滚动到该位置

## 非目标

- 不需要精确到字符级的高亮定位（大致位置够用）
- 不改造 DOCX/MD 降级模式（保留现有逻辑）
- 不引入重量级全功能 PDF 查看器

## 方案

采用 **react-pdf**（wojtekmaj/react-pdf，10k+ stars），它是 pdfjs-dist 的官方推荐 React 封装，底层就是当前项目已在使用的 pdfjs-dist。改动仅限一个文件。

### 依赖变更

- **新增：** `react-pdf`（~50KB gzipped）
- **保留：** `pdfjs-dist`（react-pdf 底层依赖，worker 配置不变）

## 组件结构

```
┌─ Header（sticky）─────────────────────────────┐
│ 文档名 · 页数    │ 页码跳转输入框              │
│                  │ 🔍 高亮关键词提示           │
├───────────────────────────────────────────────┤
│                                               │
│  滚动容器（overflow-auto）                     │
│                                               │
│  ┌─ <Document> ─────────────────────────────┐ │
│  │  <Page pageNumber={1} />                 │ │
│  │  <Page pageNumber={2} />                 │ │
│  │  <Page pageNumber={3} /> ← 自动定位到   │ │
│  │  ...                                     │ │
│  │  <Page pageNumber={N} />                 │ │
│  └───────────────────────────────────────────┘ │
│                                               │
└───────────────────────────────────────────────┘
```

## 功能变更详情

### 移除

- PDF 文档的 ← → 翻页按钮（连续滚动已替代其功能）

### 保留

- Header 中的文档名、页数、高亮关键词提示
- DOCX/MD 文本降级模式完整保留（含翻页按钮）
- 现有高亮渲染方式（canvas 上画黄色半透明矩形）

### 修改

- 页码输入框改为"跳转至"——输入页码后 `scrollIntoView` 滚动到对应页

### 新增

- react-pdf `<Document>` + 循环 `<Page>` 渲染全部页
- 高亮搜索从"当前页"扩展为"找到关键词的页面"
- 找到高亮后 `scrollIntoView({ behavior: 'smooth', block: 'center' })` 自动定位

## 高亮与自动定位流程

```
组件挂载 → PDF 加载 → 全部页面渲染
                ↓
         highlight 参数不为空？
           ↓ Yes
         遍历所有页搜索文本内容（getTextContent）
           ↓
         在哪页找到了？→ 在对应页 canvas 上画黄色高亮矩形
           ↓
         第一页有高亮的 → scrollIntoView({ behavior: 'smooth', block: 'center' })
```

- 复用现有 `searchTerms` 分词、`transform` 坐标计算、`fillStyle` 逻辑
- 边缘情况：关键词为空或未找到 → 正常显示，不滚动；多页匹配 → 滚动到第一页

## 性能

- react-pdf 自带虚拟滚动（`IntersectionObserver`），只渲染可视区及附近的页
- 不在视口内的 canvas 自动释放以节省内存
- 大文档首屏加载期间显示 loading 指示器

## 改动范围

| 文件 | 改动 |
|------|------|
| `frontend/src/pages/PdfViewer.tsx` | 重构（约 150 行 → 约 180 行） |
| `frontend/package.json` | 新增 `react-pdf` 依赖 |

不涉及后端改动。
