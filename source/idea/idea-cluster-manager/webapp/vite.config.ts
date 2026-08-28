import { defineConfig } from 'vitest/config'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import { jinjaPlaceholderGuard } from './build-support/jinja-placeholder-guard'

// REACT_APP_* vars referenced in src; missing ones compile to `undefined`,
// matching CRA's DefinePlugin behavior for unset environment variables.
const KNOWN_ENV_KEYS = [
    'REACT_APP_IDEA_RELEASE_VERSION',
    'REACT_APP_IDEA_HTTP_ENDPOINT',
    'REACT_APP_IDEA_ALB_ENDPOINT',
    'REACT_APP_TAIL_POLLING_INTERVAL'
]

export default defineConfig(({ mode }) => {
    // CRA compatibility: .env keeps the REACT_APP_ prefix (tasks/tools/build_tool.py
    // writes REACT_APP_IDEA_RELEASE_VERSION into it) and source reads process.env.*,
    // so map each var to a build-time constant instead of migrating to import.meta.env.
    const env = loadEnv(mode, process.cwd(), 'REACT_APP_')
    const define: Record<string, string> = {
        'process.env.NODE_ENV': JSON.stringify(mode),
        // CRA value for homepage "."; service-worker-registration.ts derives the
        // service worker URL from it.
        'process.env.PUBLIC_URL': JSON.stringify('.')
    }
    for (const key of KNOWN_ENV_KEYS) {
        define[`process.env.${key}`] = key in env ? JSON.stringify(env[key]) : 'undefined'
    }
    for (const [key, value] of Object.entries(env)) {
        define[`process.env.${key}`] = JSON.stringify(value)
    }

    return {
        base: './',
        define: define,
        plugins: [
            react(),
            // replaces CRA's workbox-webpack-plugin InjectManifest build of
            // src/service-worker.ts; registration stays manual (service-worker-registration.ts)
            VitePWA({
                strategies: 'injectManifest',
                srcDir: 'src',
                filename: 'service-worker.ts',
                injectRegister: false,
                manifest: false,
                injectManifest: {
                    // service-worker.ts disables precaching (self.__WB_MANIFEST is
                    // intentionally unused), so skip the manifest injection step.
                    injectionPoint: undefined
                },
                devOptions: {
                    enabled: false
                }
            }),
            // fails the build if the emitted index.html stops being renderable by
            // cluster-manager's web_portal.py
            jinjaPlaceholderGuard()
        ],
        server: {
            port: 3000
        },
        build: {
            // tasks/tools/build_tool.py packages webapp/build as-is; keep the CRA
            // output contract: build/ root, static/{js,css,media} asset layout.
            outDir: 'build',
            sourcemap: false,
            // The support floor, stated where it is actually enforced. CRA transpiled to
            // es5; this is Vite 7's default and is roughly Chrome/Edge 107, Firefox 104,
            // Safari 16. package.json's browserslist no longer influences the build.
            target: 'baseline-widely-available',
            // emit context-help .md and sample-script .txt assets as real files
            // (fetched at runtime) instead of inlining them as data: URIs
            assetsInlineLimit: 0,
            rollupOptions: {
                output: {
                    entryFileNames: 'static/js/[name].[hash].js',
                    chunkFileNames: 'static/js/[name].[hash].js',
                    assetFileNames: (assetInfo) => {
                        const name = assetInfo.names && assetInfo.names.length > 0 ? assetInfo.names[0] : ''
                        if (name.endsWith('.css')) {
                            return 'static/css/[name].[hash][extname]'
                        }
                        return 'static/media/[name].[hash][extname]'
                    }
                }
            }
        },
        test: {
            environment: 'jsdom',
            globals: true,
            setupFiles: ['./src/setupTests.ts'],
            css: false
        }
    }
})
