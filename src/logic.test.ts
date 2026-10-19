import assert from 'node:assert/strict'
import test from 'node:test'
import { buildRows, compareDecimal, eligibleFreshRows, rowsToCsv, sortRows } from './logic.ts'
import type { DataBundle, DisplayRow, Filters, Quote, Region } from './types.ts'

const region = (id: string): Region => ({ target_id: id, country_code: id, config_id: id, name_zh: id, name_en: id, region_kind: 'iso', is_country_directory: true, ranking_eligible: true, supported_status: 'unknown', supported_checked_at: null, source_url: '', discovered_from: [] })
const quote = (id: string, estimate: string | null, at: string): Quote => ({ record_id: `${id}:web:plus:month:standard`, target_id: id, country_code: id, config_id: id, region_kind: 'iso', channel: 'web', plan_id: 'plus', source_plan_key: 'plus', billing_interval: 'month', offer_type: 'standard', currency: 'USD', display_price: { amount: '20', tax: 'exclusive', source_tax_value: 'exclusive', json_pointer: '', last_success_at: at }, psp_override: null, psp_override_status: 'not_present', tax_percent: null, minor_unit_exponent: '2', availability: { configuration_present: true, purchase_verified: false, pricing_rollout_gate: null }, source: { kind: 'official', url: `https://example.com/${id}`, body_sha256: '', collector_version: '1' }, estimates: { USD: estimate ? { amount: estimate, fx_snapshot_id: 'fx' } : null, CNY: estimate ? { amount: estimate, fx_snapshot_id: 'fx' } : null }, last_attempt_at: at, last_success_at: at, price_changed_at: at, issues: [] })

test('decimal sorting preserves precision and always puts null last', () => {
  assert.equal(compareDecimal('9.999', '10'), -1)
  const base = (id: string, value: string | null): DisplayRow => ({ region: region(id), quote: null, cell: null, freshness: 'fresh', estimate: value })
  assert.deepEqual(sortRows([base('A', null), base('B', '9.999'), base('C', '10')], 'price_desc').map((r) => r.region.target_id), ['C', 'B', 'A'])
  assert.deepEqual(sortRows([base('A', null), base('B', '9.999'), base('C', '10')], 'price_asc').map((r) => r.region.target_id), ['B', 'C', 'A'])
})

test('browser clock expires a quote after 72 hours and excludes it from ranking summary', () => {
  const now = Date.parse('2026-10-19T12:00:00Z'); const old = '2026-10-16T11:59:59Z'
  const bundle = { latest: { quotes: [quote('US', '20', old)] }, coverage: { cells: [{ target_id: 'US', plan_id: 'plus', channel: 'web', status: 'valid_quote' }] }, regions: { regions: [region('US')] } } as unknown as DataBundle
  const filters: Filters = { planId: 'plus', currency: 'USD', sort: 'price_asc', tax: 'all', view: 'all', search: '' }
  const rows = buildRows(bundle, filters, now)
  assert.equal(rows[0].freshness, 'historical'); assert.equal(eligibleFreshRows(rows).length, 0)
})

test('search finds missing catalog rows even in current view and CSV uses selected order', () => {
  const now = Date.parse('2026-10-19T12:00:00Z'); const recent = '2026-10-19T11:00:00Z'
  const bundle = { latest: { quotes: [quote('US', '20', recent)] }, coverage: { cells: [{ target_id: 'US', plan_id: 'plus', channel: 'web', status: 'valid_quote' }] }, regions: { regions: [region('US'), { ...region('PH'), name_zh: '菲律宾', name_en: 'Philippines' }] } } as unknown as DataBundle
  const filters: Filters = { planId: 'plus', currency: 'USD', sort: 'price_asc', tax: 'all', view: 'current', search: '菲律宾' }
  const rows = buildRows(bundle, filters, now)
  assert.deepEqual(rows.map((r) => r.region.target_id), ['PH']); assert.match(rowsToCsv(rows, 'USD'), /菲律宾/)
})
