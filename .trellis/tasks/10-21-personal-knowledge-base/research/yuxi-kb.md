# Yuxi knowledge-base research (for kunxiaozhi personal KB port)

Reference: `/tmp/ref/Yuxi` @ `1d66405`. Paths below are relative to that checkout; line numbers are from that commit.
Backend root: `backend/package/yuxi/` (abbrev. `PKG/`), routers: `backend/server/routers/`, frontend: `web/src/`.

IMPORTANT correction vs. earlier analysis: in this Yuxi version the graph is **not Neo4j/LightRAG**. It is
"graph in Postgres tables + entity/triple vectors in Milvus + Personalized-PageRank at query time"
(`PKG/knowledge/graphs/milvus_graph_service.py`, `milvus_graph_vector_store.py`). That is very close to our
"triples in MongoDB, vectors in Qdrant" plan. See section 7.

---------------------------------------------------------------------------------------------------
## 1. Data model

Source: `PKG/storage/postgres/models_knowledge.py` (316 lines).

### 1.1 KB (`knowledge_bases`, L28-L50 approx)
| column | type | notes |
|---|---|---|
| `kb_id` | String(80) unique | generated `"kb_" + 10 random [a-z0-9]` (`PKG/knowledge/manager.py` create_database L457-L548) |
| `name` | String(255) | global unique check `database_name_exists` (raises `KBNameConflictError` -> HTTP 409) |
| `description` | Text | |
| `kb_type` | String(32) | `milvus` / `dify` / `notion`. We only need the milvus-equivalent |
| `embedding_model_spec` | String(512) | per-KB, chosen at create time. **We drop this (admin-global)** |
| `llm_model_spec` | String(512) | used for graph extraction/mindmap/sample questions. **We drop (admin-global)** |
| `query_params` | JSON `{"options": {...}}` | persisted default retrieval params (see §5.1) |
| `additional_params` | JSON | `chunk_preset_id`, `chunk_parser_config`, `auto_generate_questions`, `stats` {...} |
| `share_config` | JSON | v2 `{version:2, read_scope:{access_level:global|department|user, department_ids, user_uids}, manage_scope}` (see §9) |
| `mindmap`, `mindmap_file_ids`, `mindmap_metadata`, `sample_questions` | JSON | optional extras (AI mind map + sample questions). Skip |
| `created_by` | String(64) uid | |

Per-KB settings = `additional_params.chunk_preset_id` + `additional_params.chunk_parser_config`
(`{delimiter, chunk_token_num, overlapped_percent, language, embed_model_id...}`) + `query_params.options`.
`stats` cached in `additional_params.stats` and in Redis `yuxi:kb_file_stats:{kb_id}` (TTL const `KB_FILE_STATS_CACHE_TTL`).
KB runtime config is cached in Redis via `PKG/knowledge/cache.py` (`get_cached_kb_config`, `cache_kb_config`) – optional for us.

### 1.2 File (`knowledge_files`, L53-L85)
```
file_id String(64) unique         # "file_" + hashstr(path+time, 6)
kb_id FK knowledge_bases ON DELETE CASCADE
parent_id FK knowledge_files.file_id ON DELETE SET NULL   # folder tree (is_folder=True rows)
filename String(512)              # display name (may be source_path relative path for folder upload; for URL import = the URL)
original_filename String(512)
file_type String(64)              # lowercased extension without dot ("pdf"); URL imports forced to "html"
path String(1024)                 # MinIO URL of ORIGINAL object  (bucket kb-documents? see §2.4)
minio_url String(1024)
markdown_file String(1024)        # MinIO URL of PARSED markdown (set when status=parsed)
status String(32) default "uploaded" (indexed)
content_hash String(128) (indexed)   # sha256 hex of raw bytes
file_size BigInteger
chunk_count Integer, token_count BigInteger   # filled at index time
content_type String(64)           # "file" (URL import is pre-fetched to an html file)
processing_params JSON            # per-file chunk_preset_id/chunk_parser_config/ocr params (sanitized)
is_folder Boolean
error_message Text                # last failure text (parse or index)
processing_task_id String(64) idx # lease: which durable Task currently owns the file
processing_owner String(128)      # lease: worker owner string of that Task attempt
created_by/updated_by String(64), created_at/updated_at timestamptz
```

### 1.3 Chunk (`knowledge_chunks`, L88-L123)
`chunk_id` String(128) unique (`f"{file_id}_chunk_{idx}"`), `file_id` FK cascade, `kb_id` FK cascade, `chunk_index` int,
`content` Text, `start_char_pos`/`end_char_pos` (offset in parsed markdown, found by `source_text.find(text, search_from)`),
`start_token_pos`/`end_token_pos` (always None), graph flags `graph_structure_indexed`, `graph_indexed`,
`graph_extraction_details` JSON default `{"status":"pending","attempt_count":0}`, `ent_ids` JSON, `tags` JSON, `extraction_result` JSON.
**There is no page number / header path / line range column.** Header context is only baked into the chunk text by the
`semantic` preset (`#### title|Part n` headers). Line ranges are computed on demand from the parsed markdown by the open/find tools (§6).

### 1.4 File status state machine
`PKG/knowledge/base.py` L21-L31:
```python
class FileStatus:
    UPLOADED = "uploaded"; PARSING = "parsing"; PARSED = "parsed"; ERROR_PARSING = "error_parsing"
    INDEXING = "indexing"; INDEXED = "indexed"; ERROR_INDEXING = "error_indexing"
INDEXED_STATS_STATUSES = {FileStatus.INDEXED, "done"}   # "done"/"failed" = legacy values still tolerated
```
Transitions (all are **compare-and-set** via `KnowledgeFileRepository.update_fields_if_status(kb_id,file_id,allowed_statuses,data,processing_task_id,processing_owner)`, `PKG/repositories/knowledge_file_repository.py` L756-L808, which also verifies in the same TX that the owning Task is `running` with the same `worker_id`):
```
(record created)            -> uploaded          add_file_record  (base.py L180-L238)
uploaded|error_parsing|failed -> parsing         parse_file claim (L240-L300) sets processing_task_id/owner, error_message=None
parsing -> parsed                                 stores markdown_file, clears lease+error
parsing -> error_parsing                          error_message=str(e); clears lease; also on cancel ("File parsing was cancelled")
parsed|error_indexing|indexed|done -> indexing   index_file claim (milvus.py L678-L760). If markdown_file missing -> reset to uploaded + ValueError
indexing -> indexed                               sets chunk_count, token_count, clears lease
indexing -> error_indexing                        error_message; clears lease
parsing/indexing -> error_parsing/error_indexing  by Task failure hook when worker dies / cancelled (fail_task_processing_in_session L811-L830)
```
Pending groups used by "parse all pending / index all pending" (router L61-62): `PENDING_PARSE_STATUSES=["uploaded"]`, `PENDING_INDEX_STATUSES=["parsed","error_indexing"]`.
Stats (`query_kb_file_stats`, repo L677-L720): `processing_count` = status in `["processing","waiting","parsing","indexing"]`;
`pending_parse_count` = uploaded; `pending_index_count` = parsed|error_indexing. Note: `error_parsing` is NOT counted as pending parse, so the UI offers a per-file retry for it.
Errors are recorded ONLY in `knowledge_files.error_message` (string) plus the Task row's `error`/`result.items[].error`. No retry counter on files.

