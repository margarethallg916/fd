// ============================================
// Settings Types
// ============================================

export type SettingType =
  | "string"
  | "text"
  | "number"
  | "boolean"
  | "json"
  | "select";
export type SettingCategory =
  | "frontend"
  | "wecom"
  | "agent"
  | "llm"
  | "session"
  | "skills"
  | "mongodb"
  | "redis"
  | "checkpoint"
  | "long_term_storage"
  | "security"
  | "email"
  | "captcha"
  | "s3"
  | "file_upload"
  | "sandbox"
  | "tools"
  | "tracing"
  | "user"
  | "oauth"
  | "memory"
  | "memory_embedding"
  | "memory_search"
  | "memory_storage"
  | "audio_transcription"
  | "dify"
  | "document_parse"
  | "knowledge_base";

// Setting dependency condition
export interface SettingDependsOn {
  key: string; // Parent setting key
  value: string | number | boolean; // Expected value for visibility
}

// JSON schema field definition
export interface JsonSchemaField {
  name: string;
  type: "text" | "password" | "number" | "toggle" | "select";
  label: string; // i18n key
  placeholder?: string;
  required?: boolean;
  options?: string[];
  layout_width?: "compact" | "full";
}

// JSON schema for structured editing of JSON-type settings
export interface JsonSchema {
  type: "array" | "object";
  item_label?: string; // i18n key for array items
  key_label?: string; // i18n key for object keys (object type)
  value_type?: "array"; // for object values that are arrays
  key_options?: string[]; // allowed keys for object type
  fields: JsonSchemaField[];
}

export interface SettingItem {
  key: string;
  value: string | number | boolean | object;
  type: SettingType;
  category: SettingCategory;
  subcategory: string;
  description: string;
  default_value: string | number | boolean | object;
  requires_restart: boolean;
  is_sensitive: boolean;
  frontend_visible: boolean;
  depends_on?: string | SettingDependsOn; // Key of parent setting or condition object
  options?: string[]; // Available options for SELECT type
  json_schema?: JsonSchema; // Schema for JSON-type settings
  minimum?: number;
  maximum?: number;
  step?: number;
  updated_at?: string;
  updated_by?: string;
}

export interface SettingsResponse {
  settings: Record<SettingCategory, SettingItem[]>;
}

export interface SettingUpdate {
  value: string | number | boolean | object;
}

export interface SettingResetResponse {
  message: string;
  reset_count: number;
}

export type WeComNetworkMode =
  | "direct"
  | "reverse_gateway"
  | "forward_proxy";

export interface WeComNetworkConfig {
  mode: WeComNetworkMode;
  websocket_url: string;
  media_gateway_url: string;
  forward_proxy_url: string;
  forward_proxy_username: string;
  has_forward_proxy_password: boolean;
  ca_bundle_path: string;
  connect_timeout_seconds: number;
  media_download_timeout_seconds: number;
  media_max_bytes: number;
  revision: string;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface WeComNetworkConfigUpdate {
  mode: WeComNetworkMode;
  websocket_url: string;
  media_gateway_url: string;
  forward_proxy_url: string;
  forward_proxy_username: string;
  forward_proxy_password?: string;
  clear_forward_proxy_password: boolean;
  expected_revision?: string;
  ca_bundle_path: string;
  connect_timeout_seconds: number;
  media_download_timeout_seconds: number;
  media_max_bytes: number;
}

export interface WeComNetworkBotResult {
  preset_id: string;
  aibotid: string;
  state: string;
  reason_code?: string | null;
  reason_detail?: string | null;
  node_id?: string | null;
}

export type WeComNetworkOperationStatus =
  | "connected"
  | "partial_failure"
  | "rolled_back"
  | "saved_unverified"
  | "test_ok"
  | "test_failed";

export interface WeComNetworkOperationResponse {
  operation_id: string;
  status: WeComNetworkOperationStatus;
  revision: string;
  config: WeComNetworkConfig;
  results: WeComNetworkBotResult[];
  detail?: string | null;
}

export interface OpenSandboxNode {
  id: string;
  domain: string;
  has_api_key: boolean;
  image: string;
  timeout: number;
  work_dir: string;
  use_server_proxy: boolean;
  max_sandboxes: number;
  enabled: boolean;
  priority: number;
  draining: boolean;
  health_state: string;
  last_health_at?: string | null;
  last_error?: string | null;
  used_sandboxes: number;
  over_capacity: boolean;
}

export interface OpenSandboxNodeInput extends Omit<OpenSandboxNode, "has_api_key" | "draining" | "health_state" | "last_error" | "used_sandboxes" | "over_capacity"> {
  api_key?: string;
  clear_api_key?: boolean;
}

export interface OpenSandboxNodesResponse {
  revision: string;
  mode: "legacy" | "multi_node";
  nodes: OpenSandboxNode[];
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface OpenSandboxInventoryItem {
  node_id: string;
  sandbox_id: string;
  state: string;
  managed: boolean;
  user_id?: string | null;
  username?: string | null;
  binding_state?: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
  actions: Record<string, boolean>;
}

export interface OpenSandboxInventoryResponse {
  items: OpenSandboxInventoryItem[];
  total: number;
  skip: number;
  limit: number;
}

// ============================================
// Model Types
// ============================================

export interface ModelProfile {
  max_input_tokens?: number;
  supports_vision?: boolean;
}

export interface AvailableModelConfig {
  value: string;
  provider?: string;
  kind?: string;
  icon?: string;
  label: string;
  description?: string;
  api_key?: string;
  api_base?: string;
  temperature?: number;
  max_tokens?: number;
  profile?: ModelProfile;
}

// Backward compatibility alias
export type AvailableModel = AvailableModelConfig;
