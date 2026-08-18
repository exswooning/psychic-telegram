/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  // webui.py serves the built app under /app (not /) so it doesn't collide
  // with that file's own inline setup-wizard page at "/". Asset URLs in the
  // built index.html need this prefix or they resolve to webui.py's own
  // routes instead of the SPA's files.
  base: '/app/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  // Vitest reuses everything above -- crucially the '@' alias, so a test
  // imports a module by exactly the same specifier the app does.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})