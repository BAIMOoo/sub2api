<script setup lang="ts">
import { computed, onMounted, ref, watch, type ComputedRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import {
  Chart as ChartJS,
  CategoryScale,
  Interaction,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
  type ChartOptions,
  type InteractionItem,
  type InteractionModeFunction
} from 'chart.js'
import { Line } from 'vue-chartjs'
import { adminAPI } from '@/api'
import { opsAPI, type OpsModelTPSParams, type OpsModelTPSPoint, type OpsModelTPSTrendResponse } from '@/api/admin/ops'
import AppLayout from '@/components/layout/AppLayout.vue'
import HelpTooltip from '@/components/common/HelpTooltip.vue'
import Select from '@/components/common/Select.vue'
import Icon from '@/components/icons/Icon.vue'
import { CONCRETE_PLATFORM_OPTIONS } from '@/constants/platforms'
import { useAdminSettingsStore } from '@/stores'
import type { AdminGroup } from '@/types'
import { formatDateTimeLocalInput, parseDateTimeLocalInput } from '@/utils/format'
import { formatHistoryLabel } from './utils/opsFormatters'

declare module 'chart.js' {
  interface InteractionModeMap {
    modelTPSBucket: InteractionModeFunction
  }
}

ChartJS.register(CategoryScale, Legend, LineElement, LinearScale, PointElement, Tooltip)
Interaction.modes.modelTPSBucket = (chart, { x, y }) => {
  if (x == null || y == null || !chart.isPointInArea({ x, y })) return []

  // Map the horizontal position to a time bucket before collecting model points.
  const index = chart.scales.x.getValueForPixel(x)
  if (index === undefined) return []
  const items: InteractionItem[] = []
  for (const dataset of chart.getSortedVisibleDatasetMetas()) {
    const point = dataset.data[index]
    if (point instanceof PointElement && !point.skip) {
      items.push({ element: point, datasetIndex: dataset.index, index })
    }
  }
  return items
}

const { t, n } = useI18n()
const route = useRoute()
const settings = useAdminSettingsStore()
const ready = ref(false)
const timeRanges = ['5m', '30m', '1h', '6h', '24h', 'custom'] as const
const maxCustomSeconds = 30 * 24 * 60 * 60

function queryText(key: string): string {
  const value = route.query[key]
  return typeof value === 'string' ? value : ''
}

function customRange(start: number, end: number): Pick<OpsModelTPSParams, 'start_time' | 'end_time'> | null {
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end || (end - start) / 1000 > maxCustomSeconds) return null
  return { start_time: new Date(start).toISOString(), end_time: new Date(end).toISOString() }
}

const timeRange = ref(timeRanges.find((range) => range === queryText('tr')) ?? '1h')
const platform = ref(queryText('platform'))
const initialGroup = Number(queryText('group_id'))
const groupId = ref(Number.isSafeInteger(initialGroup) && initialGroup > 0 ? initialGroup : null)
const startFromRoute = Date.parse(queryText('start_time'))
const endFromRoute = Date.parse(queryText('end_time'))
const appliedRange = ref(customRange(startFromRoute, endFromRoute))
const localStart = ref(formatDateTimeLocalInput((appliedRange.value ? startFromRoute : Date.now() - 3600_000) / 1000))
const localEnd = ref(formatDateTimeLocalInput((appliedRange.value ? endFromRoute : Date.now()) / 1000))
const invalidRange = ref(false)

function applyRange() {
  const start = parseDateTimeLocalInput(localStart.value)
  const end = parseDateTimeLocalInput(localEnd.value)
  const range = start == null || end == null ? null : customRange(start * 1000, end * 1000)
  invalidRange.value = range == null
  if (range) appliedRange.value = range
}

