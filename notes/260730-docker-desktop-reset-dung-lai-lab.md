# 260730 — Docker Desktop crash, mất cụm, dựng lại + bật Hubble

**Chuyện gì xảy ra:** Docker Desktop crash lúc boot với lỗi
`initializing backend: … repairing vmnetd configuration: configuring privileged port mapping: applescript error` —
privileged helper `com.docker.vmnetd` cần quyền admin để sửa cấu hình nhưng hộp thoại xin quyền fail.
Sau khi bật lại (kèm reset), **VM data bị xoá trắng**: mọi container/image/volume mất → cụm kind
`lab` gốc (dựng 18-07) bay màu.

**Xử lý:** dựng lại toàn bộ theo runbook trong ~10 phút — kind 3-node → Cilium 1.19.6
kube-proxy-free → MetalLB → smoke test PASS. Khác bản cũ 2 điểm:

1. **Bật Hubble** (relay + UI) ngay từ đầu — bản 18-07 chưa có.
2. Docker network `kind` bị tạo lại → **subnet đổi `172.25.0.0/16` → `172.18.0.0/16`**,
   dải MetalLB đổi theo (`172.18.255.200-250`).

**Bài học:**

- Cụm kind sống trong VM của Docker Desktop — reset Docker Desktop = mất cụm. Lab phải
  **disposable**: mọi thứ tái tạo được từ config + script, không giữ state quý trong cụm.
- Subnet docker network không cố định qua các lần tạo lại → script dựng cụm phải **tự dò subnet**
  khi đặt dải MetalLB (đã đưa vào `scripts/up.sh`), đừng hardcode.
- `kubectl port-forward` là tiến trình phải giữ chạy — "mở UI không được" thì check
  `lsof -iTCP:<port> -sTCP:LISTEN` trước đã.
