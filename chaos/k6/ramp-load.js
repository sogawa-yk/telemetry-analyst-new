// 段階的負荷増加: 1 → 100 VUs まで 5 分でランプアップし、最後 1 分持続。
// HPA のスケールアウト挙動 + CPU/メモリの段階的な変化を観測したいとき。
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "1m", target: 20 },
        { duration: "2m", target: 60 },
        { duration: "2m", target: 100 },
        { duration: "1m", target: 100 },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.10"],
  },
};

const TARGET = __ENV.TARGET || "https://ec-shop.devday26.sogawa-yk.com";
const PRODUCT_IDS = [1, 2, 3, 4, 5, 6, 7, 8];

export default function () {
  const session = `k6-${__VU}-${__ITER}`;
  // 参照系
  http.get(`${TARGET}/api/products`, { tags: { endpoint: "products" } });
  // 書き込み系（カート追加）
  const pid = PRODUCT_IDS[Math.floor(Math.random() * PRODUCT_IDS.length)];
  const res = http.post(
    `${TARGET}/api/cart/add`,
    JSON.stringify({ product_id: pid, quantity: 1 }),
    {
      headers: { "Content-Type": "application/json", "X-Session-Id": session },
      tags: { endpoint: "cart_add" },
    },
  );
  check(res, { "cart add ok": (r) => r.status === 200 });
  sleep(Math.random() * 0.3 + 0.2);
}