const groups = ref<AdminGroup[]>([])
const groupError = ref(false)
const platformOptions = computed(() => [{ value: '', label: t('common.all') }, ...CONCRETE_PLATFORM_OPTIONS])
const groupOptions = computed(() => [
  { value: null, label: t('common.all') },
  ...groups.value.filter((group) => !platform.value || group.platform === platform.value)
    .map((group) => ({ value: group.id, label: group.name }))
])
const timeOptions = computed(() => timeRanges.map((range) => ({ value: range, label: t(`admin.ops.timeRange.${range}`) })))
const overviewQuery = computed(() => ({
  tr: timeRange.value === 'custom' ? undefined : timeRange.value,
  platform: platform.value || undefined,
  group_id: groupId.value?.toString()
}))

function changePlatform(value: string | number | boolean | null) {
  const next = String(value ?? '')
  if (next === platform.value) return
  platform.value = next
  const group = groups.value.find(({ id }) => id === groupId.value)
  if (next && group?.platform !== next) groupId.value = null
}

onMounted(async () => {
  await settings.fetch()
  ready.value = true
  if (!settings.opsMonitoringEnabled) return
  try {
    groups.value = await adminAPI.groups.getAll()
  } catch {
    groupError.value = true
  }
})

const params = computed<OpsModelTPSParams | null>(() => {
  if (!ready.value || !settings.opsMonitoringEnabled) return null
  const time = timeRange.value === 'custom' ? appliedRange.value : { time_range: timeRange.value }
  if (!time) return null
  return {
    ...time,
    platform: platform.value || undefined,
    group_id: groupId.value ?? undefined
  }
})

const refresh = ref(0)

