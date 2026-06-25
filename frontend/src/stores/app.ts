/*
 * Copyright 2024 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Adapted for TraceVector from Google Timesketch frontend-v3.
 */

import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  Case,
  Timeline,
  EventRecord,
  EventPage,
  FilterState,
  SavedView,
} from "@/services/api";
import {
  listCases,
  getCase,
  listTimelines,
  getTimeline,
  listEvents,
  listViews,
} from "@/services/api";

export const useAppStore = defineStore("app", () => {
  // State
  const cases = ref<Case[]>([]);
  const currentCase = ref<Case | null>(null);
  const currentTimeline = ref<Timeline | null>(null);
  const timelines = ref<Timeline[]>([]);
  const events = ref<EventRecord[]>([]);
  const eventTotal = ref(0);
  const eventOffset = ref(0);
  const eventLimit = ref(50);
  const loading = ref(false);
  const errorMessage = ref<string | null>(null);
  const selectedEventIds = ref<Set<string>>(new Set());
  const savedViews = ref<SavedView[]>([]);
  const activeFilters = ref<FilterState>({});

  // Getters
  const currentCaseName = computed(() => currentCase.value?.name || "");
  const currentTimelineName = computed(() => currentTimeline.value?.name || "");
  const hasActiveFilters = computed(
    () =>
      !!(
        activeFilters.value.q ||
        activeFilters.value.source ||
        activeFilters.value.tag ||
        activeFilters.value.start ||
        activeFilters.value.end ||
        Object.keys(activeFilters.value.fields || {}).length > 0 ||
        Object.keys(activeFilters.value.exclude || {}).length > 0
      ),
  );
  const selectedEvents = computed(() =>
    events.value.filter((e) => selectedEventIds.value.has(e.event_id)),
  );
  const totalPages = computed(
    () => Math.ceil(eventTotal.value / eventLimit.value) || 1,
  );
  const currentPage = computed(
    () => Math.floor(eventOffset.value / eventLimit.value) + 1,
  );

  // Actions
  async function loadCases() {
    loading.value = true;
    try {
      cases.value = await listCases();
    } catch (e) {
      errorMessage.value = "Failed to load cases";
    } finally {
      loading.value = false;
    }
  }

  async function loadCase(caseId: string) {
    loading.value = true;
    try {
      const response = await getCase(caseId);
      currentCase.value = response.case;
    } catch (e) {
      errorMessage.value = "Failed to load case";
    } finally {
      loading.value = false;
    }
  }

  async function loadTimelines(caseId: string) {
    loading.value = true;
    try {
      timelines.value = await listTimelines(caseId);
    } catch (e) {
      errorMessage.value = "Failed to load timelines";
    } finally {
      loading.value = false;
    }
  }

  async function loadTimeline(caseId: string, timelineId: string) {
    loading.value = true;
    try {
      const [caseResponse, timelineResponse, timelinesResponse] =
        await Promise.all([
          getCase(caseId),
          getTimeline(caseId, timelineId),
          listTimelines(caseId),
        ]);
      currentCase.value = caseResponse.case;
      currentTimeline.value = timelineResponse.timeline;
      timelines.value = timelinesResponse;
    } catch (e) {
      errorMessage.value = "Failed to load timeline";
    } finally {
      loading.value = false;
    }
  }

  async function loadEvents(
    caseId: string,
    timelineId: string,
    filters: FilterState = {},
  ) {
    loading.value = true;
    activeFilters.value = { ...filters };
    try {
      const page: EventPage = await listEvents(caseId, timelineId, {
        ...filters,
        limit: eventLimit.value,
        offset: eventOffset.value,
      });
      events.value = page.events;
      eventTotal.value = page.total;
      eventOffset.value = page.offset;
      eventLimit.value = page.limit;
    } catch (e) {
      errorMessage.value = "Failed to load events";
    } finally {
      loading.value = false;
    }
  }

  async function loadSavedViews(caseId: string) {
    try {
      savedViews.value = await listViews(caseId);
    } catch {
      savedViews.value = [];
    }
  }

  function setPage(page: number) {
    eventOffset.value = (page - 1) * eventLimit.value;
  }

  function setLimit(limit: number) {
    eventLimit.value = limit;
    eventOffset.value = 0;
  }

  function selectEvent(eventId: string, selected: boolean) {
    if (selected) {
      selectedEventIds.value.add(eventId);
    } else {
      selectedEventIds.value.delete(eventId);
    }
  }

  function selectAllEvents(selected: boolean) {
    if (selected) {
      events.value.forEach((e) => selectedEventIds.value.add(e.event_id));
    } else {
      selectedEventIds.value.clear();
    }
  }

  function clearSelection() {
    selectedEventIds.value.clear();
  }

  function clearError() {
    errorMessage.value = null;
  }

  return {
    cases,
    currentCase,
    currentTimeline,
    timelines,
    events,
    eventTotal,
    eventOffset,
    eventLimit,
    loading,
    errorMessage,
    selectedEventIds,
    savedViews,
    activeFilters,
    currentCaseName,
    currentTimelineName,
    hasActiveFilters,
    selectedEvents,
    totalPages,
    currentPage,
    loadCases,
    loadCase,
    loadTimelines,
    loadTimeline,
    loadEvents,
    loadSavedViews,
    setPage,
    setLimit,
    selectEvent,
    selectAllEvents,
    clearSelection,
    clearError,
  };
});
