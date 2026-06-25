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
  <v-card>
    <v-card-title>Upload timeline file</v-card-title>
    <v-card-text>
      <v-file-input
        v-model="selectedFile"
        label="CSV, JSONL, TSV or JSON file"
        accept=".csv,.jsonl,.tsv,.json"
        density="comfortable"
        show-size
        :disabled="uploading"
      />
      <v-select
        v-model="parser"
        :items="parserOptions"
        label="Parser"
        density="comfortable"
        :disabled="uploading"
      />
      <v-progress-linear
        v-if="uploading"
        indeterminate
        color="primary"
        class="mt-2"
      />
      <v-alert
        v-if="result"
        type="success"
        density="compact"
        class="mt-4"
        closable
        @click:close="result = null"
      >
        Ingested {{ result.events_inserted }} events using
        <code>{{ result.parser }}</code>. Generate embeddings from the timeline
        view when you want vector search.
      </v-alert>
    </v-card-text>
    <v-card-actions>
      <v-spacer />
      <v-btn variant="text" :disabled="uploading" @click="$emit('cancel')">
        Cancel
      </v-btn>
      <v-btn
        color="primary"
        :loading="uploading"
        :disabled="!firstFile()"
        @click="upload"
      >
        Ingest
      </v-btn>
    </v-card-actions>
  </v-card>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { uploadTimeline } from "@/services/api";
import type { UploadResult } from "@/services/api";

const props = defineProps<{
  caseId: string;
  timelineId: string;
}>();

const emit = defineEmits<{
  (e: "cancel"): void;
  (e: "uploaded", result: UploadResult): void;
}>();

const parserOptions = ["auto", "timesketch_csv", "jsonl"];
const selectedFile = ref<File | File[] | null>(null);
const parser = ref("auto");
const uploading = ref(false);
const result = ref<UploadResult | null>(null);

function firstFile(): File | null {
  const value = selectedFile.value;
  if (!value) return null;
  if (Array.isArray(value)) return value[0] || null;
  return value;
}

async function upload() {
  const file = firstFile();
  if (!file) return;
  uploading.value = true;
  try {
    const res = await uploadTimeline(
      props.caseId,
      props.timelineId,
      file,
      parser.value,
    );
    result.value = res;
    emit("uploaded", res);
  } catch {
    // Error is shown by global notification interceptor.
  } finally {
    uploading.value = false;
  }
}
</script>
