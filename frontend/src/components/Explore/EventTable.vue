<!--
Copyright 2024 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Adapted for TraceVector from Google Timesketch frontend-v3.
-->
<template>
  <v-data-table
    :headers="visibleHeaders"
    :items="events"
    :items-per-page="limit"
    :page="page"
    :loading="loading"
    item-value="event_id"
    v-model:expanded="expanded"
    show-expand
    hide-default-footer
    class="elevation-0 event-table"
    @click:row="onRowClick"
  >
    <!-- ── Toolbar ── -->
    <template #top>
      <v-toolbar density="compact" flat color="surface">
        <v-checkbox
          v-model="selectAll"
          hide-details
          density="compact"
          class="ml-2 flex-grow-0"
          @update:model-value="toggleSelectAll"
        />
        <span class="text-body-2 ml-1">{{ selectedCount }} selected</span>
        <v-spacer />
        <v-btn
          size="small"
          variant="text"
          prepend-icon="mdi-tag-plus"
          :disabled="selectedCount === 0"
          @click="emit('tag-selected')"
        >
          Tag
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          prepend-icon="mdi-comment-plus"
          :disabled="selectedCount === 0"
          @click="emit('comment-selected')"
        >
          Comment
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          prepend-icon="mdi-export"
          :disabled="events.length === 0"
          @click="emit('export')"
        >
          Export
        </v-btn>

        <!-- Column picker -->
        <v-menu :close-on-content-click="false" location="bottom end">
          <template #activator="{ props: menuProps }">
            <v-btn
              size="small"
              variant="text"
              icon="mdi-view-column-outline"
              title="Toggle columns"
              v-bind="menuProps"
            />
          </template>
          <v-card min-width="180">
            <v-list density="compact" select-strategy="independent">
              <v-list-item
                v-for="col in OPTIONAL_COLUMNS"
                :key="col.key"
                :title="col.title"
              >
                <template #prepend>
                  <v-checkbox
                    :model-value="visibleKeys.has(col.key)"
                    hide-details
                    density="compact"
                    class="mr-1"
                    @click.stop
                    @update:model-value="toggleColumn(col.key, !!$event)"
                  />
                </template>
              </v-list-item>
            </v-list>
          </v-card>
        </v-menu>
      </v-toolbar>
    </template>

    <!-- ── Per-row checkbox (custom column so it never triggers expand) ── -->
    <template #item.check="{ item }">
      <v-checkbox
        :model-value="selectedIds.has(item.event_id)"
        hide-details
        density="compact"
        @click.stop
        @update:model-value="onSelect(item.event_id, !!$event)"
      />
    </template>

    <!-- ── Expand chevron – always visible, always correct ── -->
    <template #item.data-table-expand="{ item }">
      <v-btn
        :icon="expanded.includes(item.event_id) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
        variant="text"
        size="small"
        density="compact"
        @click.stop="toggleRow(item.event_id)"
      />
    </template>

    <!-- ── Cell renderers ── -->
    <template #item.timestamp="{ item }">
      {{ formatDate(item.timestamp) }}
    </template>

    <template #item.tags="{ item }">
      <v-chip
        v-for="tag in item.tags || []"
        :key="tag"
        size="x-small"
        class="mr-1"
        color="primary"
        @click.stop="emit('filter-tag', tag)"
      >
        {{ tag }}
      </v-chip>
      <template v-if="annotationsByEvent[item.event_id]">
        <v-chip
          v-for="ann in annotationsByEvent[item.event_id].filter(
            (a) => a.annotation_type === 'tag',
          )"
          :key="ann.id"
          size="x-small"
          class="mr-1"
          color="secondary"
          prepend-icon="mdi-account-tag"
        >
          {{ ann.content }}
        </v-chip>
        <v-icon
          v-if="annotationsByEvent[item.event_id].some((a) => a.annotation_type === 'comment')"
          size="small"
          color="secondary"
          title="Has comments"
        >
          mdi-comment-text
        </v-icon>
      </template>
    </template>

    <template #item.source="{ item }">
      <v-chip
        size="x-small"
        variant="outlined"
        @click.stop="emit('filter-field', { key: 'source', value: item.source })"
      >
        {{ item.source }}
      </v-chip>
    </template>

    <!-- ── Expanded detail row ── -->
    <template #expanded-row="{ columns, item }">
      <td :colspan="columns.length" class="pa-4 bg-surface-light">
        <EventDetail
          :event="item"
          :annotations="annotationsByEvent[item.event_id] ?? []"
          @filter-field="emit('filter-field', $event)"
          @exclude-field="emit('exclude-field', $event)"
          @add-annotation="
            emit('add-annotation', {
              eventId: item.event_id,
              type: $event.type,
              content: $event.content,
            })
          "
          @delete-annotation="
            emit('delete-annotation', {
              eventId: item.event_id,
              annotationId: $event,
            })
          "
        />
      </td>
    </template>
  </v-data-table>

  <!-- ── Pagination bar ── -->
  <v-row class="pa-4 align-center" no-gutters>
    <v-col cols="auto">
      <v-select
        v-model="localLimit"
        :items="[10, 25, 50, 100]"
        label="Rows"
        density="compact"
        hide-details
        style="width: 100px"
        @update:model-value="emit('update:limit', localLimit)"
      />
    </v-col>
    <v-col class="d-flex justify-center">
      <v-pagination
        v-if="totalPages > 1"
        :model-value="page"
        :length="totalPages"
        density="comfortable"
        @update:model-value="emit('update:page', $event)"
      />
    </v-col>
    <v-col cols="auto" class="text-caption text-disabled">
      {{ total }} events
    </v-col>
  </v-row>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import EventDetail from "@/components/Explore/EventDetail.vue";
