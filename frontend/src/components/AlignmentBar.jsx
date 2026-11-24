import { useEffect, useState } from 'react'
import { api } from '../api'

// ADR-0001 对齐诊断条：只认真源重放。陈旧时红条显因，禁止亮「已对齐」。
export default function AlignmentBar({ scope = 'global', sessionId, onLoaded }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => {
    const p = scope === 'global' ? api.全局对齐(true) : api.会话对齐(sessionId, true)
    p.then((d) => {
      setData(d)
      setErr('')
      onLoaded?.(d)
    }).catch((e) => setErr(e.message))
  }
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, sessionId])
  if (err) return <div className="align-bar stale"><span>诊断条取数失败：{err}</span><button onClick={load}>重试</button></div>
  if (!data) return <div className="align-bar">对齐诊断加载中…</div>

  if (scope === 'session') {
    const ok = data.对齐
    return (
      <div className={`align-bar ${ok ? 'ok' : 'stale'}`}>
        <strong>对齐诊断：{data.对齐状态}</strong>
        <span>
          真源重放 {data.重放电量} · 累计列缓存 {data.缓存电量} · 漂移 {data.漂移电量}
          （流水 {data.流水行数} 行 / 回执 {data.回执条数} 条）
        </span>
        {!ok && <span className="reason">原因：{data.原因}</span>}
        {data.自愈 && <span className="reason">已按真源自愈：{data.自愈.校准前缓存} → {data.自愈.校准后}</span>}
        <button onClick={load}>重新诊断</button>
      </div>
    )
  }

  const ok = data.全部对齐
  return (
    <div className={`align-bar ${ok ? 'ok' : 'stale'}`}>
      <strong>电量真源：{data.真源口径}</strong>
      <span>
        全局对齐诊断：{ok ? '已对齐' : `陈旧 ${data.陈旧会话数}/${data.会话总数}`}
      </span>
      {!ok && (
        <ul className="align-detail">
          {data.明细.filter((d) => !d.对齐).map((d) => (
            <li key={d.会话编号}>
              会话 #{d.会话编号}：重放 {d.重放电量} / 缓存 {d.缓存电量} / 漂移 {d.漂移电量}
              {d.原因 ? `（${d.原因}）` : ''}
            </li>
          ))}
        </ul>
      )}
      <button onClick={load}>重新诊断</button>
    </div>
  )
}
