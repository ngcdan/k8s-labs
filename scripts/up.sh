#!/usr/bin/env bash
# up.sh — Dựng lại cụm kind lab từ đầu: 3-node + Cilium (kube-proxy-free, Hubble) + MetalLB
set -euo pipefail

CLUSTER=lab
CONFIG="$(cd "$(dirname "$0")/.." && pwd)/cluster/kind-lab.yaml"

echo "==> Xóa cụm cũ (bỏ qua nếu chưa có)"
kind delete cluster --name "$CLUSTER" 2>/dev/null || true

echo "==> Tạo cụm 3-node (CNI tắt)"
kind create cluster --config "$CONFIG"

echo "==> Lấy IP control-plane (bắt buộc cho kube-proxy-free)"
CP_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${CLUSTER}-control-plane")
echo "    CP_IP=$CP_IP"

echo "==> Cài Cilium 1.19.6 (kube-proxy-free + Hubble relay/UI)"
helm repo add cilium https://helm.cilium.io/ --force-update
helm install cilium cilium/cilium --version 1.19.6 -n kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="$CP_IP" --set k8sServicePort=6443 \
  --set operator.replicas=1 \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true
kubectl -n kube-system rollout status ds/cilium

echo "==> Cài MetalLB 0.16.1"
helm repo add metallb https://metallb.github.io/metallb --force-update
helm install metallb metallb/metallb --version 0.16.1 -n metallb-system --create-namespace
kubectl -n metallb-system rollout status deploy/metallb-controller

echo "==> Cấp dải LoadBalancer theo subnet THẬT của docker network 'kind'"
# subnet kind mặc định là x.y.0.0/16 → lấy 2 octet đầu, cấp dải x.y.255.200-250
PREFIX=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' \
  | grep -E '^[0-9]+\.' | head -1 | cut -d. -f1-2)
echo "    pool ${PREFIX}.255.200-${PREFIX}.255.250"
kubectl apply -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: lab-pool, namespace: metallb-system }
spec:
  addresses: ["${PREFIX}.255.200-${PREFIX}.255.250"]
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: lab-l2, namespace: metallb-system }
spec:
  ipAddressPools: [lab-pool]
EOF

echo "==> Cụm sẵn sàng:"
kubectl config current-context
kubectl get nodes -o wide
