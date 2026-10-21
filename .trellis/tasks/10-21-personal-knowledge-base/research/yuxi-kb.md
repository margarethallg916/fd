# Yuxi knowledge-base research (for kunxiaozhi personal KB port)

Reference: `/tmp/ref/Yuxi` @ `1d66405`. Paths below are relative to that checkout; line numbers are from that commit.
Backend root: `backend/package/yuxi/` (abbrev. `PKG/`), routers: `backend/server/routers/`, frontend: `web/src/`.

IMPORTANT correction vs. earlier analysis: in this Yuxi version the graph is a **three-store design**, not LightRAG:
PostgreSQL = facts (entities / triples / mentions / per-chunk extraction state), Neo4j (optional) = graph structure for Cypher sub-graph queries,
Milvus = entity & triple vectors. Retrieval-time graph enhancement = vector-seed entities/triples -> 2-hop sub-graph -> Personalized PageRank over chunk nodes (networkx).
That maps cleanly onto our plan "triples in MongoDB, vectors in Qdrant, PPR in-app" (Neo4j role = Mongo adjacency lookups). See section 7.
Yuxi has no personal KBs, quotas, subscriptions or end-user KB creation: every KB management endpoint is admin-only (section 9).

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
path String(1024)                 # MinIO URL of ORIGINAL object (bucket `knowledgebases`, see §2.4)
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

---------------------------------------------------------------------------------------------------
## 2. Supported file types and parsing

### 2.1 Extension whitelist
`PKG/knowledge/parser/capabilities.py` L9-L27:
```python
TEXT_FILE_EXTENSIONS   = (".txt", ".md")
OFFICE_FILE_EXTENSIONS = (".docx", ".pptx", ".xls", ".xlsx")
HTML_FILE_EXTENSIONS   = (".html", ".htm")
IMAGE_FILE_EXTENSIONS  = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp")
PDF_FILE_EXTENSIONS    = (".pdf",)
OCR_FILE_EXTENSIONS    = frozenset((*PDF, *IMAGE))
SUPPORTED_FILE_EXTENSIONS = (*TEXT, *OFFICE, *HTML, ".json", ".csv", *PDF, *IMAGE, ".zip")
def is_supported_file_extension(file_name) -> bool: return Path(file_name).suffix.lower() in SUPPORTED_FILE_EXTENSIONS
```
(No `.ppt`/`.doc`/`.rtf`.) Upload endpoint rejects others with 400 `Unsupported file type: {ext}`
(`backend/server/routers/knowledge_router.py` upload_file L1639-L1711). Size limit `MAX_UPLOAD_SIZE_BYTES = 100*1024*1024`
(`PKG/utils/upload_utils.py` L6) — message "文件过大，当前仅支持 100 MB 以内的文件". Upload path also lowercases the whole filename.

### 2.2 Per-extension parsing (`PKG/knowledge/parser/unified.py` `parse_resolved_document` L116-L233)
| ext | parser |
|---|---|
| `.pdf` | `validate_pdf_page_tree_loadable` then `parse_pdf` -> OCR engine chosen by admin options; if engine == `"disable"` -> `pdfreader` = `pypdf.PdfReader`, pages `extract_text(extraction_mode="plain")` joined by `\n\n` (text layer only) |
| `.txt`, `.md` | read utf-8 as-is |
| `.docx` | Docling (`_convert_with_docling`, images -> MinIO) ; on exception fallback `_convert_docx_with_python_docx` (paragraphs + tables -> md tables) |
| `.pptx`, `.xlsx` | Docling (`_convert_with_docling`) |
| `.xls` | `pandas.read_excel(engine="xlrd", sheet_name=None, header=None, dtype=object, keep_default_na=False)` -> per sheet `## {sheet}` + `df.to_markdown(index=False, headers=[""]*n, disable_numparse=True)` |
| `.csv` | `pandas.read_csv` then **one markdown table per row** joined by `\n\n` (`_convert_csv_to_markdown` L375) |
| `.html/.htm` | `markdownify.markdownify(content, heading_style="ATX")` |
| `.json` | `json.dumps(indent=2, ensure_ascii=False)` wrapped in ```` ```json ```` fence |
| images | OCR engine only; if OCR disabled -> ValueError "图像文件必须启用OCR才能提取文本内容" |
| `.zip` | `zip_utils.process_zip_file`: reject abs paths/`..`; pick the `.md` named `full.md` else first `.md`; ValueError "压缩包中未找到 .md 文件" if none; finds `images/` dir next to md, uploads each `.jpg/.jpeg/.png/.gif/.webp/.bmp` to MinIO and rewrites `![alt](path)` links. (This is exactly the MinerU `response_format_zip` output format.) |
OCR engines (all admin/ENV configured; we only need MinerU): `rapid_ocr`, `mineru_ocr`, `mineru_official`, `pp_structure_v3_ocr`, `deepseek_ocr`,
`paddleocr_vl_1_6`, `paddleocr_pp_ocrv6` (`capabilities.py` L54-L100). Engine resolved in `PKG/services/ocr_service.py::parse_document` (L75-L118): only OCR extensions (pdf+images) go through `resolve_ocr_task_params`.

MinerU local parser request (`PKG/knowledge/parser/mineru.py` L120-L160): POST `{server}/file_parse` multipart `files=...` with
```python
data = {"lang_list": ["ch"], "backend": "hybrid-auto-engine", "parse_method": "auto", "formula_enable": True,
        "table_enable": True, "image_analysis": True, "start_page_id": 0, "end_page_id": 99999,
        "return_md": True, "response_format_zip": True, "return_images": True}
