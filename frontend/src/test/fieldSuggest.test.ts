/**
 * Merge-suggestion heuristics for the timeline wizard (issue #10):
 * value-shape classification, name tokenization with synonym expansion, and
 * the grouping rules (shared meaningful token + compatible shape, never
 * across conflicting directional tokens).
 */
import { describe, it, expect } from "vitest";
import {
  classifyValue,
  classifySamples,
  nameTokens,
  suggestGroups,
} from "@/lib/fieldSuggest";

describe("classifyValue", () => {
  it("recognizes common forensic value shapes", () => {
    expect(classifyValue("10.0.0.1")).toBe("ip");
    expect(classifyValue("2001:db8::1")).toBe("ip");
    expect(classifyValue("42")).toBe("number");
    expect(classifyValue("2026-01-01T10:00:00Z")).toBe("timestamp");
    expect(classifyValue("a@b.example")).toBe("email");
    expect(classifyValue("d41d8cd98f00b204e9800998ecf8427e")).toBe("hash");
    expect(classifyValue("https://example.test/x")).toBe("url");
    expect(classifyValue("plain words")).toBe("text");
    expect(classifyValue("")).toBe("unknown");
  });
});

describe("classifySamples", () => {
  it("returns the dominant shape", () => {
    expect(classifySamples(["10.0.0.1", "10.0.0.2", "not-an-ip"])).toBe("ip");
    expect(classifySamples([])).toBe("unknown");
  });
});

describe("nameTokens", () => {
  it("splits snake/camel case and expands synonyms", () => {
    expect(nameTokens("src_ip")).toEqual(["source", "ip"]);
    expect(nameTokens("ipAddr")).toEqual(["ip", "address"]);
    expect(nameTokens("username")).toEqual(["user"]);
  });
});

describe("suggestGroups", () => {
  it("merges src_ip and ip_addr on shared token + ip shape", () => {
    const groups = suggestGroups([
      { key: "src_ip", samples: ["10.0.0.1"], sourceIds: ["a"] },
      { key: "ip_addr", samples: ["10.0.0.2"], sourceIds: ["b"] },
      { key: "status", samples: ["200"], sourceIds: ["a"] },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].fields).toEqual(["ip_addr", "src_ip"]);
    expect(groups[0].name).toContain("ip");
    expect(groups[0].reason).toContain("ip");
  });

  it("never merges across conflicting directional tokens", () => {
    const groups = suggestGroups([
      { key: "src_ip", samples: ["10.0.0.1"], sourceIds: ["a"] },
      { key: "dst_ip", samples: ["10.0.0.2"], sourceIds: ["a"] },
    ]);
    expect(groups).toHaveLength(0);
  });

  it("does not merge same-named tokens with conflicting value shapes", () => {
    const groups = suggestGroups([
      { key: "user_id", samples: ["12345"], sourceIds: ["a"] },
      { key: "user_email", samples: ["a@b.example"], sourceIds: ["b"] },
    ]);
    expect(groups).toHaveLength(0);
  });

  it("merges user/username via synonym expansion", () => {
    const groups = suggestGroups([
      { key: "user", samples: ["alice"], sourceIds: ["a"] },
      { key: "username", samples: ["bob"], sourceIds: ["b"] },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].fields).toEqual(["user", "username"]);
    expect(groups[0].name).toBe("user_name");
  });

  it("returns nothing for singleton fields", () => {
    expect(
      suggestGroups([{ key: "status", samples: ["200"], sourceIds: ["a"] }]),
    ).toHaveLength(0);
  });
});
