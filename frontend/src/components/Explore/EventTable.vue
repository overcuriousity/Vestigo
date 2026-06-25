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
    :headers="headers"
    :items="events"
    :items-per-page="limit"
    :page="page"
    :loading="loading"
    hide-default-footer
    show-expand
    class="elevation-0 event-table"
  >
    <template #top>
      <v-toolbar density="compact" flat color="surface">
        <v-checkbox
          v-model="selectAll"
          hide-details
          density="compact"
          class="ml-2"
          @update:model-value="toggleSelectAll"
        />
        <v-toolbar-title class="text-body-2">
          {{ selectedCount }} selected
        </v-toolbar-title>
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
          prepend-icon="mdi-export"
          :disabled="events.length === 0"
          @click="emit('export')"
        >
          Export
        </v-btn>
      </v-toolbar>
    </template>

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

    <template
      #item.data-table-select="{ internalItem, isSelected, toggleSelect }"
    >
      <v-checkbox
        :model-value="isSelected(internalItem)"
        hide-details
        density="compact"
        @update:model-value="toggleSelect(internalItem)"
      />
    </template>

    <template #expanded-row="{ columns, item }">
      <td :colspan="columns.length" class="pa-4 bg-surface-light">
        <EventDetail
          :event="item"
          @filter-field="emit('filter-field', $event)"
          @exclude-field="emit('exclude-field', $event)"
        />
      </td>
    </template>
  </v-data-table>

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
import type { EventRecord } from "@/services/api";
import dayjs from "dayjs";

const props = defineProps<{
  events: EventRecord[];
  total: number;
  page: number;
  limit: number;
  totalPages: number;
  loading: boolean;
  selectedIds: Set<string>;
}>();

const emit = defineEmits<{
  (e: "update:page", page: number): void;
  (e: "update:limit", limit: number): void;
  (e: "update:selectedIds", ids: Set<string>): void;
  (e: "filter-field", payload: { key: string; value: string }): void;
  (e: "exclude-field", payload: { key: string; value: string }): void;
  (e: "filter-tag", tag: string): void;
  (e: "tag-selected"): void;
  (e: "export"): void;
}>();

const localLimit = ref(props.limit);
const selectAll = ref(false);

const headers = [
  { title: "Time", key: "timestamp", width: "180px" },
  { title: "Source", key: "source", width: "120px" },
  { title: "Message", key: "message" },
  { title: "Tags", key: "tags", width: "150px" },
];

const selectedCount = computed(() => props.selectedIds.size);

watch(
  () => props.limit,
  (val) => {
    localLimit.value = val;
  },
);

watch(
  () => props.events,
  () => {
    selectAll.value = false;
  },
);

function toggleSelectAll(selected: boolean | null) {
  const newSet = new Set(props.selectedIds);
  if (selected) {
    props.events.forEach((e) => newSet.add(e.event_id));
  } else {
    props.events.forEach((e) => newSet.delete(e.event_id));
  }
  emit("update:selectedIds", newSet);
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}
</script>

<style scoped lang="scss">
.event-table :deep(tbody tr) {
  cursor: pointer;
}
</style>
