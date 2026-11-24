import { useEffect, useState } from 'react'
import { api } from '../api'
import AlignmentBar from '../components/AlignmentBar'

export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  const load = () => api.会话().then(setList)
  useEffect(() => {
    load()
  }, [])
  return (
    <div className="page">
      <h2>会话册</h2>
      <p className="lead">
        电量列只认真源重放；同时给出累计列缓存与漂移，陈旧会话红标，禁止只看缓存数字。
      </p>
      <AlignmentBar scope="global" />
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>真源重放</th>
              <th>累计列缓存</th>
              <th>漂移</th>
              <th>对齐</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id} className={s.对齐状态 === '陈旧' ? 'stale-row' : ''}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.重放电量 ?? s.kwh}</td>
                <td>{s.缓存电量 ?? s.kwh}</td>
                <td>{s.漂移电量 ?? 0}</td>
                <td>
                  <span className={`align-pill ${s.对齐 ? 'ok' : 'stale'}`}>{s.对齐状态}</span>
                </td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <AlignmentBar scope="session" sessionId={detail.id} />
          <p>
            锁定电价 {detail.price_locked} · 真源重放 {detail.重放电量} · 累计列缓存{' '}
            {detail.缓存电量} · 金额 {detail.amount}
          </p>
          <h3>脉冲流水（真源，{detail.脉冲?.length || 0} 行）</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                  <td>{p.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>脉冲回执（{detail.回执?.length || 0} 条，与流水一一对应）</h3>
          <table>
            <thead>
              <tr>
                <th>回执</th>
                <th>对应脉冲</th>
                <th>增量</th>
                <th>回执重放累计</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {(detail.回执 || []).map((r) => (
                <tr key={r.id}>
                  <td>#{r.id}</td>
                  <td>#{r.pulse_id}</td>
                  <td>{r.delta_kwh}</td>
                  <td>{r.replayed_kwh}</td>
                  <td>{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
