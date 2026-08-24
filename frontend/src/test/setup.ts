import "@testing-library/jest-dom";

// jsdom ships no ResizeObserver, but components that measure themselves (the
// event grid's header offset, react-virtual) construct one unconditionally —
// as they should, since every browser has it. A no-op stub keeps those
// components on their real code path; anything that depends on an actual
// resize callback firing needs a browser, not a better stub.
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
