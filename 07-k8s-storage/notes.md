# Kubernetes — Storage: tách data khỏi vòng đời Pod

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [k8s-storage.md](k8s-storage.md).

## Volume cơ bản: emptyDir & hostPath

<details>
<summary>1. Vì sao filesystem container mặc định "chết" cùng container? emptyDir mất khi nào?</summary>

Filesystem container là **lớp writable mỏng phủ lên image read-only** — container bị replace (rolling
update, crash-restart, reschedule) → lớp đó biến mất. `emptyDir` cấp thư mục rỗng khi Pod schedule, vòng
đời **= vòng đời Pod**: xoá Pod → kubelet xoá luôn emptyDir. Lab thật: xoá `shared-vol` rồi tạo lại →
timestamp reset từ `07:52:48` sang `08:22:48` (thư mục rỗng toanh). Dùng cho cache/temp hoặc share file
sidecar↔app trong cùng Pod.
</details>

<details>
<summary>2. 2 container trong 1 Pod mount chung 1 file bằng cơ chế nào? (chi tiết YAML)</summary>

Volume và mount **tách rời**, nối bằng `name`. ① Khai báo **một** ổ đĩa ở `spec.volumes[]` (`name: html` +
`emptyDir: {}`). ② Mỗi container gắn ổ đĩa đó ở `volumeMounts[]`, trỏ **cùng `name: html`**, chọn
`mountPath` riêng. Có **đúng một thư mục vật lý** trên node; hai `mountPath` khác tên chỉ là **hai cửa sổ
nhìn vào một phòng** — updater ghi `/html/index.html`, nginx đọc `/usr/share/nginx/html/index.html` =
cùng file. Đổi `name` lệch → mount 2 volume khác → hết share. Bộ khung này **không đổi** khi lên PVC.
</details>

<details>
<summary>3. hostPath rủi ro gì khi cluster nhiều node?</summary>

`hostPath` bind-mount đường dẫn của **một node** cụ thể vào container. Pod reschedule sang node khác → thấy
path đó trên node mới, **trống hoặc nội dung khác** → mất data. Chỉ an toàn khi cluster 1 node hoặc khóa
Pod lại node bằng Node Affinity. Dùng chính đáng: mount `/var/run/docker.sock`, dev 1 node.
</details>

## PV / PVC

<details>
<summary>4. PV là gì, PVC là gì, scope mỗi cái? Ai tạo?</summary>

**PV** = object **cluster-scoped** (không namespace), do admin/CSI driver tạo, ánh xạ 1-1 với storage thật,
vòng đời độc lập khỏi Pod. **PVC** = object **namespace-scoped**, do developer tạo, là "đơn xin" storage.
Ẩn dụ: PV = căn hộ chủ nhà đăng ký cho thuê; PVC = đơn thuê nêu yêu cầu; K8s = môi giới ghép đôi.
</details>

<details>
<summary>5. PVC bind PV theo mấy điều kiện? (thực chạy) PVC xin 5Gi bind PV 10Gi thì CAPACITY hiện bao nhiêu?</summary>

**3 điều kiện, khớp hết mới bind:** ① capacity PV ≥ PVC xin · ② accessMode khớp · ③ storageClassName trùng.
Lệch 1 cái → PVC treo `Pending` mãi. Bind là **exclusive** (1 PVC giữ trọn 1 PV). Lab thật: PVC `my-pvc`
xin 5Gi bind PV `my-pv` 10Gi → cột `CAPACITY` hiện **10Gi** (chiếm cả PV, dư 5Gi bị phí, không ai dùng
được). PVC không bao giờ bind PV nhỏ hơn request.
</details>

<details>
<summary>6. RWO nghĩa là gì? 2 container trong 1 Pod dùng chung PVC RWO được không?</summary>

`ReadWriteOnce` = **1 node** mount read/write (block storage: EBS, Ceph RBD). 2 container **trong cùng 1
Pod** dùng chung PVC RWO **được** — vì chúng cùng node. Container ở Pod khác trên node khác thì không.
`RWX` (ReadWriteMany) mới cho nhiều node ghi (file-based: NFS, CephFS); `ROX` nhiều node chỉ đọc.
</details>

<details>
<summary>7. Retain vs Delete reclaim policy — khi nào PV bị xoá, khi nào không?</summary>

