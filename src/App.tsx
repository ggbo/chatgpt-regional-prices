import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { buildRows, eligibleFreshRows, isFresh, quoteFor, regionStatus, rowsToCsv, sortRows } from './logic'
import { loadBundle } from './data'
import type { DataBundle, DisplayRow, Filters, PlanId, TaxStatus } from './types'

const planIds: PlanId[] = ['go', 'plus', 'pro_100', 'pro_200', 'pro_500']
const defaultFilters: Filters = { planId: 'plus', currency: 'CNY', sort: 'price_asc', tax: 'all', view: 'current', search: '' }
const statusLabels: Record<string, string> = {
  valid_quote: '本轮有效', stale_quote: '历史报价', plan_field_missing: '字段缺失', request_failed: '请求失败', access_blocked: '访问受限', country_mismatch: '地区不匹配', amount_invalid: '金额异常', period_unverified: '周期待核验', unverified_range: '未核验区间', not_attempted: '尚未尝试', fresh: '新鲜', historical: '历史数据', missing: '暂缺数据',
}
const taxLabels: Record<TaxStatus, string> = { inclusive: '含税', exclusive: '未税', unknown: '未知' }

function initialFilters(): Filters {
  const params = new URLSearchParams(location.search)
  const plan = params.get('plan') as PlanId | null
  const currency = params.get('currency')
  const sort = params.get('sort') as Filters['sort'] | null
  const tax = params.get('tax') as Filters['tax'] | null
  const view = params.get('view') as Filters['view'] | null
  const validSorts: Filters['sort'][] = ['price_asc', 'price_desc', 'name', 'verified']
  const validTaxes: Filters['tax'][] = ['all', 'inclusive', 'exclusive', 'unknown']
  const validViews: Filters['view'][] = ['current', 'all', 'history', 'missing']
  return { ...defaultFilters, planId: plan && planIds.includes(plan) ? plan : 'plus', currency: currency === 'USD' ? 'USD' : 'CNY', sort: sort && validSorts.includes(sort) ? sort : params.get('sort') === 'desc' ? 'price_desc' : 'price_asc', tax: tax && validTaxes.includes(tax) ? tax : 'all', view: view && validViews.includes(view) ? view : 'current', search: params.get('search') ?? '' }
}

function formatDecimal(amount: string | null, currency: string): string {
  if (!amount) return '—'
  const value = Number(amount)
  if (!Number.isFinite(value)) return `${currency} ${amount}`
  try { return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(value) }
  catch { return `${currency} ${amount}` }
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date) : value
}

function download(name: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a'); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url)
}

