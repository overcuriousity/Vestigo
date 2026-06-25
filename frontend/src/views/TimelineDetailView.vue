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
  <ViewLayout>
    <template #left>
      <v-expansion-panels v-model="panel" multiple variant="accordion">
        <TimelinePanel
          :case-id="caseId"
          :timelines="appStore.timelines"
          :current-timeline-id="timelineId"
        />
        <FilterPanel v-model="filters" @apply="applyFilters" />
        <SavedViews
          :views="appStore.savedViews"
          :can-save="appStore.hasActiveFilters"
          @select="loadView"
          @save-current="saveCurrentView"
        />
        <Anomalies
          :results="anomalies"
          :loading="anomaliesLoading"
          @load="loadAnomalies"
        />
      </v-expansion-panels>
    </template>

    <div>
      <v-toolbar density="compact" flat color="surface" class="mb-2 rounded">
        <v-btn
          variant="text"
          size="small"
          :to="`/cases/${caseId}`"
          prepend-icon="mdi-arrow-left"
        >
          Case
        </v-btn>
        <v-toolbar-title class="text-body-1">
          {{ appStore.currentTimeline?.name || "Timeline" }}
        </v-toolbar-title>
        <v-spacer />
        <v-btn
          v-if="appStore.currentTimeline && appStore.currentTimeline.event_count > 0"
          :loading="embedJob?.status === 'running' || embedJob?.status === 'queued'"
          :disabled="embedJob?.status === 'running' || embedJob?.status === 'queued'"
          color="secondary"
          variant="text"
          prepend-icon="mdi-vector-arrange-below"
          class="mr-2"
          @click="startEmbed"
        >
          {{ appStore.currentTimeline.vector_count > 0 ? "Re-embed" : "Generate embeddings" }}
        </v-btn>
        <UploadFormButton
          :case-id="caseId"
          :timeline-id="timelineId"
          @uploaded="onUploaded"
        />
      </v-toolbar>
      <v-progress-linear
        v-if="embedJob?.status === 'running' || embedJob?.status === 'queued'"
        :model-value="
          embedJob.progress.total > 0
            ? (embedJob.progress.processed / embedJob.progress.total) * 100
            : 0
        "
        color="secondary"
        height="6"
        class="mb-2 rounded"
      />
      <v-alert
        v-if="embedJob?.status === 'failed'"
        type="error"
        density="compact"
        class="mb-2"
        closable
        @click:close="embedJob = null"
      >
        Embedding failed: {{ embedJob.error }}
      </v-alert>

      <v-card class="mb-3">
        <v-card-text>
          <SearchInput
            v-model="filters.q"
            @search="applyFilters"
            @clear="clearQuery"
          />
          <FilterChips
            :filters="appStore.activeFilters"
            class="mt-2"
            @remove="removeFilter"
            @clear="resetFilters"
          />
        </v-card-text>
      </v-card>

      <v-card>
        <EventTable
          :events="appStore.events"
          :total="appStore.eventTotal"
          :page="appStore.currentPage"
          :limit="appStore.eventLimit"
          :total-pages="appStore.totalPages"
          :loading="appStore.loading"
          :selected-ids="appStore.selectedEventIds"
          @update:page="onPageChange"
          @update:limit="onLimitChange"
          @update:selected-ids="onSelectionChange"
          @filter-field="addFieldFilter"
          @exclude-field="addFieldExclusion"
          @filter-tag="setTagFilter"
          @tag-selected="tagSelected"
          @export="exportEvents"
        />
      </v-card>
    </div>
  </ViewLayout>

  <v-dialog v-model="tagDialog" width="400">
    <v-card>
      <v-card-title>Tag selected events</v-card-title>
      <v-card-text>
        <v-text-field
          v-model="newTag"
          label="Tag"
          density="comfortable"
          hide-details
        />
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="tagDialog = false">Cancel</v-btn>
        <v-btn color="primary" :disabled="!newTag" @click="applyTag">Tag</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="saveViewDialog" width="400">
    <v-card>
      <v-card-title>Save view</v-card-title>
      <v-card-text>
        <v-text-field
          v-model="viewName"
          label="Name"
          density="comfortable"
          hide-details
        />
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="saveViewDialog = false">Cancel</v-btn>
        <v-btn color="primary" :disabled="!viewName" @click="confirmSaveView"
          >Save</v-btn
        >
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import ViewLayout from "@/layouts/View.vue";
import TimelinePanel from "@/components/LeftPanel/TimelinePanel.vue";
import FilterPanel from "@/components/LeftPanel/FilterPanel.vue";
import SavedViews from "@/components/LeftPanel/SavedViews.vue";
import Anomalies from "@/components/LeftPanel/Anomalies.vue";
import SearchInput from "@/components/Explore/SearchInput.vue";
import FilterChips from "@/components/Explore/FilterChips.vue";
import EventTable from "@/components/Explore/EventTable.vue";
import UploadFormButton from "@/components/UploadFormButton.vue";
import { useAppStore } from "@/stores/app";
import type { FilterState, SavedView, SimilarityResult } from "@/services/api";
import { getAnomalies, createView, startEmbedding, getJob } from "@/services/api";

const route = useRoute();
const appStore = useAppStore();
const caseId = route.params.caseId as string;
const timelineId = route.params.timelineId as string;

