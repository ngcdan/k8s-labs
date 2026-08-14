# Storage — giải thích chi tiết (bản đầy đủ theo buổi học)

Bản này giữ nguyên mạch giảng của buổi walkthrough: mỗi khái niệm đi kèm *vì sao*, cơ chế, ẩn dụ, và số
liệu **lab thật** đã chạy trên OrbStack. Runbook tóm tắt ở [k8s-storage.md](k8s-storage.md); tự kiểm nhanh ở
[notes.md](notes.md). File này để **đọc hiểu sâu**.

---

## 1. Vì sao filesystem container "chết cùng container"

Filesystem của container là một **lớp writable mỏng phủ lên image read-only**. Khi container bị replace —
rolling update, crash-restart, reschedule sang node khác — lớp writable đó **biến mất**, chỉ còn lại image
gốc. Đây là lý do mọi thứ ghi vào container (không qua volume) đều là tạm bợ.

Kubernetes cho gắn thêm **volume** để có chỗ ghi bền hơn. Hai loại cơ bản nhất — và giới hạn của chúng:

| | `emptyDir` | `hostPath` |
|---|---|---|
| Vòng đời | = vòng đời Pod | = đường dẫn trên node (tồn tại dù Pod chết) |
| Scope | trong Pod | node cụ thể |
| Mất khi | **Pod bị xoá** | Pod **reschedule sang node khác** |
| Dùng khi | cache/temp, sidecar↔app share file | mount `/var/run/docker.sock`, dev 1 node |

### emptyDir — bảng trắng phòng họp

K8s cấp một thư mục rỗng khi Pod được schedule lên node. Ai trong Pod cũng ghi/đọc được. Nhưng **kubelet xoá
thư mục này khi xoá Pod** — không có gì được lưu ra ngoài node.

> **Ẩn dụ:** bảng trắng trong phòng họp — ghi thoải mái, mọi người dùng chung, nhưng ai xoá phòng thì mất
> sạch.

**Lab thật (bước 3):** tạo Pod `shared-vol`, updater ghi timestamp mỗi 5s; `curl` thấy dãy `07:52:48 →
07:53:43`. Xoá Pod rồi `apply` lại cùng tên → `curl` ra dãy **mới toanh `08:22:48 → ...`**, không còn
`07:52:48`. Chứng minh: xoá Pod → emptyDir bay sạch → Pod mới nhận thư mục rỗng, ghi lại từ số 0.

### hostPath — vì sao NGUY HIỂM (không chỉ vì "gắn 1 node")

"Gắn 1 node" mới là hiện tượng. **Cơ chế nguy hiểm** nằm ở lúc Pod nhảy node:

- hostPath bind-mount đường dẫn của **node A** vào container.
- Pod reschedule sang **node B** (node A chết/drain/scale) → Pod mới nhìn `hostPath` path đó **trên node B**
  — nơi đó **trống, hoặc chứa data của app khác**.
- Data cũ vẫn nằm ở node A nhưng **Pod không tới được** → "biến mất".
- Tệ hơn: 2 Pod ở 2 node cùng path → mỗi thằng thấy **một bản khác nhau**, tưởng chung mà không chung.

An toàn chỉ khi cluster **1 node**, hoặc **khóa Pod vào đúng node** bằng Node Affinity.

---

## 2. Cơ chế mount: `volumes` + `volumeMounts` nối bằng `name` — "hai cửa sổ, một phòng"

Đây là phần hay gây khó. Nguyên tắc: **khai báo ổ đĩa một chỗ, mount vào nhiều chỗ, nối bằng trường `name`.**

```yaml
spec:
  volumes:                 # ① Ổ ĐĨA — khai báo Ở CẤP POD, dùng chung cho cả Pod
  - name: html             #    đặt tên ổ đĩa là "html"
    emptyDir: {}           #    loại ổ đĩa (rỗng, chết cùng Pod)

  containers:
  - name: nginx
    volumeMounts:          # ② nginx GẮN ổ đĩa "html" vào chỗ của nó
    - name: html           #    ← trỏ NGƯỢC lên volume "html" ở ①
      mountPath: /usr/share/nginx/html   # gắn vào web-root mặc định của nginx
      readOnly: true
  - name: updater
    volumeMounts:          # ③ updater GẮN đúng ổ đĩa "html" đó vào chỗ khác
    - name: html           #    ← cũng trỏ lên volume "html"
      mountPath: /html     #    gắn vào /html cho gọn
```

