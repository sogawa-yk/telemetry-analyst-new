"""ec-shop を対象とする読み取り専用 K8s ツール群.

OpenAI Responses API の function tools 形式で提供する. 戻り値は LLM が読みやすい
短文テキストに整形する. 書込み系 API は実装しない (ホワイトリスト方式の二重防御).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from ta.config import get_settings

# -----------------------------------------------------------------------------
# クライアント初期化
# -----------------------------------------------------------------------------


def _load_k8s() -> None:
    """In-cluster → kubeconfig の順でフォールバック."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_loaded = False


def _core() -> client.CoreV1Api:
    global _loaded
    if not _loaded:
        _load_k8s()
        _loaded = True
    return client.CoreV1Api()


def _apps() -> client.AppsV1Api:
    global _loaded
    if not _loaded:
        _load_k8s()
        _loaded = True
    return client.AppsV1Api()


def _autoscaling() -> client.AutoscalingV2Api:
    global _loaded
    if not _loaded:
        _load_k8s()
        _loaded = True
    return client.AutoscalingV2Api()


# -----------------------------------------------------------------------------
# スコープ境界 (監視対象 NS のみ許可)
# -----------------------------------------------------------------------------


def _check_scope(namespace: str) -> str | None:
    """許可された NS 以外は拒否. 返り値がエラーメッセージ (None なら許可)."""
    allowed = get_settings().target_namespace
    if namespace != allowed:
        return (
            f"エラー: この エージェントの調査対象は `{allowed}` NS に限定されています。"
            f"`{namespace}` は権限外のため確認できません。"
        )
    return None


# -----------------------------------------------------------------------------
# ツール実装
# -----------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


def k8s_list_pods(namespace: str | None = None) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        pods = _core().list_namespaced_pod(ns).items
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"
    if not pods:
        return f"NS `{ns}` に Pod はありません。"
    lines = [f"Namespace `{ns}` の Pod 一覧 ({len(pods)} 件):", ""]
    lines.append(f"{'NAME':<48} {'STATUS':<12} {'RESTARTS':<9} {'AGE':<10} {'NODE':<30}")
    for p in pods:
        restarts = sum((c.restart_count or 0) for c in (p.status.container_statuses or []))
        age = _age(p.metadata.creation_timestamp)
        node = (p.spec.node_name or "")[:30]
        lines.append(
            f"{p.metadata.name:<48} {p.status.phase:<12} {restarts:<9} {age:<10} {node:<30}"
        )
    return "\n".join(lines)


def k8s_describe_pod(name: str, namespace: str | None = None) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        pod = _core().read_namespaced_pod(name=name, namespace=ns)
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"

    lines = [
        f"Pod: {pod.metadata.name} (NS: {ns})",
        f"Node: {pod.spec.node_name or '(未スケジュール)'}",
        f"Status: {pod.status.phase}",
        f"Created: {pod.metadata.creation_timestamp} ({_age(pod.metadata.creation_timestamp)})",
        "",
        "## Containers",
    ]
    for c in pod.spec.containers or []:
        limits = (c.resources.limits or {}) if c.resources else {}
        lines.append(f"- {c.name} (image={c.image}, limits={dict(limits)})")

    lines.append("")
    lines.append("## Container Statuses")
    for cs in pod.status.container_statuses or []:
        state = "running" if cs.state.running else ("waiting" if cs.state.waiting else "terminated")
        reason = ""
        if cs.state.waiting and cs.state.waiting.reason:
            reason = f"[waiting: {cs.state.waiting.reason}]"
        if cs.state.terminated and cs.state.terminated.reason:
            reason = (
                f"[terminated: {cs.state.terminated.reason}, exit={cs.state.terminated.exit_code}]"
            )
        lines.append(f"- {cs.name}: state={state} restarts={cs.restart_count} {reason}")

    lines.append("")
    lines.append("## Recent Events (pod)")
    try:
        events = (
            _core()
            .list_namespaced_event(
                namespace=ns,
                field_selector=f"involvedObject.name={name},involvedObject.namespace={ns}",
            )
            .items
        )
    except ApiException:
        events = []
    for ev in events[-10:]:
        lines.append(f"- [{ev.type}] {ev.reason}: {ev.message} (count={ev.count})")
    if not events:
        lines.append("  (なし)")
    return _truncate("\n".join(lines), max_chars=6000)


