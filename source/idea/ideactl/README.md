# ideactl

`ideactl` is the TypeScript CDK administrator and cluster CLI for IDEA. It is
shipped with the control-plane CLI in one image, not as a separately deployed
service. The port's primary requirement is strict behavioural and
CloudFormation-template parity with the existing Python administrator: preserve
the construct tree, logical IDs, ordering, types, and deliberate quirks rather
than improving them.

## Layout

| Path | Purpose |
| --- | --- |
| `src/cdk/` | CDK app, base stack, stack implementations, constructs, policy rendering, code assets, and replayable synth reads. |
| `src/config/` | Cluster-settings access, values/config generation, ARN construction, and AMI selection. |
| `src/cli/` | The `ideactl` command tree and CDK invocation helpers. |
| `src/lambda/` | Custom-resource and event handler ports, with shared CloudFormation response support in `commons/`. |
| `src/util/` | Compatibility helpers such as identifiers, names, hashes, and YAML handling. |
| `resources/` | Config templates, policies, CDK inputs, Lambda sources, installer parameters and integration-test data, copied into `dist/resources` during a build. |
| `resources-ecs/` | The container control plane's observability specification. |
| `tools/parity/` | Offline template comparison, fixture capture, fixture-driven synth, and config flattening. |
| `tools/e2e/` | Opt-in tools for exercising a deployed control plane through its public endpoints. |
| `test/` | Focused `node:test` suites, one directory per area under test. |

## Compatibility pins

`package.json` is a compatibility surface, not a dependency wishlist. Exact
pins keep CDK-generated logical IDs, singleton resources, asset conventions,
and bootstrap behaviour reproducible. Keep the CDK library, CDK CLI,
constructs, and compliance package aligned with the recorded templates; do not
refresh one independently. The renderer, YAML parser, CLI parser, and SDK
clients have compatibility version constraints so their parsing and request
behaviour cannot drift.

Use the installed Node runtime's native TypeScript stripping for focused tests;
do not compile first:

```sh
node --test 'test/parity/*.test.ts'
```

The project is ESM. Relative imports use `.ts` extensions, type-only imports
use `import type`, and TypeScript must remain erasable: no enums, namespaces,
or parameter properties. Use Node built-ins before adding a dependency.

## Offline parity

The parity harness compares a synthesized template with the recorded Python
template strictly for deployed state. It intentionally masks only documented
volatile values. It compares security-linter suppression metadata and
`aws:cdk:path` exactly. Other metadata is excluded.

- `parity.ts` accepts `diff [--ignore-version] <live.json> <synth.json>` for a
  comparison and `paths <template.json>` to report logical IDs and construct
  paths. Its result is `PARITY` or `MISMATCH`, followed by resource and property
  counts.
- `synth.ts` requires `--cluster` and `--stack`. It supports `--against synth`
  for a Python-synth reference, repeatable `--context key=value`, plus
  `--ignore-version`, `--deployment-id`, `--app-override`, and `--keep`. It creates a temporary working directory
  containing the package CDK configuration and captured context, then runs the
  bundled CDK CLI without lookups.
- `capture.ts --from-raw` converts saved command output into the table dumps
  and synth-read replay file. `--from-local` copies values, CDK context, and
  Python outputs from a local administrator directory. `--live` also captures
  live tables, reads, and deployed templates; it requires credentials and must
  not be used for fixture-only development.
- `flatten.py` is the independent Layer-A config oracle. It reads a generated
  config directory, emits sorted flat JSON, and supports `-o` and
  `--key-prefix`.

Run the harness checks and inspect the flattener interface from this package
directory:

```sh
node --test 'test/parity/*.test.ts'
python3 tools/parity/flatten.py --help
```

Fixture material is gitignored. Use these shapes, never committed live values:

```text
tools/parity/live/<cluster>-<stack>.json
tools/parity/fixtures/<cluster>/raw/
tools/parity/fixtures/<cluster>/{cluster-settings.json,modules.json,synth-reads.json}
tools/parity/fixtures/<cluster>/{values.yml,flat.json,cdk.context.json}
tools/parity/fixtures/<cluster>/python/_cdk/cdk.out.<module>/
```

Tests using these local fixtures must skip when they are absent. Committed test
data must be synthetic.

## Lambda handlers

Each ported handler lives in `src/lambda/<function>/index.ts`. Keep the
exported production `handler` thin and put injectable collaborators behind a
factory when calls or responses need observation. The shared
`src/lambda/commons/cfn-response.ts` module defines the CloudFormation event
shape and sends success or failure responses with the same response semantics
as the Python helper.

A handler test provides a synthetic event and context, injects SDK and response
recorders, then asserts the request payload, response status, physical ID, and
failure behaviour. It must cover every relevant CloudFormation request type
when Python treats them differently. For example:

```sh
node --test 'test/lambda/*.test.ts'
```

## End-to-end tools

The E2E tools are deliberately separate from parity. They contact an already
deployed control plane over its load-balancer or gateway endpoint, accept
credentials only by a password-file path, cache tokens locally per user, and
verify TLS unless `--insecure` is explicitly supplied.

- `api.ts` makes one namespace request and routes it to the appropriate API.
- `load-api.ts` drives the portal-like API request mix and reports latency and
  error percentiles.
- `load-gateway.ts` opens, holds, and reports TLS gateway connections.
- `load-jobs.ts` submits a bounded burst of short scheduler jobs and polls for
  completion.

Inspect their accepted flags before running a load against a deployment:

```sh
node tools/e2e/api.ts --help
node tools/e2e/load-api.ts --help
node tools/e2e/load-gateway.ts --help
node tools/e2e/load-jobs.ts --help
```

## Hygiene and completion

Do not make credentialed calls while developing the port. Real fixture
directories are ignored; do not place account identifiers, resource
identifiers, deployment hostnames, production cluster names, personal data, or
external-assistant names in tracked source, tests, tools, scripts, or images.
Use synthetic identifiers and `example.invalid` hosts in committed tests.

A stack is declared done only after its offline fixture run prints a `PARITY`
line with zero missing resources, zero extra resources, and zero hard property
differences (asset notices are allowed), and a credentialed live
`ideactl cdk diff` reports `There were no differences`. Exercise the additional
synth-shaped fixtures for branches the recorded deployment does not cover
before calling the port complete.
