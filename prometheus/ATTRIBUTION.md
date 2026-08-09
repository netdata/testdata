# Prometheus profile evidence attribution

The fixtures in this directory are original, sanitized structural data created
for Netdata profile validation. They were derived from metric registrations,
update callsites, tests, and documentation in the public upstream sources below;
they are not verbatim operational scrapes.

Detailed file-level source paths, feature gates, and semantic reconciliation
remain in the corresponding `netdata/netdata` profile proof.

## Ceph

- `ceph/ceph` at:
  - `efac5a54607c13fa50d4822e50242b86e6e446df`
  - `abc7aa7f2701e5d46878fd5e6bb7e2955f1a395a`
  - `0fcffee29411e3a38036764817b6e1afc59741cc`
- `ceph/ceph-nvmeof` at `c79b6f44bd2288f7ec5c48e3cc47f6e566573d3f`.
- `prometheus/client_python` at `2dcd17efd0ce2f0a1ad15cb3c150ffcdc42ced65`.
- Licenses:
  - <https://github.com/ceph/ceph/blob/efac5a54607c13fa50d4822e50242b86e6e446df/COPYING>
  - <https://github.com/ceph/ceph-nvmeof/blob/c79b6f44bd2288f7ec5c48e3cc47f6e566573d3f/LICENSE>
  - <https://github.com/prometheus/client_python/blob/2dcd17efd0ce2f0a1ad15cb3c150ffcdc42ced65/LICENSE>

## LiteLLM

- `BerriAI/litellm` at:
  - `b3086ccd74553565c9a39716e72303ae985555f9`
  - `23de7a15d9d40006ee596e617475ba101d60c5e9`
  - `de706a35a6f1e9cb8c3cb527271df0b76a69f410`
- `prometheus/client_python` at `f417f6ea8f058165a1934e368fed245e91aafc14`.
- Licenses:
  - <https://github.com/BerriAI/litellm/blob/23de7a15d9d40006ee596e617475ba101d60c5e9/LICENSE>
  - <https://github.com/prometheus/client_python/blob/f417f6ea8f058165a1934e368fed245e91aafc14/LICENSE>

## FastAPI instrumentator

- `trallnag/prometheus-fastapi-instrumentator` at `2f841527277a21ac9ea622a9f923a5f9078234c4`.
- `prometheus/client_python` at `f417f6ea8f058165a1934e368fed245e91aafc14`.
- Licenses:
  - <https://github.com/trallnag/prometheus-fastapi-instrumentator/blob/2f841527277a21ac9ea622a9f923a5f9078234c4/LICENSE>
  - <https://github.com/prometheus/client_python/blob/f417f6ea8f058165a1934e368fed245e91aafc14/LICENSE>

## Shared Python runtime collectors

- The `process_runtime` and `python_gc` source packs use `prometheus/client_python` 0.24.1 at
  `f417f6ea8f058165a1934e368fed245e91aafc14`.
- License:
  <https://github.com/prometheus/client_python/blob/f417f6ea8f058165a1934e368fed245e91aafc14/LICENSE>

## vLLM

- `vllm-project/vllm` at:
  - `adf15cadb9d0151663b001a7286674892c4daa3c`
  - `dc818c198d3ff50a16f38eba567da006478239c8`
- License:
  <https://github.com/vllm-project/vllm/blob/dc818c198d3ff50a16f38eba567da006478239c8/LICENSE>

## vLLM on Ray

- `vllm-project/vllm` at `dc818c198d3ff50a16f38eba567da006478239c8`.
- `ray-project/ray` at `03491225d59a1ffde99c3628969ccf456be13efd`.
- Licenses:
  - <https://github.com/vllm-project/vllm/blob/dc818c198d3ff50a16f38eba567da006478239c8/LICENSE>
  - <https://github.com/ray-project/ray/blob/03491225d59a1ffde99c3628969ccf456be13efd/LICENSE>
