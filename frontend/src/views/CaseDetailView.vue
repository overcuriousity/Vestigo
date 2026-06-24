<template>
  <v-container>
    <v-row>
      <v-col cols="12">
        <v-btn variant="text" to="/cases" prepend-icon="mdi-arrow-left">Back to cases</v-btn>
        <h1 class="text-h4 mt-2">{{ caseItem?.name || "Case" }}</h1>
        <p class="text-body-2 text-disabled">{{ caseItem?.description }}</p>
      </v-col>
    </v-row>

    <v-row>
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>Create Timeline</v-card-title>
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
              <v-btn type="submit" color="primary" :loading="creating">Create</v-btn>
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
              v-for="timeline in timelines"
              :key="timeline.id"
              :to="`/cases/${caseId}/timelines/${timeline.id}`"
              link
            >
              <v-list-item-title>{{ timeline.name }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ timeline.event_count }} events / {{ timeline.vector_count }} vectors
                &mdash; {{ timeline.description || "No description" }}
              </v-list-item-subtitle>
            </v-list-item>
            <v-list-item v-if="timelines.length === 0">
              <v-list-item-title class="text-disabled">No timelines yet.</v-list-item-title>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import {
  getCase as apiGetCase,
  listTimelines,
  createTimeline as apiCreateTimeline,
} from "../services/api";
import type { Case, Timeline } from "../services/api";

const route = useRoute();
const caseId = route.params.caseId as string;

const caseItem = ref<Case | null>(null);
const timelines = ref<Timeline[]>([]);
const creating = ref(false);
const newTimeline = reactive({ name: "", description: "", parser: "" });

async function loadData() {
  caseItem.value = await apiGetCase(caseId).then((r) => r.case);
  timelines.value = await listTimelines(caseId);
}

async function createTimeline() {
  if (!newTimeline.name.trim()) return;
  creating.value = true;
  await apiCreateTimeline(
    caseId,
    newTimeline.name,
    newTimeline.description || undefined,
    newTimeline.parser || undefined
  );
  newTimeline.name = "";
  newTimeline.description = "";
  newTimeline.parser = "";
  await loadData();
  creating.value = false;
}

onMounted(loadData);
</script>
