import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fetchDiscovery } from "../worker/discovery.js";

const catalogPath = resolve(process.argv[2] ?? "site/data/catalog.json");
const outputPath = resolve(process.argv[3] ?? "site/data/discovery-seed.json");
const catalog = JSON.parse(await readFile(catalogPath, "utf8"));
const snapshot = await fetchDiscovery(catalog.providers);
await writeFile(outputPath, JSON.stringify(snapshot, null, 2) + "\n");
console.log(`Discovered ${snapshot.candidate_provider_count} candidates / ${snapshot.candidate_model_count} models`);
