# Kubernetes — Chiến lược triển khai (Rolling / Rollback / Canary / Blue-Green)

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành + giải thích đầy đủ ở [k8s-deploy-strategies.md](k8s-deploy-strategies.md).

## Rolling Update

<details>
<summary>1. Rolling Update là gì? Đảm bảo zero-downtime bằng cách nào?</summary>

Chiến lược **mặc định** của Deployment: thay **từng Pod một** — tạo Pod mới → đợi Ready (readiness) → xoá Pod
cũ → lặp. Luôn còn Pod phục vụ trong suốt quá trình → không downtime. Chạy qua **2 ReplicaSet**: RS mới tăng
dần, RS cũ giảm dần (bài học module 04).
</details>

<details>
<summary>2. <code>maxSurge:1</code> + <code>maxUnavailable:1</code> với <code>replicas:4</code> nghĩa là gì?</summary>

`maxSurge:1` = được dư tối đa 1 Pod → **tối đa 5 Pod** cùng lúc. `maxUnavailable:1` = được thiếu tối đa 1 Pod
→ **tối thiểu 3 Pod phục vụ**. K8s dịch trong khoảng [3, 5] Pod. `maxUnavailable:0, maxSurge:1` = an toàn nhất
(luôn đủ 4) nhưng chậm nhất (thêm từng Pod một).
</details>

<details>
<summary>3. (thực chạy) Đọc log <code>rollout status</code>: "updated" vs "available" vs "pending termination"?</summary>

**updated** = Pod mới đã được *tạo*; **available** = Pod đã *Ready + ổn định* (qua readiness/minReadySeconds),
tính vào số phục vụ; **pending termination** = Pod cũ đang *chờ xoá* (Terminating). `rollout status` **poll mỗi
~1s** nên nhiều dòng giống nhau lặp lại = "đang chờ", không phải lỗi. Lab: `2/3/4 out of 4 new updated → 1 old
pending termination → 3 of 4 available → successfully rolled out`.
</details>

## Rollback

<details>
<summary>4. Rollback hoạt động thế nào? Có build lại image không? (thực chạy)</summary>

`rollout undo` đổi `spec.template` về revision trước → K8s **scale RS cũ lên lại** (không build gì, không tạo
RS mới). Lab thật: sau undo `get rs` vẫn **2 RS**, RS `688bcdbbf4` **AGE 7m15s** (= RS gốc) bật lại `4/4/4` →
bằng chứng **RS tái dùng**. `Image` quay về `1.16.1-alpine`. Nhanh vì chỉ scale RS có sẵn.
</details>

<details>
<summary>5. Sau <code>rollout undo</code>, số revision thay đổi thế nào? <code>revisionHistoryLimit</code>?</summary>

Undo là **hành động mới** → cấp revision kế tiếp (revision `1,2` → sau undo thành `2,3`), KHÔNG quay về số cũ —
revision là số đếm chỉ-tăng (module 04). `revisionHistoryLimit` (mặc định 10) = số RS cũ K8s giữ lại → giới hạn
bao nhiêu bản có thể rollback. Nhảy bản cụ thể: `rollout undo --to-revision=N`.
</details>

## Canary

<details>
<summary>6. Canary chia traffic bằng cơ chế gì (K8s thuần, không Istio)? (thực chạy)</summary>

**2 Deployment cùng label chung** (`app:myapp`) → **1 Service** (selector chỉ bắt label chung) → gom cả 2 vào 1
pool. Tỉ lệ traffic = **tỉ lệ replicas**: 4 stable + 1 canary = 1/5 → ~20% canary. Lab: `curl -sI` ×20 → 16
nginx/1.16.1 + 4 nginx/1.17.8 = đúng 80/20. kube-proxy chia **ngẫu nhiên theo xác suất** (không round-robin
cứng) nên dao động quanh 20%. Muốn chính xác 1% → Istio VirtualService weight / Argo Rollouts.
</details>

<details>
<summary>7. Promote canary làm gì? Có "cửa sổ" tốn tài nguyên không?</summary>

Promote: `scale canary` lên đủ + `delete stable`. Có **cửa sổ tạm** dùng nhiều Pod: `scale canary=4` khi stable
còn 4 → **8 Pod** cùng chạy (traffic canary nhảy 20%→50%), rồi `delete stable` → 4 Pod (100% canary). Promote
"mượt" thật sự (Argo Rollouts): tăng canary + giảm stable **song song từng bước** → tổng ~5 Pod, tỉ lệ tăng dần.
Rollback canary: chỉ `delete deployment app-canary` → về 100% stable.
</details>

## Blue-Green

<details>
<summary>8. Blue-Green khác Canary chỗ nào về trải nghiệm user?</summary>

**Canary**: user thật hit **cả 2 version** (mixing có kiểm soát 20%). **Blue-Green**: user chỉ thấy **một môi
trường** tại một thời điểm — trước cutover 100% blue, sau 100% green, **không mixing**. Green chạy song song
nhưng chỉ dev thấy qua Service test riêng (cổng 9001); user thật vẫn blue qua public Service (cổng 80).
</details>

<details>
<summary>9. Cutover blue-green làm bằng gì? Vì sao "tức thì" và rollback nhanh? (thực chạy)</summary>

**Đổi selector** của public Service: `kubectl set selector svc/nginx-public role=green`. Service không đổi
(cùng IP:port), chỉ đổi **nhóm Pod nó trỏ tới**. Cơ chế (module 05): endpointslice-controller quét lại Pod khớp
`role=green` → viết lại EndpointSlice (thay IP blue bằng IP green) → kube-proxy đổi iptables → request kế DNAT
sang green. Lab: `:80` nhảy 1.16→1.17 tức thì, **0 Pod tạo/xoá**. Rollback = `set selector role=blue` (blue vẫn
còn nguyên). Chi phí: ~2x tài nguyên (blue+green song song).
</details>

<details>
<summary>10. (thực chạy) Vì sao 2 Service LoadBalancer chung EXTERNAL-IP trên OrbStack?</summary>

OrbStack không có cloud LB thật → map mọi Service LoadBalancer lên **1 IP host** (`192.168.139.2`), phân biệt
bằng **cổng** (public :80, green-test :9001). Chúng vẫn là 2 Service độc lập — **CLUSTER-IP khác nhau** mới là
danh tính thật (mỗi Service 1 IP ảo riêng). Kernel định tuyến theo cặp IP:port nên 2 luồng tách hẳn. Trên
cloud/MetalLB mỗi LB thường có external IP riêng (bài học môi trường-phụ-thuộc, module 05).
</details>

## Chọn chiến lược + Bắc cầu production

<details>
<summary>11. Bảng chọn: khi nào dùng chiến lược nào?</summary>

| Chiến lược | Mixing | Downtime | Tài nguyên | Rollback | Dùng khi |
|---|---|---|---|---|---|
| Rolling | có (tạm) | không | 1x + surge | `rollout undo` | mặc định, đa số |
| Recreate | không | có (ngắn) | 1x | `rollout undo` | schema DB không tương thích ngược (chỉ 1 version tồn tại) |
| Canary | có (kiểm soát) | không | ~1.25x | xoá canary | thử real traffic, quan sát metric |
| Blue-Green | không | không | ~2x | đổi selector | cần test kỹ trước user thấy, instant cutover/rollback |

Production: Recreate khi 2 version chạy song song sẽ xung khắc (schema/API). GitOps (Argo CD): không `rollout
undo` tay (bị sync ghi đè) — revert commit git. Kiểm soát traffic mịn (1%, header-based) → Istio/Argo Rollouts.
</details>
