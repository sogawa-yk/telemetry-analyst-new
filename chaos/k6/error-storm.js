// エラー誘発シナリオ: 不正な入力と存在しないパスを混ぜて
// アプリの error ログ（Loki 側）を大量に生成する。
//
// - /api/cart/add に invalid な product_id / 負の quantity を送る
// - 存在しないパス /api/does-not-exist/<n> に GET
// - 期待: 4xx/5xx が Prometheus・Loki に出現、TA の analysis で
//   「エラー率上昇」候補が top に来る
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    errors: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 30),
      duration: __ENV.DURATION || "3m",
    },
  },
};

const TARGET = __ENV.TARGET || "https://ec-shop.devday26.sogawa-yk.com";

export default function () {
  const session = `k6-err-${__VU}-${__ITER}`;
  const pattern = __ITER % 3;

  if (pattern === 0) {
    // 存在しないエンドポイント（404 の嵐）
    const path = `/api/does-not-exist/${__ITER}`;
    const res = http.get(`${TARGET}${path}`, { tags: { endpoint: "404" } });
    check(res, { "is 4xx": (r) => r.status >= 400 && r.status < 500 });
  } else if (pattern === 1) {
    // 不正な JSON
    const res = http.post(`${TARGET}/api/cart/add`, "not a json", {
      headers: {
        "Content-Type": "application/json",
        "X-Session-Id": session,
      },
      tags: { endpoint: "cart_invalid_json" },
    });
    check(res, { "bad request handled": (r) => r.status >= 400 });
  } else {
    // 未知の product_id / 異常な quantity
    const res = http.post(
      `${TARGET}/api/cart/add`,
      JSON.stringify({ product_id: 999999, quantity: -5 }),
      {
        headers: {
          "Content-Type": "application/json",
          "X-Session-Id": session,
        },
        tags: { endpoint: "cart_invalid_args" },
      },
    );
    check(res, { "rejected or tolerated": (r) => r.status > 0 });
  }

  sleep(Math.random() * 0.2 + 0.1);
}
