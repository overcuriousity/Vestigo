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
  <div :class="$props.class" class="field-row">
    <p class="text-caption text-disabled mb-1">{{ label }}</p>
    <div class="d-flex align-start">
      <p class="text-body-2 flex-grow-1">{{ displayValue }}</p>
      <div class="field-actions ml-2">
        <v-btn
          icon="mdi-filter-plus"
          variant="text"
          density="compact"
          size="small"
          title="Filter for this value"
          :disabled="!canFilter"
          @click="emit('filter', { key: fieldKey, value: rawValue })"
        />
        <v-btn
          icon="mdi-filter-minus"
          variant="text"
          density="compact"
          size="small"
          title="Exclude this value"
          :disabled="!canFilter"
          @click="emit('exclude', { key: fieldKey, value: rawValue })"
        />
        <v-btn
          icon="mdi-content-copy"
          variant="text"
          density="compact"
          size="small"
          title="Copy value"
          :disabled="!rawValue"
          @click="emit('copy', rawValue)"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  label: string;
  fieldKey: string;
  value: string | null;
  class?: string;
}>();

const emit = defineEmits<{
  (e: "filter", payload: { key: string; value: string }): void;
  (e: "exclude", payload: { key: string; value: string }): void;
  (e: "copy", value: string): void;
}>();

const rawValue = computed(() => props.value || "");
const displayValue = computed(() => props.value || "—");
const canFilter = computed(() => !!props.value);
</script>

<style scoped lang="scss">
.field-row {
  .field-actions {
    opacity: 0.4;
    transition: opacity 0.15s ease;
    white-space: nowrap;
  }

  &:hover .field-actions,
  .field-actions:focus-within {
    opacity: 1;
  }
}
</style>