**Ý nghĩa:** có **đúng MỘT thư mục vật lý** trên node (ổ đĩa tên `html`). Cả hai `volumeMounts` đều ghi
`name: html` = "gắn cái ổ đĩa tên html ấy". Hai `mountPath` khác tên chỉ là **hai cửa sổ nhìn vào cùng một
phòng**:

```
                 ┌─────────────────────────────┐
                 │  emptyDir "html"            │   ← 1 thư mục THẬT trên node
                 │  (một bản duy nhất)         │      (chứa index.html)
                 └──────┬───────────────┬──────┘
        mount vào       │               │      mount vào
     /usr/share/nginx/html            /html
                 │               │
          ┌──────▼──────┐  ┌─────▼───────┐
          │  nginx      │  │  updater    │
          │ đọc         │  │ ghi         │
          │ .../index   │  │ /html/index │
          └─────────────┘  └─────────────┘
```

- `updater` ghi `/html/index.html`, `nginx` đọc `/usr/share/nginx/html/index.html` = **cùng một file**.
- Đổi `name` lệch nhau (nginx `html`, updater `data`) → gắn **hai ổ đĩa khác nhau** → hết share, mỗi thằng
  một phòng riêng.

> **Ẩn dụ:** `name` = căn phòng (kho); `mountPath` = cửa mỗi người mở để vào kho, dán biển tên khác nhau.
> Đồ trong kho là một đống chung. `readOnly: true` = cửa nginx chỉ cho nhìn, không cho bê đồ vào.

**Điểm mấu chốt cho cả chương:** bộ khung `volumes` + `volumeMounts` này **KHÔNG đổi** khi lên PVC — chỉ thay
dòng `emptyDir: {}` bằng `persistentVolumeClaim: {claimName: ...}`. Học một lần, dùng cho mọi loại volume.

### Teachable moment: vì sao `curl` lần đầu ra 502?

Lab thật: `port-forward ... & curl` ngay lập tức → lần curl **đầu** ra `502 Bad Gateway` (body có
`nginx/1.29.5` → chính nginx sinh ra), lần sau bình thường.

Lý do: lúc `t=0` emptyDir còn **rỗng** (updater `sleep 5` xong mới ghi `index.html` lần đầu) + tunnel
port-forward chưa ổn định → request rớt vào khe hở → nginx nhả trang lỗi. Vài giây sau có file + tunnel ổn →
curl ra timestamp.

**Nối module 03:** đây đúng là lý do tồn tại của **readinessProbe**. Nếu Pod có readiness gác cửa, K8s không
cho nó vào Service đến khi thật sự phục vụ được → client **không bao giờ** thấy 502 warmup. readiness =
"chưa sẵn sàng thì đừng gửi khách tới".

---

## 3. PV / PVC — tách data thành object riêng

`emptyDir` mất khi Pod xoá, `hostPath` mất khi reschedule — cả hai **không đủ cho stateful app** (DB,
transaction log, file upload). Giải pháp: tách data thành **object K8s riêng**, sống độc lập khỏi Pod.

- **PersistentVolume (PV)** — object **cluster-scoped** (không namespace), do **admin** (hoặc CSI driver)
  tạo, ánh xạ 1-1 với storage thật. Vòng đời độc lập khỏi mọi Pod.
- **PersistentVolumeClaim (PVC)** — object **namespace-scoped**, do **developer** tạo, là "đơn xin" storage.
- Pod tham chiếu PVC trong `spec.volumes[]`; container **không biết gì** về backend, chỉ thấy `mountPath`.

> **Ẩn dụ:** PV = căn hộ chủ nhà đăng ký cho thuê (tài sản chung của toà nhà = cluster). PVC = đơn thuê của
> bạn, nêu yêu cầu diện tích (thuộc phòng ban = namespace). K8s = môi giới ghép đôi. Thuê rồi thì không ai
> khác thuê căn đó. Dọn đi (xoá PVC) → đồ trong căn (data) giữ hay mất tuỳ **reclaim policy** của chủ nhà.

### Bind theo 3 điều kiện — lệch một cái là `Pending`

**PersistentVolumeController** trong control-plane chạy vòng lặp liên tục: quét PVC `Pending` + PV
`Available`, khớp **cả 3** thì bind cả hai sang `Bound`:

