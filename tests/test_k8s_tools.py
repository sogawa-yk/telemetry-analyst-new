"""K8s ツール群の回帰テスト.

ec-shop スコープ二重防御 (`_check_scope`) と Pod 名インジェクション対策
(`_validate_name`) を中心に検証する. 実 K8s API は呼ばず、
`kubernetes.client.*` を unittest.mock で差し替える.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ta.agent.tools import k8s as k

# ---------------------------------------------------------------------------
# _check_scope / _validate_name (純関数)
# ---------------------------------------------------------------------------


def test_check_scope_allows_target_namespace() -> None:
    assert k._check_scope("ec-shop") is None


def test_check_scope_rejects_kube_system() -> None:
    err = k._check_scope("kube-system")
    assert err is not None
    assert "ec-shop" in err
    assert "権限外" in err


def test_check_scope_rejects_observability() -> None:
    err = k._check_scope("observability")
    assert err is not None
    assert "kube-system" not in err  # 漏らしてはいけない


@pytest.mark.parametrize(
    "name",
    [
        "ec-web-557766b744-5xvl4",
        "postgres-0",
        "load-generator",
        "a",
        "a.b.c",
    ],
)
def test_validate_name_accepts_valid(name: str) -> None:
    assert k._validate_name(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "Foo",  # 大文字
        "foo;rm -rf /",  # コマンドインジェクション
        "foo bar",  # スペース
        "foo/bar",  # スラッシュ
        "../etc/passwd",  # パストラバース
        "-leadingdash",  # 先頭ハイフン
        "trailingdash-",  # 末尾ハイフン
        "",  # 空
    ],
)
def test_validate_name_rejects_invalid(name: str) -> None:
    err = k._validate_name(name)
    assert err is not None
    assert "不正な" in err


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_truncate_passthrough_when_short() -> None:
    s = "short"
    assert k._truncate(s, max_chars=100) == s


def test_truncate_appends_suffix_when_long() -> None:
    s = "a" * 200
    out = k._truncate(s, max_chars=50, suffix="...")
    assert len(out) == 53
    assert out.endswith("...")


def test_age_minutes_hours_days() -> None:
    now = datetime.now(UTC)
    assert k._age(now - timedelta(minutes=5)).endswith("m")
    assert k._age(now - timedelta(hours=3)).endswith("h")
    assert k._age(now - timedelta(days=2)).endswith("d")
    assert k._age(None) == "?"


# ---------------------------------------------------------------------------
# k8s_list_pods (Mock 経由)
# ---------------------------------------------------------------------------


def _make_pod(name: str, phase: str = "Running", restarts: int = 0, age_hours: int = 1):
    cs = SimpleNamespace(restart_count=restarts)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, creation_timestamp=datetime.now(UTC) - timedelta(hours=age_hours)
        ),
        status=SimpleNamespace(phase=phase, container_statuses=[cs]),
        spec=SimpleNamespace(node_name="node-a"),
    )


def test_list_pods_formats_table() -> None:
    pods = [_make_pod("ec-web-1"), _make_pod("postgres-0", restarts=2)]
    mock_core = MagicMock()
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    with patch.object(k, "_core", return_value=mock_core):
        out = k.k8s_list_pods()
    assert "ec-shop" in out
    assert "ec-web-1" in out
    assert "postgres-0" in out
    assert "Running" in out
    mock_core.list_namespaced_pod.assert_called_once_with("ec-shop")


def test_list_pods_handles_empty() -> None:
    mock_core = MagicMock()
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    with patch.object(k, "_core", return_value=mock_core):
        out = k.k8s_list_pods()
    assert "Pod はありません" in out


def test_list_pods_returns_api_error() -> None:
    from kubernetes.client.exceptions import ApiException

    mock_core = MagicMock()
    err = ApiException(status=403, reason="Forbidden")
    mock_core.list_namespaced_pod.side_effect = err
    with patch.object(k, "_core", return_value=mock_core):
        out = k.k8s_list_pods()
    assert "K8s API エラー: 403" in out


# ---------------------------------------------------------------------------
# k8s_describe_pod / k8s_pod_logs の name バリデーション
# ---------------------------------------------------------------------------


def test_describe_pod_rejects_invalid_name() -> None:
    out = k.k8s_describe_pod("foo;rm -rf /")
    assert "不正な" in out


def test_pod_logs_rejects_invalid_name() -> None:
    out = k.k8s_pod_logs("foo bar")
    assert "不正な" in out


def test_pod_logs_rejects_invalid_container() -> None:
    out = k.k8s_pod_logs("ec-web-1", container="../etc/passwd")
    assert "不正な" in out


def test_describe_pod_success_with_mock() -> None:
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name="ec-web-1", creation_timestamp=datetime.now(UTC) - timedelta(minutes=10)
        ),
        spec=SimpleNamespace(
            node_name="node-a",
            containers=[
                SimpleNamespace(
                    name="ec-web", image="repo:tag", resources=SimpleNamespace(limits=None)
                )
            ],
        ),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name="ec-web",
                    restart_count=0,
                    state=SimpleNamespace(running=True, waiting=None, terminated=None),
                )
            ],
        ),
    )
    mock_core = MagicMock()
    mock_core.read_namespaced_pod.return_value = pod
    mock_core.list_namespaced_event.return_value = SimpleNamespace(items=[])
    with patch.object(k, "_core", return_value=mock_core):
        out = k.k8s_describe_pod("ec-web-1")
    assert "ec-web-1" in out
    assert "node-a" in out
    assert "Running" in out


# ---------------------------------------------------------------------------
# Function tool ラッパが ALL_TOOLS に列挙されている
# ---------------------------------------------------------------------------


def test_all_tools_exposes_seven_function_tools() -> None:
    from agents import FunctionTool

    assert len(k.ALL_TOOLS) == 7
    names = {t.name for t in k.ALL_TOOLS if isinstance(t, FunctionTool)}
    assert names == {
        "k8s_list_pods",
        "k8s_describe_pod",
        "k8s_pod_logs",
        "k8s_list_events",
        "k8s_list_deployments",
        "k8s_list_services",
        "k8s_list_hpa",
    }
