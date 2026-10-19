import assert from 'node:assert/strict'
import test from 'node:test'
import { buildRows, compareDecimal, DEFAULT_FILTERS, eligibleFreshRows, rowsToCsv, sortRows } from './logic.ts'
import type { DataBundle, DisplayRow, Filters, PriceVariant, Quote, Region, TaxStatus } from './types.ts'

const RECENT = '2026-10-19T11:00:00Z'
const NOW = Date.parse('2026-10-19T12:00:00Z')

const region = (id: string): Region => ({ target_id: id, country_code: id, config_id: id, name_zh: id, name_en: id, region_kind: 'iso', is_country_directory: true, ranking_eligible: true, supported_status: 'unknown', supported_checked_at: null, source_url: '', discovered_from: [] })

function price(amount: string, tax: TaxStatus, at = RECENT): PriceVariant {
  return { amount, tax, source_tax_value: tax, json_pointer: '', last_success_at: at }
}

function quote(id: string, options: { amount?: string; tax?: TaxStatus; at?: string; currency?: string; psp?: PriceVariant | null; pspStatus?: Quote['psp_override_status'] } = {}): Quote {
  const at = options.at ?? RECENT
  return {
    record_id: `${id}:web:plus:month:standard`, target_id: id, country_code: id, config_id: id, region_kind: 'iso', channel: 'web', plan_id: 'plus', source_plan_key: 'plus', billing_interval: 'month', offer_type: 'standard', currency: options.currency ?? 'USD',
    display_price: price(options.amount ?? '20', options.tax ?? 'exclusive', at), psp_override: options.psp ?? null, psp_override_status: options.pspStatus ?? (options.psp ? 'valid' : 'not_present'), tax_percent: null, minor_unit_exponent: '2',
    availability: { configuration_present: true, purchase_verified: false, pricing_rollout_gate: null }, source: { kind: 'official', url: `https://example.com/${id}`, body_sha256: '', collector_version: '1' },
    estimates: { USD: null, CNY: null }, last_attempt_at: at, last_success_at: at, price_changed_at: at, issues: [],
  }
}

function bundle(quotes: Quote[], regions = quotes.map((item) => region(item.target_id)), rates: Array<{ quote: string; rate: string }> = [{ quote: 'CNY', rate: '7' }, { quote: 'PHP', rate: '56' }]): DataBundle {
  return {
    latest: { quotes },
    coverage: { cells: quotes.map((item) => ({ target_id: item.target_id, plan_id: 'plus', channel: 'web', status: 'valid_quote' })) },
    regions: { regions },
    fx: { fx_snapshot_id: 'test-fx', base: 'USD', rates: rates.map((item) => ({ ...item, rate_date: '2026-10-19', providers: [] })) },
  } as unknown as DataBundle
}

const filters = (overrides: Partial<Filters> = {}): Filters => ({ planId: 'plus', currency: 'CNY', sort: 'price_asc', tax: 'inclusive', view: 'all', search: '', ...overrides })

test('default view is CNY and strictly inclusive', () => {
  assert.equal(DEFAULT_FILTERS.currency, 'CNY')
  assert.equal(DEFAULT_FILTERS.tax, 'inclusive')
})

test('decimal sorting preserves precision and always puts null last', () => {
  assert.equal(compareDecimal('9.999', '10'), -1)
  const base = (id: string, value: string | null): DisplayRow => ({ region: region(id), quote: null, cell: null, selectedPrice: null, selectedPriceKind: null, freshness: 'fresh', estimate: value, excludedTax: null })
  assert.deepEqual(sortRows([base('A', null), base('B', '9.999'), base('C', '10')], 'price_desc').map((row) => row.region.target_id), ['C', 'B', 'A'])
  assert.deepEqual(sortRows([base('A', null), base('B', '9.999'), base('C', '10')], 'price_asc').map((row) => row.region.target_id), ['B', 'C', 'A'])
})

test('tax switch selects main inclusive or PSP exclusive and recalculates estimate and CSV', () => {
  const ph = quote('PH', { amount: '56', tax: 'inclusive', currency: 'PHP', psp: price('28', 'exclusive') })
  const data = bundle([ph])
  const inclusive = buildRows(data, filters({ tax: 'inclusive' }), NOW)[0]
  assert.equal(inclusive.selectedPriceKind, 'display_price'); assert.equal(inclusive.selectedPrice?.amount, '56'); assert.equal(inclusive.estimate, '7')
  const exclusive = buildRows(data, filters({ tax: 'exclusive' }), NOW)[0]
  assert.equal(exclusive.selectedPriceKind, 'psp_override'); assert.equal(exclusive.selectedPrice?.amount, '28'); assert.equal(exclusive.estimate, '3.5')
  assert.match(rowsToCsv([exclusive], 'CNY'), /"28","PHP","3\.5","exclusive"/)
})

test('all tax mode prefers current inclusive PSP over current exclusive display price', () => {
  const row = buildRows(bundle([quote('US', { amount: '20', tax: 'exclusive', psp: price('24', 'inclusive') })]), filters({ tax: 'all', currency: 'USD' }), NOW)[0]
  assert.equal(row.selectedPriceKind, 'psp_override'); assert.equal(row.selectedPrice?.amount, '24'); assert.equal(row.estimate, '24')
})