const panel = ref(["timelines", "filters"]);
const filters = ref<FilterState>({});
const tagDialog = ref(false);
const newTag = ref("");
const saveViewDialog = ref(false);
const viewName = ref("");
const anomalies = ref<SimilarityResult[]>([]);
const anomaliesLoading = ref(false);
const embedJob = ref<{ id: string; status: string; progress: { total: number; processed: number }; error: string | null } | null>(null);
const embedPolling = ref<number | null>(null);

async function loadAll() {
  await appStore.loadTimeline(caseId, timelineId);
  await appStore.loadSavedViews(caseId);
  await appStore.loadEvents(caseId, timelineId, filters.value);
}

function applyFilters() {
  appStore.setPage(1);
  appStore.loadEvents(caseId, timelineId, filters.value);
}

function onPageChange(page: number) {
  appStore.setPage(page);
  appStore.loadEvents(caseId, timelineId, filters.value);
}

function onLimitChange(limit: number) {
  appStore.setLimit(limit);
  appStore.loadEvents(caseId, timelineId, filters.value);
}

function onSelectionChange(ids: Set<string>) {
  appStore.selectedEventIds = ids;
}

function onUploaded() {
  embedJob.value = null;
  appStore.loadTimeline(caseId, timelineId);
  appStore.loadEvents(caseId, timelineId, filters.value);
}

function clearQuery() {
  filters.value.q = "";
  applyFilters();
}

function setTagFilter(tag: string) {
  filters.value.tag = tag;
  applyFilters();
}

function addFieldFilter(payload: { key: string; value: string }) {
  if (!filters.value.fields) {
    filters.value.fields = {};
  }
  filters.value.fields[payload.key] = payload.value;
  applyFilters();
}

function addFieldExclusion(payload: { key: string; value: string }) {
  if (!filters.value.exclude) {
    filters.value.exclude = {};
  }
  filters.value.exclude[payload.key] = payload.value;
  applyFilters();
}

function removeFilter(
  key:
    | "q"
    | "source"
    | "tag"
    | "timerange"
    | `field:${string}`
    | `exclude:${string}`,
) {
  if (key === "q" || key === "source" || key === "tag") {
    filters.value[key] = undefined;
  } else if (key === "timerange") {
    filters.value.start = undefined;
    filters.value.end = undefined;
  } else if (key.startsWith("field:")) {
    const fieldKey = key.slice(6);
    if (filters.value.fields) {
      delete filters.value.fields[fieldKey];
      if (Object.keys(filters.value.fields).length === 0) {
        filters.value.fields = undefined;
      }
    }
  } else if (key.startsWith("exclude:")) {
    const fieldKey = key.slice(8);
    if (filters.value.exclude) {
      delete filters.value.exclude[fieldKey];
      if (Object.keys(filters.value.exclude).length === 0) {
        filters.value.exclude = undefined;
      }
    }
  }
  applyFilters();
}

function resetFilters() {
  filters.value = {};
  applyFilters();
}

function loadView(view: SavedView) {
  filters.value = { q: view.query, ...view.filter };
  applyFilters();
}

function saveCurrentView() {
  viewName.value = "";
  saveViewDialog.value = true;
}

async function confirmSaveView() {
  if (!viewName.value) return;
  try {
    await createView(caseId, viewName.value, filters.value.q || "", {
      ...filters.value,
    });
    await appStore.loadSavedViews(caseId);
    window.dispatchEvent(
      new CustomEvent("app-success", { detail: "View saved" }),
    );
  } finally {
    saveViewDialog.value = false;
  }
}

function tagSelected() {
  newTag.value = "";
  tagDialog.value = true;
}

async function applyTag() {
  // TODO: wire to annotation endpoint once backend supports tagging.
  window.dispatchEvent(
    new CustomEvent("app-success", {
      detail: `Tagged ${appStore.selectedEventIds.size} events (stub)`,
    }),
  );
  tagDialog.value = false;
}

async function exportEvents() {
  // TODO: wire to export endpoint once backend supports it.
  window.dispatchEvent(
    new CustomEvent("app-success", { detail: "Export started (stub)" }),
  );
}

async function loadAnomalies() {
  anomaliesLoading.value = true;
  try {
    anomalies.value = await getAnomalies(caseId, timelineId);
  } finally {
    anomaliesLoading.value = false;
  }
}

async function startEmbed() {
  if (embedJob.value?.status === "running" || embedJob.value?.status === "queued") {
    return;
  }
  try {
    const { job_id, status } = await startEmbedding(caseId, timelineId);
    embedJob.value = { id: job_id, status, progress: { total: 0, processed: 0 }, error: null };
    pollEmbedJob();
  } catch {
    // Error is shown by global notification interceptor.
  }
}

async function pollEmbedJob() {
  if (!embedJob.value) return;
  if (embedPolling.value !== null) {
    window.clearTimeout(embedPolling.value);
  }
  try {
    const { job } = await getJob(embedJob.value.id);
    embedJob.value = {
      id: job.id,
      status: job.status,
      progress: job.progress || { total: 0, processed: 0 },
      error: job.error,
    };
    if (job.status === "running" || job.status === "queued") {
      embedPolling.value = window.setTimeout(pollEmbedJob, 2000);
    } else if (job.status === "completed") {
      await appStore.loadTimeline(caseId, timelineId);
    }
  } catch {
    // Error is shown by global notification interceptor.
  }
}

watch(
  () => route.params.timelineId,
  () => {
    loadAll();
  },
);

onMounted(() => {
  loadAll();
});
</script>
