import { request } from './client'

export interface AiModelConfig {
  id: string
  name: string
  provider: 'mock' | 'cloud' | 'local' | string
  base_url: string
  api_key: string
  model: string
  temperature: number
  timeout: number
  /** 是否支持推理思考（None=未探测；测试连接自动标定，可手动覆盖） */
  reasoning?: boolean | null
  /** 系统内置模型（开机自带，不可删除） */
  builtin?: boolean
  last_test?: {
    ok?: boolean
    latency_ms?: number
    capabilities?: {
      connectivity?: boolean
      function_calling?: boolean
      reasoning?: boolean | null
      streaming?: boolean
      context_window?: number | null
    }
  }
}

export interface EmbeddingModelConfig {
  id: string
  name: string
  provider: 'hash' | 'api' | string
  base_url: string
  api_key: string
  model: string
  dimensions?: number | null
  last_test?: { ok?: boolean; dimensions?: number; latency_ms?: number }
}

export interface SettingsPublic {
  ai_models: AiModelConfig[]
  default_ai_model: string
  embedding_models: EmbeddingModelConfig[]
  default_embedding_model: string
  gate_review_threshold: number
  gate_rules: Record<string, unknown>
  kb_sample_rows: number
  kb_ai_annotation_samples: boolean
  query_max_rows: number
  pool_size: number
  runtime?: { data_dir: string; port: number; auth: string }
  // 兼容字段（旧前端仍可读）
  ai_provider: string
  ai_base_url: string
  ai_model: string
  ai_temperature: number
  ai_timeout: number
  embedding_provider: string
  embedding_base_url: string
  embedding_model: string
}

export interface SettingsPatch {
  ai_models?: Partial<AiModelConfig>[]
  default_ai_model?: string
  embedding_models?: Partial<EmbeddingModelConfig>[]
  default_embedding_model?: string
  // 旧格式兼容
  ai_provider?: string
  ai_base_url?: string
  ai_api_key?: string
  ai_model?: string
  ai_temperature?: number
  ai_timeout?: number
  embedding_provider?: string
  embedding_base_url?: string
  embedding_api_key?: string
  embedding_model?: string
  gate_review_threshold?: number
  gate_rules?: Record<string, unknown>
  kb_sample_rows?: number
  kb_ai_annotation_samples?: boolean
  query_max_rows?: number
  pool_size?: number
}

export function getSettings(): Promise<SettingsPublic> {
  return request('/api/v1/settings')
}

export function updateSettings(patch: SettingsPatch): Promise<SettingsPublic> {
  return request('/api/v1/settings', { method: 'PUT', body: JSON.stringify(patch) })
}
