import { describe, it, expect } from 'vitest'
import { renderToString } from 'react-dom/server'
import { Badge } from './Badge'

/**
 * 用 react-dom/server 把组件序列化为 HTML 字符串做类名断言——无需 @testing-library/react。
 *
 * 重点关注 issue #46：Badge 渲染的 <span> 必须含 `whitespace-nowrap`，
 * 否则在 w-24 (96px) 列里 3 字符中文标签"已向量化"会折行。
 */

describe('Badge', () => {
  it('渲染 embedding_status 对应的中文标签', () => {
    // 回归保护：3 字符的中文标签"已向量化"在窄列里不准换行
    const html = renderToString(<Badge value="embedded" />)
    expect(html).toContain('已向量化')
  })

  it('默认 className 含 whitespace-nowrap（修复 #46）', () => {
    const html = renderToString(<Badge value="embedded" />)
    expect(html).toMatch(/class="[^"]*whitespace-nowrap[^"]*"/)
  })

  it('默认 className 含 shrink-0 防止 flex 父容器挤压', () => {
    const html = renderToString(<Badge value="embedded" />)
    expect(html).toMatch(/class="[^"]*shrink-0[^"]*"/)
  })

  it('保留 embedded 状态对应的色调（per ADR-0003）', () => {
    const html = renderToString(<Badge value="embedded" />)
    // 'embedded' → bg-emerald-50 text-emerald-700（终态色）
    expect(html).toContain('bg-emerald-50')
    expect(html).toContain('text-emerald-700')
  })

  it('调用方的 className 仍能与默认合并', () => {
    const html = renderToString(
      <Badge value="embedded" className="custom-x" />,
    )
    expect(html).toContain('whitespace-nowrap')
    expect(html).toContain('custom-x')
  })

  it('labelMap 缺失时回退到原始 value', () => {
    const html = renderToString(<Badge value="unknown_kind" />)
    expect(html).toContain('unknown_kind')
  })
})
