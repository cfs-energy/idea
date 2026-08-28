/// <reference types="vite/client" />

// URL imports for the sample job script assets under src/pages/hpc.
declare module '*.txt?url' {
    const src: string
    export default src
}

// CRA-era globals kept for compatibility: vite.config.ts defines these via
// `define`, matching the types react-scripts used to declare.
declare namespace NodeJS {
    interface ProcessEnv {
        readonly NODE_ENV: 'development' | 'production' | 'test'
        readonly PUBLIC_URL: string
    }
}
