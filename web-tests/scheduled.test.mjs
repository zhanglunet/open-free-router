import test from "node:test";
import assert from "node:assert/strict";

import { isSixHourlySlot } from "../worker/index.js";

const at = (iso) => Date.parse(iso);

test("一整天的 */15 触发格里，恰好命中四格，且都在整点", () => {
  const hits = [];
  for (let m = 0; m < 24 * 60; m += 15) {
    const t = Date.UTC(2026, 8, 5, 0, m);
    if (isSixHourlySlot(t)) hits.push(new Date(t).toISOString().slice(11, 16));
  }
  // 与合并前的 `17 */6 * * *` 同为每天 4 次，只是时刻从 :17 移到 :00
  assert.deepEqual(hits, ["00:00", "06:00", "12:00", "18:00"]);
});

test("判的是计划时刻，迟到的执行仍算命中", () => {
  // cron 是尽力而为的触发：真实执行可能晚几十秒。scheduledTime 是计划时刻，
  // 用它判格子，延迟不会让这一格被判丢。
  assert.equal(isSixHourlySlot(at("2026-09-05T06:00:00Z")), true);
  assert.equal(isSixHourlySlot(at("2026-09-05T06:00:47Z")), true);
});

test("非整点、或小时不是 6 的倍数，一律不命中", () => {
  assert.equal(isSixHourlySlot(at("2026-09-05T06:15:00Z")), false);
  assert.equal(isSixHourlySlot(at("2026-09-05T06:30:00Z")), false);
  assert.equal(isSixHourlySlot(at("2026-09-05T07:00:00Z")), false);
  assert.equal(isSixHourlySlot(at("2026-09-05T17:00:00Z")), false);
});

test("判定按 UTC，不随运行机器的时区漂", () => {
  // 若误用 getHours()，在 UTC+8 的机器上 06:00Z 的本地小时是 14，%6 不为 0，
  // 这一格就会被判丢——而且只在某些时区丢，最难查。
  const t = at("2026-09-05T06:00:00Z");
  assert.equal(new Date(t).getUTCHours(), 6);
  assert.equal(isSixHourlySlot(t), true);
});

test("拿不到计划时刻时跳过本轮，而不是每格都跑", () => {
  // 目录刷新单次约 245 ms CPU；误判成「每格都跑」会把它从 4 次/天变成 96 次/天。
  // 漏跑有兜底：getDiscovery 在快照超过 7 小时时自行补刷。
  assert.equal(isSixHourlySlot(undefined), false);
  assert.equal(isSixHourlySlot(NaN), false);
  assert.equal(isSixHourlySlot("不是时间"), false);
});
