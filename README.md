# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

## 电量真源口径（ADR-0001）

全局唯一口径：**以脉冲流水 `pulses` 重放为真源，`sessions.kwh` 仅为加速缓存**。
详见 [docs/adr/0001-在充电量真源以脉冲流水重放为准.md](docs/adr/0001-在充电量真源以脉冲流水重放为准.md)。

- 任何展示电量（总览在充电量、会话册、会话详情、记脉冲页、对齐诊断条）与关账电量都由流水重放得到。
- 脉冲写口在单事务内同刷：流水 → 重放校准累计列 → 脉冲回执 → 事件 → 对齐诊断快照；`BEGIN IMMEDIATE` 串行化，并发双脉冲不漂格。
- 旁路直改累计列被 SQLite 触发器整笔回滚并写 `bypass_audit`，绝不产生回执、不盖重放。
- 流水/回执/诊断快照只追加（禁 UPDATE/DELETE）；缓存陈旧时诊断条标「陈旧」并禁止亮「已对齐」，读侧可按真源自愈。
- 仅覆盖电量真源；计价档、关账争用另立需求。

### 故障复现夹具（`/api/chaos/*`，前端「对齐诊断」页）

与脉冲写口物理隔离、全程留痕，用于复现：旁路改累计列（应被拒绝）、累计列篡改（列动流水不动）、
半截脉冲（只落流水无回执）、伪对齐快照（历史亮齐但实时对不上）。

## 本地运行

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

- `tests/test_energy_truth.py`：同刷/重放、并发双脉冲、旁路拒绝、篡改陈旧与自愈、半截脉冲、伪诊断、只追加、全读面同口径。
- `tests/test_api_contract.py`：HTTP 路由层契约（脉冲回执、对齐诊断、chaos 路由、关账重放）。

WAL + `synchronous=NORMAL` 为默认生产档；测试临时库用 `CHARGEBOUT_SYNCHRONOUS=OFF`
规避 WSL2 等环境的 fsync 高延迟（不影响正确性验证）。
