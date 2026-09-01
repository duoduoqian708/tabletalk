import { request } from './client'

export interface SafetyRule {
  id: string
  tier: string
  scope: string
  default_verdict: string
  floor: boolean
  override: string | null
}

export interface SafetyRules {
  rules: SafetyRule[]
  policy: {
    version: number
    table_rules: Record<string, string>
    pattern_rules: unknown[]
    threshold: number
  } | null
}

export async function getSafetyRules(): Promise<SafetyRules> {
  return request<SafetyRules>('/api/v1/safety/rules')
}

/** 全量覆盖配置（仅含实际收严项；默认值省略）。后端 normalize 契约兜底。 */
export async function putGateRules(gateRules: Record<string, string>): Promise<{ gate_rules: Record<string, string> }> {
  return request('/api/v1/settings', { method: 'PUT', body: JSON.stringify({ gate_rules: gateRules }) })
}