def k8s_pod_logs(
    name: str,
    container: str | None = None,
    tail: int = 200,
    since_seconds: int = 900,
    previous: bool = False,
    namespace: str | None = None,
) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        logs = _core().read_namespaced_pod_log(
            name=name,
            namespace=ns,
            container=container,
            tail_lines=tail,
            since_seconds=since_seconds,
            previous=previous,
        )
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"
    if not logs:
        return f"Pod `{name}` (container={container}) のログはありません (tail={tail})."
    return _truncate(logs, max_chars=8000, suffix="\n... (truncated)")


def k8s_list_events(
    namespace: str | None = None,
    since_seconds: int = 900,
    kind: str | None = None,
) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        events = _core().list_namespaced_event(namespace=ns).items
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"

    # 時間で絞る
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=since_seconds)
    events = [
        e
        for e in events
        if (e.last_timestamp or e.event_time or e.metadata.creation_timestamp) > cutoff
    ]
    if kind:
        events = [e for e in events if e.involved_object and e.involved_object.kind == kind]
    events.sort(
        key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
        reverse=True,
    )

    if not events:
        return f"NS `{ns}` の直近 {since_seconds}s にイベントはありません."
    lines = [f"Namespace `{ns}` 直近 {since_seconds}s のイベント ({len(events)} 件):", ""]
    for ev in events[:50]:
        ts = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
        obj = f"{ev.involved_object.kind}/{ev.involved_object.name}" if ev.involved_object else "?"
        lines.append(f"- {ts} [{ev.type}] {ev.reason} {obj}: {ev.message} (count={ev.count})")
    return _truncate("\n".join(lines), max_chars=6000)


def k8s_list_deployments(namespace: str | None = None) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        deps = _apps().list_namespaced_deployment(ns).items
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"
    if not deps:
        return f"NS `{ns}` に Deployment はありません."
    lines = [f"Namespace `{ns}` の Deployment ({len(deps)} 件):", ""]
    lines.append(f"{'NAME':<32} {'READY':<10} {'UPDATED':<10} {'AVAILABLE':<10} {'AGE':<10}")
    for d in deps:
        ready = f"{d.status.ready_replicas or 0}/{d.spec.replicas or 0}"
        lines.append(
            f"{d.metadata.name:<32} {ready:<10} "
            f"{d.status.updated_replicas or 0!s:<10} "
            f"{d.status.available_replicas or 0!s:<10} "
            f"{_age(d.metadata.creation_timestamp):<10}"
        )
    lines.append("")
    lines.append("## 各 Deployment のコンテナイメージ")
    for d in deps:
        for c in d.spec.template.spec.containers:
            lines.append(f"- {d.metadata.name} / {c.name}: {c.image}")
    return "\n".join(lines)


def k8s_list_services(namespace: str | None = None) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        svcs = _core().list_namespaced_service(ns).items
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"
    if not svcs:
        return f"NS `{ns}` に Service はありません."
    lines = [f"Namespace `{ns}` の Service ({len(svcs)} 件):", ""]
    for s in svcs:
        ports = ", ".join(f"{p.port}/{p.protocol}" for p in (s.spec.ports or []))
        lines.append(
            f"- {s.metadata.name} type={s.spec.type} clusterIP={s.spec.cluster_ip} ports=[{ports}]"
        )
    return "\n".join(lines)


def k8s_list_hpa(namespace: str | None = None) -> str:
    ns = namespace or get_settings().target_namespace
    if err := _check_scope(ns):
        return err
    try:
        hpas = _autoscaling().list_namespaced_horizontal_pod_autoscaler(ns).items
    except ApiException as e:
        return f"K8s API エラー: {e.status} {e.reason}"
    if not hpas:
        return f"NS `{ns}` に HPA はありません."
    lines = [f"Namespace `{ns}` の HPA ({len(hpas)} 件):", ""]
    for h in hpas:
        target = f"{h.spec.scale_target_ref.kind}/{h.spec.scale_target_ref.name}"
        lines.append(
            f"- {h.metadata.name} target={target} "
            f"minReplicas={h.spec.min_replicas} maxReplicas={h.spec.max_replicas} "
            f"currentReplicas={h.status.current_replicas}"
        )
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _age(ts) -> str:  # type: ignore[no-untyped-def]
    if ts is None:
        return "?"
    from datetime import UTC, datetime

    delta = datetime.now(UTC) - ts
    if delta.days >= 1:
        return f"{delta.days}d"
    h = delta.seconds // 3600
    if h >= 1:
        return f"{h}h"
    m = delta.seconds // 60
    return f"{m}m"


