import { useSchema } from '@renderer/store/schema'
import { useResults } from '@renderer/store/results'
import { previewTable } from '@renderer/api/schema'
import { useConnections } from '@renderer/store/connections'

interface Props {
  collapsed: boolean
  width: number
  onToggleRail: () => void
}

function Chevron({ deg }: { deg: number }): React.JSX.Element {
  return (
    <svg className="sch-chev" viewBox="0 0 16 16" style={{ transform: `rotate(${deg}deg)` }} aria-hidden>
      <path d="M4 6 L8 10 L12 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function SchemaRail({ collapsed, width, onToggleRail }: Props): React.JSX.Element {
  const { data, selectedTable, expandedTables, selectTable, toggleExpand, expandAll, collapseAll } = useSchema()
  const currentId = useConnections((s) => s.currentId)
  const push = useResults((s) => s.push)

  if (!data) {
    return (
      <div className={`schemaview${collapsed ? ' collapsed' : ''}`} style={{ width: collapsed ? 30 : width }}>
        {collapsed && (
          <button className="sch-rail-toggle" onClick={onToggleRail} title="展开侧栏"><Chevron deg={-90} /></button>
        )}
      </div>
    )
  }

  async function preview(t: string): Promise<void> {
    if (!currentId) return
    try {
      const p = await previewTable(currentId, t)
      push({
        title: `schema · ${t}`,
        name: t,
        headers: p.columns,
        types: p.types,
        rows: p.rows,
        meta: '结构预览'
      })
    } catch {
      /* 预览失败静默 */
    }
  }

  function handleTableClick(t: string): void {
    // 点击表名：选中并预览（不再展开字段列表，展开由行尾箭头控制）
    selectTable(t)
    void preview(t)
  }

  const allExpanded = data.tables.every((t) => expandedTables.includes(t.name))

  return (
    <div className={`schemaview${collapsed ? ' collapsed' : ' on'}`} style={{ width: collapsed ? 30 : width }}>
      {collapsed ? (
        <button className="sch-rail-toggle" onClick={onToggleRail} title="展开侧栏"><Chevron deg={-90} /></button>
      ) : (
        <div className="sch-body">
          <div className="sch-toolbar">
            <span className="sch-grp">{data.dialect}</span>
            <span className="spacer" />
            <button
              className="sch-tb sch-expandall"
              title={allExpanded ? '收起全部字段' : '展开全部字段'}
              onClick={allExpanded ? collapseAll : expandAll}
            ><Chevron deg={allExpanded ? 180 : 0} /></button>
            <button
              className="sch-tb"
              title="收起侧栏"
              onClick={onToggleRail}
            ><Chevron deg={90} /></button>
          </div>
        {data.tables.map((t) => {
          const cols = data.columns.filter((c) => c.table === t.name)
          const open2 = expandedTables.includes(t.name)
          return (
            <div className="tbl" key={t.name}>
              <div
                className={`sch-item${selectedTable === t.name ? ' on' : ''}`}
                onClick={() => handleTableClick(t.name)}
              >
                <span className={`tdot${open2 ? ' open' : ''}`} />
                <span className="tname">{t.name}</span>
                <button
                  type="button"
                  className="sch-row-toggle"
                  title={open2 ? '收起字段' : '展开字段'}
                  onClick={(e) => {
                    e.stopPropagation()
                    toggleExpand(t.name)
                  }}
                ><Chevron deg={open2 ? 180 : 0} /></button>
              </div>
              <div className={`sch-cols${open2 ? ' open' : ''}`}>
                {cols.map((c) => (
                  <div className="sch-col" key={c.name}>
                    <span className="cname">{c.name}</span>
                    <span className="ct">{c.pk ? 'PK' : c.fk ? 'FK' : c.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
        </div>
      )}
    </div>
  )
}
