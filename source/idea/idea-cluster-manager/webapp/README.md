# Cluster Manager Web Portal

React + TypeScript single-page app for the IDEA web portal, built with [Vite](https://vitejs.dev/).

## Available Scripts

In the project directory, you can run:

### `yarn serve` (or `yarn start`)

Runs the app in development mode on [http://localhost:3000](http://localhost:3000).
The page reloads on edit. API endpoints for local development come from `.env`
(`REACT_APP_IDEA_HTTP_ENDPOINT` / `REACT_APP_IDEA_ALB_ENDPOINT`).

### `yarn test`

Runs the [vitest](https://vitest.dev/) suite once. Use `yarn test:watch` for watch mode.

### `yarn typecheck`

Runs `tsc --noEmit`. Also runs as the first step of `yarn build`.

### `yarn build`

Type-checks, then builds the production bundle into `build/`:

- `build/index.html` remains a Jinja2 template; cluster-manager's `web_portal.py`
  renders it and substitutes the `app_init_data` variable at request time. The
  `idea:jinja-placeholder-guard` plugin (`build-support/`) fails the build if the
  emitted file loses the `{{ app_init_data }}` placeholder or gains any other
  Jinja sequence.
- `build/service-worker.js` is the compiled `src/service-worker.ts` (auth token
  handling; see `vite.config.ts`, built via vite-plugin-pwa injectManifest).
- Assets follow the `static/{js,css,media}` layout consumed by
  `tasks/tools/build_tool.py` packaging.

## Notes

- Environment variables keep the CRA-era `REACT_APP_` prefix: the build tooling
  (`tasks/tools/build_tool.py`) writes `REACT_APP_IDEA_RELEASE_VERSION` into
  `.env`, and `vite.config.ts` maps each `REACT_APP_*` var onto `process.env.*`
  at build time.
- Service workers cannot be exercised via the dev server; see the comments in
  `src/index.tsx` for how to test the service worker flow locally.
- `engines` in `package.json` requires node >= 22.12, which yarn enforces as a hard
  error. Vite 7 itself also accepts `^20.19`, but node 20 is end-of-life and CI pins
  the version in `software_versions.yml`.
- Browser support floor: `build.target` in `vite.config.ts`. Vite does not read
  `browserslist`, so that field was removed rather than left to mislead.
- `workbox-build` and `workbox-window` are devDependencies that nothing in `src`
  imports. They satisfy vite-plugin-pwa's declared peer dependencies; dropping
  them re-introduces `yarn install` peer warnings.
- ace loads syntax workers by URL, not through `esm-resolver`; the mappings live in
  `src/common/ace-worker-urls.ts`. Every call site that configures ace registers
  them. A new editor language with a worker needs an entry there.