timeout = params.timeout_seconds or env MINERU_TIMEOUT or 1800
```
response is a ZIP -> `process_zip_file_sync` (above). "no text" -> `DocumentParserException("MinerU 未返回任何文本内容")`.
Parsing runs in `asyncio.to_thread` for all blocking parsers; temp-file download from MinIO then deleted in `finally`.

### 2.3 URL import
Yuxi does **not** parse URLs at ingest time. `POST /knowledge/files/fetch-url` (router L1505-L1573) downloads the page (`PKG/knowledge/utils/url_fetcher.py`:
`MAX_DOWNLOAD_SIZE = 10 MB`, `ALLOWED_CONTENT_TYPES = ["text/html","application/xhtml+xml"]`, SSRF guard with DNS-rebinding-safe resolution, blocks private/loopback + cloud metadata IPs
`169.254.169.254`, `100.100.200.200`, `fd00:ec2::254`; domain whitelist env `YUXI_URL_WHITELIST` — empty list => feature disabled `url_validator.py`), computes sha256, dedupes, uploads as `{kb_id}/upload/{hash}.html`
and returns `{file_path, content_hash, filename: <url>, size,...}`; the file is then added like any uploaded html file. -> **Out of scope for us.**

### 2.4 Storage layout and duplicates
- MinIO buckets (`PKG/storage/minio/client.py` L63-L67): `KB_BUCKETS = {"documents":"knowledgebases","parsed":"knowledgebases","images":"kb-images"}`.
- Original: `knowledgebases/{kb_id}/upload/{basename}_{ms_timestamp}{ext}` (timestamp avoids collisions, stripped for display by regex `^(.+)_(\d{13})(\.[^.]+)$`).
- Parsed markdown: `knowledgebases/{kb_id}/parsed/{file_id}.md` (`base.py::_save_markdown_to_minio` L439).
- Images extracted by parser: `kb-images/{kb_id}/kb-images/{µs_timestamp}_{name}`; markdown links are rewritten to an **auth proxy URL**
  `/api/knowledge/databases/{kb_id}/images/kb-images/{...}` (`kb_utils.build_kb_image_proxy_url` L195; route `get_kb_image` router L1234) so private bucket images respect KB permission.
- **Duplicate detection**: sha256 of raw upload bytes (`kb_utils.calculate_content_hash`), checked at upload time per KB:
  `knowledge_base.file_existed_in_db(kb_id, content_hash)` -> `KnowledgeFileRepository.exists_by_content_hash` (non-folder, same kb_id, status not `"failed"`) -> HTTP **409** "数据库中已经存在了相同内容文件".
  Same *name* (not hash) is only a warning: response has `same_name_files` + `has_same_name`, UI asks user to confirm/overwrite.
- Upload flow is **two-phase**: (1) `POST /knowledge/files/upload?kb_id=` stores bytes in MinIO and returns `{file_path(minio url), content_hash, filename, size, same_name_files,...}`;
  (2) frontend then calls `add_documents` / `documents/add` with `items=[minio urls]` and `params={content_hashes:{url:hash}, file_sizes:{url:size}, auto_index, chunk_preset_id, chunk_parser_config, parent_id, source_paths:{url:"rel/path"}}`.
  `_validate_uploaded_document_items` (router L182-L209) enforces items are MinIO URLs and have a content hash.
- "Import from workspace" (our "from file library"): `POST /knowledge/files/import-workspace {kb_id, paths[]}` (router L1576-L1636) reads bytes of each workspace file
  (`read_workspace_file_bytes`), re-validates extension, hash-dedupes (409 per file), re-uploads to the KB bucket and returns the same shape as normal upload so the frontend continues with step (2).

---------------------------------------------------------------------------------------------------
## 3. Chunking

Entry: `PKG/knowledge/chunking/ragflow_like/dispatcher.py` `chunk_markdown(markdown_content, file_id, filename, processing_params)` L66-L76 —
all file types are first converted to markdown, then ONE dispatcher routes by `chunk_preset_id` (`_dispatch_markdown_parser` L45-L63).
Chunk engine version const: `CHUNK_ENGINE_VERSION = "ragflow_like_v1"` (presets.py).

### 3.1 Presets (`.../ragflow_like/presets.py` L9-L38 — quoted verbatim)
```python
DEFAULT_CHUNK_PRESET_ID = "general"
CHUNK_PRESETS = {
  "general":   {"label": "General",   "description": "通用分块：按分隔符和长度切分，适合大多数普通文档。"},
  "qa":        {"label": "QA",        "description": "问答分块：优先抽取问题-回答结构，适合 FAQ、题库、问答手册。"},
  "book":      {"label": "Book",      "description": "书籍分块：强化章节标题识别并做层级合并，适合教材、手册、长章节文档。"},
  "laws":      {"label": "Laws",      "description": "法规分块：按法条层级组织与合并，适合法律法规、制度规范类文本。"},
  "semantic":  {"label": "Semantic",  "description": "语义分块：利用嵌入和聚类算法进行语义切分，并自动增强标题上下文。"},
  "separator": {"label": "Separator", "description": "严格分隔：命中分隔符即切分，仅超长片段内部继续按长度切分。"},
}
```
Presets endpoint: `GET /knowledge/chunk-presets` -> `get_chunk_preset_options()` = `[{value,label,description}]` (router L1777).
Preset `general` is internally parser id `naive`. Unknown id -> falls back to `general`.
Default parser config per preset = **empty dict** (`get_default_chunk_parser_config` returns `{}`); the defaults live inside each parser:
| key | default | range in UI (`web/src/components/ChunkParamsConfig.vue`) |
|---|---|---|
| `chunk_token_num` | **512** | min 100, max 10000 (input-number) |
| `overlapped_percent` | **0** | 0-99 |
| `delimiter` | `"\n"` (escapes `\n \t \r \\` supported; backtick-wrapped custom delimiters e.g. `` `---` `` supported in naive_merge) | free text, only shown for QA-ish/separator |
| `language` (qa) | `"Chinese"` | — |
| `embed_model_id` | injected at index time from `system_options["embed_model"]` (only semantic uses it) | — |
Param merge priority (`resolve_chunk_processing_params` presets.py L98-L127): request > file.processing_params > KB.additional_params; `chunk_parser_config` is deep-merged in that order.
**Token counting is approximate** (`nlp.count_tokens`, nlp.py L49): `len(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text))` i.e. 1 English word/number = 1 token, 1 CJK char = 1 token. No tokenizer dependency. Port this as-is.

### 3.2 Algorithms (per preset)
- **general** (`parsers/general.py`): sections = split by `delimiter` (or per non-empty line for `\n`) -> `nlp.naive_merge(sections, chunk_token_num, delimiter, overlapped_percent)`
  -> `_ensure_chunk_token_limit`: hard limit = `max(N, int(N*1.5))` (`GENERAL_HARD_LIMIT_RATIO=1.5`), over-limit chunks go through `nlp.hard_split_by_token_limit(text, N, hard_limit_token_num)` (split on token boundaries, merge tail into previous span if the merged tail still <= hard limit).
  `naive_merge` (nlp.py L461-L522): greedy append of sections into the current chunk until `token_nums[-1] > N*(100-overlap)/100`, then start a new chunk whose beginning is the last `overlap%` characters of the previous chunk (character slice `prev[int(len(prev)*(100-overlap)/100):]`). Sections with < 8 tokens never get position tags appended.
  If `delimiter` contains backtick groups: pure custom-delimiter split, no merging.
- **separator** (`parsers/separator.py`): every delimiter-separated section is a chunk; only sections > N tokens are re-sliced by `_slice_text_by_tokens(text, N, overlap_tokens=int(N*overlap/100))` (char-by-char, token-bounded, with token-overlap backtrack).
- **book** (`parsers/book.py`): lines -> `remove_contents_table` (drops TOC) -> `make_colon_as_title` -> `bullets_category` picks one of `BULLET_PATTERN` groups (nlp.py L7-L46: Chinese 第X编/章/节/条, numeric 1.1.1, 一、(一), PART/Chapter/Section, markdown `#`..`######`) by sampling <=100 lines; if a bullet group matches -> `hierarchical_merge(bull, sections, depth=5)` (title-path-prefixed chunks) else `naive_merge`; then token cap via `hard_split_by_token_limit(chunk, N)`.
- **laws** (`parsers/laws.py`): normalizes markdown decoration (`#`, `-`, `**`), splits `第X条` heading from its body (`_ARTICLE_PATTERN = ^(第[零一二三四五六七八九十百千万0-9]+条)[\s　:：]*(.*)$`), `tree_merge(bull, sections, depth=3)` (章/节/条 tree, each leaf prefixed with its ancestors); docx with >1 heading-tree chunks uses `_docx_heading_tree`. Over-limit chunks: re-merge by line, then by sentence (`re.split(r"(?<=[。！？；;!?])")`), then hard split.
- **qa** (`parsers/qa.py`, 444 lines): extracts (question, answer) pairs and emits `"问题：{q}\t回答：{a}"` (or `Question:/Answer:` when `language=English`). Strategy order depends on extension (docstring L376-L388): 1) line prefixes `^(问题|答案|回答|user|assistant|Q|A|Question|Answer|问|答)[\t:： ]+`; 2) markdown headings as Q + body as A (fallback); 3) markdown 2-col tables; 4) tab/comma 2-col delimiter (csv always; txt first); 5) fallback every 2 lines = Q/A; then `_split_long_qa_chunks` keeps the question and splits the answer by paragraph/line.
- **semantic** (`parsers/semantic.py`, `utils/semantic_utils.py`, `utils/md_parser_utils.py`): markdown-it AST walk keeping a 6-level `title_stack`; every flushed chunk is emitted as `"{'#'*level} {title1|title2|...}[|Table|Part n]"` header line + content + `"-"*10` separator, i.e. **markdown header path is embedded in chunk text**; tables are kept intact (`extract_table_block`, `html_table_to_key_value`), images with captions handled by `_handle_image_caption`; over-length content split by `split_text_by_length_and_newline(content, max_length, embed_fn=...)` which uses sentence embeddings + `sklearn AgglomerativeClustering` with silhouette-score auto cluster count. (**Heavy: sklearn + embedding calls. Recommended to skip for v1.**)

### 3.3 Chunk records & metadata
`_build_chunk_records` (dispatcher.py L9-L42) returns per chunk:
```python
{"id": f"{file_id}_chunk_{idx}", "chunk_id": same, "content": text, "file_id", "filename", "chunk_index": idx, "source": filename,
 "start_char_pos": found_at, "end_char_pos": found_at+len(text),   # best-effort str.find in parsed markdown, may be None
 "start_token_pos": None, "end_token_pos": None, "extraction_result": None}
```
Empty chunks are skipped but `idx` continues from enumerate (gaps possible). No page numbers / header path / line ranges are stored. Retrieval later joins `filename` (as `metadata.source`) and file `chunk_count` from the file table (`KnowledgeFileRepository.get_chunk_sources_by_file_ids` repo L297-L313).
Stats written to file row after indexing: `chunk_count = len(chunks)`, `token_count = sum(count_tokens(chunk))` (`_calculate_chunk_stats` milvus.py L525).

---------------------------------------------------------------------------------------------------
## 4. Queue and task processing ("PG task table + arq wake-up")

### 4.1 Pattern in one paragraph
The **database row is the source of truth** and the arq/Redis message is only a *wake-up hint*. `Tasker.enqueue()` (`PKG/services/task_service.py` L130-L142 -> `_enqueue_task` L275-L297)
inserts a `tasks` row with `status="pending"` FIRST, then `publish_task(task_id)` does `arq_pool.enqueue_job("process_task", task_id)` (`task_queue_service.py` L19-L22).
If publishing fails the row stays `pending` and the **reconciler republishes it** (`publish_pending_tasks`). Duplicate arq messages are harmless because `process_task` does an atomic DB `claim`.
Handlers are **registered by name** (`PKG/services/task_registry.py` `TaskDefinition(task_type, module, function, failure_function)`), so a task can be rebuilt from its persisted `payload` after any restart:
`knowledge_ingest -> run_knowledge_ingest`, `knowledge_parse -> run_knowledge_parse`, `knowledge_index -> run_knowledge_index`, `knowledge_graph_index -> run_knowledge_graph`, `knowledge_virtual_folder_migration`, `dataset_generation`, `rag_evaluation`.
Each KB task type has `failure_function="fail_knowledge_file_task"` = domain convergence hook run in the same TX that fails/cancels the task (`knowledge_task_service.py` L24-L30).

### 4.2 Task table (`tasks`, `PKG/storage/postgres/models_business.py` L861-L888)
```
id String(32) pk (uuid4().hex) | name String(255) | type String(64) idx | status String(32) idx  pending|running|success|failed|cancelled
progress Float 0-100 | message Text | payload JSON | result JSON | error Text
cancel_requested Integer 0/1 | handler_version Integer (=1; mismatch -> task failed "Cannot rebuild")
dedupe_key String(64) | UniqueConstraint(type, dedupe_key) "uq_tasks_active_dedupe"   # sha256(f"{type}:{json(payload_match, sort_keys)}"), cleared when task goes terminal
attempt_count Integer | worker_id String(128) (owner = f"{worker_id}:{uuid4().hex}") | heartbeat_at | lease_expires_at
timeout_seconds Float default 21600 (env TASKER_DEFAULT_TIMEOUT_SECONDS=6h) | created_at/updated_at/started_at/completed_at
Index(status, lease_expires_at)
```
Terminal rows pruned to the latest 200 (`TaskRepository.prune_terminal(keep=200)`).

### 4.3 Constants
```python
# task_queue_service.py L9-L13
TASK_LEASE_SECONDS = 30.0; TASK_HEARTBEAT_SECONDS = 10.0; TASK_RECONCILIATION_SECONDS = 30.0
# task_service.py
PROGRESS_PERSIST_DELTA = 2.0          # progress writes throttled unless change >= 2 points or message changes
DURABLE_TASK_MAX_RUNNING = 4          # GLOBAL concurrency of durable tasks across all workers
TASKER_DEFAULT_TIMEOUT_SECONDS = 6*60*60
# run_worker.py WorkerSettings (L1616-L1632): max_jobs = env ARQ_MAX_JOBS (default 10), poll_delay=0.05, max_tries=2, retry_jobs=True,
#   job_timeout = env YUXI_JOB_TIMEOUT_SECONDS (default 3600), keep_result=60, functions=[process_agent_run, func(process_task, timeout=TASKER_DEFAULT_TIMEOUT_SECONDS+30)]
# knowledge_task_service.py
DOCUMENT_ACTION_BATCH_SIZE = 500; DOCUMENT_ACTION_RESULT_ITEM_LIMIT = 200   # result keeps only first 200 item samples
# knowledge_router.py: MAX_DIRECT_DOCUMENT_ACTION_FILE_IDS = 1000 (explicit parse/index of >1000 ids rejected with 400 "请使用待处理状态入口提交全量后台任务")
```

