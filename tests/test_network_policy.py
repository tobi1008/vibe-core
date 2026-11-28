import json
import subprocess

import app_scope
import builder_core
import network_policy as npol


def test_app_policy_only_allows_same_app_components():
    p = npol.app_policy("shop-be", "default")
    assert p["metadata"]["name"] == "vibe-app-shop"
    values = p["spec"]["podSelector"]["matchExpressions"][0]["values"]
    assert set(values) == app_scope.workload_names("shop") and "shopping" not in values
    assert p["spec"]["ingress"] == [{"from": [{"podSelector": p["spec"]["podSelector"]}]}]
    assert p["spec"]["policyTypes"] == ["Ingress"]  # không chặn chiều ra (Internet, DNS)


def test_baseline_denies_by_default_and_allows_traefik_and_kubelet():
    allow, deny = npol.baseline_policies("default", ["10.42.0.1/32"])
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    sources = allow["spec"]["ingress"][0]["from"]
    assert sources[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
    assert sources[0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "traefik"
    assert {"ipBlock": {"cidr": "10.42.0.1/32"}} in sources


def fake_kubectl(monkeypatch, fail_apps=()):
    applied = []

    def run(cmd, **kwargs):
        if cmd[:3] == ["kubectl", "get", "nodes"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="10.42.0.0/24 10.42.1.0/24", stderr="")
        if cmd[:2] == ["kubectl", "apply"]:
            items = json.loads(kwargs["input"])["items"]
            names = [i["metadata"]["name"] for i in items]
            applied.append(names)
            bad = any(n in (f"vibe-app-{a}" for a in fail_apps) for n in names)
            return subprocess.CompletedProcess(cmd, 1 if bad else 0, stdout="", stderr="boom" if bad else "")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(npol.subprocess, "run", run)
    npol._ensured.clear()
    return applied


def test_ensure_all_applies_app_policies_before_default_deny(monkeypatch):
    monkeypatch.setenv("NETWORK_POLICY", "1")
    applied = fake_kubectl(monkeypatch)
    res = npol.ensure_all(["demo", "demo-be", "shop"], "default")
    assert res["baseline"] and res["apps"] == ["demo", "shop"]
    assert applied[:2] == [["vibe-app-demo"], ["vibe-app-shop"]]
    assert applied[-1] == [npol.INGRESS_NAME, npol.DENY_NAME]
    assert npol.node_gateways() == ["10.42.0.1/32", "10.42.1.1/32"]


def test_no_default_deny_when_an_app_policy_fails(monkeypatch):
    monkeypatch.setenv("NETWORK_POLICY", "1")
    applied = fake_kubectl(monkeypatch, fail_apps=("shop",))
    res = npol.ensure_all(["demo", "shop"], "default")
    assert res["failed"] == ["shop"] and res["baseline"] is False
    assert all(npol.DENY_NAME not in names for names in applied)


def test_run_cmd_ensures_policy_before_apply(monkeypatch, paths, tmp_path):
    monkeypatch.setenv("NETWORK_POLICY", "1")
    order = []
    monkeypatch.setattr(npol, "ensure_app", lambda app, ns, force=False: order.append(("policy", app)) or True)
    monkeypatch.setattr(builder_core.subprocess, "run",
                        lambda cmd, **k: order.append(("run", cmd[1])) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    (tmp_path / "d.yaml").write_text("kind: Service\n")
    builder_core.run_cmd(["kubectl", "apply", "-f", "d.yaml"], "shop", cwd=str(tmp_path))
    assert order == [("policy", "shop"), ("run", "apply")]


def test_ensure_app_is_cached(monkeypatch):
    monkeypatch.setenv("NETWORK_POLICY", "1")
    applied = fake_kubectl(monkeypatch)
    npol.ensure_app("shop", "default")
    npol.ensure_app("shop-fe", "default")
    assert applied == [["vibe-app-shop"]]


def test_hermes_cannot_touch_network_policies():
    assert "NetworkPolicy" not in app_scope.ALLOWED_KINDS
