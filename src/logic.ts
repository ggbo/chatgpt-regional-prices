import type { CoverageCell, DataBundle, DisplayRow, Filters, FxDocument, PriceVariant, Quote, Region, SelectedPriceKind, TaxStatus } from './types'

export const FRESH_MS = 72 * 60 * 60 * 1000
export const DEFAULT_FILTERS: Filters = { planId: 'plus', currency: 'CNY', sort: 'price_asc', tax: 'inclusive', view: 'current', search: '' }

export function compareDecimal(a: string, b: string): number {
  const normalize = (value: string) => {
    const [integer = '0', fraction = ''] = value.replace(/^\+/, '').split('.')
    return { integer: integer.replace(/^0+(?=\d)/, ''), fraction: fraction.replace(/0+$/, '') }
  }
  const left = normalize(a); const right = normalize(b)
  if (left.integer.length !== right.integer.length) return left.integer.length < right.integer.length ? -1 : 1
  if (left.integer !== right.integer) return left.integer < right.integer ? -1 : 1
  const width = Math.max(left.fraction.length, right.fraction.length)
  const lf = left.fraction.padEnd(width, '0'); const rf = right.fraction.padEnd(width, '0')
  return lf === rf ? 0 : lf < rf ? -1 : 1
}

function variantIsFresh(price: PriceVariant, cell: CoverageCell | null, nowMs: number): boolean {
  const verified = Date.parse(price.last_success_at)
  return cell?.status === 'valid_quote' && Number.isFinite(verified) && nowMs - verified <= FRESH_MS
}

type Candidate = { price: PriceVariant; kind: SelectedPriceKind; fresh: boolean }

function candidatesFor(quote: Quote, cell: CoverageCell | null, nowMs: number): Candidate[] {
  const candidates: Candidate[] = [{ price: quote.display_price, kind: 'display_price', fresh: variantIsFresh(quote.display_price, cell, nowMs) }]
  if (quote.psp_override && (quote.psp_override_status === 'valid' || quote.psp_override_status === 'stale')) {
    candidates.push({ price: quote.psp_override, kind: 'psp_override', fresh: quote.psp_override_status === 'valid' && variantIsFresh(quote.psp_override, cell, nowMs) })
  }
  return candidates
}

function selectCandidate(candidates: Candidate[], tax: Filters['tax']): Candidate | null {
  if (tax !== 'all') return candidates.find((candidate) => candidate.price.tax === tax) ?? null
  const priority: TaxStatus[] = ['inclusive', 'exclusive', 'unknown']
  for (const status of priority) {
    const candidate = candidates.find((item) => item.price.tax === status)
    if (candidate) return candidate
  }
  return null
}

function decimalRatio(value: string): { numerator: bigint; denominator: bigint } | null {
  const match = /^\+?(\d+)(?:\.(\d+))?$/.exec(value.trim())
  if (!match) return null
  const fraction = match[2] ?? ''
  const numerator = BigInt(`${match[1]}${fraction}`)
  return { numerator, denominator: 10n ** BigInt(fraction.length) }
}

function rateRatio(fx: FxDocument, currency: string): { numerator: bigint; denominator: bigint } | null {
  if (currency === fx.base) return { numerator: 1n, denominator: 1n }
  const rate = fx.rates.find((item) => item.quote === currency)?.rate
  return rate ? decimalRatio(rate) : null
}

function decimalFromRatio(numerator: bigint, denominator: bigint, precision = 50): string | null {
  if (denominator <= 0n) return null
  const scale = 10n ** BigInt(precision)
  const scaled = numerator * scale
  let rounded = scaled / denominator
  if ((scaled % denominator) * 2n >= denominator) rounded += 1n
  const integer = rounded / scale
  const fraction = (rounded % scale).toString().padStart(precision, '0').replace(/0+$/, '')
  return fraction ? `${integer}.${fraction}` : integer.toString()
}

export function convertAmount(amount: string, sourceCurrency: string, targetCurrency: 'CNY' | 'USD', fx: FxDocument): string | null {
  if (!fx.fx_snapshot_id) return null
  const value = decimalRatio(amount)
  const sourceRate = rateRatio(fx, sourceCurrency)
  const targetRate = rateRatio(fx, targetCurrency)
  if (!value || !sourceRate || !targetRate || sourceRate.numerator <= 0n || targetRate.numerator <= 0n) return null
  return decimalFromRatio(
    value.numerator * sourceRate.denominator * targetRate.numerator,
    value.denominator * sourceRate.numerator * targetRate.denominator,
  )
}