### 4.4 `process_task(ctx, task_id)` (task_service.py L408-L503) step by step
1. load row; return if missing/terminal; if `cancel_requested` -> `request_cancel` (+failure hook) and return.
2. `owner = f"{ctx.worker_id or 'task-worker'}:{uuid4().hex}"`; `repo.claim(task_id, worker_id=owner, lease_seconds=30, max_running=4)` (`task_repository.py` L181-L228):
   inside ONE TX: `pg_advisory_xact_lock(hashtext('durable-task-capacity'))`, `count(status='running') >= 4` -> **not claimed (stays pending)**; else lock row `FOR UPDATE`, require `status=='pending'`, if cancel_requested -> mark cancelled; else set
   `status=running, worker_id, heartbeat_at=now, lease_expires_at=now+30s, attempt_count+=1, started_at, message="任务开始执行"`.
3. Spawn `execution = asyncio.create_task(handler(TaskContext))` and `heartbeat = _heartbeat_task(...)`: every 10s `renew_lease(task_id, worker_id, 30s)` -> returns `(renewed, cancel_requested)`; not renewed => `context._request_cancel("lease_lost")` + cancel execution; cancel_requested => cancel execution (reason "cancelled").
4. `asyncio.wait({execution}, timeout=timeout_seconds)`; on timeout cancel + `_finish_task_failure(status="failed", message="任务执行超时")`.
5. Success -> `finish_owned(status="success", message="任务已完成", result=result, before_finish=success_handler, before_cancel=failure_handler)` — only succeeds if still **live owner** (`status=='running' and worker_id==owner and lease_expires_at>now`), all within a row lock.
6. `CancelledError`: reason `cancelled` -> status `cancelled`; reason `lease_lost` -> do nothing (someone else owns it); else (worker shutdown) -> `release_interrupted_owner` -> **failed** with error `"worker_shutdown: worker 停止时任务中断"`. Other exceptions -> `status=failed, message="任务执行失败", error=str(exc)`.
7. `finally`: cancel heartbeat; `_publish_pending_after_slot_release()` -> `publish_pending_tasks(limit=4)` so a freed slot is immediately filled by the oldest pending tasks.
**Interrupted work is never auto-retried** ("执行中断，无法安全自动恢复"): reconciler marks tasks whose lease expired as `failed` (`reconcile_expired_leases`, repo L378-L405, error `"worker_lease_expired: 执行 worker 的 lease 已过期，任务副作用结果未知"`) and the failure hook flips files `parsing->error_parsing`, `indexing->error_indexing` (so the user sees a retry action). Retry = user clicks retry / "index pending".

### 4.5 Reconciliation loop
`PKG/services/run_worker.py::_reconcile_durable_tasks_forever` (L1514-L1528): every 30s call `reconcile_and_publish_tasks()` = `reconcile_expired_leases(before_fail=finalize_task_failure)` -> `publish_pending_tasks(limit=200)` (oldest first; unknown type/version -> fail the pending row; cancel_requested pending -> cancel) -> `prune_terminal()`. Also runs once at worker startup (`_worker_startup`). A Redis health key `yuxi:worker:health:durable-task-reconciliation-v1` (TTL 65s) lets readiness checks verify a reconciler is alive.

### 4.6 TaskContext (task_service.py L55-L127) — what handlers use
`set_progress(pct, msg)` (throttled), `set_message`, `set_result`, `raise_if_cancelled()` (reads `check_control` = (owns_lease, cancel_requested); raises `asyncio.CancelledError`), `run_owned_transaction(op)`. Every write is guarded by `update_owned(... worker_id)` so a stale worker can't write.
Handlers call `await context.raise_if_cancelled()` **between files** (cooperative cancel granularity = one file).

### 4.7 Per-file processing is SERIAL inside one task; file-level ownership lease
`run_knowledge_ingest` (knowledge_task_service.py L33-L164) — payload `{kb_id, items[minio urls], params, content_type, operator_id}`:
```
phase 0  progress 5 "准备处理文档"
phase 1  for each item (serial): add_file_record(...)           progress 5 + i/total*25 "[1/3] 添加记录 i/n"   -> status uploaded; failures recorded in processed_items (error_type add_failed/timeout)
phase 2  for each added file (serial): parse_file(kb, file_id, operator_id, processing_task_id=task.id, processing_owner=worker)   progress 30 -> (60 if auto_index else 95) "[2/3] 解析文件 i/n"
phase 3  if params.auto_index: for each parsed file: update_file_params(indexing_params) ; index_file(...)  progress 60 -> 95 "[3/3] 入库文件 i/n"
end      result summary {kb_id,item_type,submitted,failed,items[]}; progress 100; if failed_count: raise RuntimeError("文件处理完成，失败 N 个") -> task status failed (but files individually have their own status)
```
`indexing_params` = only `chunk_preset_id`, `chunk_parser_config` from request params. One file's failure never aborts the batch (try/except per file). `parse_file` / `index_file` each do the compare-and-set claim described in §1.4 and pass `processing_task_id/processing_owner` so any later write is rejected if the Task lost its lease -> `asyncio.CancelledError("File processing owner was lost")`.
`run_knowledge_parse` / `run_knowledge_index` (`_run_file_ids` L195-L248 for explicit ids; `_run_pending_files` L251-L329 for "scope=pending": loops `knowledge_base.list_document_file_ids_by_statuses(kb_id, statuses, after_file_id, limit=500)` keyset-paginated until empty, so it handles unbounded files; "index pending" statuses = `parsed, error_indexing`; "parse pending" = `uploaded`).
"Pending" submissions use `tasker.enqueue_unique_by_payload(... payload_match={"kb_id","scope":"pending","action"})` -> returns `(task, created)`; if a pending-scope task is already queued/running for the KB the API replies "已有待{解析|入库}任务正在执行" (dedupe via unique index).
Explicit single/multi-file actions use plain `tasker.enqueue` (no dedupe).
Embedding inside `index_file`: chunks embedded in batches of `MILVUS_CHUNK_EMBED_BATCH_SIZE = 200` texts; each call goes through the model's `abatch_encode(texts, batch_size=model.batch_size or 40)`; PG chunk rows are upserted first, then vectors upserted with 3 attempts and 1.0s delay (`MILVUS_CHUNK_UPSERT_ATTEMPTS=3`, `MILVUS_CHUNK_UPSERT_RETRY_DELAY_SECONDS=1.0`). Re-index = delete old chunks (vector store + graph + chunk rows, zero `chunk_count/token_count`) then insert.

