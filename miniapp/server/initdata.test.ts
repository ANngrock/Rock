/**
 * initData: подпись, порча, протухание, локальный обход.
 * Математика — та же, что описана в docs.telegram.org; signInitData — наш тестовый
 * «Telegram», поэтому roundtrip ловит расхождение кодирования, а не догадки.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { dataCheckString, parseFields, secretKeyFor, signInitData, verifyInitData } from "./initdata.ts";

const TOKEN = "123456:AAA-test-token";

function userFields(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    auth_date: String(Math.floor(Date.now() / 1000)),
    user: JSON.stringify({ id: 42, first_name: "Тимур", username: "timur" }),
    start_param: "меню",
    query_id: "aaBB",
    ...overrides,
  };
}

test("roundtrip: подпись Telegram-математикой проходит проверку", () => {
  const initData = signInitData(userFields(), TOKEN);
  const v = verifyInitData(initData, TOKEN);
  assert.equal(v.ok, true, v.reason);
  assert.equal(v.data?.user?.id, 42);
  assert.equal(v.data?.startParam, "меню");
});

test("порча любого поля убивает подпись", () => {
  const initData = signInitData(userFields(), TOKEN);
  const bad = initData.replace("id%22%3A42", "id%22%3A43");
  assert.notEqual(bad, initData, "подмена не сработала — тест бессмысленный");
  const v = verifyInitData(bad, TOKEN);
  assert.equal(v.ok, false);
  assert.match(v.reason ?? "", /не от Telegram/);
});

test("чужой токен не проверяется нашим ключом", () => {
  const initData = signInitData(userFields(), "999999:другой-бот");
  const v = verifyInitData(initData, TOKEN);
  assert.equal(v.ok, false);
});

test("протухшая сессия отклоняется даже с валидной подписью", () => {
  const initData = signInitData(userFields({ auth_date: "1700000000" }), TOKEN);
  const v = verifyInitData(initData, TOKEN, { maxAgeSec: 3600 });
  assert.equal(v.ok, false);
  assert.match(v.reason ?? "", /протухла/);
});

test("без токена: жёсткий отказ, кроме явного dev-обхода", () => {
  const raw = "user=" + encodeURIComponent('{"id":7}') + "&auth_date=" + Math.floor(Date.now() / 1000);
  const hard = verifyInitData(raw, "");
  assert.equal(hard.ok, false);
  assert.match(hard.reason ?? "", /невозможна/);
  const dev = verifyInitData(raw, "", { allowUnverified: true });
  assert.equal(dev.ok, true);
  assert.equal(dev.data?.user?.id, 7);
});

test("secretKey стабилен и не пустой", () => {
  const a = secretKeyFor(TOKEN);
  assert.equal(a.length, 32);
  assert.deepEqual(a, secretKeyFor(TOKEN));
});

test("dataCheckString: сортировка и отсутствие hash", () => {
  const fields = parseFields("b=2&a=1&hash=zz");
  assert.equal(dataCheckString(fields), "a=1\nb=2");
});