Xảy ra **sau khi xoá PVC**. **Retain**: PV → `Released`, KHÔNG bị xoá, data còn, admin dọn tay (an toàn
prod — lỡ `delete pvc` không bay data). **Delete**: K8s xoá PV + volume backend luôn → mất data. Delete là
mặc định của **dynamic provisioning**. Lab thật: `my-pv` (static) = Retain; `pvc-aa0a...` (dynamic) = Delete.
</details>

## StorageClass & dynamic provisioning

<details>
<summary>8. StorageClass giải quyết vấn đề gì của static? (thực chạy so 2 PV)</summary>

Static: admin nặn **từng PV tay** → không scale khi có hàng trăm service. **StorageClass + provisioner** =
self-service: developer chỉ tạo PVC, K8s **tự đẻ PV** on-demand. Lab thật: tạo 1 PVC `test-pvc` không khai
báo `storageClassName` → rơi vào SC mặc định `local-path` → K8s tự sinh PV tên UUID `pvc-aa0a6f8a-...` mà
mình **không hề tạo**. Đối lập `my-pv` (tên tự đặt, static).
</details>

<details>
<summary>9. WaitForFirstConsumer trong StorageClass để làm gì?</summary>

Hoãn tạo PV + volume backend đến khi **Pod dùng PVC được schedule** → đảm bảo volume tạo đúng zone/node với
Pod (tránh cross-zone latency trên cloud, hoặc chọn đúng node cho local storage). Vì thế PVC có thể `Pending`
một lúc ngay sau `apply` — **không phải lỗi**. Đối lập `Immediate` (tạo PV ngay khi PVC xuất hiện) — hợp
storage networked truy cập từ mọi node như Ceph.
</details>

<details>
<summary>10. StorageClass đã deploy sửa được không?</summary>

Không — StorageClass **immutable** sau khi tạo. Sai config phải xoá và tạo lại.
</details>

## CSI

<details>
<summary>11. CSI là gì, tại sao K8s bỏ in-tree driver? (nối cụm Ceph thật)</summary>

**CSI** = chuẩn interface gRPC (`CreateVolume`, `DeleteVolume`, `NodePublishVolume`…) giữa K8s và storage
vendor; plugin chạy **out-of-tree**: vendor tự release, không phụ thuộc K8s release cycle, không buộc mở
source. Trước đó **in-tree** driver nằm thẳng trong repo `kubernetes/kubernetes` → vendor fix bug phải chờ
K8s release (~3 tháng) + bug vendor có thể crash cả cluster. Ẩn dụ: CSI như USB-C — thiết bị nào implement
đúng chuẩn cũng cắm được. Ví dụ thật: cụm Ceph có provisioner `rook-ceph.rbd.csi.ceph.com` — đúng là một CSI
driver out-of-tree, cấp block storage (RADOS Block Device) truy cập được từ mọi node → dùng `Immediate` +
`allowVolumeExpansion: true`. App chỉ thấy PVC/PV chuẩn, mọi chi tiết Ceph ẩn hoàn toàn.
</details>

## Bắc cầu sang Kubernetes production

<details>
<summary>12. Storage các bài học này dùng ở cụm thật thế nào?</summary>

- Bộ khung `volumes` + `volumeMounts` học trên OrbStack (`local-path`) gõ **y nguyên** lên cụm Ceph — chỉ
 đổi `storageClassName`. Đó là sức mạnh CSI: app không biết backend là gì.
- Stateful (DB, Kafka, upload) **bắt buộc** PV/PVC — không để trong container. Trên cụm thật thường qua
 **StatefulSet** (mỗi replica 1 PVC riêng, tên ổn định).
- `Immediate` vs `WaitForFirstConsumer` do storage **networked** (Ceph → tạo ngay) hay **node-local**
 (local-path → chờ biết Pod ở đâu) quyết định, không phải ngẫu nhiên.
- `Retain` cho volume quan trọng (DB) để `delete pvc` lỡ tay không bay data; `Delete` cho volume tạm.

| Storage (module này) | Kubernetes production |
|---|---|
| emptyDir chết cùng Pod | chỉ dùng cache/scratch/sidecar |
| PV/PVC tách vòng đời | DB/upload/log persist qua Pod restart, node fail |
| StorageClass + dynamic | self-service, không cần ticket admin |
| CSI driver (local-path) | Ceph RBD / cloud disk, cùng một YAML |
</details>
