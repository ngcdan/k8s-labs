# Kubernetes — ReplicaSet & Deployment: self-healing, scale, rolling update

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [k8s-deployment.md](k8s-deployment.md).

## ReplicaSet & self-healing

<details>
<summary>1. ReplicaSet làm gì khi một Pod trong nhóm nó quản bị xóa?</summary>

Vòng lặp reconcile phát hiện `current < desired` → **tạo Pod mới thay thế trong vài giây**, tự động,
không cần can thiệp tay. Lab thật: xóa `my-nginx-769df8fff-7gw2f` → `...-4bmjb` mọc lên ngay, tổng
luôn giữ 2 Pod. Self-healing **không nằm ở Pod** mà ở vòng lặp reconcile của controller.
</details>

<details>
<summary>2. <code>ownerReferences</code> của Pod do ReplicaSet tạo trỏ về đâu? Khác Pod trần thế nào?</summary>

Trỏ về `kind: ReplicaSet` (lab thật: `ownerReferences[0].kind = ReplicaSet`). Pod trần (module 03)
có `ownerReferences` **rỗng** → không ai reconcile → xóa là mất. Đây là trường quyết định "ai canh để
tạo lại".
</details>

<details>
<summary>3. Đọc tên Pod <code>my-nginx-769df8fff-7gw2f</code> — 3 phần nghĩa là gì?</summary>

`my-nginx` = tên Deployment · `769df8fff` = **pod-template-hash** của ReplicaSet (K8s tự gắn thành
label) · `7gw2f` = id ngẫu nhiên của instance Pod. Cùng một template → cùng hash RS; đổi template
(vd đổi image) → hash RS **khác** → đó là cách phân biệt RS cũ/mới khi rolling update.
</details>

## Deployment — chuỗi 3 tầng

<details>
<summary>4. Deployment và ReplicaSet khác nhau ở điểm cốt lõi nào?</summary>

Deployment là wrapper cao hơn: nó quản **ReplicaSet** (không trực tiếp chạm Pod), và thêm **rolling
update zero-downtime + rollback**. ReplicaSet thuần chỉ đảm bảo *số lượng* Pod. Chuỗi 3 tầng:
`Deployment → ReplicaSet → Pod(s)`. Thực tế hiếm khi tạo ReplicaSet trực tiếp — luôn qua Deployment.
</details>

<details>
<summary>5. Trong YAML Deployment, <code>selector.matchLabels</code> phải khớp trường nào? Sai thì sao?</summary>

Phải khớp `spec.template.metadata.labels`. Sai → K8s báo lỗi `selector does not match template labels`
ngay khi apply. ReplicaSet dùng selector để `list/watch` Pod qua API — chỉ Pod có label khớp mới được
tính vào `current` (đây cũng là cách một Pod trùng label có thể bị RS khác "nhận nuôi").
</details>

## Scale

<details>
<summary>6. Scale imperative vs declarative — lệnh gì, và cạm bẫy khi trộn hai cách?</summary>

Imperative: `kubectl scale deployment my-nginx --replicas=4` (patch thẳng `spec.replicas`).
Declarative: sửa `replicas:` trong YAML rồi `kubectl apply -f`. **Cạm bẫy** (lab thật): scale tay lên
4, nhưng file vẫn `replicas: 2` → lần `apply` kế báo `configured` và **kéo về 2** (terminate 2 Pod mới
nhất). Git "nói dối" thực tế → bất ngờ mất Pod. Chọn một cách và nhất quán.
</details>

<details>
<summary>7. Scale có làm restart/ảnh hưởng Pod đang chạy không?</summary>

Không. Scale chỉ thêm/bớt Pod để khớp `desired`; Pod đang chạy giữ nguyên (lab thật: RS `769df8fff`
đổi DESIRED 2→4→2, các Pod cũ `RESTARTS 0`, không đụng). Toàn bộ async — lệnh trả về ngay, Pod
mọc/tắt sau vài giây.
</details>

## Rolling update & rollback

<details>
<summary>8. Rolling update xảy ra theo trình tự nào? Vì sao zero-downtime?</summary>

