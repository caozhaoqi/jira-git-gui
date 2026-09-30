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
    rollupOptions: {
      output: {
        // 把体积大且很少变的第三方库拆成独立 chunk：
        //  - 应用代码改动时，用户不必重新下载 react/highlight（长期缓存命中）
        //  - 各 chunk 可并行下载解析
        // 实测拆分前：单个 626KB chunk（应用代码 417 + react 142 + highlight 78）。
        // xterm 已在 K8sShell 里动态 import，会自动单独成包，无需在此声明。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('highlight.js')) return 'v-highlight';
          if (id.includes('@xterm')) return 'v-xterm';
          if (id.includes('react-dom') || id.includes('/react/') || id.includes('scheduler')) {
            return 'v-react';
          }
          if (id.includes('zustand')) return 'v-zustand';
          return 'v-other';
        },
      },
    },
  },
});
