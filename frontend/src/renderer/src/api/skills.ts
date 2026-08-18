import { request } from './client'

export interface SkillPublic {
  id: string
  name: string
  description: string
  tools: string[]
  system_prompt: string
  builtin: boolean
  read_only: boolean
  enabled: boolean
  triggers: string[]
}

export interface SkillToolInfo {
  name: string
  description: string
}

export interface SkillCatalog {
  skills: SkillPublic[]
  tools: SkillToolInfo[]
}

export function listSkills(): Promise<SkillCatalog> {
  return request('/api/v1/skills')
}

export function createSkill(body: {
  name: string
  description?: string
  system_prompt?: string
  tools: string[]
  read_only?: boolean
  enabled?: boolean
  triggers?: string[]
}): Promise<SkillPublic> {
  return request('/api/v1/skills', { method: 'POST', body: JSON.stringify(body) })
}

export function updateSkill(id: string, patch: Partial<Omit<SkillPublic, 'id' | 'builtin'>>): Promise<SkillPublic> {
  return request(`/api/v1/skills/${id}`, { method: 'PUT', body: JSON.stringify(patch) })
}

export function removeSkill(id: string): Promise<{ deleted: boolean }> {
  return request(`/api/v1/skills/${id}`, { method: 'DELETE' })
}
