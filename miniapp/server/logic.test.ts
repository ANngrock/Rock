import assert from "node:assert/strict";
import { test } from "node:test";

import { TokenBucket } from "./ratelimit.ts";
import { parseClientMessage } from "./protocol.ts";
import { buildMessages } from "./vision.ts";

test("бакет: burst пропускается, лишнее — нет, время возвращает токены", () => {
  let now = 0;
  const b = new TokenBucket(2, 2, () => now); // 2/сек, burst 2
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), false);
  now += 500; // +1 токен
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), false);
});

test("бакет не копится впрок дольше burst", () => {
  let now = 0;
  const b = new TokenBucket(10, 3, () => now);
  now += 100_000;
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), true);
  assert.equal(b.allow(), false);
});

test("бакет на нулевых параметрах — ошибка конфигурации, не тихое «ничего не пропускать»", () => {
  assert.throws(() => new TokenBucket(0, 1));
});

test("протокол: мусор не проходит, схемы — проходят", () => {
  assert.equal(parseClientMessage("{не json"), null);
  assert.equal(parseClientMessage("[1,2]"), null);
  assert.equal(parseClientMessage('{"t":"неткая"}'), null);
  assert.equal(parseClientMessage('{"t":"init"}'), null);
  assert.deepEqual(parseClientMessage('{"t":"init","initData":"a=1"}'), { t: "init", initData: "a=1" });
  assert.deepEqual(parseClientMessage('{"t":"start","mode":"evil"}'), { t: "start", mode: "stream" });
  assert.deepEqual(parseClientMessage('{"t":"ask","text":"  го  "}'), { t: "ask", text: "го" });
  assert.equal(parseClientMessage('{"t":"ask","text":"   "}'), null);
  assert.equal(parseClientMessage('{"t":"frame","img":"' + "A".repeat(2_000_001) + '"}'), null);
  assert.deepEqual(parseClientMessage('{"t":"stop"}'), { t: "stop" });
});

test("промпт: вопрос, непрерывность стрима и data-url", () => {
  type Part = { type: string; text?: string; image_url?: { url: string } };
  type Msg = { role: string; content: unknown };
  const [sys, user] = buildMessages({ img: "ABCD", recent: ["был кот"] }) as Msg[];
  assert.ok(sys && user);
  assert.match(String(sys.content), /предыдущие наблюдения/i);
  assert.match(String(sys.content), /был кот/);
  const parts = (user.content ?? []) as Part[];
  assert.equal(parts[1]?.image_url?.url, "data:image/jpeg;base64,ABCD");
  const [_, qUser] = buildMessages({ img: "X", question: "кто у двери?", recent: [] }) as Msg[];
  const qParts = ((qUser ?? { content: [] }).content ?? []) as Part[];
  assert.match(String(qParts[0]?.text ?? ""), /кто у двери/);
});
