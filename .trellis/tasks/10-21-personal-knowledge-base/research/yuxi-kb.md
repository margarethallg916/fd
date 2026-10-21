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

