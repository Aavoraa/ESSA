import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { copyFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const shopStatic = resolve(here, '..', 'Textile Retail Shop', 'app', 'static')

// The design system has ONE source, in src/design. The shop (Flask, in its own
// folder) gets a copy of the tokens and component styles, and an SVG sprite of
// the same icon set, every time the frontend is built or the dev server starts,
// so the two front ends cannot drift apart.
function shareDesignSystem() {
  const run = async () => {
    mkdirSync(resolve(shopStatic, 'css'), { recursive: true })
    mkdirSync(resolve(shopStatic, 'icons'), { recursive: true })
    for (const f of ['tokens.css', 'components.css']) {
      copyFileSync(resolve(here, 'src', 'design', f), resolve(shopStatic, 'css', f))
    }
    const { ICON_SVG } = await import(pathToFileURL(resolve(here, 'src', 'design', 'icons.js')).href + '?t=' + Date.now())
    const symbols = Object.entries(ICON_SVG).map(([name, svg]) =>
      `  <symbol id="i-${name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${svg}</symbol>`)
    writeFileSync(resolve(shopStatic, 'icons', 'sprite.svg'),
      `<!-- generated from frontend/src/design/icons.js — do not edit -->\n<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n${symbols.join('\n')}\n</svg>\n`)
  }
  return { name: 'share-design-system', buildStart: run, configureServer: run }
}

// Dev server proxies /api to the FastAPI backend on :8000, and /pos to the
// retail shop it mounts there — same-origin in dev as in a build, which is what
// keeps the shop's login cookie working inside the POS frame.
export default defineConfig({
  plugins: [react(), shareDesignSystem()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/pos': 'http://localhost:8000',
    },
  },
  build: { outDir: 'dist' },
})
