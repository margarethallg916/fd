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
  "metadata":{"source":"文件名.pdf","chunk_id":"file_ab12_chunk_3","file_id":"file_ab12","chunk_index":3,"chunk_count":27}}]
```
  `QuerySection.vue` renders per hit: `#n`, 相似度 `(score*100).toFixed(2)%`, 重排序分数, chunk text, 来源/文件ID/块索引/距离, a raw-JSON toggle (braces icon), sample-question suggestions, `Enter` to search (`press-enter.prevent`), 清空 button. No per-result click-through in this component.
- Agent `query_kb` returns `SearchOutputSchema` (`PKG/knowledge/schemas.py`): `{kb_id, results:[{id: chunk_id, kb_id, file_id, content, metadata:{source, chunk_id, chunk_index, chunk_count, score, distance,...}}]}` (path-like metadata keys `filepath/parsed_path/path/markdown_file` stripped by `build_search_output`, base.py L611-L648).

