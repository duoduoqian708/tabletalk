import { useEffect, useRef } from 'react'
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from '@codemirror/view'
import { EditorState, Compartment } from '@codemirror/state'
import { sql } from '@codemirror/lang-sql'
import { linter, Diagnostic, lintGutter } from '@codemirror/lint'
import { autocompletion } from '@codemirror/autocomplete'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { getRuntime } from '@renderer/api/client'

interface Props {
  value: string
  onChange: (v: string) => void
  connectionId: string | null
  dialect?: string
}

export function CmEditor({ value, onChange, connectionId, dialect = 'sqlite' }: Props): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const connRef = useRef(connectionId)
  connRef.current = connectionId
  const dialectRef = useRef(dialect)
  dialectRef.current = dialect

  // debounce for lint（零模型调用，本地 sidecar，<100ms）
  const lintSource = async (view: EditorView): Promise<Diagnostic[]> => {
    const text = view.state.doc.toString()
    if (!text.trim() || !connRef.current) return []
    const cid = connRef.current
    const rt = getRuntime()
    const token = rt?.token
    if (!token) return []
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), 1500)
      const r = await fetch('/api/v1/sql/lint', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': token },
        body: JSON.stringify({ connection_id: cid, sql: text }),
        signal: ctrl.signal,
      })
      clearTimeout(timer)
      if (!r.ok) return []
      const j = (await r.json()) as { diagnostics: { rule_id: string; severity: string; message: string; message_en: string; from: number | null; to: number | null; suggestion: string | null; objects?: string[] }[] }
      const diags: Diagnostic[] = []
      for (const d of j.diagnostics) {
        const from = d.from ?? 0
        const to = d.to ?? from + ((d.objects?.[0]?.length ?? 4))
        const severity = d.severity === 'error' ? 'error' : 'warning'
        const msg = d.message + (d.suggestion ? ` → ${d.suggestion}` : '')
        diags.push({
          from: Math.max(0, Math.min(from, text.length)),
          to: Math.max(0, Math.min(to ?? from + 4, text.length)),
          severity,
          message: msg,
          source: d.rule_id,
        })
      }
      return diags
    } catch {
      return []
    }
  }

  useEffect(() => {
    if (!ref.current) return
    const languageConf = new Compartment()
    const tabSizeConf = new Compartment()

    const state = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        autocompletion(),
        lintGutter(),
        languageConf.of(sql()),
        linter(lintSource, { delay: 120 } as any),
        EditorView.lineWrapping,
        EditorView.updateListener.of((upd) => {
          if (upd.docChanged) {
            onChangeRef.current(upd.state.doc.toString())
          }
        }),
        EditorView.theme({
          '&': { backgroundColor: 'var(--void-2)', color: 'var(--ink)' },
          '.cm-content': { fontFamily: 'var(--font-mono)', fontSize: '13px', padding: '10px 0' },
          '.cm-gutters': { backgroundColor: 'var(--void-1)', color: 'var(--ink-faint)', borderRight: '1px solid var(--line)' },
          '.cm-activeLine': { backgroundColor: 'var(--void-3)' },
          '.cm-activeLineGutter': { backgroundColor: 'var(--void-4)' },
          '.cm-lintRange-error': { textDecoration: 'underline wavy var(--red)' },
          '.cm-lintRange-warning': { textDecoration: 'underline wavy var(--amber)' },
          '.cm-tooltip': { backgroundColor: 'var(--void-1)', color: 'var(--ink)', border: '1px solid var(--line-strong)', fontSize: '11px' },
          '.cm-tooltip-lint': { maxWidth: '420px', whiteSpace: 'pre-wrap' },
        }),
        tabSizeConf.of(EditorState.tabSize.of(2)),
      ],
    })
    const view = new EditorView({ state, parent: ref.current })
    viewRef.current = view
    return () => {
      view.destroy()
      viewRef.current = null
    }
  }, [])

  // 外部 value 变化时同步（避免循环）
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    const cur = view.state.doc.toString()
    if (cur !== value) {
      view.dispatch({ changes: { from: 0, to: cur.length, insert: value } })
    }
  }, [value])

  // dialect 变化时更新
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    // 暂不动态切换 dialect，保持 sqlite 通用
  }, [dialect])

  return <div ref={ref} className="cm-editor" style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-sm)', overflow: 'hidden' }} />
}
