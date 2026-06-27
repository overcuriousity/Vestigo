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
  <v-expansion-panel value="anomalies">
    <v-expansion-panel-title>
      <v-icon start size="small">mdi-sigma</v-icon>
      Unusual Events
      <v-chip size="x-small" class="ml-2" color="warning">triage</v-chip>
    </v-expansion-panel-title>
    <v-expansion-panel-text>
      <!-- Descriptive caption (honest framing) -->
      <p class="text-caption text-medium-emphasis mb-2">
        Surfaces log lines most unlike the rest of this timeline — statistical
        triage, not threat detection. Rare ≠ malicious.
      </p>

      <!-- Not-embedded state -->
      <template v-if="status === 'not_embedded'">
        <v-alert type="info" density="compact" variant="tonal" class="mb-2 text-caption">
          Embeddings not yet generated for this timeline.
        </v-alert>
        <v-btn
          size="small"
          color="secondary"
          block
          :disabled="embedRunning"
          :loading="embedRunning"
          prepend-icon="mdi-brain"
          @click="emit('generate-embeddings')"
        >
          Generate Embeddings
        </v-btn>
      </template>

      <!-- Insufficient vectors -->
      <template v-else-if="status === 'insufficient_vectors'">
        <v-alert type="warning" density="compact" variant="tonal" class="text-caption">
          Not enough embedded events to compute outliers (need ≥ 2).
        </v-alert>
      </template>

      <!-- Main controls (idle or ok) -->
      <template v-else>
        <v-btn
          size="small"
          color="primary"
          block
          :loading="loading"
          prepend-icon="mdi-magnify"
          @click="emit('load')"
        >
          Find Unusual Lines
        </v-btn>

        <!-- Results list -->
        <template v-if="results.length > 0">
          <v-list density="compact" class="mt-2">
            <v-list-item
              v-for="result in results"
              :key="result.event_id"
              class="px-0"
            >
              <v-list-item-title class="text-caption text-truncate">
                {{ result.event.message || "(no message)" }}
              </v-list-item-title>
              <v-list-item-subtitle class="text-caption">
                score: {{ result.score.toFixed(3) }}
                <template v-if="result.details">
                  &nbsp;· rank {{ result.details.rank }}/{{ result.details.of }}
                </template>
              </v-list-item-subtitle>
            </v-list-item>
          </v-list>

          <!-- Tag outliers action -->
          <v-btn
            size="small"
            color="warning"
            variant="tonal"
            block
            class="mt-2"
            :loading="tagging"
            prepend-icon="mdi-tag-multiple"
            @click="emit('tag')"
          >
            Tag Outliers in Timeline
          </v-btn>
          <p class="text-caption text-medium-emphasis mt-1">
            Adds system annotations with the math to each outlier row.
          </p>
        </template>

        <p
          v-else-if="!loading && status === 'ok'"
          class="text-caption text-disabled mt-2"
        >
          No results yet — click "Find Unusual Lines".
        </p>
      </template>
    </v-expansion-panel-text>
  </v-expansion-panel>
</template>

<script setup lang="ts">
import type { SimilarityResult, VectorStatus } from "@/services/api";

defineProps<{
  results: SimilarityResult[];
  loading: boolean;
  tagging: boolean;
  embedRunning: boolean;
  status: VectorStatus | "";
}>();

const emit = defineEmits<{
  (e: "load"): void;
  (e: "tag"): void;
  (e: "generate-embeddings"): void;
}>();
</script>
