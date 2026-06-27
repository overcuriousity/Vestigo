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

    <!-- ── Human annotations ───────────────────────────────────────────────── -->
    <v-row>
      <v-col cols="12">
        <p class="text-caption text-disabled mb-1">Annotations</p>
        <v-table
          v-if="userAnnotations.length > 0"
          density="compact"
          class="mb-2"
        >
          <tbody>
            <tr v-for="ann in userAnnotations" :key="ann.id">
              <td style="width: 32px">
                <v-icon
                  size="small"
                  :color="ann.annotation_type === 'tag' ? 'secondary' : 'default'"
                >
                  {{
                    ann.annotation_type === "tag"
                      ? "mdi-account-tag"
                      : "mdi-comment-text"
                  }}
                </v-icon>
              </td>
              <td class="text-body-2">{{ ann.content }}</td>
              <td class="text-right" style="width: 48px">
                <v-btn
                  icon="mdi-close"
                  variant="text"
                  density="compact"
                  size="small"
                  color="error"
                  title="Delete annotation"
                  @click="emit('delete-annotation', ann.id)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>
        <p v-else class="text-caption text-disabled mb-2">No annotations yet.</p>
        <div class="d-flex gap-2">
          <v-text-field
            v-model="newAnnotationContent"
            density="compact"
            hide-details
            placeholder="Add tag or comment…"
            style="max-width: 300px"
            @keydown.enter.prevent="addTag"
          />
          <v-btn
            size="small"
            variant="tonal"
            color="secondary"
            prepend-icon="mdi-account-tag"
            :disabled="!newAnnotationContent.trim()"
            @click="addTag"
          >
            Tag
          </v-btn>
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-comment-plus"
            :disabled="!newAnnotationContent.trim()"
            @click="addComment"
          >
            Comment
          </v-btn>
        </div>
      </v-col>
    </v-row>

    <!-- ── System analysis annotations ────────────────────────────────────── -->
    <v-row v-if="systemAnnotations.length > 0">
      <v-col cols="12">
        <p class="text-caption text-medium-emphasis mb-1">
          <v-icon size="x-small" class="mr-1">mdi-sigma</v-icon>
          Analysis
        </p>
        <v-table density="compact" class="mb-2">
          <tbody>
            <tr v-for="ann in systemAnnotations" :key="ann.id">
              <td style="width: 32px">
                <v-icon size="small" color="warning">mdi-sigma</v-icon>
              </td>
              <td class="text-body-2">
                {{ ann.content }}
                <template v-if="ann.details">
                  <br />
                  <span class="text-caption text-disabled">
                    method: {{ ann.details.method }} &nbsp;·&nbsp;
                    distance: {{ ann.details.distance.toFixed(4) }} &nbsp;·&nbsp;
                    sample: {{ ann.details.sample_size }}
                  </span>
                </template>
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-col>
    </v-row>

    <!-- ── Similarity search ────────────────────────────────────────────────── -->
    <v-row>
      <v-col cols="12">
        <div class="d-flex align-center gap-2 mb-2">
          <p class="text-caption text-disabled mb-0">
            <v-icon size="x-small" class="mr-1">mdi-vector-link</v-icon>
            Similar Events
          </p>
          <v-btn
            size="x-small"
            variant="tonal"
            color="primary"
            :loading="similarLoading"
            prepend-icon="mdi-magnify"
            @click="loadSimilar"
          >
            Find similar
          </v-btn>
        </div>
        <template v-if="similarStatus === 'not_embedded'">
          <p class="text-caption text-disabled">
            Embeddings not generated — run "Generate embeddings" first.
          </p>
        </template>
        <template v-else-if="similarStatus === 'vector_not_found'">
          <p class="text-caption text-disabled">
            No vector stored for this event.
          </p>
        </template>
        <template v-else-if="similarResults.length > 0">
          <v-list density="compact" class="pa-0">
            <v-list-item
              v-for="result in similarResults"
              :key="result.event_id"
              class="px-0"
            >
              <v-list-item-title class="text-caption text-truncate">
                {{ result.event.message || "(no message)" }}
              </v-list-item-title>
              <v-list-item-subtitle class="text-caption">
                similarity: {{ result.score.toFixed(3) }}
              </v-list-item-subtitle>
            </v-list-item>
          </v-list>
        </template>
        <p
          v-else-if="!similarLoading && similarSearched"
          class="text-caption text-disabled"
        >
          No similar events found.
        </p>
      </v-col>
    </v-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import type { Annotation, EventRecord, SimilarityResult, VectorStatus } from "@/services/api";
import { searchSimilar } from "@/services/api";
import FieldRow from "@/components/Explore/FieldRow.vue";

const props = defineProps<{
  event: EventRecord;
  annotations: Annotation[];
  caseId?: string;
  timelineId?: string;
}>();

const emit = defineEmits<{
  (e: "filter-field", payload: { key: string; value: string }): void;
  (e: "exclude-field", payload: { key: string; value: string }): void;
  (e: "add-annotation", payload: { type: "comment" | "tag"; content: string }): void;
  (e: "delete-annotation", annotationId: string): void;
}>();

// Split annotations by origin.
const userAnnotations = computed(() =>
  props.annotations.filter((a) => (a.origin ?? "user") === "user"),
);
const systemAnnotations = computed(() =>
  props.annotations.filter((a) => a.origin === "system"),
);

const newAnnotationContent = ref("");

function addTag() {
  const content = newAnnotationContent.value.trim();
  if (!content) return;
  emit("add-annotation", { type: "tag", content });
  newAnnotationContent.value = "";
}

function addComment() {
  const content = newAnnotationContent.value.trim();
  if (!content) return;
  emit("add-annotation", { type: "comment", content });
  newAnnotationContent.value = "";
}

async function copyValue(value: string) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    // Clipboard may be unavailable; ignore silently.
  }
}

// Similarity search state (local to this detail panel instance).
const similarResults = ref<SimilarityResult[]>([]);
const similarLoading = ref(false);
const similarStatus = ref<VectorStatus | "">("");
const similarSearched = ref(false);

async function loadSimilar() {
  if (!props.caseId || !props.timelineId) return;
  similarLoading.value = true;
  similarSearched.value = true;
  try {
    const resp = await searchSimilar(
      props.caseId,
      props.timelineId,
      props.event.event_id,
    );
    similarStatus.value = resp.status;
    similarResults.value = resp.results;
  } catch {
    similarStatus.value = "not_embedded";
  } finally {
    similarLoading.value = false;
  }
}
</script>