### 4.8 What the frontend polls (no WebSocket in Yuxi)
- KB detail page auto-refresh (`web/src/stores/database.js` L11-L16, L705-L780): chained `setTimeout` (waits for previous round to return). **Base 10s** (`AUTO_REFRESH_POLL_INTERVAL_MS=10000`, aligned with backend stats cache TTL 10s), doubles while `stats.processing_count` is unchanged up to **30s** (`AUTO_REFRESH_MAX_INTERVAL_MS`), and **auto-stops after 6 consecutive unchanged polls** (`AUTO_REFRESH_STALE_POLLS_LIMIT=6`); resets to 10s when the processing count changes. Each tick calls `GET /knowledge/databases/{kb_id}` (stats) + `GET /knowledge/databases/{kb_id}/documents?page&page_size&status&parent_id` in parallel. Auto-refresh is enabled (`enableAutoRefresh('auto')`) after any add/parse/index submit and whenever stats show `processing_count>0`; user can toggle manually (manual override suppresses auto-enable). After every submit a one-off `delayedRefresh()` (sleep 1s, refresh info+files).
- Task Center (admin-only, `/api/tasks`): `web/src/stores/tasker.js` L221-L232 poll `interval = min(5000 * 2**min(failures,3), 30000)` ms (5s base, exponential on errors), skipped while `document.visibilityState === 'hidden'`, only while drawer open or active tasks exist. Endpoints (`backend/server/routers/system_task_router.py`): `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `DELETE /tasks/{id}`. **All admin-only (`get_admin_user`)** — so ordinary end-users in Yuxi only see per-file statuses; for kunxiaozhi (all users) we need per-KB file status endpoints, not a global task center.
- After submit the store registers the task in the tasker store for the drawer: `registerTask({task_id, name, task_type:'knowledge_ingest', payload:{kb_id,count,content_type}})`.

### 4.9 Client-side batch upload behavior
`FileUploadModal.vue`: `MAX_UPLOAD_CONCURRENCY = 10` (L475) — antd `a-upload-dragger` with custom `customRequest` pushing into `uploadQueue`; `processUploadQueue()` runs up to 10 XHR uploads (`POST /knowledge/files/upload?kb_id=`) at once; aggregate progress card shown when `totalUploadCount >= 10` (hides per-file list). Each upload returns `{file_path, content_hash, size, same_name_files}`; "same name" files show a conflict panel (download existing / delete existing then re-add). After all uploads finish the user clicks the primary button -> `chunkData()` (L1145) builds `items/content_hashes/file_sizes/source_paths` and calls ONE `POST /knowledge/databases/{kb}/documents` (-> one `knowledge_ingest` Task for the whole batch).
Folder upload uses `<a-upload-dragger directory>` and `webkitRelativePath` as `source_paths[url]` (display path stored in `filename`, e.g. `a/b/c.pdf`; tree built from it as virtual folders; real folders via `POST /databases/{kb}/folders`).

---------------------------------------------------------------------------------------------------
## 5. Retrieval

### 5.1 Vector store schema (Milvus, one collection per KB; collection name = `kb_id`; `milvus.py` L401-L450)
```
id          VARCHAR(100) PK            # == chunk_id
content     VARCHAR(65535) enable_analyzer=True, analyzer_params={"type": "chinese"}   # CONTENT_ANALYZER_PARAMS (L38)
chunk_id    VARCHAR(100)
file_id     VARCHAR(100)
chunk_index INT64
embedding   FLOAT_VECTOR(dim = embedding_info.dimension or 1024)
content_sparse SPARSE_FLOAT_VECTOR     # filled by Function(name="content_bm25", BM25, in=["content"], out=["content_sparse"])
dense index : IVF_FLAT, metric COSINE, nlist 1024            (VECTOR_METRIC_TYPE="COSINE")
sparse index: SPARSE_INVERTED_INDEX, metric BM25, params {"inverted_index_algo": "DAAT_MAXSCORE"}
collection.description = f"Knowledge base collection for {kb_id} using {model_name}"; if the embedding model id isn't contained in description on load -> collection DROPPED and recreated (index rebuilt from scratch!).
```
Chinese BM25 tokenization is Milvus' built-in analyzer `{"type":"chinese"}` (jieba-based) executed server-side. **For Qdrant we need to supply our own sparse vectors** (jieba tokenization + BM25/TF weights or `fastembed` BM25) – flag as a porting task.
No metadata beyond `file_id/chunk_id/chunk_index` is stored in the vector DB; filename/chunk_count are joined back from the file table at query time; `file_name` filter is resolved to a `file_id in [...]` expression via a SQL ILIKE on filenames (`_build_file_name_expr` L662-L675, `list_file_ids_by_filename_contains` limit 10_000; no matching file -> `file_id == "__no_matching_file__"`).

### 5.2 Query parameter set (`MilvusRetrievalConfig`, `milvus.py` L89-L268) — this is exactly what the UI renders (`GET /knowledge/databases/{kb}/query-params` -> `{params:{type,options:[{key,label,type,default,min,max,step,description,options,depend_on}]}}`)
| key | type | default | range | notes |
|---|---|---|---|---|
| `search_mode` | select | `vector` | `vector` / `keyword`(BM25) / `hybrid` | labels 向量检索 / BM25 全文检索 / 混合检索 |
| `final_top_k` | number | **10** | 1-100 | 最终返回 Chunk 数 |
| `similarity_threshold` | number | **0.0** in UI config (code fallback `merged_kwargs.get(..., 0.2)` if key absent) | 0-1 step 0.1 | applied only to vector & hybrid hits (`score < threshold -> skip`) |
| `bm25_top_k` | number | 50 | 1-200 | BM25 candidates |
| `vector_weight` | number | **0.7** | 0-1 | hybrid weight |
| `bm25_weight` | number | **0.3** | 0-1 | hybrid weight |
| `bm25_drop_ratio_search` | number | 0.0 | 0-1 | sparse term drop ratio |
| `include_distances` | boolean | True | | |
| `use_graph_retrieval` | boolean | False | | enables §7.3 |
| `graph_entity_top_k` / `graph_triple_top_k` | number | 10 / 10 | 1-100 | depend_on use_graph_retrieval |
| `graph_max_nodes` | number | 10000 | 100-50000 | 2-hop subgraph cap |
| `graph_top_k` | number | 20 | 1-200 | chunks recalled via PPR |
| `graph_weight` | number | 1.0 | 0-5 | RRF weight of graph ranking |
| `ppr_damping` | number | 0.85 | 0.1-0.99 | |
| `use_reranker` | boolean | False | | |
| `reranker_model` | select | "" | options = admin rerank models | depend_on use_reranker |
| `recall_top_k` | number | **50** | 10-200 | candidates before rerank/graph fusion; `max(recall_top_k, final_top_k)` |
Persistence: defaults are materialised into `knowledge_bases.query_params = {"options": {...}}` at creation (`get_default_query_params`). `PUT /databases/{kb}/query-params` saves new defaults (manage perm). Each call merges `{**kb.query_options, **kwargs}` — per-request `meta` overrides persisted. Frontend UI (`SearchConfigPanel.vue`) renders every option generically (select/boolean→启用/关闭 select/number) filtering by `depend_on`, saves snapshot only if changed.
The agent's `query_kb` passes ONLY `file_name` (optional); everything else comes from the KB's persisted query params.

### 5.3 Search pipeline (`MilvusKB.aquery`, milvus.py L871-L1070)
```
recall_top_k = max(recall_top_k, final_top_k) if (use_reranker or use_graph_retrieval) else final_top_k
vector :  embed(query) -> collection.search(anns_field="embedding", metric COSINE, params {"nprobe":10}, limit=recall_top_k, expr=file_expr); drop hits with similarity < similarity_threshold
keyword:  collection.search(data=[query_text], anns_field="content_sparse", {"metric_type":"BM25","params":{"drop_ratio_search":drop}}, limit=bm25_top_k)  # no threshold
hybrid :  hybrid_search(reqs=[AnnSearchRequest(dense, limit=recall_top_k), AnnSearchRequest(bm25, limit=bm25_top_k)], rerank=WeightedRanker(vector_weight, bm25_weight), limit=recall_top_k); threshold on fused score
```
**Hybrid fusion = Milvus `WeightedRanker(0.7, 0.3)` (weighted score sum with Milvus' internal per-ranking normalisation), NOT RRF.** RRF (`rrf_k = 60.0`, score = weight/(60+rank)) is only used to fuse *graph* results with chunk results (`_fuse_chunk_rankings`, L1182-L1210; weights chunk=1.0, graph=`graph_weight`).
Then: `_hydrate_chunk_sources` (adds `metadata.source = filename`, `metadata.chunk_count`; **drops chunks whose file_id no longer exists in the file table** => orphan-vector safety) -> if `use_reranker`: `get_reranker(spec).acompute_score([query, [chunk texts]], normalize=True)`, sets `chunk["rerank_score"]`, sorts desc (on failure: logs and falls back to un-reranked order; if `reranker_model` missing raises ValueError) -> `[:final_top_k]`.
There is **no minimum rerank threshold** in Yuxi (only the similarity threshold pre-rerank).
Embedding calls run in a thread-offload semaphore: `MILVUS_QUERY_OFFLOAD_LIMIT = 8` concurrent blocking Milvus/embedding calls.

### 5.4 Response shapes
- UI "retrieval test" (`POST /knowledge/databases/{kb}/query-test` body `{query, meta:{...query params}}`) returns the raw list:
```json
[{"content":"…chunk text…","score":0.83,"distance":0.83,"hybrid_score":0.83,"rerank_score":0.91,"graph_score":0.12,"fusion_score":0.03,"fusion_sources":["chunk","graph"],
  "metadata":{"source":"文件名.pdf","chunk_id":"file_ab12cd_chunk_3","file_id":"file_ab12cd","chunk_index":3,"chunk_count":27}}]
```
  `QuerySection.vue` renders per hit: `#n`, 相似度 `(score*100).toFixed(2)%`, 重排序分数, chunk text, 来源/文件ID/块索引/距离, a raw-JSON toggle (braces icon), sample-question suggestions, `Enter` to search (`press-enter.prevent`), 清空 button. No per-result click-through in this component.
- Agent `query_kb` returns `SearchOutputSchema` (`PKG/knowledge/schemas.py`): `{kb_id, results:[{id: chunk_id, kb_id, file_id, content, metadata:{source, chunk_id, chunk_index, chunk_count, score, distance,...}}]}` (path-like metadata keys `filepath/parsed_path/path/markdown_file` stripped by `build_search_output`, base.py L611-L648).

---------------------------------------------------------------------------------------------------
## 6. Agent tools for knowledge bases

Source: `PKG/agents/toolkits/kbs/tools.py` (495 lines); schemas `PKG/knowledge/schemas.py`; scoping `PKG/agents/backends/knowledge_base_backend.py`; tutorial skill `PKG/agents/skills/buildin/knowledge-base/SKILL.md`.
`get_common_kb_tools()` (L28-L45) returns **7 tools**: `list_kbs, get_mindmap, query_kb, find_kb_document, open_kb_document, search_file, download_kb_file`. (There is NO write/“save to KB” tool in Yuxi.)
All are registered with `@tool(category="knowledge", tags=["知识库"], display_name=..., args_schema=...)` (LangChain `ToolRuntime` injected; model-visible description = function docstring). The docstrings below are **prompts** (Chinese; quoted verbatim).

### 6.1 Tools

**list_kbs** (display "列出知识库"; arg `dummy: str = ""` only because ToolRuntime injection needs at least one arg)
```
列出当前用户可访问的知识库列表

返回用户基于权限可访问的知识库名称列表。这个列表是根据用户的角色和部门信息过滤后的结果，
但不包括用户在当前对话中未启用的知识库。
```
Returns `[{"kb_id","name","description" (or "无描述")}]`; strings `"无法获取用户信息"` / `"当前没有可访问的知识库"` on failure.

**query_kb** (display "检索知识库"; args = `SearchInputSchema`: `kb_id: str` "知识库资源 ID，也就是 kb_id"; `query_text: str` "检索关键词，应提炼为有助于召回答案的关键词或短语"; `file_name: str|None` "可选文件名关键词过滤，非必要不要使用")
```
在指定知识库中检索内容

当用户需要查询具体内容时使用此工具。kb_id 是知识库资源 ID，也就是 kb_id；返回结果中的
file_id 可继续用于 find_kb_document 或 open_kb_document。
```
Flow: validate kb_id in the **visible set**, then `knowledge_base.retrieve(kb_id, query_text, **({"file_name":...} if file_name else {}))` -> `aquery(..., agent_call=True)` + `build_search_output` => `SearchOutputSchema {kb_id, results:[{id, kb_id, file_id, content, metadata{source,chunk_id,chunk_index,chunk_count,score,...}}]}`. Errors returned as strings `"请提供 kb_id"`, `"请提供查询内容"`, `"检索失败: …"`.

