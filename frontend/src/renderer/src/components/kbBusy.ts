import { useKnowledge } from '@renderer/store/knowledge'

/**
 * 知识库构建是否进行中（全局只订阅 busy 一个字段，构建结束回到 false）。
 * 供工作台图谱等重渲染组件在外部任务期间自动降载（暂停自转）使用。
 */
export function useKbGateBusy(): boolean {
  return useKnowledge((s) => s.busy)
}