| # | Điều kiện | Nghĩa | Sai thì |
|---|---|---|---|
| 1 | **capacity** | PV có dung lượng **≥** PVC xin | PV nhỏ hơn → bỏ qua |
| 2 | **accessModes** | PV và PVC **cùng mode** (RWO=RWO) | lệch → không ghép |
| 3 | **storageClassName** | hai bên **cùng tên** | khác → không ghép |

Không PV nào khớp cả 3 → PVC treo **`Pending`** — đây là **lỗi debug số 1** với storage.

**Hai chi tiết tinh tế (lab thật, static provisioning):**

1. **Bind exclusive + PVC chiếm CẢ PV.** PVC `my-pvc` xin **5Gi**, PV `my-pv` có **10Gi** → bind (10 ≥ 5),
   nhưng cột `CAPACITY` hiện **10Gi**. PVC nuốt trọn PV, **5Gi dư bị phí**, không PVC khác dùng được. Một PV
   phục vụ đúng một PVC.
2. **Không bao giờ bind PV nhỏ hơn request.** Xin 5Gi mà chỉ có PV 1Gi → `Pending`.

### accessModes — RWO không phải "chỉ 1 container"

| Mode | Viết tắt | Ý nghĩa | Backend |
|---|---|---|---|
| `ReadWriteOnce` | RWO | **1 node** read/write | block (EBS, Ceph RBD) |
| `ReadOnlyMany` | ROX | nhiều node, chỉ đọc | hầu hết |
| `ReadWriteMany` | RWX | nhiều node read/write | file-based (NFS, CephFS) |
| `ReadWriteOncePod` | RWOP | 1 Pod duy nhất cả cluster | block, K8s ≥1.22 |

RWO = 1 **node**, nên **2 container trong cùng 1 Pod** dùng chung PVC RWO **được** (cùng node). Container ở
Pod khác trên node khác thì không.

### reclaimPolicy — chuyện gì xảy ra khi xoá PVC

- **Retain** — PV → `Released`, **KHÔNG bị xoá**, data còn, admin dọn tay. An toàn prod: lỡ `kubectl delete
  pvc` không bay data DB.
- **Delete** — xoá PV **+ volume backend** luôn (data mất). Là **mặc định của dynamic provisioning**.

---

## 4. StorageClass & dynamic provisioning — K8s tự đẻ PV

Static provisioning (nặn PV tay) **không scale**: team lớn hàng trăm service, admin không tạo PV kịp.
**StorageClass** = template + provisioner (CSI driver); PVC tham chiếu SC → K8s **tự tạo PV + volume backend
on-demand**. Developer chỉ tạo PVC, không cần gửi ticket cho admin.

**Lab thật (so 2 PV cạnh nhau):** sau khi làm cả static lẫn dynamic, `kubectl get pv` in **2 PV đối lập**:

```text
NAME                                       CAPACITY  RECLAIM  STATUS  CLAIM              STORAGECLASS
my-pv                                      10Gi      Retain   Bound   default/my-pvc     local-storage   ← static: mình đặt tên, Retain
pvc-aa0a6f8a-6a04-486c-b53d-1783902c772a   1Gi       Delete   Bound   default/test-pvc   local-path      ← dynamic: K8s sinh UUID, Delete
```

| | static (`my-pv`) | dynamic (`pvc-aa0a...`) |
|---|---|---|
| Ai tạo PV | **bạn** nặn tay | **provisioner** tự đẻ |
| Tên | tự đặt | UUID K8s sinh |
| storageClass | `local-storage` (bịa) | `local-path` (SC mặc định) |
| reclaim | `Retain` | `Delete` |

### volumeBindingMode — vì sao `WaitForFirstConsumer` cố tình ĐỢI

- **`Immediate`** — tạo PV + volume ngay khi PVC xuất hiện.
- **`WaitForFirstConsumer`** (SC mặc định OrbStack) — đợi đến khi **Pod dùng PVC được schedule** mới tạo PV.

Vì sao đợi? Để **đặt volume đúng node với Pod**:

> Storage **node-local** (local-path, hostPath): data nằm trên disk *một* node. Tạo PV ngay (Immediate) lúc
> chưa biết Pod đậu node nào → có thể tạo volume ở node A nhưng scheduler đặt Pod lên node B → Pod không tới
> được. `WaitForFirstConsumer` **đợi biết Pod schedule node nào rồi mới tạo volume đúng node đó**.