export function buildRows(bundle: DataBundle, filters: Filters, nowMs = Date.now()): DisplayRow[] {
  const quotes = new Map(bundle.latest.quotes.filter((q) => q.plan_id === filters.planId && q.channel === 'web').map((q) => [q.target_id, q]))
  const cells = new Map(bundle.coverage.cells.filter((c) => c.plan_id === filters.planId && c.channel === 'web').map((c) => [c.target_id, c]))
  const query = filters.search.trim().toLocaleLowerCase()
  let rows = bundle.regions.regions.map((region): DisplayRow => {
    const availableQuote = quotes.get(region.target_id) ?? null
    const cell = cells.get(region.target_id) ?? null
    const candidates = availableQuote ? candidatesFor(availableQuote, cell, nowMs) : []
    const freshCandidates = candidates.filter((candidate) => candidate.fresh)
    const historicalCandidates = candidates.filter((candidate) => !candidate.fresh)
    const selected = filters.view === 'history'
      ? selectCandidate(historicalCandidates, filters.tax)
      : selectCandidate(freshCandidates, filters.tax) ?? selectCandidate(historicalCandidates, filters.tax)
    const excludedTax = filters.tax !== 'all' && !selected ? (freshCandidates[0] ?? historicalCandidates[0])?.price.tax ?? null : null
    const quote = selected ? availableQuote : null
    const freshness = selected ? (selected.fresh ? 'fresh' : 'historical') : 'missing'
    const estimate = quote && selected ? convertAmount(selected.price.amount, quote.currency, filters.currency, bundle.fx) : null
    return { region, quote, cell, selectedPrice: selected?.price ?? null, selectedPriceKind: selected?.kind ?? null, freshness, estimate, excludedTax }
  })
  if (query) rows = rows.filter(({ region }) => [region.name_zh, region.name_en, region.country_code, region.config_id, quotes.get(region.target_id)?.currency].some((value) => value?.toLocaleLowerCase().includes(query)))
  if (!query) {
    if (filters.view === 'current') rows = rows.filter((row) => row.freshness === 'fresh')
    if (filters.view === 'history') rows = rows.filter((row) => row.freshness === 'historical')
    if (filters.view === 'missing') rows = rows.filter((row) => row.freshness === 'missing')
  } else {
    if (filters.view === 'current') rows = rows.filter((row) => row.freshness !== 'historical')
    if (filters.view === 'history') rows = rows.filter((row) => row.freshness === 'historical')
    if (filters.view === 'missing') rows = rows.filter((row) => row.freshness === 'missing')
  }
  return sortRows(rows, filters.sort)
}

export function sortRows(rows: DisplayRow[], sort: Filters['sort']): DisplayRow[] {
  const stable = (a: DisplayRow, b: DisplayRow) => a.region.target_id.localeCompare(b.region.target_id) || (a.quote?.record_id ?? '').localeCompare(b.quote?.record_id ?? '')
  return [...rows].sort((a, b) => {
    if (sort === 'name') return a.region.name_zh.localeCompare(b.region.name_zh, 'zh-CN') || stable(a, b)
    if (sort === 'verified') {
      const av = a.selectedPrice?.last_success_at ?? ''; const bv = b.selectedPrice?.last_success_at ?? ''
      return (bv ? Date.parse(bv) : -Infinity) - (av ? Date.parse(av) : -Infinity) || stable(a, b)
    }
    if (a.estimate === null || b.estimate === null) return a.estimate === b.estimate ? stable(a, b) : a.estimate === null ? 1 : -1
    const compared = compareDecimal(a.estimate, b.estimate)
    return (sort === 'price_desc' ? -compared : compared) || stable(a, b)
  })
}

export function eligibleFreshRows(rows: DisplayRow[]): DisplayRow[] {
  return rows.filter((row) => row.freshness === 'fresh' && row.region.ranking_eligible && row.region.is_country_directory && row.estimate !== null)
}

export function rowsToCsv(rows: DisplayRow[], currency: 'CNY' | 'USD'): string {
  const escape = (value: unknown) => `"${String(value ?? '').replaceAll('"', '""')}"`
  const header = ['地区代码', '中文名', '英文名', '渠道', '套餐', '原币金额', '币种', `${currency}估算`, '税状态', '数据状态', '最近核实', '官方来源']
  const lines = rows.map((row) => [row.region.country_code ?? row.region.target_id, row.region.name_zh, row.region.name_en, '官网直购', row.quote?.plan_id, row.selectedPrice?.amount, row.quote?.currency, row.estimate, row.selectedPrice?.tax, row.freshness, row.selectedPrice?.last_success_at, row.quote?.source.url].map(escape).join(','))
  return ['\uFEFF' + header.map(escape).join(','), ...lines].join('\r\n')
}

export function regionStatus(cell: CoverageCell | null): string { return cell?.status ?? 'not_attempted' }
export function quoteFor(bundle: DataBundle, region: Region, planId: string): Quote | null { return bundle.latest.quotes.find((q) => q.target_id === region.target_id && q.plan_id === planId && q.channel === 'web') ?? null }