def _truncate(s: str, max_chars: int, suffix: str = "\n... (truncated)") -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + suffix


# 入力バリデーション (パス/インジェクション対策)
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


def _validate_name(name: str) -> str | None:
    if not _NAME_RE.match(name):
        return f"不正な Kubernetes リソース名: {name!r}"
    return None


# -----------------------------------------------------------------------------
# OpenAI Responses API 用の function tool spec + dispatcher
# -----------------------------------------------------------------------------


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "k8s_list_pods",
        "description": (
            "監視対象 namespace の Pod 一覧を取得する. 状態 / restart 数 / age が分かる. "
            "障害調査で最初の一手として使う. 他 namespace は権限外のため失敗する."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "k8s_describe_pod",
        "description": (
            "指定した Pod の詳細 (コンテナ状態、終了コード、直近イベント) を取得する. "
            "CrashLoopBackOff や ImagePullBackOff などの原因特定に使う."
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pod 名"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "k8s_pod_logs",
        "description": (
            "Pod のログを取得する (最大 tail 行 / since_seconds 秒前まで). "
            "previous=true で直前のコンテナ終了時のログも取得可. 出力は 8KB で切詰め."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pod 名"},
                "container": {
                    "type": "string",
                    "description": "コンテナ名 (省略可、マルチコンテナ時に指定)",
                },
                "tail": {"type": "integer", "description": "末尾何行か (既定 200)", "default": 200},
                "since_seconds": {
                    "type": "integer",
                    "description": "何秒前からのログか (既定 900 = 15 分)",
                    "default": 900,
                },
                "previous": {
                    "type": "boolean",
                    "description": "前回終了時のコンテナログを取得するか (既定 false)",
                    "default": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "k8s_list_events",
        "description": (
            "監視対象 namespace の直近 since_seconds 秒のイベントを取得する. "
            "OOMKilled / FailedScheduling / BackOff などの理由特定に必須."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since_seconds": {
                    "type": "integer",
                    "description": "過去何秒分か (既定 900)",
                    "default": 900,
                },
                "kind": {
                    "type": "string",
                    "description": "絞り込む Kind (Pod / Deployment 等). 未指定なら全て",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "k8s_list_deployments",
        "description": (
            "監視対象 namespace の Deployment 一覧を取得する. "
            "コンテナイメージのタグ確認 (直近デプロイ時刻の推定) に使う."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "k8s_list_services",
        "description": "監視対象 namespace の Service 一覧を取得する.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "k8s_list_hpa",
        "description": (
            "監視対象 namespace の HorizontalPodAutoscaler 一覧を取得する. "
            "負荷急増時のスケール追随状況を確認するのに使う."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


_DISPATCH = {
    "k8s_list_pods": k8s_list_pods,
    "k8s_describe_pod": k8s_describe_pod,
    "k8s_pod_logs": k8s_pod_logs,
    "k8s_list_events": k8s_list_events,
    "k8s_list_deployments": k8s_list_deployments,
    "k8s_list_services": k8s_list_services,
    "k8s_list_hpa": k8s_list_hpa,
}


def dispatch(name: str, arguments: dict[str, Any]) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"不明なツール: {name}"

    # 入力バリデーション
    if "name" in arguments:
        err = _validate_name(str(arguments["name"]))
        if err:
            return err
    if arguments.get("container"):
        err = _validate_name(str(arguments["container"]))
        if err:
            return err

    try:
        return fn(**arguments)  # type: ignore[arg-type]
    except TypeError as e:
        return f"ツール呼出し引数エラー: {e}"
    except Exception as e:
        return f"ツール実行エラー: {type(e).__name__}: {e}"
