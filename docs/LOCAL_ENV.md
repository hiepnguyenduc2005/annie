# Local environment and sponsor integration

The requested deployment profile runs inference, speech, storage, and Elastic
locally. The root [.env.example](../.env.example) is the fill-in template;
launchers read the ignored root `.env`. Merge settings into an existing file.
Model downloads and package installation may need internet; runtime inference
does not require a cloud account. Configuration does not start services or
prove the complete local workflow meets its latency gate.

## What we have

| Component | Current implementation | Local profile |
| --- | --- | --- |
| Image brain | Ollama/Qwen supported; completed story runs used cloud vision | `annie-qwen3-vl:2b` on loopback; full local story qualification remains open |
| App journal | SQLite | `.data/annie.sqlite3` |
| Scene memory | Graphiti + FalkorDBLite, exercised in the story | Keep `ANNIE_MEMORY_PROVIDER=graphiti` |
| Embeddings | Ollama Nomic | `nomic-embed-text`; used by Graphiti and optional app recall |
| Speech | Local Whisper and macOS synthesis/playback | No Deepgram or ElevenLabs key |
| Family UI | Web app and separate Swift app; robot relay incomplete | Local/LAN app delivery; no Linq dependency |
| Elastic | Caption write/search adapter; no provisioned node verified | Local HTTPS node, its API key and CA certificate |
| Profiles | Checked-in Swift code uses on-device storage | Expected MongoDB integration is unverified; current `main` contains no driver or connection setting |

The [story evidence](../robot/simulation/STORY_DEMO.md) establishes simulated
movement, speech playback, and local cited memory. Its cloud-model timings are
not evidence of a completed all-local run. The Go2 has completed supervised
physical movement; integrated physical story/patrol acceptance remains open.

Existing cloud credentials in a private `.env` are not needed by this profile.
`ANNIE_AUDIO_ENABLED=false` specifically disables cloud transcription;
`ANNIE_ENABLE_CLOUD_AGENTS=false` disables the advisory service. Do not use the
cloud launcher flags for a local run. These settings are configuration, not a
machine-wide network sandbox.

## Start the existing local brain and memory

Use [local memory setup](../robot/simulation/LOCAL_MEMORY.md) for the isolated
Graphiti runtime. The model alias and Nomic weights are already installed on
the current development Mac; other machines must prepare them first.

```sh
# Separate terminals, from the repository root. Use unoccupied ports.
.cache/graphiti-venv/bin/python -m robot.simulation.local_graph_memory --port 8005
.venv/bin/python robot/simulation/run_brain.py --mode local --model annie-qwen3-vl:2b --port 8004
```

The brain launcher overrides the model environment variable, so pass the alias
explicitly. Follow [the local agent bridge instructions](../robot/simulation/AI_BRAIN.md)
to connect it. Local microphone/host speech use their existing launchers; there
are no `DEEPGRAM_API_KEY` or `ELEVENLABS_API_KEY` consumers in the current code.

## Bring local Elasticsearch online

Use Elastic's [single-node Docker instructions](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-docker-basic)
with a pinned image version, persistent storage, and port mapping
`127.0.0.1:9200:9200`. Keep HTTPS and authentication enabled. Copy the generated
`http_ca.crt` into `.data/elastic/http_ca.crt`; the adapter uses `ELASTIC_CA_CERT`
to verify it. This task supplies configuration; it does not provision a node.

Before selecting Elastic, create `annie-sim-observations` on that local node
using this body with `PUT /annie-sim-observations` in local Kibana Dev Tools.
The explicit mapping preserves exact map filters and capture timestamps:

```json
{
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "frame_id": {"type": "keyword"},
      "ts": {"type": "date", "format": "epoch_millis"},
      "map_id": {"type": "keyword"},
      "pose": {"properties": {
        "x": {"type": "double"},
        "y": {"type": "double"},
        "yaw": {"type": "double"}
      }},
      "caption": {"type": "text"},
      "person": {"type": "boolean"},
      "posture": {"type": "keyword"},
      "location": {"type": "keyword"},
      "confidence": {"type": "double"},
      "source": {"type": "keyword"}
    }
  }
}
```

Create a local [Elasticsearch API key](https://www.elastic.co/docs/deploy-manage/api-keys/elasticsearch-api-keys)
with `read` and `index` privileges restricted to `annie-sim-observations`.
Put its encoded value in `ELASTIC_API_KEY`, without an `ApiKey ` prefix.
Use `ELASTIC_URL=https://localhost:9200` and the certificate path above.
Then change the existing `ANNIE_MEMORY_PROVIDER` line to `elastic` and restart
the owned bridge. No Elastic Cloud signup or cloud API key is needed.

Elastic replaces Graphiti for bridge retrieval; this does not migrate old
Graphiti observations or replace the SQLite journal. The current adapter
indexes only `simulation_vlm` captions and pose/time evidence, with lexical
search. It does not upload images or implement Elastic semantic search.
Verify a new synthetic observation and a same-map cited retrieval before
calling the integration connected. Check disconnected/error behavior too.

Mapping references: [create index](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-create),
[date fields](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/date).
