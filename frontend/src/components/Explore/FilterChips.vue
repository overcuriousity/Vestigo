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
  <div class="d-flex flex-wrap align-center ga-2">
    <v-chip
      v-if="filters.q"
      closable
      size="small"
      color="primary"
      @click:close="emit('remove', 'q')"
    >
      query: {{ filters.q }}
    </v-chip>
    <v-chip
      v-if="filters.source"
      closable
      size="small"
      color="info"
      @click:close="emit('remove', 'source')"
    >
      source: {{ filters.source }}
    </v-chip>
    <v-chip
      v-if="filters.tag"
      closable
      size="small"
      color="success"
      @click:close="emit('remove', 'tag')"
    >
      tag: {{ filters.tag }}
    </v-chip>
    <v-chip
      v-if="filters.start || filters.end"
      closable
      size="small"
      color="warning"
      @click:close="emit('remove', 'timerange')"
    >
      time: {{ filters.start || "*" }} to {{ filters.end || "*" }}
    </v-chip>
    <v-chip
      v-for="(value, key) in filters.fields"
      :key="`field:${key}`"
      closable
      size="small"
      color="primary"
      variant="outlined"
      @click:close="emit('remove', `field:${key}`)"
    >
      {{ key }}: {{ value }}
    </v-chip>
    <v-chip
      v-for="(value, key) in filters.exclude"
      :key="`exclude:${key}`"
      closable
      size="small"
      color="error"
      variant="outlined"
      @click:close="emit('remove', `exclude:${key}`)"
    >
      {{ key }} != {{ value }}
    </v-chip>
    <v-btn
      v-if="hasFilters"
      size="x-small"
      variant="text"
      @click="emit('clear')"
    >
      Clear all
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { FilterState } from "@/services/api";

const props = defineProps<{
  filters: FilterState;
}>();

const emit = defineEmits<{
  (
    e: "remove",
    key: "q" | "source" | "tag" | "timerange" | `field:${string}` | `exclude:${string}`,
  ): void;
  (e: "clear"): void;
}>();

const hasFilters = computed(
  () =>
    !!(
      props.filters.q ||
      props.filters.source ||
      props.filters.tag ||
      props.filters.start ||
      props.filters.end ||
      Object.keys(props.filters.fields || {}).length > 0 ||
      Object.keys(props.filters.exclude || {}).length > 0
    ),
);
</script>