Deployment tạo **ReplicaSet mới** (hash khác, revision +1, replicas=0) → tăng dần Pod mới, chờ mỗi Pod
`Ready` → giảm dần Pod RS cũ → lặp đến khi RS cũ `replicas=0`. Vì mặc định `maxSurge 25% /
maxUnavailable 25%` nên **lúc nào cũng còn Pod phục vụ** → không downtime. Lab thật: RS `769df8fff`
(alpine) 2→0, RS `65bd86f78b` (1.25) 0→2.
</details>

<details>
<summary>9. Sau rolling update, RS cũ ra sao? Vì sao quan trọng?</summary>

RS cũ **không bị xóa**, chỉ về `replicas=0` (lab thật: `65bd86f78b DESIRED 0` sau update, `769df8fff
DESIRED 0` trước update). Nó là **checkpoint** để rollback: `rollout undo` chỉ việc bật lại RS cũ →
nhanh & xác định, không cần build lại image.
</details>

<details>
<summary>10. <code>rollout undo</code> ảnh hưởng số revision và ReplicaSet thế nào? (chi tiết thực chạy)</summary>

`rollout history` đổi từ `1, 2` sang **`2, 3`**: undo được coi là một thay đổi mới → cấp revision kế
tiếp (**3**), *không* tái dùng số 1. Nhưng **ReplicaSet thì được tái dùng** — K8s thấy template
rollback khớp hash RS đã có (`769df8fff`, cùng AGE với RS gốc) → bật lại đúng RS đó thay vì đúc mới.
Hai RS chỉ **đổi vai** `replicas` qua lại.
</details>

<details>
<summary>11. <code>rollout status</code> / <code>history</code> / <code>undo</code> — mỗi lệnh dùng khi nào?</summary>

`status` = theo dõi tiến độ rolling update real-time (`Waiting for ... N out of M new replicas...` →
`successfully rolled out`). `history` = xem danh sách revision (CHANGE-CAUSE `<none>` nếu không ghi
chú). `undo` = phát hiện version mới lỗi → quay về revision/RS trước.
</details>

## Imperative vs declarative

<details>
<summary>12. <code>kubectl create</code> vs <code>kubectl apply</code> khi resource đã tồn tại?</summary>

`create` báo lỗi `AlreadyExists`; `apply` tạo-hoặc-cập-nhật (idempotent, patch diff). `apply` lưu
`kubectl.kubernetes.io/last-applied-configuration` để tính diff lần sau; nó so 3 thứ: state cluster
hiện tại, last-applied, YAML mới. Lab thật: apply lại cùng file khi khớp → `unchanged`; khi khác →
`configured`.
</details>

<details>
<summary>13. Vì sao nên đặt <code>resources.limits</code> (memory/cpu)? <code>minReadySeconds</code> để làm gì?</summary>

`limits`: container không giới hạn có thể dùng hết RAM/CPU node → kéo sập workload khác cùng node.
`minReadySeconds`: Pod mới phải sống ổn định N giây trước khi được tính `Ready` — buffer tránh traffic
vào Pod đang khởi động chưa ổn định (đặc biệt quan trọng trong rolling update).
</details>

## Bắc cầu sang Kubernetes production

<details>
<summary>14. Các bài học này dùng lại ở cụm thật thế nào?</summary>

- Mỗi Pod prod có `ownerReferences → ReplicaSet → Deployment`. Thấy Pod tự restart/tự thay **không
 phải bug** — ReplicaSet đang reconcile.
- `kubectl rollout` = cơ chế deploy version mới không cần downtime maintenance window; `rollout undo` =
 rollback nhanh khi hotfix chưa kịp build.
- `Recreate` thay `RollingUpdate` khi v2 không tương thích ngược (schema DB/API) — chấp nhận downtime
 ngắn để tránh chạy 2 version xung khắc song song.
- Stateful (DB, Kafka) → dùng `StatefulSet`, không phải Deployment.

| Deployment (module này) | Kubernetes production |
|---|---|
| self-healing qua ReplicaSet | Pod tự thay khi node sập/OOM/crash |
| rolling update zero-downtime | deploy bất kỳ lúc nào, không maintenance window |
| RS cũ `replicas=0` làm checkpoint | `rollout undo` rollback tức thì |
| declarative + Git | GitOps, Git là nguồn sự thật |
</details>

