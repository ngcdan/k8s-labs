# Runbook — cụm lab `lab` (kind trên Docker Desktop)

> As-built chụp `2026-07-30` từ cụm đang chạy. Cụm là **disposable** — phá thoải mái,
> dựng lại ~10 phút bằng [scripts/up.sh](scripts/up.sh).

## 1. Cụm là gì

3 node kind (kubeadm thật chạy trong container) trên Docker Desktop:

| Thành phần | Bản đang chạy | Ghi chú |
|---|---|---|
| Kubernetes | v1.36.1 (`kindest/node`) | kind v0.32.0 |
| Runtime | containerd 2.3.1 | |
| CNI | **Cilium 1.19.6** | `kubeProxyReplacement=true` (không có kube-proxy), Hubble relay + UI bật |
| LoadBalancer | **MetalLB 0.16.1** | L2 mode |
| Storage | local-path (default StorageClass) | |
| Client | kubectl v1.33.9 · helm v4.2.3 · k9s 0.51.0 | |

## 2. Bốn dải mạng (thuộc lòng khi debug)

```
① NODE (docker network 'kind')      172.18.0.0/16
   ├─ lab-control-plane  172.18.0.3
   ├─ lab-worker         172.18.0.4
   └─ lab-worker2        172.18.0.2
② POD (Cilium cluster-pool)         10.0.0.0/8, cắt /24 mỗi node
   (control-plane 10.0.1.x · worker 10.0.2.x · worker2 10.0.0.x)
③ SERVICE (ClusterIP)               10.96.0.0/12  (kubernetes=10.96.0.1, kube-dns=10.96.0.10)
④ LOADBALANCER (MetalLB pool)       172.18.255.200 – 172.18.255.250
```

⚠️ Dải ①④ **phụ thuộc subnet docker network `kind`** — nếu network bị tạo lại (reset Docker
Desktop), subnet có thể đổi. Luôn kiểm tra: `docker network inspect kind`.

## 3. Truy cập

### kubectl / context

```bash
kubectl config get-contexts               # dấu * = đang dùng
kubectl config use-context kind-lab   # trỏ cụm lab
kubectl get nodes -o wide                 # 3 node Ready là khoẻ
```

Bẫy kinh điển: `connection refused` thường do **context trỏ cụm đã chết** (vd `orbstack`,
`docker-desktop` khi runtime đó tắt) — check `kubectl config current-context` trước khi kết luận cụm hỏng.

### k9s (TUI)

```bash
k9s          # :pods, :svc, :deploy … / l=log, s=shell, d=describe, 0=all-namespaces
```

### Hubble UI (quan sát traffic L3/L7 của Cilium)

```bash
kubectl -n kube-system port-forward svc/hubble-ui 12000:80
# → mở http://localhost:12000 — GIỮ terminal chạy, Ctrl-C là UI chết
```

Port-forward là đường hầm sống theo tiến trình: lệnh phải chạy liên tục, không phải chạy
một lần rồi thôi. Muốn chạy nền: thêm `&` (nhớ `kill` khi xong).

## 4. Vòng đời

```bash
# Cụm sống theo Docker Desktop: tắt Docker → cụm dừng; mở lại → container kind tự chạy tiếp.
kind get clusters                      # cụm đang có
kind delete cluster --name lab     # xoá sạch
./scripts/up.sh                        # dựng lại từ đầu (kind + Cilium + MetalLB + Hubble)
kind load docker-image <img> --name lab   # nạp image build local vào cụm (khỏi cần registry)
```

## 5. Smoke test (sau mỗi lần dựng lại)

```bash
kubectl create deployment smoke --image=nginx:1.27
kubectl expose deployment smoke --port=80 --type=LoadBalancer
kubectl get svc smoke                     # EXTERNAL-IP phải nhận IP trong dải MetalLB ④
kubectl get endpointslice | grep smoke    # phải trỏ Pod IP dải ②
kubectl delete svc smoke && kubectl delete deployment smoke
```

Kết quả PASS ngày 30-07: `EXTERNAL-IP 172.18.255.200`, EndpointSlice → Pod `10.0.0.220`.

## 6. Gotcha đã đụng thật

- **Node `NotReady` ngay sau `kind create`** — bình thường: CNI mặc định đã tắt, cài Cilium
  xong node mới Ready. Đừng tưởng lỗi.
- **Cilium kube-proxy-free bắt buộc set `k8sServiceHost`** = IP container control-plane
  (`docker inspect lab-control-plane`) — quên là Cilium không lên.
- **EXTERNAL-IP treo `<pending>`** — dải MetalLB không khớp subnet `kind`. Sửa IPAddressPool.
- **Reset/repair Docker Desktop = mất trắng cụm** (sự cố 30-07: crash `vmnetd … applescript
  error`, reset xoá sạch VM data, cụm gốc 18-07 bay màu). Không giữ state quan trọng trong
  lab; mọi thứ phải dựng lại được từ repo này.
- **Hubble UI "không hoạt động"** — 99% là chưa chạy (hoặc đã tắt) `port-forward`. Check:
  `lsof -nP -iTCP:12000 -sTCP:LISTEN`.