test('tax selection can reverse regional ranking without mixing estimates', () => {
  const data = bundle([quote('A', { amount: '10', tax: 'exclusive', psp: price('100', 'inclusive') }), quote('B', { amount: '20', tax: 'exclusive', psp: price('50', 'inclusive') })])
  assert.deepEqual(buildRows(data, filters({ tax: 'exclusive', currency: 'USD' }), NOW).map((row) => row.region.target_id), ['A', 'B'])
  assert.deepEqual(buildRows(data, filters({ tax: 'inclusive', currency: 'USD' }), NOW).map((row) => row.region.target_id), ['B', 'A'])
})

test('conversion preserves distinctions beyond 18 decimal places for sorting', () => {
  const data = bundle([quote('B', { amount: '1.00000000000000000002', tax: 'inclusive' }), quote('A', { amount: '1.00000000000000000001', tax: 'inclusive' })])
  const rows = buildRows(data, filters({ currency: 'USD' }), NOW)
  assert.deepEqual(rows.map((row) => row.region.target_id), ['A', 'B'])
  assert.equal(rows[0].estimate, '1.00000000000000000001')
  assert.equal(rows[1].estimate, '1.00000000000000000002')
})

test('old or invalid PSP does not masquerade as a fresh inclusive price', () => {
  const old = '2026-10-16T11:59:59Z'
  const expired = quote('OLD', { amount: '20', tax: 'exclusive', psp: price('24', 'inclusive', old), pspStatus: 'valid' })
  const stale = quote('STALE', { amount: '20', tax: 'exclusive', psp: price('24', 'inclusive'), pspStatus: 'stale' })
  const invalid = quote('BAD', { amount: '20', tax: 'exclusive', psp: price('24', 'inclusive'), pspStatus: 'invalid' })
  const data = bundle([expired, stale, invalid])
  const all = buildRows(data, filters({ tax: 'inclusive', view: 'all' }), NOW)
  assert.deepEqual(all.filter((row) => row.freshness === 'historical').map((row) => row.region.target_id), ['OLD', 'STALE'])
  assert.equal(all.find((row) => row.region.target_id === 'BAD')?.freshness, 'missing')
  assert.equal(eligibleFreshRows(all).length, 0)
  const history = buildRows(data, filters({ tax: 'inclusive', view: 'history' }), NOW)
  assert.deepEqual(history.map((row) => row.region.target_id), ['OLD', 'STALE'])
  assert.equal(history[0].selectedPriceKind, 'psp_override'); assert.equal(history[0].selectedPrice?.last_success_at, old)
})

test('missing source FX keeps original price and puts estimate last', () => {
  const data = bundle([quote('US', { amount: '20', tax: 'inclusive' }), quote('ZZ', { amount: '1', tax: 'inclusive', currency: 'ZZZ' })])
  const rows = buildRows(data, filters(), NOW)
  assert.deepEqual(rows.map((row) => row.region.target_id), ['US', 'ZZ'])
  assert.equal(rows[1].selectedPrice?.amount, '1'); assert.equal(rows[1].estimate, null)
  assert.deepEqual(eligibleFreshRows(rows).map((row) => row.region.target_id), ['US'])
})

test('missing FX snapshot id disables conversion even when rates are present', () => {
  const data = bundle([quote('US', { amount: '20', tax: 'inclusive' })])
  data.fx.fx_snapshot_id = null
  const row = buildRows(data, filters(), NOW)[0]
  assert.equal(row.selectedPrice?.amount, '20'); assert.equal(row.estimate, null)
})

test('strict tax mismatch keeps the directory row but excludes its amount from CSV', () => {
  const data = bundle([quote('US', { tax: 'exclusive' }), quote('PH', { amount: '18', tax: 'inclusive' })])
  const rows = buildRows(data, filters(), NOW)
  const us = rows.find((row) => row.region.target_id === 'US')!
  assert.equal(us.quote, null); assert.equal(us.estimate, null); assert.equal(us.excludedTax, 'exclusive')
  assert.deepEqual(eligibleFreshRows(rows).map((row) => row.region.target_id), ['PH'])
  assert.doesNotMatch(rowsToCsv(rows, 'CNY'), /"US"[^\r\n]*"20"/)
})

test('all view preserves historical evidence while current excludes it from ranking', () => {
  const old = '2026-10-16T11:59:59Z'
  const data = bundle([quote('US', { at: old, tax: 'inclusive' })])
  const all = buildRows(data, filters({ tax: 'all', view: 'all' }), NOW)
  assert.equal(all[0].freshness, 'historical'); assert.equal(all[0].selectedPrice?.last_success_at, old)
  assert.equal(eligibleFreshRows(all).length, 0)
  assert.equal(buildRows(data, filters({ tax: 'all', view: 'current' }), NOW).length, 0)
  const history = buildRows(data, filters({ tax: 'all', view: 'history' }), NOW)
  assert.equal(history[0].freshness, 'historical'); assert.equal(eligibleFreshRows(history).length, 0)
})

test('search retains a matching missing catalog row in current view', () => {
  const philippines = { ...region('PH'), name_zh: '菲律宾', name_en: 'Philippines' }
  const data = bundle([quote('US', { tax: 'inclusive' })], [region('US'), philippines])
  const rows = buildRows(data, filters({ view: 'current', search: '菲律宾' }), NOW)
  assert.deepEqual(rows.map((row) => row.region.target_id), ['PH'])
  assert.equal(rows[0].selectedPrice, null); assert.match(rowsToCsv(rows, 'CNY'), /菲律宾/)
})
