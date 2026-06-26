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
  <v-expansion-panel value="views">
    <v-expansion-panel-title>
      <v-icon start size="small">mdi-eye</v-icon>
      Saved views
      <v-chip v-if="views.length" size="x-small" class="ml-2">
        {{ views.length }}
      </v-chip>
    </v-expansion-panel-title>
    <v-expansion-panel-text>
      <v-list density="compact" nav>
        <v-list-item
          v-for="view in views"
          :key="view.id"
          :title="view.name"
          link
          @click="emit('select', view)"
        >
          <template #append>
            <v-btn
              icon
              variant="text"
              size="x-small"
              color="error"
              @click.stop="emit('delete', view.id)"
            >
              <v-icon size="small">mdi-close</v-icon>
            </v-btn>
          </template>
        </v-list-item>
        <v-list-item v-if="views.length === 0">
          <v-list-item-title class="text-caption text-disabled">
            No saved views yet.
          </v-list-item-title>
        </v-list-item>
      </v-list>
      <v-btn
        v-if="canSave"
        size="small"
        variant="text"
        block
        prepend-icon="mdi-content-save"
        @click="emit('save-current')"
      >
        Save current
      </v-btn>
    </v-expansion-panel-text>
  </v-expansion-panel>
</template>

<script setup lang="ts">
import type { SavedView } from "@/services/api";

defineProps<{
  views: SavedView[];
  canSave: boolean;
}>();

const emit = defineEmits<{
  (e: "select", view: SavedView): void;
  (e: "save-current"): void;
  (e: "delete", viewId: string): void;
}>();
</script>
