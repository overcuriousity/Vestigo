import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/",
});

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

export async function listCases(): Promise<Case[]> {
  const response = await api.get("/api/cases/");
  return response.data.cases;
}

export async function createCase(name: string, description?: string): Promise<Case> {
  const response = await api.post("/api/cases/", { name, description });
  return response.data.case;
}

export async function getCase(caseId: string): Promise<{ case: Case }> {
  const response = await api.get(`/api/cases/${caseId}`);
  return response.data;
}

export async function listTimelines(caseId: string): Promise<Timeline[]> {
  const response = await api.get(`/api/cases/${caseId}/timelines`);
  return response.data.timelines;
}

export async function createTimeline(
  caseId: string,
  name: string,
  description?: string,
  parser?: string
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
  timelineId: string
): Promise<{ timeline: Timeline }> {
  const response = await api.get(`/api/cases/${caseId}/timelines/${timelineId}`);
  return response.data;
}

export async function uploadTimeline(
  caseId: string,
  timelineId: string,
  file: File,
  parser?: string
): Promise<{
  timeline_id: string;
  events_parsed: number;
  events_inserted: number;
  vectors_inserted: number;
  parser: string;
}> {
  const formData = new FormData();
  formData.append("file", file);
  if (parser) {
    formData.append("parser", parser);
  }
  const response = await api.post(
    `/api/cases/${caseId}/timelines/${timelineId}/upload`,
    formData,
    { headers: { "Content-Type": "multipart/form-data" } }
  );
  return response.data;
}

export async function listEvents(
  caseId: string,
  timelineId: string,
  params: {
    q?: string;
    source?: string;
    tag?: string;
    limit?: number;
    offset?: number;
  }
): Promise<EventPage> {
  const response = await api.get(`/api/cases/${caseId}/timelines/${timelineId}/events`, {
    params,
  });
  return response.data;
}
