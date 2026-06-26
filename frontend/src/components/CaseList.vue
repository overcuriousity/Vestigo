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
  <!-- Confirm-delete dialog -->
  <v-dialog v-model="confirmDialog" max-width="420" persistent>
    <v-card>
      <v-card-title class="text-h6">Confirm delete</v-card-title>
      <v-card-text>
        Delete case "{{ pendingCaseName }}"? This will permanently remove the
        case and ALL its timelines, events, and vectors.
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" :disabled="deleting" @click="confirmDialog = false">
          Cancel
        </v-btn>
        <v-btn color="error" :loading="deleting" @click="confirmDelete">
          Delete
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-card>
    <v-card-title>Recent investigations</v-card-title>
    <v-list>
      <v-list-item
        v-for="caseItem in cases"
        :key="caseItem.id"
        :to="`/cases/${caseItem.id}`"
        link
      >
        <template #prepend>
          <v-icon color="primary">mdi-folder-open</v-icon>
        </template>
        <v-list-item-title>{{ caseItem.name }}</v-list-item-title>
        <v-list-item-subtitle>
          {{ caseItem.description || "No description" }}
        </v-list-item-subtitle>
        <template #append>
          <v-btn
            icon
            variant="text"
            color="error"
            size="small"
            @click.prevent="promptDelete(caseItem.id, caseItem.name)"
          >
            <v-icon>mdi-delete</v-icon>
          </v-btn>
        </template>
      </v-list-item>
      <v-list-item v-if="cases.length === 0">
        <v-list-item-title class="text-disabled">
          No cases yet.
        </v-list-item-title>
      </v-list-item>
    </v-list>
  </v-card>
</template>

<script setup lang="ts">
import { ref } from "vue";
import type { Case } from "@/services/api";
import { useAppStore } from "@/stores/app";

defineProps<{
  cases: Case[];
}>();

const appStore = useAppStore();

const confirmDialog = ref(false);
const deleting = ref(false);
const pendingCaseId = ref("");
const pendingCaseName = ref("");

function promptDelete(caseId: string, caseName: string) {
  pendingCaseId.value = caseId;
  pendingCaseName.value = caseName;
  confirmDialog.value = true;
}

async function confirmDelete() {
  deleting.value = true;
  try {
    await appStore.deleteCase(pendingCaseId.value);
    confirmDialog.value = false;
  } finally {
    deleting.value = false;
  }
}
</script>
