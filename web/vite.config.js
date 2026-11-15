import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 代理到后端，**不开放 CORS**：
// 浏览器同源策略在这里是免费的防线，为了省一个 proxy 配置去开 `*` 不值得。
// 生产环境由后端直接托管 dist（`python -m kitchen.api.server --static web/dist`），
// 天然同源，连代理都不需要。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8848',
        changeOrigin: false,
        // SSE 必须关掉代理的缓冲，否则事件会被攒成一坨再发
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // ECharts 单独一个 chunk：它占 700KB+，和业务代码混在一起会让
        // 首屏必须等它下载完才能渲染壳
        manualChunks: { echarts: ['echarts'] },
      },
    },
  },
})
