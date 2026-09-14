import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // The container's bind mounts are read-only and inotify does not always
    // propagate through them; polling keeps hot reload working in compose.
    watch: { usePolling: true },
  },
})
