/**
 * SESSION_START — epoch ms captured at module-load time.
 * Used to track per-session annotation activity without any backend state.
 * Resets on full page reload.
 */
export const SESSION_START = Date.now();
