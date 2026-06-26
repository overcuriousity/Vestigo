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
  <DefaultLayout>
    <!-- Confirm-delete dialog (shared for both case and timeline deletes) -->
    <v-dialog v-model="confirmDialog" max-width="420" persistent>
      <v-card>
        <v-card-title class="text-h6">Confirm delete</v-card-title>
        <v-card-text>
          {{ confirmMessage }}
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="deleting" @click="confirmDialog = false">
            Cancel
          </v-btn>
          <v-btn color="error" :loading="deleting" @click="confirmAction">
            Delete
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-row>
      <v-col cols="12" class="d-flex align-center">
        <v-btn variant="text" to="/cases" prepend-icon="mdi-arrow-left">
          Back to cases
        </v-btn>
        <v-spacer />
        <v-btn
          color="error"
          variant="outlined"
          prepend-icon="mdi-delete"
          size="small"
          @click="promptDeleteCase"
        >
          Delete case
        </v-btn>
      </v-col>
      <v-col cols="12">
        <h1 class="text-h4 mt-2">{{ appStore.currentCase?.name || "Case" }}</h1>
        <p class="text-body-2 text-disabled">
          {{ appStore.currentCase?.description }}
        </p>
      </v-col>
    </v-row>

    <v-row>
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>Create timeline</v-card-title>
          <v-card-text>
            <v-form @submit.prevent="createTimeline">
              <v-text-field
                v-model="newTimeline.name"
                label="Timeline name"
                required
                density="comfortable"
              />
              <v-textarea
                v-model="newTimeline.description"
                label="Description (optional)"
                rows="2"
                density="comfortable"
              />
              <v-select
                v-model="newTimeline.parser"
                :items="['', 'timesketch_csv', 'jsonl']"
                label="Parser (optional)"
                density="comfortable"
                clearable
              />
              <v-btn
                type="submit"
                color="primary"
                :loading="creating"
                :disabled="!newTimeline.name"
              >
                Create
              </v-btn>
            </v-form>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-4">
      <v-col cols="12">
        <v-card>
          <v-card-title>Timelines</v-card-title>
          <v-list>
            <v-list-item
              v-for="timeline in appStore.timelines"
              :key="timeline.id"
              :to="`/cases/${caseId}/timelines/${timeline.id}`"
              link
            >
              <template #prepend>
                <v-icon color="primary">mdi-timeline</v-icon>
              </template>
              <v-list-item-title>{{ timeline.name }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ timeline.event_count }} events /
                {{ timeline.vector_count }} vectors &mdash;
                {{ timeline.description || "No description" }}
              </v-list-item-subtitle>
              <template #append>
                <v-btn
                  icon
                  variant="text"
                  color="error"
                  size="small"
                  @click.prevent="promptDeleteTimeline(timeline.id, timeline.name)"
                >
                  <v-icon>mdi-delete</v-icon>
                </v-btn>
              </template>
            </v-list-item>
            <v-list-item v-if="appStore.timelines.length === 0">
              <v-list-item-title class="text-disabled">
                No timelines yet.
              </v-list-item-title>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>
  </DefaultLayout>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import DefaultLayout from "@/layouts/Default.vue";
import { useAppStore } from "@/stores/app";
import { createTimeline as apiCreateTimeline } from "@/services/api";

const route = useRoute();
const router = useRouter();
const appStore = useAppStore();
const caseId = route.params.caseId as string;

const creating = ref(false);
const deleting = ref(false);
const newTimeline = reactive({ name: "", description: "", parser: "" });

// Confirmation dialog state
const confirmDialog = ref(false);
const confirmMessage = ref("");
let pendingAction: (() => Promise<void>) | null = null;

function promptDeleteTimeline(timelineId: string, timelineName: string) {
  confirmMessage.value = `Delete timeline "${timelineName}"? This will permanently remove all its events and vectors.`;
  pendingAction = async () => {
    await appStore.deleteTimeline(caseId, timelineId);
  };
  confirmDialog.value = true;
}

function promptDeleteCase() {
  const name = appStore.currentCase?.name || caseId;
  confirmMessage.value = `Delete case "${name}"? This will permanently remove the case and ALL its timelines, events, and vectors.`;
  pendingAction = async () => {
    await appStore.deleteCase(caseId);
    router.push("/cases");
  };
  confirmDialog.value = true;
}

async function confirmAction() {
  if (!pendingAction) return;
  deleting.value = true;
  try {
    await pendingAction();
    confirmDialog.value = false;
  } finally {
    deleting.value = false;
    pendingAction = null;
  }
}

async function createTimeline() {
  if (!newTimeline.name.trim()) return;
  creating.value = true;
  try {
    await apiCreateTimeline(
      caseId,
      newTimeline.name,
      newTimeline.description || undefined,
      newTimeline.parser || undefined,
    );
    newTimeline.name = "";
    newTimeline.description = "";
    newTimeline.parser = "";
    await appStore.loadTimelines(caseId);
  } finally {
    creating.value = false;
  }
}

onMounted(async () => {
  await appStore.loadCase(caseId);
  await appStore.loadTimelines(caseId);
});
</script>
