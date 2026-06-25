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

import axios, { AxiosError } from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/",
  headers: {
    "Content-Type": "application/json",
  },
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ message?: string; detail?: string }>) => {
    const message =
      error.response?.data?.message ||
      error.response?.data?.detail ||
      error.message ||
      "Unknown API error";
    window.dispatchEvent(new CustomEvent("api-error", { detail: message }));
    return Promise.reject(error);
  },
);

export interface Case {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface Timeline {
  id: string;
  case_id: string;
  name: string;
  description: string | null;
  parser: string | null;
  embedding_model: string | null;
  event_count: number;
  vector_count: number;
  created_at: string;
  updated_at: string;
}

export interface EventRecord {
  event_id: string;
  case_id: string;
  timeline_id: string;
  message: string;
  timestamp: string;
  timestamp_desc: string;
  source: string;
  source_long: string;
  display_name: string;
  tags: string[];
  attributes: Record<string, string>;
  vector_id: string;
}

export interface EventPage {
  total: number;
  offset: number;
  limit: number;
  events: EventRecord[];
}

export interface FilterState {
  q?: string;
  source?: string;
  tag?: string;
  start?: string;
  end?: string;
  fields?: Record<string, string>;
  exclude?: Record<string, string>;
}

export interface SavedView {
  id: string;
  case_id: string;
  name: string;
  query: string;
  filter: FilterState;
  created_at: string;
  updated_at: string;
}

export interface Annotation {
  id: string;
  event_id: string;
  annotation_type: "comment" | "tag";
  content: string;
  created_at: string;
  created_by?: string;
}

export interface UploadResult {
  timeline_id: string;
  events_parsed: number;
  events_inserted: number;
  parser: string;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed";
  progress: {
    total: number;
    processed: number;
  };
  result: {
    vectors_inserted: number;
  } | null;
  error: string | null;
}

export interface SimilarityResult {
  event_id: string;
  score: number;
  event: EventRecord;
}

export async function listCases(): Promise<Case[]> {
  const response = await api.get("/api/cases/");
  return response.data.cases;
}

export async function createCase(
  name: string,
  description?: string,
): Promise<Case> {
  const response = await api.post("/api/cases/", { name, description });
  return response.data.case;
}

export async function getCase(caseId: string): Promise<{ case: Case }> {
  const response = await api.get(`/api/cases/${caseId}`);
  return response.data;
}

export async function updateCase(
  caseId: string,
  name: string,
  description?: string,
): Promise<Case> {
  const response = await api.put(`/api/cases/${caseId}`, { name, description });
  return response.data.case;
}

export async function listTimelines(caseId: string): Promise<Timeline[]> {
  const response = await api.get(`/api/cases/${caseId}/timelines`);
  return response.data.timelines;
}

export async function createTimeline(
  caseId: string,
  name: string,
  description?: string,
  parser?: string,
): Promise<Timeline> {
  const response = await api.post(`/api/cases/${caseId}/timelines`, {
    name,
    description,
    parser,
  });
  return response.data.timeline;
}

export async function getTimeline(
  caseId: string,
  timelineId: string,
): Promise<{ timeline: Timeline }> {
  const response = await api.get(
    `/api/cases/${caseId}/timelines/${timelineId}`,
  );
  return response.data;
}

export async function updateTimeline(
  caseId: string,
  timelineId: string,
  updates: Partial<Pick<Timeline, "name" | "description" | "parser">>,
): Promise<Timeline> {
  const response = await api.put(
    `/api/cases/${caseId}/timelines/${timelineId}`,
    updates,
  );
  return response.data.timeline;
}

export async function deleteTimeline(
  caseId: string,
  timelineId: string,
): Promise<void> {
  await api.delete(`/api/cases/${caseId}/timelines/${timelineId}`);
}

export async function uploadTimeline(
  caseId: string,
  timelineId: string,
  file: File,
  parser?: string,
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  if (parser && parser !== "auto") {
    formData.append("parser", parser);
  }
  const response = await api.post(
    `/api/cases/${caseId}/timelines/${timelineId}/upload`,
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

export async function startEmbedding(
  caseId: string,
  timelineId: string,
): Promise<{ job_id: string; status: string }> {
  const response = await api.post(
    `/api/cases/${caseId}/timelines/${timelineId}/embed`,
  );
  return response.data;
}

export async function getJob(jobId: string): Promise<{ job: Job }> {
  const response = await api.get(`/api/jobs/${jobId}`);
  return response.data;
}

export async function listEvents(
  caseId: string,
  timelineId: string,
  params: FilterState & { limit?: number; offset?: number },
): Promise<EventPage> {
  const queryParams: Record<string, unknown> = { ...params };
  if (params.fields && Object.keys(params.fields).length > 0) {
    queryParams.filters = JSON.stringify(params.fields);
  } else {
    delete queryParams.fields;
  }
  if (params.exclude && Object.keys(params.exclude).length > 0) {
    queryParams.exclusions = JSON.stringify(params.exclude);
  } else {
    delete queryParams.exclude;
  }

  const response = await api.get(
    `/api/cases/${caseId}/timelines/${timelineId}/events`,
    { params: queryParams },
  );
  return response.data;
}

// Saved views (stub endpoints – backend support pending).
export async function listViews(caseId: string): Promise<SavedView[]> {
  try {
    const response = await api.get(`/api/cases/${caseId}/views`);
    return response.data.views;
  } catch {
    return [];
  }
}

export async function createView(
  caseId: string,
  name: string,
  query: string,
  filter: FilterState,
): Promise<SavedView> {
  const response = await api.post(`/api/cases/${caseId}/views`, {
    name,
    query,
    filter,
  });
  return response.data.view;
}

export async function deleteView(
  caseId: string,
  viewId: string,
): Promise<void> {
  await api.delete(`/api/cases/${caseId}/views/${viewId}`);
}

// Event annotations (stub endpoints – backend support pending).
export async function listAnnotations(
  caseId: string,
  timelineId: string,
  eventId: string,
): Promise<Annotation[]> {
  try {
    const response = await api.get(
      `/api/cases/${caseId}/timelines/${timelineId}/events/${eventId}/annotations`,
    );
    return response.data.annotations;
  } catch {
    return [];
  }
}

export async function addAnnotation(
  caseId: string,
  timelineId: string,
  eventId: string,
  annotationType: "comment" | "tag",
  content: string,
): Promise<Annotation> {
  const response = await api.post(
    `/api/cases/${caseId}/timelines/${timelineId}/events/${eventId}/annotations`,
    { annotation_type: annotationType, content },
  );
  return response.data.annotation;
}

// Export (stub endpoint – backend support pending).
export async function exportEvents(
  caseId: string,
  timelineId: string,
  format: "csv" | "jsonl",
  filter: FilterState,
): Promise<Blob> {
  const response = await api.post(
    `/api/cases/${caseId}/timelines/${timelineId}/export`,
    { format, filter },
    { responseType: "blob" },
  );
  return response.data;
}

// Similarity / anomaly search (stub endpoint – backend support pending).
export async function searchSimilar(
  caseId: string,
  timelineId: string,
  eventId: string,
  limit = 10,
): Promise<SimilarityResult[]> {
  try {
    const response = await api.get(
      `/api/cases/${caseId}/timelines/${timelineId}/events/${eventId}/similar`,
      { params: { limit } },
    );
    return response.data.results;
  } catch {
    return [];
  }
}

export async function getAnomalies(
  caseId: string,
  timelineId: string,
  limit = 50,
): Promise<SimilarityResult[]> {
  try {
    const response = await api.get(
      `/api/cases/${caseId}/timelines/${timelineId}/anomalies`,
      { params: { limit } },
    );
    return response.data.results;
  } catch {
    return [];
  }
}

export default api;
