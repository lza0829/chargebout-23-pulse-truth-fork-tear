import { useEffect, useState } from 'react'
import { api } from '../api'
import AlignmentBar from '../components/AlignmentBar'

export default function Pulses() {
  const [list, setList] = useState([])
  const [id, setId] = useState('')
  const [kwh, setKwh] = useState('1.5')
  const [kw, setKw] = useState('7')
  const [msg, setMsg] = useState('')
  const [receipt, setReceipt] = useState(null)
  const load = () =>
    api.会话('充电中').then((rows) => {
      setList(rows)
      if (rows[0] && !id) setId(String(rows[0].id))
    })
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const submit = async () => {
    setMsg('')
    setReceipt(null)
    try {
      const r = await api.脉冲(Number(id), { 电量: Number(kwh), 功率: Number(kw) })
      setReceipt(r)
      setMsg(
        `已出回执 #${r.回执编号}：本次 +${r.本次增量}，真源重放/累计列缓存同为 ${r.累计电量}，预估金额 ${r.预估金额}`,
      )
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>记脉冲</h2>
      <p className="lead">
        脉冲写口单事务同刷：流水 → 重放校准累计列 → 回执 → 事件 → 对齐诊断。旁路改数永远拿不到回执。
      </p>
      <AlignmentBar scope="global" />
      <div className="panel">
        <div className="row">
          <label>会话</label>
          <select value={id} onChange={(e) => setId(e.target.value)}>
            {list.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slip_no} · {s.pile_name} · 真源重放 {s.重放电量 ?? s.kwh}
                {s.对齐状态 === '陈旧' ? ' · 陈旧!' : ''}
              </option>
            ))}
          </select>
          <label>电量</label>
          <input value={kwh} onChange={(e) => setKwh(e.target.value)} style={{ width: 90 }} />
          <label>功率</label>
          <input value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 90 }} />
          <button type="button" className="primary" onClick={submit}>
            写入脉冲
          </button>
        </div>
        <p className={`msg ${receipt ? 'ok' : ''}`}>{msg}</p>
        {receipt && (
          <table className="receipt">
            <thead>
              <tr><th>回执编号</th><th>脉冲编号</th><th>本次增量</th><th>真源重放</th><th>累计列缓存</th><th>预估金额</th><th>时间</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>{receipt.回执编号}</td>
                <td>{receipt.脉冲编号}</td>
                <td>{receipt.本次增量}</td>
                <td>{receipt.重放电量}</td>
                <td>{receipt.缓存电量}</td>
                <td>{receipt.预估金额}</td>
                <td>{receipt.脉冲时间}</td>
              </tr>
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
