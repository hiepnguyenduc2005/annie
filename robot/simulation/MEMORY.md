# Episodic memory (opt-in Elastic Cloud)

Annie's simulation brain can persist and retrieve strict, image-free
observation memories through an existing Elasticsearch index on the sponsor's
Elastic Cloud deployment. This is opt-in: when ANNIE_MEMORY_PROVIDER is not
"elastic", from_env() returns None and the local journal remains the only
memory (retained separately).

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| ANNIE_MEMORY_PROVIDER | "elastic" enables the adapter | unset (disabled) |
| ELASTIC_URL | HTTPS Cloud endpoint, no credentials or query string | required |
| ELASTIC_API_KEY | API key, sent only as Authorization: ApiKey header | required |
| ANNIE_MEMORY_INDEX | Existing index name, [a-z0-9][a-z0-9._-]* | annie-sim-observations |

The index must already exist; the adapter never auto-creates indices or
embeddings, so no hybrid/semantic billing is incurred. Only lexical match
queries are used today. Semantic recall via semantic_text plus RRF is a
future comparison, not a current claim.

## Contract

- upsert(perception) accepts only source == "simulation_vlm" perceptions and
  issues an idempotent PUT /{index}/_doc/{frame_id} with the frame UUID as the
  stable document ID. The stored projection is exactly frame_id, ts, map_id,
  pose{x,y,yaw}, caption, person, posture, location, confidence, source. No
  images, base64 payloads, or secrets are ever stored or extracted.
- search(query, map_id, ts_from=None, ts_to=None) issues POST
  /{index}/_search with a lexical match on caption, an exact term filter on
  map_id, an optional ts range, and size bounded to 6. Each hit is projected
  to the cited memory shape {caption, frame_id, ts, pose:{x,y,yaw,map_id}};
  malformed hits and hits from a different map are rejected rather than
  surfaced.
- Timeouts are 0.8 s for writes and 0.5 s for searches, so the async memory
  worker never blocks robot motion. Failures raise MemoryUnavailable with a
  sanitized message (exception type and sanitized URL only); there are no
  retries. The API key never appears in logs or error text.

## Reference

- Index API (idempotent PUT with explicit _id):
  https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-index-2
- Search API (lexical query with filters):
  https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-search
- Future comparison: semantic_text field mapping plus RRF retrieval.
