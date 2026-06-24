<template>
  <v-container>
    <v-row>
      <v-col cols="12">
        <h1 class="text-h4 mb-4">Cases</h1>
      </v-col>
    </v-row>

    <v-row>
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>Create Case</v-card-title>
          <v-card-text>
            <v-form @submit.prevent="createCase">
              <v-text-field
                v-model="newCase.name"
                label="Case name"
                required
                density="comfortable"
              />
              <v-textarea
                v-model="newCase.description"
                label="Description (optional)"
                rows="2"
                density="comfortable"
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
          <v-card-title>Existing Cases</v-card-title>
          <v-list>
            <v-list-item
              v-for="caseItem in cases"
              :key="caseItem.id"
              :to="`/cases/${caseItem.id}`"
              link
            >
              <v-list-item-title>{{ caseItem.name }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ caseItem.description || "No description" }}
              </v-list-item-subtitle>
            </v-list-item>
            <v-list-item v-if="cases.length === 0">
              <v-list-item-title class="text-disabled">No cases yet.</v-list-item-title>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { listCases, createCase as apiCreateCase } from "../services/api";
import type { Case } from "../services/api";

const cases = ref<Case[]>([]);
const creating = ref(false);
const newCase = reactive({ name: "", description: "" });

async function loadCases() {
  cases.value = await listCases();
}

async function createCase() {
  if (!newCase.name.trim()) return;
  creating.value = true;
  await apiCreateCase(newCase.name, newCase.description || undefined);
  newCase.name = "";
  newCase.description = "";
  await loadCases();
  creating.value = false;
}

onMounted(loadCases);
</script>
