export interface SidecarRuntime {
  baseUrl: string
  token: string
  dataDir: string
}

export interface HealthStatus {
  status: string
  gate: string
  gate_model_independent: boolean
  dialects: string[]
  ai_provider: string
  ai_model: string
  ai_effective_provider: string
  ai_mock_downgraded: boolean
  connections: number
}