function useModelTPSQuery(query: ComputedRef<OpsModelTPSParams | null>) {
  const data = ref<OpsModelTPSTrendResponse | null>(null)
  const loading = ref(false)
  const failed = ref(false)

  watch([query, refresh], async ([filter], _, onCleanup) => {
    data.value = null
    failed.value = false
    loading.value = filter != null
    if (!filter) return

    const controller = new AbortController()
    onCleanup(() => controller.abort())
    try {
      const result = await opsAPI.getModelTPSData(filter, { signal: controller.signal })
      if (!controller.signal.aborted) data.value = result
    } catch {
      if (!controller.signal.aborted) failed.value = true
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  }, { immediate: true })

  return { data, loading, failed }
}

const { data: modelList, loading: loadingModels, failed: modelError } = useModelTPSQuery(params)
const availableModels = computed(() => modelList.value?.models ?? [])
const models = ref<string[]>([])
const modelOptions = computed(() => availableModels.value.filter((model) => !models.value.includes(model))
  .map((model) => ({ value: model, label: model })))

function addModel(value: string | number | boolean | null) {
  if (typeof value === 'string' && value && !models.value.includes(value)) {
    models.value = [...models.value, value]
  }
}

function removeModel(model: string) {
  models.value = models.value.filter((selected) => selected !== model)
}

const trendParams = computed(() => {
  const filter = params.value
  return filter && models.value.length ? { ...filter, models: models.value } : null
})
const { data: trend, loading: loadingTrend, failed: trendError } = useModelTPSQuery(trendParams)
const metric = ref<'p50' | 'p10'>('p50')
const metricOptions = ['p50', 'p10'] as const

const buckets = computed(() => {
  const data = trend.value
  if (!data?.points.length || data.bucket_seconds <= 0) return []

  const size = data.bucket_seconds * 1000
  const start = Date.parse(data.start_time)
  const end = Date.parse(data.end_time)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return []

  const first = Math.floor(start / size) * size
  return Array.from({ length: Math.ceil((end - first) / size) }, (_, index) => first + index * size)
})

const pointsByModel = computed(() => {
  const result = new Map<string, Map<number, OpsModelTPSPoint>>()
  for (const point of trend.value?.points ?? []) {
    let points = result.get(point.model)
    if (!points) {
      points = new Map()
      result.set(point.model, points)
    }
    points.set(Date.parse(point.bucket_start), point)
  }
  return result
})

const colors = ['#2563eb', '#059669', '#d97706', '#9333ea', '#dc2626', '#0891b2', '#db2777', '#4f46e5']
const chartData = computed(() => {
  if (!buckets.value.length || !trend.value) return null
  const minutes = (Date.parse(trend.value.end_time) - Date.parse(trend.value.start_time)) / 60_000
  return {
    labels: buckets.value.map((bucket) => formatHistoryLabel(new Date(bucket).toISOString(), minutes)),
    datasets: models.value.map((model, index) => {
      const points = pointsByModel.value.get(model)
      return {
        label: model,
        data: buckets.value.map((bucket) => points?.get(bucket)?.[metric.value] ?? null),
        borderColor: colors[index % colors.length],
        borderDash: index >= colors.length ? [6, 3] : [],
        spanGaps: false,
        pointRadius: buckets.value.length > 120 ? 1 : 2,
        pointHoverRadius: 5,
        borderWidth: 2
      }
    })
  }
})

const chartOptions: ChartOptions<'line'> = {
  responsive: true,
  animation: false,
  maintainAspectRatio: false,
  interaction: { mode: 'modelTPSBucket' },
  scales: {
    x: { ticks: { maxTicksLimit: 9 } },
    y: { beginAtZero: true, title: { display: true, text: 'tok/s' } }
  },
  plugins: {
    legend: { position: 'bottom' },
    tooltip: {
      callbacks: {
        label: ({ dataset, dataIndex }) => {
          const model = dataset.label ?? ''
          const point = pointsByModel.value.get(model)?.get(buckets.value[dataIndex])
          if (!point) return model
          const samples = t('admin.ops.outputTpsSamples', { count: n(point.sample_count) })
          return `${model} · P50 ${point.p50.toFixed(1)} · P10 ${point.p10.toFixed(1)} tok/s · ${samples}`
        }
      }
    }
  }
}
</script>

<template>
  <AppLayout>
    <div class="space-y-5 pb-10">
      <div>
        <RouterLink :to="{ name: 'AdminOps', query: overviewQuery }" class="text-sm font-medium text-primary-600 hover:underline dark:text-primary-400">
          {{ t('admin.ops.title') }}
        </RouterLink>
        <h1 class="mt-2 text-2xl font-bold text-gray-900 dark:text-white lg:hidden">{{ t('admin.ops.modelTps.title') }}</h1>
      </div>

      <p v-if="!ready" role="status" class="card p-5 text-sm text-gray-500">{{ t('admin.ops.loadingText') }}</p>
      <p v-else-if="!settings.opsMonitoringEnabled" class="card p-5 text-sm text-gray-500">{{ t('admin.settings.opsMonitoring.disabled') }}</p>
      <template v-else>
        <div class="card space-y-4 p-4 sm:p-5">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <label for="model-tps-time-range" class="input-label">{{ t('usage.timeRange') }}</label>
              <Select id="model-tps-time-range" :aria-label="t('usage.timeRange')" v-model="timeRange" :options="timeOptions" />
            </div>
            <div>
              <label for="model-tps-platform" class="input-label">{{ t('admin.ops.requestDetails.table.platform') }}</label>
              <Select id="model-tps-platform" :aria-label="t('admin.ops.requestDetails.table.platform')" :model-value="platform" :options="platformOptions" @update:model-value="changePlatform" />
            </div>
            <div>
              <label for="model-tps-group" class="input-label">{{ t('admin.usage.group') }}</label>
              <Select id="model-tps-group" :aria-label="t('admin.usage.group')" v-model="groupId" :options="groupOptions" searchable />
            </div>
            <div>
              <label for="model-tps-model" class="input-label">{{ t('admin.ops.modelTps.models') }}</label>
              <Select
                id="model-tps-model"
                data-testid="model-tps-select"
                :aria-label="t('admin.ops.modelTps.addModel')"
                :model-value="null"
                :options="modelOptions"
                :placeholder="t('admin.ops.modelTps.addModel')"
                :disabled="!params || loadingModels"
                searchable
                @update:model-value="addModel"
              />
            </div>
          </div>

          <form v-if="timeRange === 'custom'" class="flex flex-wrap items-end gap-3" @submit.prevent="applyRange">
            <label class="space-y-1 text-sm">
              <span>{{ t('admin.ops.customTimeRange.startTime') }}</span>
              <input v-model="localStart" type="datetime-local" class="input" required />
            </label>
            <label class="space-y-1 text-sm">
              <span>{{ t('admin.ops.customTimeRange.endTime') }}</span>
              <input v-model="localEnd" type="datetime-local" class="input" required />
            </label>
            <button type="submit" class="btn btn-secondary">{{ t('common.apply') }}</button>
            <p v-if="invalidRange" role="alert" class="w-full text-sm text-red-600 dark:text-red-400">{{ t('admin.ops.modelTps.invalidRange') }}</p>
          </form>

          <ul v-if="models.length" class="flex flex-wrap gap-2">
            <li v-for="model in models" :key="model" class="max-w-full">
              <button
                type="button"
                class="inline-flex max-w-full items-center gap-2 rounded-full bg-primary-50 px-3 py-1 text-sm text-primary-700 hover:bg-primary-100 dark:bg-primary-900/30 dark:text-primary-300"
                :aria-label="t('admin.ops.modelTps.removeModel', { model })"
                @click="removeModel(model)"
              >
                <span class="truncate">{{ model }}</span>
                <Icon name="x" size="xs" class="shrink-0" aria-hidden="true" />
              </button>
            </li>
          </ul>
          <p v-if="groupError" role="alert" class="text-sm text-red-600 dark:text-red-400">{{ t('admin.ops.modelTps.groupsFailed') }}</p>
          <p v-if="modelError" role="alert" class="text-sm text-red-600 dark:text-red-400">{{ t('admin.ops.modelTps.modelsFailed') }}</p>
        </div>

        <section class="card p-4 sm:p-5">
          <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2">
              <div class="inline-flex rounded-lg border border-gray-200 p-1 dark:border-dark-600" :aria-label="t('admin.ops.outputTps')" role="group">
                <button
                  v-for="value in metricOptions"
                  :key="value"
                  type="button"
                  :data-testid="`model-tps-${value}`"
                  :aria-pressed="metric === value"
                  class="rounded-md px-3 py-1 text-sm font-medium"
                  :class="metric === value ? 'bg-primary-50 text-primary-700 dark:bg-primary-900/40 dark:text-primary-300' : 'text-gray-600 dark:text-gray-300'"
                  @click="metric = value"
                >
                  {{ value.toUpperCase() }}
                </button>
              </div>
              <HelpTooltip :content="t('admin.ops.tooltips.outputTps')" />
            </div>
            <button type="button" class="btn btn-secondary" :disabled="!params || loadingModels || loadingTrend" @click="refresh++">
              {{ t('common.refresh') }}
            </button>
          </div>

          <div v-if="chartData" class="h-[360px] md:h-[420px]">
            <Line :data="chartData" :options="chartOptions" />
          </div>
          <p v-else-if="trendError" role="alert" class="py-12 text-center text-sm text-red-600 dark:text-red-400">
            {{ t('admin.ops.modelTps.loadFailed') }}
          </p>
          <p v-else-if="!modelError" class="py-12 text-center text-sm text-gray-500 dark:text-gray-400" role="status">
            {{ t(loadingModels || loadingTrend ? 'admin.ops.loadingText'
              : !params ? 'admin.ops.modelTps.pickRange'
              : !models.length && availableModels.length ? 'admin.ops.modelTps.pickModels'
              : 'admin.ops.modelTps.noSamples') }}
          </p>
        </section>
      </template>
    </div>
  </AppLayout>
</template>
