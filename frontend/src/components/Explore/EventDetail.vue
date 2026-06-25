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
  <div>
    <v-row>
      <v-col cols="12" md="6">
        <FieldRow
          label="Message"
          field-key="message"
          :value="event.message"
          @filter="emit('filter-field', $event)"
          @exclude="emit('exclude-field', $event)"
          @copy="copyValue"
        />
      </v-col>
      <v-col cols="12" md="3">
        <FieldRow
          label="Timestamp"
          field-key="timestamp"
          :value="event.timestamp"
          @filter="emit('filter-field', $event)"
          @exclude="emit('exclude-field', $event)"
          @copy="copyValue"
        />
        <FieldRow
          label="Description"
          field-key="timestamp_desc"
          :value="event.timestamp_desc"
          class="mt-3"
          @filter="emit('filter-field', $event)"
          @exclude="emit('exclude-field', $event)"
          @copy="copyValue"
        />
      </v-col>
      <v-col cols="12" md="3">
        <FieldRow
          label="Source"
          field-key="source"
          :value="event.source"
          @filter="emit('filter-field', $event)"
          @exclude="emit('exclude-field', $event)"
          @copy="copyValue"
        />
        <FieldRow
          label="Display name"
          field-key="display_name"
          :value="event.display_name"
          class="mt-3"
          @filter="emit('filter-field', $event)"
          @exclude="emit('exclude-field', $event)"
          @copy="copyValue"
        />
      </v-col>
    </v-row>
    <v-row v-if="Object.keys(event.attributes || {}).length > 0">
      <v-col cols="12">
        <p class="text-caption text-disabled mb-1">Attributes</p>
        <v-table density="compact">
          <tbody>
            <tr v-for="(value, key) in event.attributes" :key="key">
              <td class="text-caption font-weight-bold" style="width: 200px">
                {{ key }}
              </td>
              <td class="text-body-2">{{ value }}</td>
              <td class="text-right" style="width: 120px">
                <v-btn
                  icon="mdi-filter-plus"
                  variant="text"
                  density="compact"
                  size="small"
                  title="Filter for this value"
                  @click="emit('filter-field', { key, value })"
                />
                <v-btn
                  icon="mdi-filter-minus"
                  variant="text"
                  density="compact"
                  size="small"
                  title="Exclude this value"
                  @click="emit('exclude-field', { key, value })"
                />
                <v-btn
                  icon="mdi-content-copy"
                  variant="text"
                  density="compact"
                  size="small"
                  title="Copy value"
                  @click="copyValue(value)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-col>
    </v-row>
  </div>
</template>

<script setup lang="ts">
import type { EventRecord } from "@/services/api";
import FieldRow from "@/components/Explore/FieldRow.vue";

defineProps<{
  event: EventRecord;
}>();

const emit = defineEmits<{
  (e: "filter-field", payload: { key: string; value: string }): void;
  (e: "exclude-field", payload: { key: string; value: string }): void;
}>();

async function copyValue(value: string) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    // Clipboard may be unavailable; ignore silently.
  }
}
</script>