**open_kb_document** (display "打开知识库文档"; `OpenInputSchema`: `kb_id`, `file_id` "要打开的文件 ID", `line: int|None ge=1` "可选，1-based 起始行号", `offset: int|None ge=0` "可选，0-based 起始偏移；line 优先于 offset", `window_size: int = 1800 (1..2000)` "读取窗口行数")
```
按行窗口打开知识库文档原文

当 query_kb 返回的片段不足以回答问题，或需要查看某个文档的上下文时使用。
kb_id 是知识库资源 ID，也就是 kb_id；file_id 是知识库文件 ID。
```
Logic: `start_offset = int(line)-1 if line is not None else int(offset or 0)`; reads the **parsed markdown** (`markdown_file` in MinIO), `base._build_open_file_window(content, offset, limit)` (base.py L590-L608):
```python
lines = content.splitlines(); total = len(lines); start = min(max(offset,0), total); window = min(max(limit,1), 2000)
selected = lines[start:start+window]; end = start + len(selected)
return {"start_line": start+1 if selected else 0, "end_line": end, "total_lines": total, "offset": start, "window_size": window,
        "has_more_before": start > 0, "has_more_after": end < total, "next_offset": end if end < total else None,
        "content": "\n".join(f"{start+i+1:6d}\t{line}" for i,line in enumerate(selected))}   # cat -n style, 6-wide right-aligned line numbers + TAB
```
Output = `OpenOutputSchema` (`kb_id,file_id,start_line,end_line,total_lines,offset,window_size,has_more_before,has_more_after,next_offset,content`). Exempted from the agent filesystem middleware's big-tool-result eviction (`composite.py` L17-L19 `_TOOL_RESULT_EVICTION_EXEMPT_TOOLS |= {"open_kb_document"}`) because it paginates itself. Errors: `"文件不存在: {id}"`, `"文件 {id} 是文件夹"`, `"文件 {id} 没有解析后的 Markdown 内容"`.

**find_kb_document** (display "定位文档内容"; `FindInputSchema`: `kb_id`, `file_id` "要检索的文件 ID", `patterns: list[str]` "关键词或正则模式列表，至少提供一个", `use_regex: bool=False` "是否将 patterns 作为正则表达式处理", `case_sensitive: bool=False`, `max_windows: int=5 (1..20)` "最多返回的上下文窗口数量", `window_size: int=80 (1..200)` "每个上下文窗口的行数")
```
在已知知识库文件内做关键词或正则定位。

当 query_kb 已找到候选文件，但需要在该文件内定位术语、指标、章节或实体时使用。
```
Logic (`_build_find_file_windows` base.py L651-L711): line-wise match over parsed markdown (any pattern, case-insens. by default; regex `re.compile(p, IGNORECASE)`); for each matching line not already covered, build a window of `window_size` lines centred on it (`half = size//2`, clamp to file bounds), merge by skipping lines `< covered_until`, stop at `max_windows`. Output `FindOutputSchema {kb_id,file_id,semantic:false,match_mode:"keyword"|"regex",total_matches (= number of matched LINES),windows:[{start_line,end_line,matched_lines:[1-based],content (numbered like open)}]}`.

**search_file** (display "搜索知识库文件"; args: `kb_name: str|None` "知识库名称，为空时搜索所有知识库", `query: str|None` "搜索关键词，为空时返回所有文件", `offset: int=0 (ge 0)` "偏移量，从 0 开始", `limit: int=300 (1..5000)` "返回数量限制，默认 300")
```
搜索知识库中的文件

当用户需要查找特定文件时使用此工具。可以指定知识库名称和搜索关键词。
如果不指定知识库，将搜索所有可访问的知识库。
如果不指定搜索关键词，将返回所有文件。
(Args/Returns sections list kb_name/query/offset/limit; "Returns: 匹配的文件列表和分页信息")
```
Rejects both empty (`"请提供知识库名称或搜索关键词，不能同时为空"`). Filename-only, case-insensitive `LIKE %q%` (`list_file_ids_by_filename_contains`/`search_files`, scan limit `KB_FILE_SEARCH_SCAN_LIMIT`); across KBs merges by `updated_at desc`. Output `{"files":[{kb_id,kb_name,file_id,filename,file_type,status,created_at,updated_at,file_size}], "total","offset","limit","has_more"}` (`manager.search_document_files` L857-L929).

**get_mindmap** (arg `kb_name`) and **download_kb_file** (`kb_id,file_id,save_as`; copies the original binary into the sandbox `{workdir}/outputs/` and returns `{virtual_path, filename, media_type, size_bytes, saved_as}`) — mindmap/sandbox-specific, **skip** (download could be reconsidered only if kunxiaozhi has a sandbox/file-output concept).

