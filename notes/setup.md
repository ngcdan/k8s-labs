# Setup môi trường lab K8s trên macOS (từ số 0)

> Kết quả cuối: cụm kind `lab` 3-node chạy trong Docker Desktop, CNI Cilium
> kube-proxy-free + Hubble, LoadBalancer thật bằng MetalLB. Toàn bộ ~15 phút,
> phần lớn là thời gian tải image.

## 1. Cài công cụ (một lần)

```bash
# Homebrew (nếu chưa có): https://brew.sh
brew install kubectl kind helm k9s
```

Docker Desktop: tải từ https://www.docker.com/products/docker-desktop/ và mở app
(cần daemon đang chạy — kiểm tra: `docker info`).

Version đã kiểm chứng chạy tốt (mới hơn thường vẫn ổn):

| Tool | Version |
|---|---|
| kubectl | v1.33.9 |
| kind | v0.32.0 |
| helm | v4.2.3 |
| k9s | 0.51.0 |
| Docker Desktop engine | 29.x |

## 2. Clone repo & dựng cụm

```bash
git clone git@github.com:ngcdan/k8s-labs.git
cd k8s-labs
./scripts/up.sh
```

Script làm tuần tự (xem chi tiết trong `scripts/up.sh`):

1. `kind delete` cụm cũ tên `lab` (nếu có) → `kind create` 3 node từ
   `cluster/kind-lab.yaml` (CNI mặc định + kube-proxy đều TẮT — node sẽ
   **NotReady**, đây là chủ đích, đừng tưởng lỗi).
2. Cài **Cilium 1.19.6** qua helm: `kubeProxyReplacement=true` +
   `k8sServiceHost=<IP container control-plane>` (bắt buộc vì không có kube-proxy),
   bật Hubble relay + UI.
3. Cài **MetalLB 0.16.1**, tự dò subnet của docker network `kind` rồi cấp pool
   `x.y.255.200-250` (IPAddressPool + L2Advertisement).

Chờ tới dòng `==> Cụm sẵn sàng` và 3 node **Ready** là xong.

## 3. Verify — smoke test LoadBalancer

```bash
kubectl create deployment smoke --image=nginx:1.27
kubectl expose deployment smoke --port=80 --type=LoadBalancer
kubectl get svc smoke          # EXTERNAL-IP phải là IP trong pool (vd 172.18.255.200)
curl http://<EXTERNAL-IP>/     # trả về trang nginx là chuỗi LB→Service→Pod thông
kubectl delete svc smoke && kubectl delete deployment smoke
```

Nếu EXTERNAL-IP treo `<pending>` → pool MetalLB không khớp subnet, xem Gotcha
trong [../runbook.md](../runbook.md).

## 4. Truy cập hằng ngày

```bash
kubectl config use-context kind-lab   # trỏ đúng cụm lab
kubectl get nodes -o wide             # 3 node Ready là khoẻ
k9s                                   # TUI xem pod/log/exec

# Hubble UI (quan sát traffic L3/L7) — giữ terminal chạy:
kubectl -n kube-system port-forward svc/hubble-ui 12000:80
# → mở http://localhost:12000
```

## 5. Vòng đời

- Cụm sống theo Docker Desktop: tắt Docker → cụm dừng, mở lại → tự chạy tiếp.
- Phá đi làm lại bất cứ lúc nào: `kind delete cluster --name lab && ./scripts/up.sh`.
- Nạp image build local vào cụm (khỏi cần registry):
  `kind load docker-image <image> --name lab`.

⚠️ Cụm là **disposable** — đừng giữ state quan trọng trong đó. Reset Docker Desktop
sẽ xoá trắng mọi container/volume (đã dính một lần, xem
[260730-docker-desktop-reset-dung-lai-lab.md](260730-docker-desktop-reset-dung-lai-lab.md)).
