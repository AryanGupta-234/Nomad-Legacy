import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: '/static/nomad-ui/',
  build: {
    outDir: '../static/nomad-ui',
    emptyOutDir: true,
    sourcemap: true,
    target: 'baseline-widely-available',
    lib: {
      entry: 'src/main.ts',
      formats: ['es'],
      fileName: () => 'nomad-ui.js',
      cssFileName: 'nomad-ui',
    },
  },
  server: {
    strictPort: true,
  },
});