### 6.2 Scoping (per user / per agent)
- `resolve_visible_knowledge_bases_for_context(context)` (`knowledge_base_backend.py`): `knowledge_base.get_databases_by_uid(context.uid)` (permission-filtered: `resolve_knowledge_base_permission != NONE`) -> list of `{kb_id,name,description,kb_type}`; then **if the agent config has `context.knowledges` selection** (an agent-level allow-list of kb_ids; default `"all"`), intersect with it. Result cached on the context as `_visible_knowledge_bases`.
- **Every** tool re-validates its `kb_id` against that visible list via `_find_query_target` — error strings `"无法获取当前会话可访问的知识库"` and `f"知识库资源 '{kb_id}' 不存在或当前会话未启用"` (the model can never address a KB it wasn't given, even by guessing ids). `search_file`/`get_mindmap` take KB *names* and resolve them within the same visible set.
- Agent config UI: `agents/context.py` L238-L247 field `knowledges` (name "知识库", description "知识库列表，可以在左侧知识库页面中创建知识库。默认选择当前用户可访问的全部知识库。", default `"all"`).
- Chat citation rendering: `PKG/agents/middlewares/summary.py` L41, L625-L720 compresses `query_kb` tool results for context-window summarising (keeps keys `id,kb_id,file_id,title,source,score,distance` + metadata `source,filename,title,chunk_index,score,rerank_score,hybrid_score,graph_score,distance`, up to 8 results, content previews clipped to a char budget).

### 6.3 Prompts that teach the model how to use them
(a) Built-in skill `knowledge-base` (SKILL.md) — loaded on demand; `tool_dependencies: [list_kbs, query_kb, find_kb_document, open_kb_document, get_mindmap, search_file, download_kb_file]`. Key text (verbatim excerpts):
```
## 操作流程
1. 需要先确认当前会话有哪些知识库可用；不确定时调用 `list_kbs`。
2. 针对用户问题选择最相关的知识库，使用 `query_kb` 检索。
3. 检索结果的 `metadata.chunk_count == 1` 表示文件只有一个分片；除非需要原文行号或额外上下文，否则无需打开。大于 1 且当前片段不足以回答时，使用返回的 `file_id` 调用 `open_kb_document` 查看上下文。字段缺失或为 0 表示未知，不据此判断文档完整。
4. 如果用户要求定位术语、指标、章节或原文证据，使用 `find_kb_document` 在候选文档内查找。
5. 当用户关心知识库结构、文件分类或知识框架时，使用 `get_mindmap`。
## 关键约束
- 只能访问当前会话配置和用户权限允许的知识库。
- 不要编造 `kb_id` 或 `file_id`；优先从 `list_kbs` 和 `query_kb` 的返回结果中获取。
- 回答需要可追溯时，应说明依据来自哪个知识库、文件或检索片段。
- Dify 等外部只读知识库可能只支持检索，不一定支持打开全文或文档内查找；遇到工具返回限制说明时，应如实告知用户。
```
(b) Citation prompt `SOURCE_CITE_PROMPT` (`PKG/agents/buildin/chatbot/prompt.py` L18-L31; marked "效果不好，暂时不启用" = NOT enabled in Yuxi):
```
<| 引用来源 |>
当你提供的信息来自于用户上传的文件或者知识库中的内容时，请务必在回答中注明信息来源，以增加答案的可信度和透明度。
对于论断内容，需要添加参考文献信息，将对应段落的末尾添加 cite 信息。使用
<cite source="$SOURCE" type="$TYPE">$INDEX</cite>
- $SOURCE：信息来源，可以是文件名，可以是url
- $TYPE：引用类型，可以是 "file"、"url"，... 对于用户上传的文件或者知识库中的内容应该使用 "file"
- $INDEX：引用索引，应该从 1 开始
比如 <cite source="食品工艺学.pdf" type="file">1</cite>
```
(c) Base prompt `<| 内部执行约束:重要 |>`: do not volunteer internal details such as "工作区、文件系统、知识库路径、工具调用方式" unless asked.
(d) Research sub-agent preset (`PKG/agents/presets/subagents/research_explorer.py`): "围绕单个子问题多轮检索网页与知识库，交叉验证后返回带引用的结构化发现。"
Recommended typical call chain to teach: `list_kbs -> query_kb(kb_id, keywords) -> (chunk_count>1 and insufficient) open_kb_document(file_id, line=…) / find_kb_document(file_id, patterns) -> answer with source filenames`; `search_file` when the user names a file.

---------------------------------------------------------------------------------------------------
## 7. Knowledge graph

Source: `PKG/knowledge/graphs/` (`milvus_graph_service.py` 1228 lines, `milvus_graph_vector_store.py`, `graph_utils.py`, `extractors/{base,llm,factory}.py`), repos `PKG/repositories/knowledge_graph_repository.py` / `knowledge_chunk_repository.py`, router `backend/server/routers/graph_router.py` + `knowledge_router.py` L454-L607.
The graph is **opt-in per KB and built manually** ("开始索引" button), never automatically at upload.

### 7.1 Configuration (locked per KB)
`POST /knowledge/databases/{kb}/graph-build/config` body `{extractor_type:"llm", extractor_options:{model_spec, concurrency_count (1..1000, default 1), timeout_seconds (>0, <=600, default 60), schema (free text), model_params}}`
-> stored in `knowledge_bases.additional_params["graph_build_config"] = {locked:true, extractor_type, extractor_options, created_at, created_by[, updated_*]}` (`configure`, service L191-L223). Extractor *type* is locked once set (409 on change); model/schema can be edited ("only affects later builds"). Custom full prompt is rejected ("请使用 schema 配置抽取约束").
For us: model is admin-global, so only expose `schema` (optional) + a build button; `concurrency_count` = admin setting.

### 7.2 Extraction (one LLM call PER CHUNK — no chunk batching)
Prompt (`extractors/llm.py` L12-L26, verbatim; the model must return strict JSON, parsed with `json_repair.loads`; optional `SCHEMA_INSTRUCTION = "抽取 Schema 约束：\n{schema}\n"` appended; user text appended as `"\n\n文本：\n{chunk.content}"`):
```
请从下面文本中抽取实体和实体关系，返回严格 JSON，不要输出解释。
JSON 格式：
{
  "relations": [
    {
      "source": {"text": "实体文本", "label": "实体类型", "attributes": [{"text": "属性值", "label": "属性名称"}]},
      "target": {"text": "实体文本", "label": "实体类型", "attributes": [{"text": "属性值", "label": "属性名称"}]},
      "text": "关系显示文本",
      "label": "关系类型"
    }
  ]
}
```
Call: `select_model(model_spec, timeout=60, model_params).call(prompt, stream=False)`.
Normalisation (`extractors/base.py` `normalize_extraction_result`): entities deduped by `(normalize_entity_name(text), label)` with attributes unioned; relation endpoints may be objects or refs; entity text/normalized-name > 512 chars or label > 128 chars -> **entity dropped (not error)** and relations touching it dropped; relation label > 256 dropped; relation `text` empty -> ValueError; default relation label `"RELATED_TO"`, default entity label `"Entity"`. `normalize_entity_name = " ".join(text.strip().lower().split())`.
Ids: `entity_id = hashstr(f"{kb_id}:{normalized_name}:{label}", 32)`, `triple_id = hashstr(f"{kb_id}:{src_norm}:{src_label}:{rel}:{tgt_norm}:{tgt_label}", 32)` (graph_utils.py L19-L35) => idempotent merges across chunks/files.
Retry: `GRAPH_EXTRACTION_MAX_ATTEMPTS = 3`, delays `GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS = (2.0, 10.0)`; per-chunk state in `knowledge_chunks.graph_extraction_details {status: pending|failed|..., attempt_count, last error}`; `extraction_result` JSON cached on the chunk so a rebuild after a write failure doesn't re-call the LLM.

### 7.3 Build pipeline (`MilvusGraphService.build_pending_chunks`, service L230-L510; task type `knowledge_graph_index`, deduped per KB by `enqueue_unique_by_payload(payload_match={"kb_id"})`; 409 "该知识库已有正在运行的图谱构建任务")
Producer/consumer with bounded `asyncio.Queue`s inside ONE durable task:
1. fetch pending chunks (`graph_indexed = false`) in pages of `fetch_size = max(100, min(workers*2, 1000))` into `extraction_queue(maxsize=workers*2)`;
2. `workers = concurrency_count` extraction coroutines: LLM extract (+3 attempts) -> push chunk_id to `write_queue`;
3. write workers: `write_chunk_graph` (Neo4j MERGE `Chunk` node (props chunk_id,file_id,kb_id,chunk_index,content_preview[:300],start/end_char_pos), `Entity` nodes with `MENTIONS` edges Chunk->Entity, `RELATION` edges Entity->Entity with triple_id/text/extractor_type) + PG `upsert_chunk_graph` (entities/triples/mentions rows) + mark chunk `graph_structure_indexed=true` with `ent_ids`;
4. vector worker: claims pending entity/triple rows in batches (`GRAPH_VECTOR_BATCH_SIZE=100`, lease `GRAPH_VECTOR_LEASE_SECONDS=300`, lock token) -> embeds text (entity `content = normalized_name`; triple `content = f"{src_norm} → {rel} → {tgt_norm}"`) -> Milvus upsert into `{kb_id}_entity` (`id, content, embedding, content_sparse`) / `{kb_id}_triple` (`id, content, source_id, target_id, embedding, content_sparse`) -> row `vector_status` pending -> processing (claimed with lock token/lease) -> `indexed` else `failed` with `vector_attempt_count/vector_last_error/vector_next_retry_at`; `finalize_graph_indexed_chunks` flips chunk `graph_indexed` once all its entities/triples are vector-indexed.
5. progress reported every 5s (`GRAPH_BUILD_LOG_INTERVAL_SECONDS`) via `context.set_progress`; cancellation checked via `context.raise_if_cancelled()` in every loop.
Maintenance endpoints: `GET graph-build/status` (returns `total_chunks, pending_chunks, indexed_chunks, structured_chunks, extraction_counts, vector_counts, entity_count, relationship_count, build_task_status (pending|running|completed|failed), build_task_progress, locked, config`), `GET graph-build/failed-chunks?limit=10` (samples), `POST graph-build/reset` (`clear_extraction_result`, `clear_config`), `POST graph-build/reconcile` (`reconcile_mode: failed|all_vectors` -> task action `reconcile` re-embeds failed/all vectors then builds pending). File deletion/re-index removes that file's graph via `delete_file_graph` first (`milvus.py::delete_file_chunks_only`).
Entity/triple PG tables: see §1 (`knowledge_graph_entities` unique `(kb_id, normalized_name, label)`, `knowledge_graph_entity_mentions (entity_id, chunk_id)`, `knowledge_graph_triples`, `knowledge_graph_triple_mentions (triple_id, chunk_id, text, extractor_type)`, with `vector_status/vector_attempt_count/vector_last_error/vector_next_retry_at/vector_locked_until/vector_lock_token`).

### 7.4 Graph-enhanced retrieval (`MilvusKB._retrieve_graph_chunks`, milvus.py L1072-L1130; enabled by query param `use_graph_retrieval`)
1. entity vector search `top_k = graph_entity_top_k (10)` and triple vector search `top_k = graph_triple_top_k (10)` over the query embedding (dense COSINE, `nprobe 10`);
2. **seed weights** (`_build_graph_seed_weights` L1132-L1168): each entity hit adds `score*1.0` to its entity; each triple hit adds `score*0.8` to BOTH endpoint entities; entities mentioned by the already-retrieved chunks (`chunk.ent_ids`) add `chunk_score*0.3`; normalised to sum 1;
3. sub-graph: Cypher `MATCH (seed:Entity)-[*1..2]-(n)` limited to `path_limit = max_nodes*4` paths, `max_nodes = graph_max_nodes (10000)`;
4. PPR (`rank_chunks_by_ppr` service L918-L970): build undirected `networkx.Graph`, personalisation vector = seed weights on entity nodes, `nx.pagerank(alpha=clamp(ppr_damping 0.85, 0.1, 0.99), personalization=...)`, rank only **Chunk nodes**, take `graph_top_k (20)`;
5. fetch those chunk rows from PG -> chunks with `graph_score`; fuse with the dense/hybrid chunk list by **RRF** (`rrf_k = 60`, weights chunk 1.0 / graph `graph_weight` 1.0) -> `fusion_score`, `fusion_sources:["chunk","graph"]`; then hydrate sources, optional rerank, cut to `final_top_k`.
There are no LightRAG-style local/global/hybrid/naive/mix modes in this implementation — only this single PPR-based mode toggled by `use_graph_retrieval`.
Port note for Mongo: entities/triples/mentions collections + an in-memory 2-hop BFS limited to `graph_max_nodes` + `networkx.pagerank` (or a tiny pure-python power iteration) is sufficient; vectors for entity/triple names go to Qdrant collections `{kb}_entity` / `{kb}_triple`.

### 7.5 Visualisation API and component
- `GET /graph/subgraph?kb_id=&node_label=*&max_depth=2 (1..5; service clamps to <=3)&max_nodes=100 (1..1000)&exclude_chunk=false` (read perm) -> `{"success":true,"data":{"nodes":[...],"edges":[...]}}`; `node_label` is a keyword matched (case-insens. CONTAINS) against node `name`, `content_preview` or `chunk_id`, `"*"` = all. Queries: seeds = first `max_nodes` matches; expand `*1..max_depth` paths with `path_limit = max_nodes*10`; result truncated to `limit` nodes and `limit*2` edges, edges whose endpoints were cut are removed.
- Node shape: `{id, name (entity text | chunk content_preview | chunk_id), original_id, type ("Chunk" or entity label, default "Entity"), labels[], properties{kb_id, entity_id, name, normalized_name, label, attributes(json str), chunk_id, file_id, chunk_index, content_preview, start_char_pos, end_char_pos}, normalized:{name,type,source:"milvus"}, graph_type:"milvus"}`.
- Edge shape: `{id, source_id, target_id, type (relation label), properties{kb_id, chunk_id, file_id, source_name, target_name, type, triple_id, text, extractor_type} (one edge per (chunk, triple); Chunk->Entity `MENTIONS` edges carry {chunk_id,file_id,kb_id}), normalized:{type, direction:"directed"}}`.
- Other: `GET /graph/labels?kb_id` -> `{data:{labels:[...]}}`, `GET /graph/stats?kb_id` -> counts, `GET /graph/list` (admin).
- Frontend: **`@antv/g6` v5** (`web/src/components/GraphCanvas.vue`, imports `{ Graph } from '@antv/g6'`), wrapped by `KnowledgeGraphSection.vue` (1283 lines) + `GraphDetailPanel.vue` (floating detail card for clicked node/edge). Defaults `subgraphParams = {maxNodes: 100, maxDepth: 2, excludeChunk: true}` (settings panel: 最大节点数 10-1000 step 10, 搜索深度 1-5, 排除 Chunk 节点 switch, 刷新 / 应用). Canvas has a floating stats panel (实体 N / 关系 M; clicking opens colour-coded type legend with counts), search box "搜索实体" (Enter), settings gear, and (for managers) a Database icon that opens the **build panel**: counters 待构建 / 已构建 / 结构完成 / 抽取失败 (click -> failed chunk samples) / 向量待处理 / 向量失败 / 实体 / 关系, plus one primary button that morphs: `配置抽取器` (not locked) -> `开始索引` -> disabled `构建中 {progress}%` -> `重试失败向量` / `重试索引`. Extractor config modal (640px): 模型 select, `LLM 抽取并发数` (1-1000), `Schema` textarea, with copy "修改配置仅影响后续构建；已构建的图谱不会自动重算，如需一致请重置后重新抽取。抽取器类型创建后不可修改。" Empty states: "暂无知识图谱" (+配置抽取器 button; readonly users: "图谱尚未构建，请等待知识库管理员配置抽取器并索引后查看。").

---------------------------------------------------------------------------------------------------
## 8. Frontend interaction flow (Vue 3 + Ant Design Vue + pinia + @lucide/vue icons) — to recreate in React

Files: list `web/src/views/DataBaseView.vue` (357 lines) ; create wizard `web/src/components/knowledge/DatabaseCreateFlowModal.vue` (709) + `web/src/utils/databaseCreateForm.js` ; detail `web/src/views/DataBaseInfoView.vue` (1661) ; table `components/FileTable.vue` (1901) on generic `components/common/FileBrowserTable.vue` ; upload `components/FileUploadModal.vue` (2064) ; chunk params `components/ChunkParamsConfig.vue` ; preview `components/FileDetailModal.vue` (869) ; retrieval test `components/QuerySection.vue` (753) + `SearchConfigPanel.vue` (255) ; graph `KnowledgeGraphSection.vue`, `GraphCanvas.vue`, `GraphDetailPanel.vue` ; state/polling `stores/database.js` (858) + `utils/knowledge_file_policy.js` ; API `apis/knowledge_api.js` (664).

### 8.1 KB list page (`DataBaseView.vue`)
Route `/extensions?tab=knowledge` (embedded in an "extensions" hub) ; detail route `/extensions/knowledgebase/:kb_id` (`?section=filetable|query|graph|evaluation`, `?action=edit` opens config modal).
- Header: `PageHeader` title "知识库" with a single tab "文档知识库"; `PageShoulder` row = search input "搜索知识库..." (client-side filter by name/description) + type filter select ("全部类型") + primary button `+ 新建知识库`.
- States: loading spinner "正在加载知识库..."; empty `ResourceEmptyState` title "暂无知识库", description "创建知识库后，可以上传文件并配置检索、图谱和评估能力。", action "创建知识库".
- Grid of `InfoCard`s: icon (by kb type), `title=name`, `subtitle` = "{今天|昨天|N 天前|N 周前|N 个月前|N 年前}创建 · {row_count} 文件", `description` (or "暂无描述"), tags = [type label, share-scope label (`getShareConfigLabel`), embedding model short name]. Click card -> detail. Card "…" menu: 复制 ID / 编辑知识库 (manage perm) / divider / 删除知识库 (danger; `Modal.confirm` "确定要删除知识库“{name}”吗？此操作不可撤销。" OK=删除). Read-only connector KBs (Dify/Notion) card is disabled.

### 8.2 Create wizard (modal 840px, 3 steps, pill "1/3 · 类型")
Step 0 "类型": 知识库名称* (placeholder "例如：产品资料库") + type radio-cards (name, description, badge 支持文档/只读连接).
Step 1 "配置": 嵌入模型* (selector) + 分块策略 select (presets from `/knowledge/chunk-presets`, description shown below) ; type-specific params ; 知识库描述 textarea with AI-assist (`AiTextarea`; hint "描述会帮助智能体判断何时使用这个知识库。", placeholder "说明包含的内容、适用任务和使用限制").
Step 2 "权限": summary card (name, type tag, embedding model, chunking preset, description) + `ShareConfigForm` (read_scope / manage_scope each: 全局 / 部门 / 指定用户 mode cards; validation "管理权限范围大于读取权限范围").
Footer: left = "{type} · {name}" summary; right = 取消|上一步 / 下一步 / 创建知识库. Validation on next: name required, embedding required. Request built by `buildDatabaseRequest`: `{database_name, description, kb_type, additional_params:{chunk_preset_id,...}, share_config, embedding_model_spec}` -> `POST /knowledge/databases`; 409 on duplicate name. **Defaults**: `chunk_preset_id='general'`, `share_config = {version:2, read_scope:{access_level:'global'}, manage_scope:null}`, embedding = config default.
=> kunxiaozhi simplification: single-step dialog: name*, description (+ optional AI assist), chunk preset select (+ collapsed "高级": token size 100-10000 default 512, overlap 0-99% default 0), "对话中启用" switch; NO model/embedding picker.

### 8.3 KB detail page (`DataBaseInfoView.vue`)
`ExtensionDetailLayout`: breadcrumb "知识库 > {name}"; actions "复制 ID", "配置" (manage perm); tabs (icons): **文件管理** (FileText), **检索测试** (Search, forceRender), **知识图谱** (Network), **评估** (BarChart3). Non-document connector KBs show only 检索测试. Active tab mirrored into `?section=`.
**文件管理 tab**:
- Top-left `上传 ▾` split menu (manage perm): 上传文件 / 上传文件夹 / 新建文件夹.
- Stat chips row: 待解析 N (clickable when >0 and manage -> confirm parse all pending), 待入库 N (-> index all pending), 文件 N (turns warning w/ CircleAlert if legacy virtual folders), size text, Chunks N (click = repair stats), Tokens N.
- `FileTable` toolbar: 思维导图, 搜索文件 (opens `FileSearchModal`: filename search `GET documents/search?query&offset&limit`), status filter dropdown (全部状态 / 待解析 / 解析中 / 待入库 / 重试解析 / 入库中 / 已入库 / 重试入库), multi-select toggle, overflow menu (刷新 / 筛选 / 多选), refresh button, breadcrumb for folder navigation (drag files onto breadcrumb/folders to move).
- Columns (`columnsCompact`): 文件名 (type icon + name; folders open in place; click file opens detail modal) | 内容量 (tokens, tooltip chunks) | 创建人 (avatar+name) | 状态 | 时间 | 操作 (…popover). Server pagination `page_size` default 100 (max 500).
- Status pill (from `knowledge_file_policy.js` `STATUS_VIEW`): uploaded=**待解析**(warning, clock), parsing=**解析中**(info, spinner), parsed=**待入库**(primary, file), error_parsing=**重试解析**(error), indexing=**入库中**(info), indexed/done=**已入库**(success), error_indexing=**重试入库**(error), failed=入库失败, processing=处理中, waiting=等待中. For `uploaded/error_parsing/parsed/error_indexing` the pill is a **button** that runs the primary action (解析文件/重试解析/入库/重试入库) for managers. Hover on error pills should show `error` text (UNCONFIRMED whether tooltip is implemented; `error_message` is in list payload? — NOT in `_file_record_list_item`, only in file basic info `meta.error`).
- Row actions popover: 下载文件 (statuses done/indexed/parsed/error_indexing), 解析文件/重试解析, 入库/重试入库, 重新入库 (indexed only), 删除文件 (disabled while processing|parsing|indexing); folders: 重命名 / 新建子文件夹 / 删除文件夹.
- Multi-select batch bar: "{n} 项" + 批量解析 / 批量入库 / 批量删除 (danger). Selection disabled for `processing|waiting`.
- Parse dialog (`handleParseConfigConfirm` "开始解析": OCR engine selector – we drop) ; Index dialog (ChunkParamsConfig: preset select with "沿用知识库默认" + max tokens + overlap + delimiter).
- Delete: batch `DELETE /databases/{kb}/documents/batch` body `file_ids[]` -> `{deleted_count, failed_items[]}` (partial success message "部分删除成功: 已删除 N 个文件，失败 M 个").
- Polling: see §4.8 (10s base, ×2 backoff to 30s, stops after 6 unchanged polls, manual toggle).
**Upload modal** (`FileUploadModal.vue`): header segmented control `上传文件 | 上传文件夹 | 解析 URL | 个人空间(workspace)`; checkbox `上传后自动入库` (default **off**); settings panel: 存储位置 tree-select (target folder; default root), OCR engine (drop), and, when auto-index is on, `ChunkParamsConfig` (preset w/ "follow KB default", max tokens, overlap, delimiter). Dragger text "点击或将文件拖拽到此处", hint "支持类型: …" (from `GET /knowledge/files/supported-types`), ZIP tip "📦 ZIP包将自动解压提取 Markdown 与图片", warning "检测到PDF或图片文件，建议启用 OCR 以提取文本内容". Hidden/dot files and unsupported extensions are filtered client-side before upload (`isSupportedUploadFile`, `isHiddenPath`). Up to 10 parallel uploads, aggregate progress card (总计 / 上传中 / 成功 / 失败 pills) when >= 10 files, per-file list otherwise. Same-name conflict panel lists existing files with download/delete icon buttons. Primary button -> `chunkData()` (§4.9). Workspace mode: `WorkspacePathPicker` multi-select -> `POST /files/import-workspace` -> same follow-up.
**Document preview modal** (`FileDetailModal.vue`, 800px, 80vh): title = type icon + filename; right side: info text, `a-segmented` view switch **源文件 | Markdown | Chunks** (only views available for the status: `canPreviewParsed` = `has_parsed_markdown`; `canPreviewOriginal` = `has_original_file`; chunks only for `indexed|done`; default = markdown if parsed else source), download dropdown (下载原文 / 下载 Markdown), close. Markdown view = `MarkdownPreview` (markdown-it + katex; images served via auth proxy). Chunks view = responsive grid of cards: `#{chunk_order_index}` + content with newlines collapsed (`GET /documents/{id}/content` -> `{lines:[{id,content,chunk_order_index,start_char_pos,end_char_pos,...}], content: <markdown>}`; `/documents/{id}/basic` -> `{meta}`; `/documents/{id}` -> both).
**检索测试 tab** (`QuerySection.vue`): textarea (2-6 rows, placeholder "输入查询内容...", Enter submits, hint "Enter 检索知识库内容"), raw-JSON toggle (Braces icon), circular search button; results header "检索到 N 个相关文档块：" + 清空; each hit `#i` + 相似度 xx.xx% + 重排序分数, text, metadata line 来源/文件ID/块索引/距离; empty "未找到相关结果"; before first search shows sample-question suggestion rows (AI-generated, + "重新生成"). Parameters live in the 配置 modal tab **检索配置** (generic form from `/query-params`, §5.2, "调整当前知识库在检索测试和 Agent 使用时采用的参数。").
**配置 modal** (720px): tabs 基础信息 (name, description + AI, read-only embedding model "创建后不可修改", chunk preset) / 权限配置 (ShareConfigForm or read-only tag) / 检索配置.

---------------------------------------------------------------------------------------------------
## 9. Permissions / sharing / departments in Yuxi (brief)

- **Admin-only management.** Every `knowledge_router` endpoint (`list`, `create`, `upload`, `fetch-url`, `import-workspace`, `supported-types`, `chunk-presets`, `stats`, …) depends on `get_admin_user` (role `admin`/`superadmin`, `auth_middleware.py` L124-L130); per-KB endpoints use `require_knowledge_base_read/manage` (`server/utils/knowledge_permissions.py`) which are also built on `get_admin_user`. Ordinary users only get `GET /knowledge/databases/accessible` (name/id/description list for agent configuration) and consume KBs through agent tools. `get_required_user` additionally requires `user.department_id` (400 "当前用户未绑定部门").
- **Model** (`PKG/permissions/resource_permission.py`): `ResourcePermission = none < read < manage`. `share_config` v2 = `{version:2, read_scope, manage_scope}`; each scope = `{access_level: "global"|"department"|"user", department_ids:[int], user_uids:[str]}` (department scope needs >=1 dept; user scope needs >=1 uid; `manage_scope` must be a subset of `read_scope` unless read is global; `manage_scope` may be null).
  Resolution (`resolve_resource_permission`): superadmin => manage; `created_by == uid` => manage; matches manage_scope (and read_scope) => manage; matches read_scope => read; else none; then capped by role ceiling `KNOWLEDGE_BASE_PERMISSION_POLICY = {user: read, admin: manage, superadmin: manage}`.
  Creation forces creator's department/uid into the scope (`_normalize_share_config(..., user_uid, department_id)`), KB name is globally unique.
- **Not in Yuxi** (we must design ourselves): per-user KB creation, quotas (KB count / doc count / storage / file size), subscription/“broadcast square”, public listing + moderation, notifications on share, share-by-employee-id with name echo, personal vs shared grouping.
- Image proxy and download endpoints re-check read permission (`get_kb_image`, `download_document`), good pattern for parsed-image URLs in a private bucket.
- Agent runtime: effective visible KBs = user permission ∩ agent's `knowledges` selection, snapshotted per run in `context._visible_knowledge_bases` ("新 Run 会重算权限，运行中的 Context 不会因撤权自动刷新" — docs/mechanisms/knowledge-base.md). Sub-agents inherit the initiating user's identity.

---------------------------------------------------------------------------------------------------
## 10. Porting guidance (derived; what to copy vs. change)

Copy as-is (logic is storage-agnostic):
1. **File state machine + compare-and-set claims + lease fields on the file** (`processing_task_id/processing_owner`), with `error_message`. Mongo: `find_one_and_update({kb_id,file_id,status:{$in:allowed},...}, {$set:{status:"parsing",...}})`; any later write must also match `{processing_task_id, processing_owner}`.
2. **Task table + arq wake-up**: insert task doc `pending` -> `enqueue_job("process_kb_task", task_id)`; claim = atomic `findOneAndUpdate({_id, status:"pending"})` guarded by a global `running < N(4)` count (Mongo has no advisory lock: use a single `kb_task_slots` counter doc or a `$expr` count + unique "slot" index; or a Redis semaphore with TTL = lease). Heartbeat 10s / lease 30s / reconcile 30s; interrupted => task `failed` and files `parsing->error_parsing`, `indexing->error_indexing`; never auto-retry; progress throttled (>=2 pts). Per-file serial processing, `raise_if_cancelled()` between files, per-file try/except so one bad file doesn't abort the batch; result keeps first 200 item samples. "Pending scope" tasks deduped per KB via unique `dedupe_key`.
3. **Chunk presets** general/qa/book/laws/separator (skip `semantic` initially: needs sklearn+embeddings) with token approximation `[A-Za-z0-9_]+|[\u4e00-\u9fff]`, defaults `chunk_token_num=512`, `overlapped_percent=0`, `delimiter="\n"`, hard limit 1.5x. The code under `PKG/knowledge/chunking/ragflow_like/` (nlp.py, parsers/{general,separator,qa,book,laws}.py) imports only stdlib + sibling modules (`nlp.py` uses only `random/re`; `presets.py` uses `yuxi.utils.logger`; only `semantic.py` pulls markdown-it/sklearn/yuxi.models) and can be vendored by replacing the package prefix.
4. **Chunk storage separate from vector index**: chunk text/positions authoritative in the primary DB (Mongo `kb_chunks`), vector DB is derived; on retrieval hydrate `source`+`chunk_count` from file docs and **drop hits whose file no longer exists** (orphan safety); delete order on re-index/delete: graph -> vectors -> chunk rows -> file row.
5. **Agent tools** (names/args/docstrings in §6): `list_kbs`, `query_kb`, `open_kb_document`, `find_kb_document`, `search_file` (+ our confirm-gated write tool). Line-numbered window format `f"{n:6d}\t{line}"`, `has_more_*`/`next_offset`, `window_size` caps, visible-KB allow-list re-validated in every tool, and tool results with `metadata.chunk_count/source/file_id` so the UI can render citation cards.
6. **Polling UX**: chained `setTimeout`, 10s base, x2 backoff to 30s when `processing_count` unchanged, stop after 6 unchanged, reset on change, manual toggle, immediate `delayedRefresh()` 1s after submit, stats cached 10s server-side.
7. **Two-phase upload**: upload bytes+sha256 first (409 on duplicate hash per KB; same-name = warning panel), then submit batch of references in ONE task; client upload concurrency 10; reject unsupported ext & dotfiles client-side.

Must change for kunxiaozhi:
- Storage: MinIO -> whatever kunxiaozhi uses for blobs (GridFS / disk); keep layout `{kb_id}/upload/…`, `{kb_id}/parsed/{file_id}.md`, `{kb_id}/kb-images/…` + auth-proxy URL for images.
- Vector index: Qdrant collection per KB (or one collection + `kb_id` payload filter) with named vectors `dense` (COSINE) + `sparse` (BM25-style, **client-side Chinese tokenization with jieba**; Qdrant `Modifier.IDF` sparse). Hybrid fusion: Yuxi uses weighted sum 0.7/0.3 (Milvus WeightedRanker) — Qdrant Query API offers RRF/DBSF only; either implement client-side weighted fusion after two searches (closest to Yuxi) or use RRF (`k=60`). Keep Yuxi query params: `search_mode`, `final_top_k=10`, `similarity_threshold`, `bm25_top_k=50`, `recall_top_k=50`, weights `0.7/0.3`, `use_reranker` (admin-configured rerank model; no model picker for users), `use_graph_retrieval`.
- Remove: OCR engine selector, embedding model picker, LLM picker, evaluation, mindmap, URL import, virtual folders, Dify/Notion connectors, task-center drawer (admin only in Yuxi; we need per-KB status, and ideally WebSocket push as accelerator).
- Add (not in Yuxi): per-user KB creation + quotas, share by employee id (name echo), subscribe/public/unpublish, notifications, "personal vs shared" tabs, import from agent-output files (cf. Yuxi `POST /files/import-workspace` -> same hash-dedupe + re-upload pattern), KB-level "enable in chat" switch.
- Graph: store entities/triples/mentions in Mongo with the same unique keys (`kb_id+normalized_name+label`, `triple_id`), chunk states `graph_extraction_details`, 3 attempts with (2s, 10s) delays, vectors for entity names/triple strings in Qdrant, PPR via networkx on a 2-hop BFS capped by `graph_max_nodes`, RRF(60) fusion with chunk results; viz API shape `{nodes:[{id,name,type,properties}],edges:[{id,source_id,target_id,type,properties}]}` + `@antv/g6` (React: `@antv/g6` works the same) with defaults max_nodes=100, depth=2, exclude_chunk=true.

## 11. UNCONFIRMED / not found
- `KB_FILE_SEARCH_SCAN_LIMIT = 5000` (manager.py L33) caps filename-search `limit` (agent `search_file` schema allows up to 5000, default 300).
- Whether `FileTable` surfaces `error_message` on error pills — no error text rendering found in `FileTable.vue`; list payload (`_file_record_list_item`) omits `error`, so failures are only visible via file basic info/ task result. Recommend exposing `error` in our list payload.
- Semantic-preset internals beyond `semantic_chunking_with_auto_clusters` (silhouette-based cluster count) were not read in full.
- `parse_pdf` MinerU *official* API variant (`mineru_official.py`) and PaddleOCR/DeepSeek parsers not read (not needed; kunxiaozhi already has MinerU).
- Mind map (`mindmap_utils.py`), evaluation (`PKG/knowledge/eval/`), sample-question generation, virtual-folder migration — intentionally skipped.
- No "save to knowledge base" write tool exists in Yuxi (searched `agents/toolkits/kbs/tools.py`; `add_file_record` is only called from the router, `knowledge_task_service.py` and manager/base).
- Qdrant-specific hybrid/BM25 recipes are NOT from Yuxi (Yuxi uses Milvus-native BM25 analyzer `{"type":"chinese"}`).
