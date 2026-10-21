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

