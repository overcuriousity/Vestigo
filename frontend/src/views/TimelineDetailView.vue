<template>
  <v-container fluid>
    <v-row>
      <v-col cols="12">
        <v-btn
          variant="text"
          :to="`/cases/${caseId}`"
          prepend-icon="mdi-arrow-left"
        >
          Back to case
        </v-btn>
        <h1 class="text-h4 mt-2">{{ timeline?.name || "Timeline" }}</h1>
        <p class="text-body-2 text-disabled">
          {{ timeline?.description }}
        </p>
      </v-col>
    </v-row>

    <v-row>
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>Upload Timeline File</v-card-title>
          <v-card-text>
            <v-file-input
              v-model="selectedFile"
              label="CSV or JSONL file"
              accept=".csv,.jsonl,.json,.tsv"
              density="comfortable"
              show-size
            />
            <v-select
              v-model="uploadParser"
              :items="['auto', 'timesketch_csv', 'jsonl']"
              label="Parser"
              density="comfortable"
            />
            <v-btn
              color="primary"
              :loading="uploading"
              :disabled="!selectedFile"
              @click="upload"
            >
              Ingest
            </v-btn>
            <v-alert
              v-if="uploadResult"
              type="success"
              class="mt-4"
              density="compact"
            >
              Ingested {{ uploadResult.events_inserted }} events
              ({{ uploadResult.vectors_inserted }} vectors)
            </v-alert>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>Filters</v-card-title>
          <v-card-text>
            <v-row>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model="filters.q"
                  label="Search message"
                  density="comfortable"
                  append-inner-icon="mdi-magnify"
                  @keydown.enter="applyFilters"
                />
              </v-col>
              <v-col cols="12" sm="3">
                <v-text-field
                  v-model="filters.source"
                  label="Source"
                  density="comfortable"
                />
              </v-col>
              <v-col cols="12" sm="3">
                <v-text-field
                  v-model="filters.tag"
                  label="Tag"
                  density="comfortable"
                />
              </v-col>
            </v-row>
            <v-btn color="primary" @click="applyFilters">Apply</v-btn>
            <v-btn variant="text" class="ml-2" @click="resetFilters">Reset</v-btn>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-4">
      <v-col cols="12">
        <v-card>
          <v-card-title>
            Events
            <span class="text-body-2 text-disabled ml-2">
              ({{ total }} total)
            </span>
          </v-card-title>
          <v-data-table
            :headers="headers"
            :items="events"
            :items-per-page="limit"
            :page="page"
            :loading="loading"
            hide-default-footer
            class="elevation-0"
          >
            <template #item.timestamp="{ item }">
              {{ formatTimestamp(item.timestamp) }}
            </template>
            <template #item.tags="{ item }">
              <v-chip
                v-for="tag in item.tags || []"
                :key="tag"
                size="x-small"
                class="mr-1"
              >
                {{ tag }}
              </v-chip>
            </template>
            <template #item.attributes="{ item }">
              <span class="text-caption">{{ formatAttributes(item.attributes) }}</span>
            </template>
          </v-data-table>
          <v-pagination
            v-if="totalPages > 1"
            v-model="page"
            :length="totalPages"
            class="pa-4"
            @update:model-value="loadEvents"
          />
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  getTimeline as apiGetTimeline,
  uploadTimeline as apiUploadTimeline,
  listEvents,
} from "../services/api";
import type { Timeline, EventRecord } from "../services/api";

const route = useRoute();
const caseId = route.params.caseId as string;
const timelineId = route.params.timelineId as string;

const timeline = ref<Timeline | null>(null);
const selectedFile = ref<File[] | null>(null);
const uploadParser = ref("auto");
const uploading = ref(false);
const uploadResult = ref<{
  events_inserted: number;
  vectors_inserted: number;
} | null>(null);

const events = ref<EventRecord[]>([]);
const total = ref(0);
const loading = ref(false);
const page = ref(1);
const limit = ref(50);
const filters = reactive({ q: "", source: "", tag: "" });

const headers = [
  { title: "Time", key: "timestamp", width: "180px" },
  { title: "Source", key: "source", width: "120px" },
  { title: "Message", key: "message" },
  { title: "Tags", key: "tags", width: "150px" },
  { title: "Attributes", key: "attributes", width: "200px" },
];

const totalPages = computed(() => Math.ceil(total.value / limit.value) || 1);

async function loadTimeline() {
  const response = await apiGetTimeline(caseId, timelineId);
  timeline.value = response.timeline;
}

async function upload() {
  const fileArray = selectedFile.value;
  if (!fileArray || fileArray.length === 0) return;
  uploading.value = true;
  const result = await apiUploadTimeline(
    caseId,
    timelineId,
    fileArray[0],
    uploadParser.value === "auto" ? undefined : uploadParser.value
  );
  uploadResult.value = result;
  selectedFile.value = null;
  uploading.value = false;
  await loadTimeline();
  await loadEvents();
}

async function loadEvents() {
  loading.value = true;
  const offset = (page.value - 1) * limit.value;
  const result = await listEvents(caseId, timelineId, {
    q: filters.q || undefined,
    source: filters.source || undefined,
    tag: filters.tag || undefined,
    limit: limit.value,
    offset,
  });
  events.value = result.events;
  total.value = result.total;
  loading.value = false;
}

function applyFilters() {
  page.value = 1;
  loadEvents();
}

function resetFilters() {
  filters.q = "";
  filters.source = "";
  filters.tag = "";
  page.value = 1;
  loadEvents();
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

function formatAttributes(attrs: Record<string, string> | null): string {
  if (!attrs) return "";
  return Object.entries(attrs)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
}

watch(page, loadEvents);

onMounted(async () => {
  await loadTimeline();
  await loadEvents();
});
</script>
