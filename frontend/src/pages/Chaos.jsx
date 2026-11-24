import { useEffect, useState } from 'react'
import { api } from '../api'
import AlignmentBar from '../components/AlignmentBar'

// ADR-0001 故障复现台：四类夹具与脉冲写口物理隔离，全部后端留痕。
export default function Chaos() {
  const [list, setList] = useState([])
  const [sid, setSid] = useState('')
  const [fake, setFake] = useState('50')
  const [delta, setDelta] = useState('1')
  const [msg, setMsg] = useState('')
  const [audit, setAudit] = useState([])
  const load = () =>
    api.会话().then((rows) => {
      setList(rows)
      if (rows[0] && !sid) setSid(String(rows[0].id))
    })
  const loadAudit = () => api.chaos审计().then(setAudit)
  useEffect(() => {
    load()
    loadAudit()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const run = async (fn, okText) => {
    setMsg('')
    try {
      const r = await fn()
      setMsg(okText(r))
    } catch (e) {
      setMsg(`拒绝/异常：${e.message}`)
    }
    load()
    loadAudit()
  }
  const id = Number(sid)
  return (
    <div className="page">
      <h2>对齐诊断与故障复现</h2>
      <p className="lead">
        电量真源为脉冲流水重放。下列夹具复现旁路改累计列、双脉冲半截、流水齐全列未动、伪诊断四类故障；
        所有动作均在 bypass_audit 留痕，正常业务路径无法触达。
      </p>
      <AlignmentBar scope="global" />
      <div className="panel">
        <div className="row">
          <label>目标会话</label>
          <select value={sid} onChange={(e) => setSid(e.target.value)}>
            {list.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slip_no} · 重放 {s.重放电量 ?? s.kwh} · {s.对齐状态}
              </option>
            ))}
          </select>
          <label>伪造电量</label>
          <input value={fake} onChange={(e) => setFake(e.target.value)} style={{ width: 90 }} />
          <label>半截增量</label>
          <input value={delta} onChange={(e) => setDelta(e.target.value)} style={{ width: 90 }} />
        </div>
        <div className="row">
          <button type="button" className="danger"
            onClick={() => run(() => api.chaos旁路(id, Number(fake)),
              (r) => r.被拒绝 ? `✅ 旁路被整笔回滚：${r.拒因}` : JSON.stringify(r))}>
            1. 旁路改累计列（应被拒绝）
          </button>
          <button type="button"
            onClick={() => run(() => api.chaos篡改(id, Number(fake)),
              (r) => `已注入陈旧态：缓存伪造为 ${r.注入缓存值}，真源重放仍为 ${r.真源重放}`)}>
            2. 注入累计列篡改（列动流水不动）
          </button>
          <button type="button"
            onClick={() => run(() => api.chaos半截(id, Number(delta)),
              (r) => `半截脉冲：真源重放 ${r.真源重放}，缓存停留 ${r.缓存停留}（无回执）`)}>
            3. 注入半截脉冲（只落流水）
          </button>
          <button type="button"
            onClick={() => run(() => api.chaos伪诊断(id),
              (r) => `已放假快照「${r.伪造快照}」，实时真源 ${r.实时真源} 仍判陈旧`)}>
            4. 注入伪对齐快照
          </button>
        </div>
        <p className={`msg ${msg.startsWith('✅') ? 'ok' : ''}`}>{msg}</p>
      </div>

      <div className="panel">
        <h3>旁路审计（bypass_audit，最近 200 条）</h3>
        <table>
          <thead>
            <tr><th>时间</th><th>会话</th><th>类别</th><th>尝试值</th><th>真源重放</th><th>说明</th></tr>
          </thead>
          <tbody>
            {audit.map((a) => (
              <tr key={a.id}>
                <td>{a.created_at}</td>
                <td>{a.session_id}</td>
                <td><span className={`align-pill ${a.kind.includes('拒绝') ? 'stale' : 'ok'}`}>{a.kind}</span></td>
                <td>{a.attempted_kwh}</td>
                <td>{a.replayed_kwh}</td>
                <td>{a.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