import type { Annotation, EventRecord } from "@/services/api";
import dayjs from "dayjs";

const props = defineProps<{
  events: EventRecord[];
  total: number;
  page: number;
  limit: number;
  totalPages: number;
  loading: boolean;
  selectedIds: Set<string>;
  annotationsByEvent: Record<string, Annotation[]>;
}>();

const emit = defineEmits<{
  (e: "update:page", page: number): void;
  (e: "update:limit", limit: number): void;
  (e: "update:selectedIds", ids: Set<string>): void;
  (e: "filter-field", payload: { key: string; value: string }): void;
  (e: "exclude-field", payload: { key: string; value: string }): void;
  (e: "filter-tag", tag: string): void;
  (e: "tag-selected"): void;
  (e: "comment-selected"): void;
  (e: "export"): void;
  (
    e: "add-annotation",
    payload: { eventId: string; type: "comment" | "tag"; content: string },
  ): void;
  (
    e: "delete-annotation",
    payload: { eventId: string; annotationId: string },
  ): void;
}>();

// ── Column definitions ──────────────────────────────────────────────────────

const FIXED_COLUMNS = [
  { title: "", key: "check", width: "48px", sortable: false },
] as const;

const OPTIONAL_COLUMNS = [
  { title: "Time", key: "timestamp", width: "180px" },
  { title: "Source", key: "source", width: "140px" },
  { title: "Message", key: "message" },
  { title: "Tags", key: "tags", width: "160px" },
  { title: "Description", key: "timestamp_desc", width: "160px" },
  { title: "Display name", key: "display_name", width: "160px" },
] as const;

type ColumnKey = (typeof OPTIONAL_COLUMNS)[number]["key"];

const visibleKeys = ref<Set<ColumnKey>>(
  new Set(["timestamp", "source", "message", "tags"]),
);

function toggleColumn(key: ColumnKey, show: boolean) {
  const next = new Set(visibleKeys.value);
  if (show) next.add(key);
  else next.delete(key);
  visibleKeys.value = next;
}

const visibleHeaders = computed(() => [
  ...FIXED_COLUMNS,
  ...OPTIONAL_COLUMNS.filter((c) => visibleKeys.value.has(c.key)),
]);

// ── Expansion ───────────────────────────────────────────────────────────────

const expanded = ref<string[]>([]);

function toggleRow(eventId: string) {
  expanded.value = expanded.value[0] === eventId ? [] : [eventId];
}

function onRowClick(event: Event, { item }: { item: EventRecord }) {
  // Ignore clicks on interactive elements so chips/buttons work independently.
  const target = event.target as HTMLElement;
  if (target.closest("button, a, .v-chip, input, .v-checkbox, .v-selection-control")) return;
  toggleRow(item.event_id);
}

// Collapse open row when the page changes.
watch(
  () => props.events,
  () => {
    expanded.value = [];
    selectAll.value = false;
  },
);

// ── Selection ───────────────────────────────────────────────────────────────

const localLimit = ref(props.limit);
const selectAll = ref(false);

const selectedCount = computed(() => props.selectedIds.size);

watch(
  () => props.limit,
  (val) => {
    localLimit.value = val;
  },
);

function onSelect(eventId: string, selected: boolean) {
  const next = new Set(props.selectedIds);
  if (selected) next.add(eventId);
  else next.delete(eventId);
  emit("update:selectedIds", next);
}

function toggleSelectAll(selected: boolean | null) {
  const next = new Set(props.selectedIds);
  if (selected) props.events.forEach((e) => next.add(e.event_id));
  else props.events.forEach((e) => next.delete(e.event_id));
  emit("update:selectedIds", next);
}

// ── Formatting ──────────────────────────────────────────────────────────────

function formatDate(value: string | null): string {
  if (!value) return "—";
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}
</script>

<style scoped lang="scss">
.event-table :deep(tbody tr:hover) {
  background: rgba(var(--v-theme-on-surface), 0.04);
  cursor: pointer;
}
</style>
