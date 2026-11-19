import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type PublishField = {
  属性名称: string
  数据类型: string
  是否可空: string
  是否必填: string
  是否可用: string
  主键字段: boolean
  登记时间: string
  可用时间: string
  计划可用时间: string
  发布快照版本: string
}

export type PublishSnapshot = {
  snapshot_id: string
  version: string
  status: string
  serves_queries: boolean
  primary_key_field: string
  primary_key_type: string
  missing_required_fields: string[]
  effective_from: string
  effective_to: string
  fields: PublishField[]
}

export type PublishView = {
  dim_code: string
  dim_name: string
  active_version: string
  active_snapshot_id: string
  candidate_version: string
  candidate_status: string
  candidate_missing_fields: string[]
  can_register: boolean
  registration_basis: string
  primary_key: { field: string; type: string; fields: { field: string; type: string }[] }
  fields: PublishField[]
  available_fields: string[]
  pending_fields: string[]
  incoming_fields: {
    属性名称: string
    数据类型: string
    是否可空: string
    是否必填: string
    是否已确认: string
    版本: string
    可见条件: string
  }[]
  field_timeline: {
    属性名称: string
    版本: string
    状态: string
    可用时间: string
    计划可用时间: string
    是否可空: string
  }[]
  snapshots: PublishSnapshot[]
  registrations: {
    属性名称: string
    数据类型: string
    发布快照版本: string
    登记时间: string
    可用时间: string
    状态: string
  }[]
  registration_versions: string[]
}

export async function loadPublishViews(): Promise<{ items: PublishView[]; total: number }> {
  const response = await request('/api/dim_attr/publish-view')
  if (!response.ok) throw new Error('维度发布视图读取失败')
  return await response.json()
}

export async function loadPublishView(dimCode: string): Promise<PublishView> {
  const response = await request(`/api/dim_attr/publish-view/${encodeURIComponent(dimCode)}`)
  if (!response.ok) throw new Error('维度发布视图读取失败')
  return await response.json()
}

export async function switchPublishSnapshot(dimCode: string): Promise<ActionResult> {
  const response = await request(`/api/dim_attr/publish-view/${encodeURIComponent(dimCode)}/switch`, {
    method: 'POST',
  })
  return await response.json()
}
