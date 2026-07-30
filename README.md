# k8s-labs

Runbook + nhật ký thực hành Kubernetes trên máy local (Mac M4, 10 core, 24 GB RAM).
Cụm lab chính: **kind `lab`** — 3 node, stack Cilium kube-proxy-free + MetalLB + Hubble.

## Kiến trúc

![Kiến trúc cụm lab](assets/lab-architecture.png)

## Cấu trúc

| Đường dẫn | Là gì |
|---|---|
| [runbook.md](runbook.md) | As-built cụm lab, cách truy cập (kubectl/k9s/Hubble UI), vòng đời dựng/xoá/dựng lại, gotcha |
| [cluster/kind-lab.yaml](cluster/kind-lab.yaml) | Config kind 3-node (CNI mặc định tắt, kube-proxy tắt — nhường Cilium) |
| [scripts/up.sh](scripts/up.sh) | Dựng lại cụm từ đầu một phát (kind + Cilium + Hubble + MetalLB, tự dò subnet) |
| `notes/` | Note theo ngày trong quá trình học, tên `YYMMDD-<chủ-đề>.md` |
| `assets/` | Sơ đồ kiến trúc (`.excalidraw` nguồn + PNG/SVG đã render) |

## Truy cập nhanh

```bash
kubectl config use-context kind-lab   # trỏ vào cụm lab
k9s                                        # TUI xem pod/log/exec
kubectl -n kube-system port-forward svc/hubble-ui 12000:80   # → http://localhost:12000
```