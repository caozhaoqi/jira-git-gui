import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发态：dev server 跑在 5173，/api 与 /ws 反向代理到本地 FastAPI(:8787)。
// 生产态：base 在 `vite build --base /web/` 时设为 /web/，使产物（index.html + /assets）
// 经由后端 api/server.py 的 app.mount("/web", ...) 提供，Electron / Tauri 无需改动。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8787',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
