import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  root: 'src/renderer',
  resolve: {
    alias: {
      '@renderer': new URL('./src/renderer/src', import.meta.url).pathname,
      '@shared': new URL('./src/shared', import.meta.url).pathname
    }
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8777', changeOrigin: true } }
  },
  build: {
    outDir: '../../dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          codemirror: ['@codemirror/view', '@codemirror/state', '@codemirror/lang-sql', '@codemirror/lint', '@codemirror/autocomplete', '@codemirror/commands'],
        },
      },
    },
  }
})
