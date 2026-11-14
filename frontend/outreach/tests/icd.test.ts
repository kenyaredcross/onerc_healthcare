import { buildIcdIndex, searchIcd } from "../src/lib/icd";
import type { IcdRow } from "../src/lib/protocol";

const rows: IcdRow[] = [
  ["n-1F4", "1F4", "Malaria", 0],
  ["n-1F40", "1F40", "Malaria due to Plasmodium falciparum", 1],
  ["n-1F40.0", "1F40.0", "Plasmodium falciparum malaria with cerebral complications", 1],
  ["n-1F41", "1F41", "Malaria due to Plasmodium vivax", 1],
  ["n-CA40", "CA40", "Pneumonia", 0],
  ["n-CA40.0", "CA40.0", "Bacterial pneumonia", 1],
  ["n-XM1F40", "XM1F40", "Some extension containing 1F40", 1],
  ["n-MG26", "MG26", "Fever of other or unknown origin", 1],
  ["n-1A00", "1A00", "Cholera", 1],
  ["n-XX", "QA00", "Poisoning by antimalarial drugs", 1],
];
const index = buildIcdIndex(rows);
const codes = (q: string) => searchIcd(index, q).map((h) => h.code);

describe("ICD-11 search ranking", () => {
  it("exact code > code prefix (leaf first) > title prefix > contains", () => {
    expect(codes("1F40")).toEqual(["1F40", "1F40.0", "XM1F40"]);
    expect(searchIcd(index, "1f40")[0]!.rank).toBe(0);
    expect(codes("1F4")).toEqual(["1F4", "1F40", "1F41", "1F40.0", "XM1F40"]);
  });

  it("title prefix (any word) before contains, leaf codes before groups", () => {
    const hits = searchIcd(index, "malaria");
    expect(hits.map((h) => [h.code, h.rank])).toEqual([
      ["1F40", 2],
      ["1F41", 2],
      ["1F40.0", 2],
      ["1F4", 2],
      ["QA00", 3],
    ]);
  });

  it("title word prefix, case-insensitive", () => {
    expect(codes("PNEUM")).toEqual(["CA40.0", "CA40"]);
    expect(codes("fever")).toEqual(["MG26"]);
    expect(codes("  ")).toEqual([]);
  });
});
