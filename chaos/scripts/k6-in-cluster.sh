#!/usr/bin/env bash
# ホストに k6 が無い環境向けに、クラスタ内で k6 を Pod として実行するラッパ。
# chaos/k6/*.js を ConfigMap にマウントして grafana/k6 image で走らせる。
#
# 使い方:
#   chaos/scripts/k6-in-cluster.sh constant-load
#   chaos/scripts/k6-in-cluster.sh spike-load
#   chaos/scripts/k6-in-cluster.sh ramp-load
#   chaos/scripts/k6-in-cluster.sh error-storm
#   TARGET=http://ec-web.ec-shop.svc.cluster.local chaos/scripts/k6-in-cluster.sh constant-load
#   chaos/scripts/k6-in-cluster.sh --cleanup
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-ec-shop}"
TARGET="${TARGET:-http://ec-web.ec-shop.svc.cluster.local}"
SCENARIO="${1:-constant-load}"

if [[ "$SCENARIO" == "--cleanup" ]]; then
  kubectl delete job -n "$NAMESPACE" -l chaos=k6 --wait=false 2>&1 || true
  kubectl delete cm -n "$NAMESPACE" -l chaos=k6 --wait=false 2>&1 || true
  exit 0
fi

SCRIPT="${REPO_ROOT}/k6/${SCENARIO}.js"
if [[ ! -f "$SCRIPT" ]]; then
  echo "[k6] unknown scenario: $SCENARIO" >&2
  echo "    available: constant-load / spike-load / ramp-load / error-storm" >&2
  exit 1
fi

JOB_NAME="chaos-k6-${SCENARIO}-$(date +%s)"
CM_NAME="${JOB_NAME}-script"

kubectl create configmap -n "$NAMESPACE" "$CM_NAME" \
  --from-file=script.js="$SCRIPT" 2>&1
kubectl label configmap -n "$NAMESPACE" "$CM_NAME" chaos=k6 --overwrite 2>&1

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    chaos: k6
    scenario: ${SCENARIO}
spec:
  ttlSecondsAfterFinished: 900
  backoffLimit: 0
  template:
    metadata:
      labels:
        chaos: k6
        scenario: ${SCENARIO}
    spec:
      restartPolicy: Never
      containers:
        - name: k6
          image: grafana/k6:0.52.0
          args: ["run", "--quiet", "/scripts/script.js"]
          env:
            - name: TARGET
              value: "${TARGET}"
          volumeMounts:
            - name: script
              mountPath: /scripts
      volumes:
        - name: script
          configMap:
            name: ${CM_NAME}
EOF

echo "[k6] Job ${JOB_NAME} launched"
echo "     kubectl logs -n ${NAMESPACE} -l scenario=${SCENARIO} -f で追跡"
