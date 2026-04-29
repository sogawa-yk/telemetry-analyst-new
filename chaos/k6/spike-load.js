// スパイク負荷: 30 秒間、200 VUs まで一気に跳ね上げる。
// 応答遅延・エラー率の急上昇を観測し、TA の bottleneck-analysis で
// 「急な負荷スパイク」シナリオを再現するのに使う。
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    spike: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "10s", target: 10 },   // ベースライン
        { duration: "15s", target: 200 },  // スパイク上昇
        { duration: "30s", target: 200 },  // スパイク持続
        { duration: "20s", target: 10 },   // 緩衝
        { duration: "5s",  target: 0 },    // 終了
      ],
      gracefulRampDown: "10s",
    },
  },
};

const TARGET = __ENV.TARGET || "https://ec-shop.devday26.sogawa-yk.com";

export default function () {
  const res = http.get(`${TARGET}/api/products`, {
    tags: { endpoint: "products" },
    timeout: "10s",
  });
  check(res, {
    "status 200": (r) => r.status === 200,
  });
  sleep(0.1);
}
