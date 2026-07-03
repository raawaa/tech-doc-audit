import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E test config.
 *
 * 启动 dev server (vite, port 3000) 后跑测试。
 * 真实环境 /api 反代到 8000 (后端 uvicorn)。
 * CI 环境可设 baseURL / port 由 CI runner 决定。
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
