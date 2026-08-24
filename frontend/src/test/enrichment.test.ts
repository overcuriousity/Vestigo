import { describe, expect, it } from "vitest";
import {
  derivedFieldKey,
  getAttributeDecoration,
  hasEnrichmentSiblings,
} from "@/lib/enrichment";

describe("derivedFieldKey", () => {
  it("matches the backend naming contract", () => {
    expect(derivedFieldKey("src_ip", "geo_country")).toBe("src_ip:geo_country");
  });
});

describe("hasEnrichmentSiblings", () => {
  const attributes = {
    src_ip: "8.8.8.8",
    "src_ip:geo_country_code": "US",
    src: "something", // prefix of src_ip — must not inherit its siblings
    dst_ip: "10.0.0.1",
  };

  it("detects sibling enrichment keys", () => {
    expect(hasEnrichmentSiblings(attributes, "src_ip")).toBe(true);
  });

  it("returns false when no enricher produced output for the attribute", () => {
    expect(hasEnrichmentSiblings(attributes, "dst_ip")).toBe(false);
  });

  it("does not false-positive on attribute keys that prefix another key", () => {
    expect(hasEnrichmentSiblings(attributes, "src")).toBe(false);
  });
});

describe("getAttributeDecoration", () => {
  const attributes = {
    src_ip: "8.8.8.8",
    "src_ip:geo_country_code": "US",
    "src_ip:geo_country": "United States",
    "src_ip:geo_city": "Mountain View",
    dst_ip: "10.0.0.1",
  };

  it("returns flag and city/country label for an enriched attribute", () => {
    expect(getAttributeDecoration(attributes, "src_ip")).toEqual({
      flag: "🇺🇸",
      label: "Mountain View, United States",
    });
  });

  it("returns null when the attribute has no country-code enrichment", () => {
    expect(getAttributeDecoration(attributes, "dst_ip")).toBeNull();
  });

  it("falls back to a generic label when only the code is present", () => {
    expect(getAttributeDecoration({ "ip:geo_country_code": "DE" }, "ip")).toEqual({
      flag: "🇩🇪",
      label: "GeoIP match",
    });
  });

  it("merges the ASN operator into the geo label when both enrichers fired", () => {
    expect(
      getAttributeDecoration(
        {
          ...attributes,
          "src_ip:asn_number": "15169",
          "src_ip:asn_org": "Google LLC",
        },
        "src_ip",
      ),
    ).toEqual({
      flag: "🇺🇸",
      label: "Mountain View, United States — AS15169 Google LLC",
    });
  });

  it("renders an AS marker with the operator label when only ASN output exists", () => {
    expect(
      getAttributeDecoration(
        { "ip:asn_number": "64512", "ip:asn_org": "Example Hosting GmbH" },
        "ip",
      ),
    ).toEqual({ flag: "AS", label: "AS64512 Example Hosting GmbH" });
  });

  it("labels by organization alone when the AS number is absent", () => {
    expect(getAttributeDecoration({ "ip:asn_org": "Example Hosting GmbH" }, "ip")).toEqual({
      flag: "AS",
      label: "Example Hosting GmbH",
    });
  });
});

describe("splitDerivedKey", () => {
  it("splits a derived key into parent and output field", async () => {
    const { splitDerivedKey } = await import("@/lib/enrichment");
    expect(splitDerivedKey("src_ip:geo_country")).toEqual({
      parent: "src_ip",
      field: "geo_country",
    });
  });

  it("splits on the last separator for multi-separator keys", async () => {
    const { splitDerivedKey } = await import("@/lib/enrichment");
    expect(splitDerivedKey("a:b:c")).toEqual({ parent: "a:b", field: "c" });
  });

  it("returns null for plain keys and degenerate forms", async () => {
    const { splitDerivedKey } = await import("@/lib/enrichment");
    expect(splitDerivedKey("src_ip")).toBeNull();
    expect(splitDerivedKey(":x")).toBeNull();
    expect(splitDerivedKey("x:")).toBeNull();
  });
});
