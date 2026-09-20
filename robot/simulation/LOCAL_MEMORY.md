# Local graph memory

Annie reuses **Graphiti 0.30.2**, the open-source engine behind Zep, with
**FalkorDBLite 0.10.0** and **nomic-embed-text** on local Ollama. The service
runs separately on `127.0.0.1:8005`; its database persists under
`.data/simulation/graphiti/`. It needs no Zep, Elastic, or cloud-model API key.

## Run

From the repository root:

```sh
uv venv .cache/graphiti-venv --python .venv/bin/python
uv pip install --python .cache/graphiti-venv/bin/python -r robot/simulation/requirements-memory.lock
ollama pull nomic-embed-text
.cache/graphiti-venv/bin/python -m robot.simulation.local_graph_memory --port 8005
```

macOS requires `libomp` for the embedded engine (`brew install libomp` if
absent). The installed native wheel supports this machine's Apple Silicon.
Docker is unnecessary. Graphiti telemetry is disabled. Dependencies are kept
out of the app and physics virtual environments.

The tested local embedding model digest is
`0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`.
The lockfile pins the Python dependency versions used for this integration.

Add these settings to the ignored root `.env`, then restart the simulator's
owned bridge through its normal launcher:

```dotenv
ANNIE_MEMORY_PROVIDER=graphiti
ANNIE_GRAPHITI_URL=http://127.0.0.1:8005
```

The bridge's memory status should say `graphiti_local`. `GET /health` on port
8005 reports readiness, the embedding model, and indexed observation count.
The separate app SQLite journal continues to preserve incidents and evidence.
The optional Elastic adapter remains available via `ANNIE_MEMORY_PROVIDER=elastic`.

## Evidence and retrieval

`POST /observations` accepts the existing typed `Perception` object with
`source=simulation_vlm`. It stores only the caption, observed person/posture/
surface/confidence, capture ID/time, and observer pose/map. Raw images and
audio never enter this store. Each observation becomes a Graphiti episode
and an `OBSERVED_IN` edge linking the capture to its map. The edge contains
the caption and a 768-dimensional local Nomic embedding. Capture timestamps
become `valid_at`; ingestion time remains separate.

The vision model already interpreted the image. This integration intentionally
does not run Graphiti's separate LLM entity-extraction pipeline or merge
people into persistent identities. Those remain unimplemented capabilities.

`POST /search` takes `{query, map_id, ts_from?, ts_to?}`. Graphiti performs
keyword and vector retrieval with reciprocal-rank fusion. It returns original
episode citations, never an uncited generated answer or newly inferred fact.
Queries stay within a map and time window. A camera coordinate remains the
observer's position; it is not a measured person position. Later empty views
do not delete earlier observations.

Writes use deterministic IDs and acknowledge only `indexed` or
`already_indexed`. Reusing a capture ID with changed evidence is rejected.
The current bounded store accepts up to 1,000 observations per map and 16 maps;
capacity exhaustion is explicit. There is no automatic retention deletion.
The HTTP adapter permits loopback only, bounds responses, uses a 1 s search
deadline and 1.2 s write deadline, and never retries. Robot motion runs in its
existing separate control loop. An unavailable memory service is reported
as unavailable, not silently replaced with fabricated context.

## Measured verification

On 2026-09-19, two accepted observations from the current simulator map were
indexed in 100.71 ms and 49.95 ms. The query “Where was somebody spotted?”
retrieved the original “A person standing or visible behind the bed.” frame
in 38.25 ms, with its original timestamp and observer pose. Evidence:
`output/local-graphiti-memory.json`. These are small warm-runtime measurements,
not a latency percentile or a full memory benchmark.

```sh
.venv/bin/python -m pytest robot/simulation/tests/test_hosted_memory.py robot/simulation/tests/test_local_graph_memory.py -q
# The actual database test requires the isolated runtime; normal tests mock HTTP.
uv pip install --python .cache/graphiti-venv/bin/python pytest
.cache/graphiti-venv/bin/python -m pytest robot/simulation/tests/test_local_graph_memory.py -q
```

## Upstream provenance

- [Graphiti](https://github.com/getzep/graphiti), version 0.30.2, Apache-2.0;
  uses its `FalkorDriver`, episode/entity/edge models, and hybrid RRF search.
- [FalkorDBLite](https://github.com/FalkorDB/falkordblite), version 0.10.0;
  the package's license and bundled native-engine licenses remain upstream.
- [Nomic Embed Text](https://ollama.com/library/nomic-embed-text), local model
  already present on this host; uses the documented query/document prefixes.

Dependencies are reused as published packages; this repository does not claim
to be a Zep hosted deployment or a fork of the entire Graphiti codebase.
