import { post } from "./client";

export const demoApi = {
  /**
   * Import a fresh copy of the demo case for the current user.
   *
   * Returns a job id; the import runs in the background and shows up in the
   * job tray like any other case import.
   */
  seed: () => post<{ job_id: string }>("/demo/seed", {}),
};
