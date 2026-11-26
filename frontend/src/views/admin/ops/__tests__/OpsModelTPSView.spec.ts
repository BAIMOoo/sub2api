import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { Line } from 'vue-chartjs'
import OpsModelTPSView from '../OpsModelTPSView.vue'

const getModelTPSTrend = vi.hoisted(() => vi.fn())

vi.mock('@/api/admin/ops', () => ({ opsAPI: { getModelTPSTrend } }))
vi.mock('@/api', () => ({ adminAPI: { groups: { getAll: async () => [] } } }))
vi.mock('@/stores', () => ({
  useAdminSettingsStore: () => ({ opsMonitoringEnabled: true, fetch: async () => {} })
}))
vi.mock('vue-router', async () => ({
  ...await vi.importActual<typeof import('vue-router')>('vue-router'),
  useRoute: () => ({ query: { tr: '5m', platform: 'openai', group_id: '17' } })
}))
vi.mock('vue-i18n', async () => ({
  ...await vi.importActual<typeof import('vue-i18n')>('vue-i18n'),
  useI18n: () => ({
    t: (key: string, params?: { count: string }) => params ? `${key} ${params.count}` : key
  })
}))
vi.mock('vue-chartjs', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    Line: defineComponent({
      props: ['data', 'options'],
      render: () => h('canvas')
    })
  }
})

it('compares selected request models, switches percentiles, and leaves empty buckets blank', async () => {
  const window = {
    start_time: '2026-11-26T00:00:00Z',
    end_time: '2026-11-26T00:05:00Z',
    bucket_seconds: 60
  }
  const points = [
    { model: 'terra', bucket_start: '2026-11-26T00:00:00Z', p50: 44, p10: 29, sample_count: 11 },
    { model: 'terra', bucket_start: '2026-11-26T00:02:00Z', p50: 48, p10: 33, sample_count: 5 },
    { model: 'sol', bucket_start: '2026-11-26T00:00:00Z', p50: 30, p10: 18, sample_count: 7 },
    { model: 'sol', bucket_start: '2026-11-26T00:01:00Z', p50: 27, p10: 15, sample_count: 3 }
  ]
  getModelTPSTrend.mockImplementation(async (params: { models?: string[] }) => ({
    ...window,
    models: params.models ? [] : ['sol', 'terra'],
    points: points.filter(({ model }) => params.models?.includes(model))
  }))

  const wrapper = mount(OpsModelTPSView, {
    global: {
      stubs: {
        AppLayout: { template: '<main><slot /></main>' },
        RouterLink: true,
        HelpTooltip: true,
        Icon: true,
        Select: {
          props: ['modelValue', 'options'],
          emits: ['update:modelValue'],
          template: `<select :value="modelValue ?? ''" @change="$emit('update:modelValue', $event.target.value)">
            <option value=""></option>
            <option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>`
        }
      }
    }
  })
  await flushPromises()

  const filters = { time_range: '5m', platform: 'openai', group_id: 17 }
  expect(getModelTPSTrend).toHaveBeenCalledWith(filters, expect.objectContaining({ signal: expect.any(AbortSignal) }))

  const modelPicker = wrapper.get('[data-testid="model-tps-select"]')
  await modelPicker.setValue('terra')
  await flushPromises()
  await modelPicker.setValue('sol')
  await flushPromises()
  expect(getModelTPSTrend).toHaveBeenLastCalledWith(
    { ...filters, models: ['terra', 'sol'] },
    expect.objectContaining({ signal: expect.any(AbortSignal) })
  )

  const chart = wrapper.getComponent(Line)
  const data = () => chart.props('data').datasets.map(({ label, data }: { label: string; data: (number | null)[] }) => ({ label, data }))
  expect(data()).toEqual([
    { label: 'terra', data: [44, null, 48, null, null] },
    { label: 'sol', data: [30, 27, null, null, null] }
  ])

  const label = chart.props('options').plugins.tooltip.callbacks.label
  expect(label({ dataset: { label: 'terra' }, dataIndex: 0 })).toBe(
    'terra · P50 44.0 · P10 29.0 tok/s · admin.ops.outputTpsSamples 11'
  )

  const queries = getModelTPSTrend.mock.calls.length
  await wrapper.get('[data-testid="model-tps-p10"]').trigger('click')
  expect(data()).toEqual([
    { label: 'terra', data: [29, null, 33, null, null] },
    { label: 'sol', data: [18, 15, null, null, null] }
  ])
  expect(chart.props('data').datasets.every(({ spanGaps }: { spanGaps: boolean }) => !spanGaps)).toBe(true)
  expect(getModelTPSTrend).toHaveBeenCalledTimes(queries)
  wrapper.unmount()
})
