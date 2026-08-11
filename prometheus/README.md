# Prometheus profile proof data

This directory stores the bulky, machine-readable evidence used to validate
stock Prometheus profiles in `netdata/netdata`.

## Data boundary

- `profiles/<profile>/fixtures/` contains sanitized, source-derived synthetic Prometheus exposition.
- `profiles/<profile>/SOURCE-SEMANTICS.yaml` records source-owned metric semantics and exact upstream revisions.
- Optional `SOURCE-REGISTRY.yaml` contains generated mechanical registration truth. Its sibling
  `SOURCE-REGISTRY.generator.yaml` and `generator/` directory reproduce it from the declared upstream source closure.

Each proof case is a realizable deployment state. Separate fixtures represent mutually exclusive releases, roles,
features, and exporter modes.

Operational dumps, credentials, private endpoints, and real deployment label values must not be committed here. Profile
design, replay cases, and operator rationale remain in the matching
`src/go/plugin/go.d/collector/prometheus/profile-proofs/<profile>/` directory in `netdata/netdata`.

## Generated registries

Run all generated-registry checks from the repository root:

```sh
python3 prometheus/tools/source_registry_runner.py
```

The fixed runner fetches each declared full upstream commit, exposes only declared source files, the reviewed generator
directory, and its shared `prometheus_client` source parser, runs conventionally discovered fail-closed tests with network
and writes disabled, and compares generated stdout byte-for-byte with the committed registry. Generators use only the
Python standard library.

FastAPI, LiteLLM, and vLLM extraction fingerprints the normalized AST of each complete Python module whose executable shape
is an input, including imports and other global bindings. Formatting and comments do not affect the fingerprint; any
executable source-shape change fails closed until the pinned upgrade is reviewed and the expected fingerprint is replaced.
The shared `prometheus_client` fingerprint also closes every consumer over the `_created` emitter implementation and its
positive feature gate. Other generators use source-language-specific closed grammars and negative tests instead.

## Consumer contract

Prometheus profile validation uses the latest `netdata/testdata` `master`. Each profile has one stable directory; proof
data and the matching Netdata profile proof are updated together when exporter coverage or validation behavior changes.

Git owns transport integrity; this repository does not preserve historical proof revisions for old Netdata checkouts.
