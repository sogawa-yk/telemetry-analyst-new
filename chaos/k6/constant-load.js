// 定常負荷: /api/products を一定 VU で叩き続け、ec-web の CPU を緩やかに上げる。
// 用途: TA の bottleneck-analysis を「通常より高いベースライン」状態で試したいとき。
//
// 実行例（ホストに k6 がある場合）:
//   k6 run -e TARGET=https://ec-shop.devday26.sogawa-yk.com chaos/k6/constant-load.js
// クラスタ内で走らせる場合:
//   TARGET=http://ec-web.ec-shop.svc.cluster.local chaos/scripts/k6-in-cluster.sh constant-load
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    steady: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 10),
      duration: __ENV.DURATION || "2m",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<800"],
  },
};

const TARGET = __ENV.TARGET || "https://ec-shop.devday26.sogawa-yk.com";

export default function () {
  const res = http.get(`${TARGET}/api/products`, {
    tags: { endpoint: "products" },
  });
  check(res, {
    "status 200": (r) => r.status === 200,
    "has body": (r) => !!r.body && r.body.length > 0,
  });
  sleep(Math.random() * 0.5 + 0.2);
}
