import type { DataBundle, Manifest } from './types'

const SAFE_PATH = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

async function fetchBytes(path: string): Promise<Uint8Array<ArrayBuffer>> {
  const response = await fetch(`${import.meta.env.BASE_URL}data/${path}`, { cache: 'no-cache' })
  if (!response.ok) throw new Error(`数据文件读取失败（${response.status}）：${path}`)
  return new Uint8Array(await response.arrayBuffer())
}

function assertEntrypoint(path: string): void {
  if (!SAFE_PATH.test(path)) throw new Error(`清单包含不安全的数据路径：${path}`)
}

export async function loadBundle(): Promise<DataBundle> {
  const decode = (bytes: Uint8Array<ArrayBuffer>) => new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  const manifest = JSON.parse(decode(await fetchBytes('manifest.json'))) as Manifest
  if (!manifest?.entrypoints || !manifest.data_snapshot_id) throw new Error('数据清单格式无效')
  const entries = Object.entries(manifest.entrypoints).filter((entry): entry is [string, string] => typeof entry[1] === 'string')
  entries.forEach(([, path]) => assertEntrypoint(path))
  const fileMeta = new Map(manifest.files.map((file) => [file.path, file]))
  const loaded = await Promise.all(entries.map(async ([key, path]) => {
    const bytes = await fetchBytes(path)
    const metadata = fileMeta.get(path)
    if (!metadata?.sha256) throw new Error(`清单缺少入口文件校验信息：${path}`)
    if (bytes.byteLength !== metadata.bytes || await sha256Hex(bytes) !== metadata.sha256.toLowerCase()) throw new Error(`数据完整性检查失败：${path}`)
    return [key, JSON.parse(decode(bytes))] as const
  }))
  const documents = Object.fromEntries(loaded) as Record<string, any>
  if (manifest.schema_version !== '1.0.0' || entries.some(([key]) => documents[key]?.schema_version !== manifest.schema_version)) throw new Error('数据结构版本不受支持或入口版本不一致')
  if (!Array.isArray(documents.latest?.quotes) || !Array.isArray(documents.coverage?.cells) || !Array.isArray(documents.fx?.rates) || !Array.isArray(documents.regions?.regions) || !Array.isArray(documents.plans?.plans)) throw new Error('数据入口缺少必要列表')
  if (documents.latest.data_snapshot_id !== manifest.data_snapshot_id || documents.coverage.data_snapshot_id !== manifest.data_snapshot_id) throw new Error('价格与覆盖数据版本不一致')
  if (documents.latest.catalog_version !== manifest.catalog_version || documents.coverage.catalog_version !== manifest.catalog_version || documents.regions.catalog_version !== manifest.catalog_version) throw new Error('地区目录版本不一致')
  if (documents.latest.fx_snapshot_id !== manifest.fx_snapshot_id || documents.fx.fx_snapshot_id !== manifest.fx_snapshot_id) throw new Error('价格与汇率数据版本不一致')
  if (documents.status && documents.status.data_snapshot_id !== manifest.data_snapshot_id) throw new Error('运行状态与价格数据版本不一致')
  const targets = new Set<string>(documents.regions.regions.map((region: any) => region.target_id))
  const plans = new Set<string>(documents.plans.plans.map((plan: any) => plan.plan_id))
  if (!targets.size || plans.size !== 5 || planIdsMissing(plans)) throw new Error('地区目录或五档套餐目录不完整')
  const cellIds = new Set<string>()
  for (const cell of documents.coverage.cells) {
    if (!targets.has(cell.target_id) || !plans.has(cell.plan_id)) throw new Error('覆盖矩阵引用了未知地区或套餐')
    const identity = `${cell.target_id}:${cell.plan_id}`
    if (cellIds.has(identity)) throw new Error('覆盖矩阵包含重复单元格')
    cellIds.add(identity)
  }
  if (cellIds.size !== targets.size * plans.size) throw new Error('覆盖矩阵未包含完整地区与五档套餐')
  for (const quote of documents.latest.quotes) if (!targets.has(quote.target_id) || !plans.has(quote.plan_id)) throw new Error('报价引用了未知地区或套餐')
  return { manifest, latest: documents.latest, coverage: documents.coverage, fx: documents.fx, referenceComparison: documents.reference_comparison, regions: documents.regions, plans: documents.plans }
}

function planIdsMissing(plans: Set<string>): boolean {
  return ['go', 'plus', 'pro_100', 'pro_200', 'pro_500'].some((plan) => !plans.has(plan))
}
