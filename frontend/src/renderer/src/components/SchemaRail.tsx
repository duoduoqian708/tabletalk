import { useSchema } from '@renderer/store/schema'
import { useResults } from '@renderer/store/results'
import { previewTable } from '@renderer/api/schema'
import { useConnections } from '@renderer/store/connections'

interface Props {
  open: boolean
  width: number
}

export function SchemaRail({ open, width }: Props): React.JSX.Element {
  const { data, selectedTable, expandedTables, selectTable, toggleExpand, expandAll, collapseAll } = useSchema()
  const currentId = useConnections((s) => s.currentId)
  const push = useResults((s) => s.push)

  if (!data) return <div className="schemaview" style={{ width: open ? width : 0 }} />

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
    // 点击表：展开/收起字段列表 + 选中并预览
    toggleExpand(t)
    selectTable(t)
    void preview(t)
  }

  const allExpanded = data.tables.every((t) => expandedTables.includes(t.name))

  return (
    <div className={`schemaview${open ? ' on' : ''}`} style={{ width: open ? width : 0 }}>
      <div className="sch-body">
        <div className="sch-toolbar">
          <span className="sch-grp">{data.dialect}</span>
          <span className="spacer" />
          <button
            className={`sch-tb${allExpanded ? ' on' : ''}`}
            title="展开全部字段"
            onClick={expandAll}
          >⊞</button>
          <button
            className="sch-tb"
            title="收起全部字段"
            onClick={collapseAll}
          >⊟</button>
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
    </div>
  )
}