export default function App() {
  const [bundle, setBundle] = useState<DataBundle | null>(null)
  const [filters, setFilters] = useState<Filters>(initialFilters)
  const [mode, setMode] = useState<'list' | 'matrix'>('list')
  const [selected, setSelected] = useState<DisplayRow | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now)

  const refresh = useCallback(async () => {
    setLoading(true); setError(null)
    try { setBundle(await loadBundle()); setSelected(null) } catch (reason) { setError(reason instanceof Error ? reason.message : '未知数据错误') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 60_000); return () => window.clearInterval(timer) }, [])

  const rows = useMemo(() => bundle ? buildRows(bundle, filters, now) : [], [bundle, filters, now])
  const allRows = useMemo(() => bundle ? buildRows(bundle, { ...filters, view: 'all' }, now) : [], [bundle, filters, now])
  const ranked = useMemo(() => sortRows(eligibleFreshRows(rows), 'price_asc'), [rows])
  const plan = bundle?.plans.plans.find((item) => item.plan_id === filters.planId)
  const counts = useMemo(() => ({ fresh: allRows.filter((row) => row.freshness === 'fresh').length, history: allRows.filter((row) => row.freshness === 'historical').length, missing: allRows.filter((row) => row.freshness === 'missing').length }), [allRows])

  const change = <K extends keyof Filters>(key: K, value: Filters[K]) => setFilters((current) => ({ ...current, [key]: value }))
  const share = async () => {
    const url = new URL(location.href); url.search = ''; url.searchParams.set('plan', filters.planId); url.searchParams.set('currency', filters.currency); url.searchParams.set('sort', filters.sort); url.searchParams.set('tax', filters.tax); url.searchParams.set('view', filters.view); if (filters.search) url.searchParams.set('search', filters.search)
    try { await navigator.clipboard.writeText(url.toString()); setNotice('筛选链接已复制') } catch { setNotice('无法访问剪贴板，请复制浏览器地址') }
    window.setTimeout(() => setNotice(null), 2400)
  }

  return <div className="app-shell">
    <header className="masthead">
      <div className="brand-block">
        <div className="eyebrow"><span className="live-dot" />官网配置快照</div>
        <h1>ChatGPT 各地区订阅价格</h1>
        <p>比较官网网页月订阅展示标价。换算仅作估算，税费状态与实际账号可购资格请以结账页为准。</p>
      </div>
      <div className="snapshot-block">
        <span>数据版本</span><strong>{bundle?.manifest.data_snapshot_id ?? '尚未载入'}</strong>
        <small>{bundle ? `生成于 ${formatTime(bundle.manifest.generated_at)}` : '正在读取已发布快照'}</small>
      </div>
    </header>

    <main>
      <section className="plan-bar" aria-label="选择套餐">
        {planIds.map((id) => {
          const item = bundle?.plans.plans.find((p) => p.plan_id === id)
          return <button key={id} className={filters.planId === id ? 'active' : ''} onClick={() => change('planId', id)}>{item?.label ?? id.replace('_', ' ')}</button>
        })}
        <span className="plan-note">官网网页 · 月付</span>
      </section>

      <section className="controls" aria-label="筛选与排序">
        <label className="search-control"><span className="search-icon">⌕</span><input value={filters.search} onChange={(event) => change('search', event.target.value)} placeholder="搜索中文、英文、地区或币种代码" aria-label="搜索地区" />{filters.search && <button onClick={() => change('search', '')} aria-label="清空搜索">×</button>}</label>
        <div className="segmented" aria-label="换算币种">{(['CNY', 'USD'] as const).map((value) => <button key={value} className={filters.currency === value ? 'active' : ''} onClick={() => change('currency', value)}>{value}</button>)}</div>
        <label>排序<select value={filters.sort} onChange={(event) => change('sort', event.target.value as Filters['sort'])}><option value="price_asc">折算价：低到高</option><option value="price_desc">折算价：高到低</option><option value="name">地区名称</option><option value="verified">最近核实</option></select></label>
        <label>税状态<select value={filters.tax} onChange={(event) => change('tax', event.target.value as Filters['tax'])}><option value="all">全部</option><option value="inclusive">含税</option><option value="exclusive">未税</option><option value="unknown">未知</option></select></label>
        <label>地区视图<select value={filters.view} onChange={(event) => change('view', event.target.value as Filters['view'])}><option value="current">当前有效报价</option><option value="all">全部地区</option><option value="history">历史报价</option><option value="missing">暂缺数据</option></select></label>
      </section>

      {error && <section className="state-panel error"><strong>暂时无法读取已发布数据</strong><p>{error}</p><button onClick={() => void refresh()}>重新读取快照</button></section>}
      {loading && !bundle && <section className="state-panel loading"><span className="spinner" /><strong>正在校验并载入数据快照…</strong><p>页面会在价格、覆盖、地区与汇率版本一致后一次显示。</p></section>}

      {bundle && <>
        <section className="summary-grid" aria-label="数据摘要">
          <article><span>地区目录</span><strong>{bundle.regions.regions.filter((r) => r.is_country_directory).length}</strong><small>完整目录，可搜索缺失项</small></article>
          <article><span>{plan?.label ?? filters.planId} · 新鲜报价</span><strong>{counts.fresh}</strong><small>最近 72 小时成功核实</small></article>
          <article><span>暂缺 / 历史</span><strong>{counts.missing} <i>/</i> {counts.history}</strong><small>当前筛选结果</small></article>
          <article className="price-card"><span>已核验地区展示价范围</span><strong>{ranked.length ? `${formatDecimal(ranked[0].estimate, filters.currency)} — ${formatDecimal(ranked[ranked.length - 1].estimate, filters.currency)}` : '暂无新鲜数据'}</strong><small>同一汇率快照 · 不含缺失换算</small></article>
        </section>

        <CoverageStrip bundle={bundle} />

        <div className="table-toolbar">
          <div className="view-switch"><button className={mode === 'list' ? 'active' : ''} onClick={() => setMode('list')}>价格排序表</button><button className={mode === 'matrix' ? 'active' : ''} onClick={() => setMode('matrix')}>全地区五档矩阵</button></div>
          <div className="actions"><button onClick={() => download(`chatgpt-${filters.planId}-${filters.currency}.csv`, rowsToCsv(rows, filters.currency))}>下载 CSV</button><button onClick={() => void share()}>分享筛选</button><button onClick={() => void refresh()} disabled={loading}>{loading ? '读取中…' : '读取最新快照'}</button></div>
        </div>

        {mode === 'list' ? <PriceTable rows={rows} ranked={ranked} currency={filters.currency} onSelect={setSelected} /> : <Matrix bundle={bundle} search={filters.search} onSelect={(row) => setSelected(row)} now={now} currency={filters.currency} />}

        <footer><div><strong>口径说明</strong><p>排名仅包含符合当前筛选、72 小时内核实、可参与排名且存在汇率换算的官网主展示金额。不同税状态不会被自动加税或去税。</p></div><div><strong>汇率快照</strong><p>{bundle.fx.fx_snapshot_id ?? '暂无汇率'} · {bundle.fx.fetched_at ? formatTime(bundle.fx.fetched_at) : '未取得'}{bundle.fx.stale ? ' · 已标记过期' : ''}</p></div></footer>
      </>}
    </main>
    {selected && <Details row={selected} bundle={bundle!} currency={filters.currency} onClose={() => setSelected(null)} />}
    {notice && <div className="toast" role="status">{notice}</div>}
  </div>
}

