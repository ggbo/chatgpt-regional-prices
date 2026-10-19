export type PlanId = 'go' | 'plus' | 'pro_100' | 'pro_200' | 'pro_500'
export type TaxStatus = 'inclusive' | 'exclusive' | 'unknown'
export type QuoteStatus = 'valid_quote' | 'stale_quote' | 'plan_field_missing' | 'request_failed' | 'access_blocked' | 'country_mismatch' | 'amount_invalid' | 'period_unverified' | 'unverified_range' | 'not_attempted'

export interface Plan { plan_id: PlanId; label: string; source_plan_key: string; billing_interval: 'month' }
export interface PlansDocument { schema_version: string; plans: Plan[] }
export interface Region { target_id: string; country_code: string | null; config_id: string; name_zh: string; name_en: string; region_kind: string; is_country_directory: boolean; ranking_eligible: boolean; supported_status: string; supported_checked_at: string | null; source_url: string; discovered_from: string[] }
export interface RegionsDocument { schema_version: string; catalog_version: string; regions: Region[] }
export interface PriceVariant { amount: string; tax: TaxStatus; source_tax_value: unknown; json_pointer: string; last_success_at: string; price_changed_at?: string | null }
export interface Estimate { amount: string; fx_snapshot_id: string }
export interface Quote {
  record_id: string; target_id: string; country_code: string | null; config_id: string; region_kind: string; channel: 'web'; plan_id: PlanId; source_plan_key: string; billing_interval: 'month'; offer_type: 'standard'; currency: string
  display_price: PriceVariant; psp_override: PriceVariant | null; psp_override_status: 'valid' | 'not_present' | 'invalid' | 'stale'; tax_percent: string | null; minor_unit_exponent: string | null
  availability: { configuration_present: boolean; purchase_verified: false; pricing_rollout_gate: unknown }
  source: { kind: string; url: string; body_sha256: string; collector_version: string }
  estimates: Record<'USD' | 'CNY', Estimate | null>; last_attempt_at: string; last_success_at: string; price_changed_at: string | null; issues: unknown[]; source_metadata?: Record<string, unknown>
}
export interface LatestDocument { schema_version: string; data_snapshot_id: string; generated_at: string; catalog_version: string; fx_snapshot_id: string | null; collector_version: string; quotes: Quote[] }
export interface CoverageCell { cell_id: string; target_id: string; country_code: string | null; config_id: string; region_kind: string; plan_id: PlanId; channel: 'web'; billing_interval: 'month'; offer_type: 'standard'; status: QuoteStatus; attempt_status: string; quote_record_id: string | null; last_attempt_at: string | null; last_success_at: string | null; http_status: number | null; reason: string | null }
export interface CoverageDocument { schema_version: string; data_snapshot_id: string; generated_at: string; catalog_version: string; summary: Record<string, number | Record<string, number>>; cells: CoverageCell[] }
export interface FxDocument { schema_version: string; fx_snapshot_id: string | null; generated_at: string; fetched_at: string | null; base: 'USD'; stale: boolean; source: unknown; rates: Array<{ quote: string; rate: string; rate_date: string; providers: unknown[] }> }
export interface ReferenceComparison { schema_version?: string; data_snapshot_id?: string; [key: string]: unknown }
export interface Manifest { schema_version: string; data_snapshot_id: string; generated_at: string; catalog_version: string; fx_snapshot_id: string | null; entrypoints: { latest: string; coverage: string; fx: string; reference_comparison: string; regions: string; plans: string; status?: string; [key: string]: string | undefined }; files: Array<{ path: string; sha256: string; bytes: number; media_type: string }> }
export interface RunStatus { schema_version: string; data_snapshot_id: string; result?: string; latest_refresh_failure?: { environment: string; run_url: string; completed_at: string; attempted_request_count: number; successful_response_count: number; reason: string }; [key: string]: unknown }
export interface DataBundle { manifest: Manifest; latest: LatestDocument; coverage: CoverageDocument; fx: FxDocument; referenceComparison: ReferenceComparison; regions: RegionsDocument; plans: PlansDocument; runStatus?: RunStatus }

export type DataView = 'current' | 'all' | 'history' | 'missing'
export type SortMode = 'price_asc' | 'price_desc' | 'name' | 'verified'
export interface Filters { planId: PlanId; currency: 'CNY' | 'USD'; sort: SortMode; tax: 'all' | TaxStatus; view: DataView; search: string }
export interface DisplayRow { region: Region; quote: Quote | null; cell: CoverageCell | null; freshness: 'fresh' | 'historical' | 'missing'; estimate: string | null }
