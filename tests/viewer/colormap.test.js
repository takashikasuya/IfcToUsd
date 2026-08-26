// colormap.js の純関数ユニットテスト（E10-9 / Issue #66）。
import test from "node:test";
import assert from "node:assert/strict";

import { TURBO_GRADIENT_CSS, TURBO_LUT, turboColor } from "../../ifc2usd/viewer/lib/colormap.js";

test("clamps the input domain to [0, 1]", () => {
  assert.deepEqual(turboColor(-1), turboColor(0));
  assert.deepEqual(turboColor(2), turboColor(1));
});

test("keeps every channel inside [0, 1]", () => {
  for (let i = 0; i <= 100; i++) {
    for (const c of turboColor(i / 100)) {
      assert.ok(c >= 0 && c <= 1, `channel out of range at ${i}: ${c}`);
    }
  }
});

test("runs blue -> red across the domain (turbo の向き)", () => {
  // x=0 は暗い紺で多項式近似だと3チャンネルがほぼ等しいため、青が支配的になる
  // 低域側(0.25付近)と赤が支配的になる高域側(0.9付近)で向きを確認する。
  const [lowR, , lowB] = turboColor(0.25);
  const [highR, , highB] = turboColor(0.9);
  assert.ok(lowB > lowR, "低域は青が優勢であるべき");
  assert.ok(highR > highB, "高域は赤が優勢であるべき");
});

test("is dark at the bottom of the domain", () => {
  for (const c of turboColor(0)) {
    assert.ok(c < 0.2, `x=0 は暗い色であるべき: ${c}`);
  }
});

test("builds a 256 entry lookup table", () => {
  assert.equal(TURBO_LUT.length, 256);
  assert.deepEqual(TURBO_LUT[0], turboColor(0));
  assert.deepEqual(TURBO_LUT[255], turboColor(1));
});

test("exposes a CSS gradient for the legend (no canvas)", () => {
  assert.match(TURBO_GRADIENT_CSS, /^linear-gradient\(to right, rgb\(/);
  assert.match(TURBO_GRADIENT_CSS, /100%\)$/);
});