Vì thế PVC `Pending` một lúc ngay sau `apply` = **đang đợi Pod, không phải lỗi**. Ngược lại storage
**networked** (Ceph) truy cập từ mọi node → dùng `Immediate`, tạo trước lúc nào cũng được. Quy tắc gọn:
**node-local → phải đợi; networked → tạo ngay tuỳ ý.**

### Chứng minh persist tách vòng đời Pod (màn chốt)

Lab thật: ghi `hello persistent - Fri Aug 14 08:36:42` vào `/data/test.txt` (Pod `writer`, PVC dynamic) →
`kubectl delete pod writer` (PVC báo `unchanged`, **chỉ Pod bị xoá**) → `apply` lại → Pod mới `cat` **vẫn ra
đúng `08:36:42`**.

Đối chiếu thẳng với bước 3: cùng thao tác "xoá Pod → tạo lại", **emptyDir mất sạch** (timestamp reset),
**PVC giữ nguyên**. Đó là toàn bộ lý do PV/PVC tồn tại — và là thứ khiến database/upload/log chạy được trên
K8s.

---

## 5. CSI — Container Storage Interface

**"tree" = cây mã nguồn Kubernetes** (repo `kubernetes/kubernetes`).

| | In-tree (cũ) | CSI / out-of-tree (mới) |
|---|---|---|
| Code driver ở đâu | **BÊN TRONG** repo K8s | **BÊN NGOÀI**, vendor tự giữ |
| Ai maintain | K8s maintainer | vendor (Ceph, AWS…) |
| Vendor fix bug | chờ **K8s release** (~3 tháng) | **tự release** bất cứ lúc nào |
| Rủi ro | bug vendor crash cả cluster | cô lập, không đụng core |

CSI là **hợp đồng chuẩn** — interface gRPC (`CreateVolume`, `DeleteVolume`, `ControllerPublishVolume`,
`NodePublishVolume`…). K8s gọi theo hợp đồng; vendor tự implement phần sau. Mọi feature volume mới (snapshot,
resize, clone, raw block) đều đi qua CSI.

> **Ẩn dụ:** CSI = cổng USB-C. Thiết bị nào implement đúng chuẩn cũng cắm được; Apple/Samsung tự thiết kế phần
> cứng bên trong, không cần xin phép Intel.

**Ví dụ thật (cụm Ceph gặp giữa buổi):** provisioner `rook-ceph.rbd.csi.ceph.com` — chữ `.csi.` là dấu hiệu
— là một CSI driver **out-of-tree**, cấp block storage RBD (RADOS Block Device) từ Ceph. Vì Ceph truy cập từ
mọi node qua mạng nên StorageClass dùng `Immediate` + `allowVolumeExpansion: true` (nới dung lượng online).
App chỉ thấy PVC/PV chuẩn — mọi chi tiết Ceph (replication, RAID, iSCSI) ẩn hoàn toàn.

**Điểm cầu nối lab → thật:** cái học trên OrbStack (`local-path`) là **bộ khung khái niệm**; cụm Ceph là
**hiện thực production** của đúng bộ khung đó. Cùng một YAML PVC, chỉ đổi `storageClassName`, chạy được cả
hai — đó là sức mạnh của lớp trừu tượng CSI: **app không biết backend là gì.**

---

## 6. Bắc cầu sang production

- Bộ khung `volumes` + `volumeMounts` học trên OrbStack gõ **y nguyên** lên cụm Ceph — chỉ đổi
  `storageClassName`.
- Stateful (DB, Kafka, upload) **bắt buộc** PV/PVC, thường qua **StatefulSet** (mỗi replica 1 PVC riêng, tên
  ổn định).
- `Immediate` vs `WaitForFirstConsumer` do storage **networked** hay **node-local** quyết định, không ngẫu
  nhiên.
- `Retain` cho volume quan trọng (DB) để lỡ tay `delete pvc` không bay data; `Delete` cho volume tạm.

| Storage (module này) | Kubernetes production |
|---|---|
| emptyDir chết cùng Pod | chỉ dùng cache/scratch/sidecar |
| PV/PVC tách vòng đời | DB/upload/log persist qua Pod restart, node fail |
| StorageClass + dynamic | self-service, không cần ticket admin |
| CSI driver (local-path) | Ceph RBD / cloud disk, cùng một YAML |