## Ôn tập — đào sâu

<details>
<summary>15. Rolling update chạy qua <b>2 ReplicaSet song song</b> — hình dung từng bước</summary>

Deployment KHÔNG sửa Pod cũ. Nó tạo **RS mới** rồi **dịch dần**: tăng replicas RS mới, giảm replicas RS cũ,
từng Pod một:

```
Trước:   RS-cũ (nginx:1.25) = 3 Pod        RS-mới (1.26) = 0 Pod
                    ↓ rolling, dịch dần từng bước ↓
Giữa:    RS-cũ = 2                          RS-mới = 1
         RS-cũ = 1                          RS-mới = 2
Sau:     RS-cũ (1.25) = 0 Pod              RS-mới (1.26) = 3 Pod
```

`maxSurge`/`maxUnavailable` khống chế "được dư/thiếu mấy Pod trong lúc dịch" → luôn còn Pod phục vụ →
**không downtime**. RS cũ **không bị xoá**, chỉ scale về 0 và nằm lại — chính là checkpoint để rollback nhanh.
</details>

<details>
<summary>16. Revision vs ReplicaSet — tách biệt: revision ĐẺ MỚI, RS TÁI DÙNG</summary>

**Revision = số thứ tự đánh dấu từng "phiên bản cấu hình" (template) trong lịch sử Deployment**, tiến đơn điệu
(chỉ tăng). Mỗi đổi template → một mốc mới, gắn với RS tương ứng. Xem: `kubectl rollout history deployment/<tên>`.

```
revision 1 → RS-A (1.25) [chạy]
đổi 1.26:   revision 2 → RS-B (1.26) [chạy];  RS-A scale 0, nằm lại
undo:       revision 3 → RS-A (1.25) [chạy]   ← revision MỚI = 3, nhưng RS thì TÁI DÙNG RS-A cũ!
```

Hai điều khắc cốt:
1. **Content về 1.25, nhưng revision KHÔNG về 1 — nhảy tới 3.** "Về bản cũ" vẫn là "một hành động mới" nên +1.
2. **RS tái dùng** (RS-A chỉ được scale 0→3 lại, khỏi tạo mới → rollback nhanh) trong khi **revision đẻ nhãn
 mới** trỏ lại RS-A. Ẩn dụ: revision = lịch sử commit git (chỉ thêm); RS = object git tái dùng —
 `git revert` tạo commit mới nhưng nội dung có thể y hệt bản cũ.
</details>

<details>
<summary>17. <code>kubectl rollout restart</code> — "rolling update giả" để thay toàn bộ Pod không downtime</summary>

```
kubectl rollout restart deployment/<tên>
```

KHÔNG phải "tắt rồi bật tại chỗ". Nó **kích hoạt một rolling update** (y hệt đổi image) nhưng **không đổi
image/config gì cả** — bằng cách chèn một annotation timestamp vào template:

```yaml
spec:
  template:
    metadata:
      annotations:
        kubectl.kubernetes.io/restartedAt: "2026-08-14T10:30:00Z"   # kubectl tự chèn
```

Template đổi → Deployment nghĩ "có bản mới" → đẻ RS mới, rolling từng Pod → toàn bộ Pod được thay bằng Pod
**mới sạch**, luôn còn Pod phục vụ (không downtime), và **sinh revision mới** (undo được).

Dùng chính cho: **ép Pod đọc lại ConfigMap/Secret khi inject qua env var** (env đóng băng lúc start — xem module
06). Khác `kubectl delete pod` (thô bạo, xoá phựt, có thể hụt Pod, không revision) — `rollout restart` là cách
chuẩn production.

| | `rollout restart` | `delete pod` |
|---|---|---|
| Cách chạy | Rolling từng Pod, chờ Ready mới xoá cũ | Xoá phựt, RS đẻ bù |
| Downtime | Không | Có thể hụt Pod giây lát |
| Revision mới / undo được | Có | Không |
| Đúng cách production | ✅ | ❌ (chỉ debug) |

Lệnh đi kèm: `rollout status` (theo tiến trình), `rollout history` (thấy revision mới), `rollout undo` (lỡ tay quay lại).
</details>