function PriceTable({ rows, ranked, currency, onSelect }: { rows: DisplayRow[]; ranked: DisplayRow[]; currency: 'CNY' | 'USD'; onSelect: (row: DisplayRow) => void }) {
  const ranks = new Map(ranked.map((row, index) => [row.region.target_id, index + 1]))
  if (!rows.length) return <div className="empty"><span>∅</span><strong>当前筛选下没有结果</strong><p>可以切换地区视图、税状态，或清空搜索条件。</p></div>
  return <div className="table-wrap price-table-wrap"><table className="price-table"><thead><tr><th className="rank">排名</th><th className="region-column">国家或地区</th><th>原币标价</th><th>{currency} 估算</th><th>税状态</th><th>数据状态</th><th>最近核实</th><th aria-label="详情" /></tr></thead><tbody>{rows.map((row) => <tr key={row.region.target_id} onClick={() => onSelect(row)} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') onSelect(row) }}><td className="rank">{ranks.get(row.region.target_id) ?? '—'}</td><td className="region-column"><strong>{row.region.name_zh}</strong><span>{row.region.name_en} · {row.region.country_code ?? row.region.config_id}</span></td><td className="money original-price" data-label="原币标价">{row.quote ? <><strong>{formatDecimal(row.quote.display_price.amount, row.quote.currency)}</strong><span>{row.quote.currency} / 月</span></> : <span>未取得</span>}</td><td className="money converted-price" data-label={`${currency} 估算`}><strong>{formatDecimal(row.estimate, currency)}</strong><span>{row.estimate ? '展示标价折算' : '暂缺汇率或报价'}</span></td><td className="tax-cell" data-label="税状态"><span className={`pill tax-${row.quote?.display_price.tax ?? 'unknown'}`}>{row.quote ? taxLabels[row.quote.display_price.tax] : '—'}</span></td><td className="status-cell" data-label="数据状态"><span className={`pill status-${row.freshness}`}>{statusLabels[row.freshness]}</span>{row.cell && row.cell.status !== 'valid_quote' && <small className="reason">{statusLabels[row.cell.status] ?? row.cell.status}</small>}</td><td className="date" data-label="最近核实">{formatTime(row.quote?.display_price.last_success_at)}</td><td className="chevron">›</td></tr>)}</tbody></table></div>
}

function Matrix({ bundle, search, onSelect, now, currency }: { bundle: DataBundle; search: string; onSelect: (row: DisplayRow) => void; now: number; currency: 'CNY' | 'USD' }) {
  const query = search.trim().toLocaleLowerCase()
  const regions = bundle.regions.regions.filter((region) => !query || [region.name_zh, region.name_en, region.country_code, region.config_id].some((v) => v?.toLocaleLowerCase().includes(query)))
  const cells = new Map(bundle.coverage.cells.map((cell) => [`${cell.target_id}:${cell.plan_id}`, cell]))
  return <div className="table-wrap matrix-wrap"><table className="matrix"><thead><tr><th className="region-column">国家或地区</th>{bundle.plans.plans.map((plan) => <th key={plan.plan_id}>{plan.label}</th>)}</tr></thead><tbody>{regions.map((region) => <tr key={region.target_id}><td className="region-column"><strong>{region.name_zh}</strong><span>{region.name_en} · {region.country_code ?? region.config_id}</span></td>{bundle.plans.plans.map((plan) => { const quote = quoteFor(bundle, region, plan.plan_id); const cell = cells.get(`${region.target_id}:${plan.plan_id}`) ?? null; const fresh = quote ? isFresh(quote, cell, now) : false; const row: DisplayRow = { region, quote, cell, freshness: fresh ? 'fresh' : quote ? 'historical' : 'missing', estimate: quote?.estimates[currency]?.amount ?? null }; return <td key={plan.plan_id}><button className={`matrix-cell ${row.freshness}`} onClick={() => onSelect(row)}><strong>{quote ? formatDecimal(quote.display_price.amount, quote.currency) : '—'}</strong><span>{fresh ? statusLabels.fresh : quote ? statusLabels.historical : statusLabels[regionStatus(cell)] ?? regionStatus(cell)}</span></button></td> })}</tr>)}</tbody></table></div>
}

function CoverageStrip({ bundle }: { bundle: DataBundle }) {
  const summary = bundle.coverage.summary
  const count = (key: string) => typeof summary[key] === 'number' ? summary[key] : 0
  const referencePath = bundle.manifest.entrypoints.reference_comparison
  return <section className="coverage-strip" aria-label="全目录采集覆盖"><div><span>预期请求</span><strong>{count('expected_request_count')}</strong></div><div><span>已尝试</span><strong>{count('attempted_request_count')}</strong></div><div><span>未尝试</span><strong>{count('not_attempted_request_count')}</strong></div><div><span>响应成功</span><strong>{count('successful_response_count')}</strong></div><div><span>五档完整</span><strong>{count('complete_five_plan_target_count')}</strong></div><div><span>辅助配置</span><strong>{count('auxiliary_target_count')}</strong></div><a href={`${import.meta.env.BASE_URL}data/${referencePath}`} target="_blank" rel="noreferrer">查看参考覆盖差异 ↗</a></section>
}

function Details({ row, bundle, currency, onClose }: { row: DisplayRow; bundle: DataBundle; currency: 'CNY' | 'USD'; onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    closeButton.current?.focus()
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])
  const quote = row.quote
  const plan = bundle.plans.plans.find((item) => item.plan_id === (quote?.plan_id ?? row.cell?.plan_id))
  const relevantRates = quote ? bundle.fx.rates.filter((rate) => [quote.currency, currency].includes(rate.quote)) : []
  const providers = relevantRates.flatMap((rate) => rate.providers).map(formatProvider).filter(Boolean)
  const rateDates = [...new Set(relevantRates.map((rate) => `${rate.quote} ${rate.rate_date}`))]
  return <div className="dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><aside className="drawer" role="dialog" aria-modal="true" aria-label="价格详情"><div className="drawer-head"><div><span className="eyebrow">价格详情</span><h2>{row.region.name_zh} <small>{row.region.country_code ?? row.region.config_id}</small></h2><p>{plan?.label ?? '套餐'} · 官网网页 · 月付</p></div><button ref={closeButton} className="close" onClick={onClose} aria-label="关闭">×</button></div>{quote ? <div className="detail-content"><section className="price-focus"><span>官网主展示金额</span><strong>{formatDecimal(quote.display_price.amount, quote.currency)}</strong><p><span className={`pill tax-${quote.display_price.tax}`}>{taxLabels[quote.display_price.tax]}</span> 原始值 {quote.display_price.amount} {quote.currency}</p></section><dl><Detail label="换算估算" value={formatDecimal(row.estimate, currency)} /><Detail label="官方另列金额" value={quote.psp_override ? `${formatDecimal(quote.psp_override.amount, quote.currency)} · ${taxLabels[quote.psp_override.tax]}${quote.psp_override_status === 'stale' ? ' · 历史值' : ''}` : quote.psp_override_status === 'invalid' ? '本轮字段无效' : '未提供'} /><Detail label="另列金额核实时间" value={formatTime(quote.psp_override?.last_success_at)} /><Detail label="另列金额状态" value={quote.psp_override_status} /><Detail label="地区支持状态" value={row.region.supported_status} /><Detail label="账号可购验证" value={quote.availability.purchase_verified ? '已验证' : '未验证'} /><Detail label="配置开放条件" value={quote.availability.pricing_rollout_gate == null ? '未列出（不代表所有账号均可购）' : JSON.stringify(quote.availability.pricing_rollout_gate)} /><Detail label="价格最近核实" value={formatTime(quote.display_price.last_success_at)} /><Detail label="价格首次变动" value={formatTime(quote.display_price.price_changed_at ?? quote.price_changed_at)} /><Detail label="汇率来源" value={providers.length ? [...new Set(providers)].join('、') : formatProvider(bundle.fx.source) || '未列出'} /><Detail label="汇率业务日期" value={rateDates.join('；') || '未取得'} /><Detail label="汇率获取时间" value={formatTime(bundle.fx.fetched_at)} /><Detail label="汇率快照" value={bundle.fx.fx_snapshot_id ?? '暂无'} /><Detail label="数据版本" value={bundle.manifest.data_snapshot_id} /></dl><a className="source-link" href={quote.source.url} target="_blank" rel="noreferrer">打开官方原始配置 <span>↗</span></a><details><summary>内部字段与证据</summary><pre>{JSON.stringify({ record_id: quote.record_id, config_id: quote.config_id, source_plan_key: quote.source_plan_key, billing_interval: quote.billing_interval, display_price: quote.display_price, psp_override: quote.psp_override, psp_override_status: quote.psp_override_status, tax_percent: quote.tax_percent, minor_unit_exponent: quote.minor_unit_exponent, fx_source: bundle.fx.source, fx_rates: relevantRates, body_sha256: quote.source.body_sha256, issues: quote.issues }, null, 2)}</pre></details></div> : <div className="empty detail-empty"><span>∅</span><strong>该单元格没有有效报价</strong><p>{row.cell ? `${statusLabels[row.cell.status] ?? row.cell.status}${row.cell.reason ? `：${row.cell.reason}` : ''}` : '本次发布数据中没有对应采集结果。'}</p><dl><Detail label="最近尝试" value={formatTime(row.cell?.last_attempt_at)} /><Detail label="HTTP 状态" value={row.cell?.http_status?.toString() ?? '—'} /><Detail label="地区配置 ID" value={row.region.config_id} /><Detail label="支持状态" value={row.region.supported_status} /><Detail label="账号可购验证" value="未验证" /></dl></div>}</aside></div>
}

function formatProvider(provider: unknown): string {
  if (typeof provider === 'string') return provider
  if (provider && typeof provider === 'object') {
    const data = provider as Record<string, unknown>
    const name = data.name ?? data.provider ?? data.id ?? data.source
    if (typeof name === 'string') return name
    return JSON.stringify(provider)
  }
  return ''
}

function Detail({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div> }
