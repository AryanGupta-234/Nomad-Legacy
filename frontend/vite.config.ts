import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: '/static/nomad-ui/',
  build: {
    outDir: '../static/nomad-ui',
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
    cssCodeSplit: true,
    target: 'baseline-widely-available',
  },
  server: {
    strictPort: true,
  },
});
