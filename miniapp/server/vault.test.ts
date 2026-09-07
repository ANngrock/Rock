/**
 * Кросс-языковая совместимость криптоконверта: файл векторов порождён python'ом
 * (aegis.platform.vault), здесь проверяем, что TS его вскрывает и печатает побайтно то же.
 * Это не «тест на тест»: разъезд формата между ботом и мини-аппом молча расшифровывается
 * в «след не читается» только в проде.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { deriveKek, openText, sealText } from "./vault.ts";

interface Case {
  gen: number;
  plaintext: string;
  py_sealed: string;
  ts_sealed: string;
}
interface Vectors {
  master_kek_b64: string;
  keep: number;
  cases: Case[];
}

const data = JSON.parse(
  readFileSync(new URL("../testdata/vault_vectors.json", import.meta.url), "utf8"),
) as Vectors;

function ringFor(gen: number): Map<number, Buffer> {
  const master = Buffer.from(data.master_kek_b64, "base64");
  const m = new Map<number, Buffer>();
  for (let v = Math.max(1, gen - data.keep + 1); v <= gen; v++) m.set(v, deriveKek(master, v));
  return m;
}

test("python-запечатанное читается отсюда", () => {
  const ring = ringFor(Math.max(...data.cases.map((c) => c.gen)));
  for (const c of data.cases) {
    assert.ok(c.py_sealed.startsWith("aeg1s:"), "префикс конверта");
    assert.equal(openText(ring, c.py_sealed), c.plaintext, `открыть «${c.plaintext.slice(0, 24)}»`);
  }
});

test("наше детерминированное запечатывание совпадает с эталоном из файла", () => {
  for (const c of data.cases) {
    const ring = ringFor(c.gen);
    const fixed = () => Buffer.alloc(12, 0x2a);
    const mine = sealText(ring, c.gen, c.plaintext, { nonce: fixed, dek: Buffer.alloc(32, 0x7e) });
    assert.equal(mine, c.ts_sealed, `байт-в-байт для v${c.gen} «${c.plaintext.slice(0, 24)}»`);
  }
});

test("открытое проходит насквозь, а обрезанное — бросает", () => {
  const ring = ringFor(1);
  assert.equal(openText(ring, "как есть"), "как есть");
  assert.equal(sealText(null, 1, "без ключей"), "без ключей");
  assert.throws(() => openText(ring, 'aeg1s:{"v":1,"d":"!!","c":"zz"}'));
  const first = data.cases[0];
  assert.ok(first);
  assert.throws(() => openText(null, first.py_sealed), /ключ/);
});
