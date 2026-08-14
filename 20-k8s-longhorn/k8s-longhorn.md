# 20 · Longhorn — block storage phân tán

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Ingress stack production · kế tiếp: MinIO — S3 object storage · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu Longhorn là gì và tại sao nó giải quyết bài toán block storage trên bare-metal/edge mà cloud-provider volume không làm được; cài Longhorn lên kind-lab 3-node; tạo StorageClass, PVC, kiểm replica trải đều; thực hành snapshot; và nắm được GOLDEN LESSON về double-replicate khi kết hợp với database tự replicate.

**Nền:** đã quen PV/PVC/StorageClass (lab 07), đã biết dynamic provisioning qua CSI. Lab này thay provisioner `rancher.io/local-path` bằng `driver.longhorn.io` và thấy volume tự nhân bản qua nhiều node.

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

---

## ⚙️ Tiền đề

**kind-lab 3-node** (1 control-plane + 2 worker):

```bash
# kiểm cụm đang chạy
kubectl get nodes
# NAME                 STATUS   ROLES           AGE   VERSION
# kind-control-plane   Ready    control-plane   10m   v1.31.0
# kind-worker          Ready    <none>          10m   v1.31.0
# kind-worker2         Ready    <none>          10m   v1.31.0
```

**open-iscsi trong node container** — Longhorn cần `iscsiadm` để giao tiếp với block device. Với kind (node là container Docker), cài vào bên trong từng container:

```bash
# cài open-iscsi + nfs-common vào mỗi node kind
for node in kind-control-plane kind-worker kind-worker2; do
  docker exec -it "$node" bash -c "
    apt-get update -qq &&
    apt-get install -y open-iscsi nfs-common &&
    modprobe iscsi_tcp 2>/dev/null || true
  "
done
```

```text
# mỗi node in ra
Preparing to unpack .../open-iscsi_2.1.9-3_amd64.deb ...
Setting up open-iscsi ...
Setting up nfs-common ...
```

**Helm** đã cài:

```bash
helm version
# version.BuildInfo{Version:"v3.16.0", ...}
```

---

## 1. Longhorn là gì — distributed block storage

**Chốt:** Longhorn là **CSI block storage** chạy hoàn toàn trong Kubernetes — mỗi volume là tập hợp **N replica** phân tán trên nhiều node, tự sửa chữa, có UI quản lý; không cần Ceph hay cloud-provider volume.

- **Longhorn volume** = một khối lưu trữ logic. Data được ghi đồng thời lên N replica — mỗi replica là một tiến trình `instance-manager` trên 1 worker node, lưu trên disk thật của node đó.
- **Replica** được đặt trên các node khác nhau → node chết, replica còn lại đủ quorum → volume tiếp tục hoạt động và tự sao thêm replica mới.
- **Longhorn Manager** (DaemonSet, 1 Pod/node) điều phối toàn bộ: tạo/xóa replica, reconcile, monitor health.
- **Longhorn UI** — web dashboard tích hợp, xem real-time replica placement, snapshot, backup.
- **So với hostPath/local-path:** hostPath và local-path provisioner đều gắn chặt 1 node — node chết là PVC mất; Longhorn replica trải nhiều node → HA thật.

**Vì sao:** bare-metal cluster (on-prem, edge, homelab) không có EBS hay GCE PD. Rook-Ceph mạnh nhưng cồng kềnh, cần RAID card, yêu cầu OSD disk riêng. Longhorn đơn giản hơn nhiều: cài bằng Helm, dùng disk sẵn có của node, UI trực quan, snapshot + backup tích hợp — hợp với cluster nhỏ-vừa.

**Cơ chế:**

1. Longhorn CSI driver nhận `CreateVolume` từ K8s khi PVC bound.
2. Longhorn Controller chọn N node để đặt replica (chiến lược mặc định: trải đều, ưu tiên node chưa có replica của volume đó).
3. Mỗi replica là một tiến trình `longhorn-instance-manager` trên node tương ứng, lưu data vào `/var/lib/longhorn/replicas/<volume-id>/`.
4. Longhorn Engine (chạy cùng node với Pod đang dùng volume) ghi/đọc đồng thời tới tất cả replica — giống RAID-1 nhưng phân tán qua mạng (iSCSI over TCP trong cluster).

> 💡 **Ẩn dụ:** Longhorn volume như một cuốn nhật ký có 3 bản sao — một bản ở bàn làm việc (node 1), một bản ở tủ sách (node 2), một bản ở két (node 3). Bạn ghi vào cuốn trên bàn, hệ thống tự chép sang 2 bản kia ngay lập tức. Mất két vẫn còn 2 bản đọc/ghi được.

| | hostPath / local-path | Longhorn (replica ≥ 2) |
|---|---|---|
| Node fail | PVC mất (data trên node đó) | Volume vẫn hoạt động, replica tự heal |
| Snapshot | Không có | Snapshot trong-cluster; backup ra S3/NFS |
| Multi-node | Không | Replica trải đều |
| Cài đặt | Cực đơn giản | Helm + open-iscsi |
| UI | Không | Web UI tích hợp |
| Phù hợp | Dev 1 node | Prod bare-metal/edge ≤ ~20 node |

**Phản đề:** Longhorn không phải Ceph — không hỗ trợ `ReadWriteMany` (RWX) cho block (chỉ RWO). Nếu nhiều Pod cần ghi đồng thời vào cùng volume → dùng CephFS hoặc MinIO (lab 21). Cluster lớn (>30 node, hàng trăm TB) → Rook-Ceph phù hợp hơn.

![[longhorn-replica.excalidraw]]

---

## 2. Cài đặt + prerequisites

**Chốt:** cài Longhorn qua Helm vào namespace `longhorn-system`; cần `open-iscsi` và `nfs-common` trên mỗi node; sau khi cài, `longhorn-manager` DaemonSet chạy 1 Pod trên mỗi node.

- **Namespace `longhorn-system`** — toàn bộ Longhorn component sống ở đây: manager, driver, UI, CSI pods.
- **longhorn-manager** (DaemonSet) — chạy trên **mỗi node** (kể cả control-plane nếu không taint); điều phối tạo/xóa replica, chạy volume controller.
- **longhorn-instance-manager** — process con của manager, chạy Longhorn Engine và replica processes.
- **CSI pods** — `longhorn-csi-plugin` (DaemonSet), `longhorn-driver-deployer` (Deployment) — nhận call từ kubelet qua CSI interface.
- **Longhorn UI** — Deployment, expose qua Service `longhorn-frontend` port 80; có thể port-forward ra xem.

**Vì sao:** Longhorn đóng gói hoàn toàn bằng K8s resource (Deployment, DaemonSet, CRD) → upgrade, rollback bằng Helm như mọi app khác; không cần cài daemon ngoài cluster (khác Ceph dùng `ceph-volume`, `ceph-osd`…).

**Cơ chế:** Helm chart deploy ~30 object bao gồm DaemonSet longhorn-manager, Deployment longhorn-ui, CSI DaemonSet + Deployment, và 7 CRD (`volumes.longhorn.io`, `replicas.longhorn.io`, `snapshots.longhorn.io`, `backups.longhorn.io`, `engines.longhorn.io`, `nodes.longhorn.io`, `settings.longhorn.io`). longhorn-manager mỗi node tự discover disk local → đăng ký vào CRD `nodes.longhorn.io`.

> 💡 **Ẩn dụ:** longhorn-manager trên mỗi node như quản lý kho hàng — biết kho mình còn bao nhiêu chỗ, nhận lệnh từ tổng kho (controller) để cất hoặc lấy hàng.

| Component | Loại | Số instance (3-node lab) |
|---|---|---|
| longhorn-manager | DaemonSet | 3 (1 / node) |
| longhorn-csi-plugin | DaemonSet | 3 (1 / node) |
| longhorn-ui | Deployment | 1 |
| longhorn-driver-deployer | Deployment | 1 |
| instance-manager | Per-volume | tạo động khi có volume |

**Dùng/KHÔNG:** Longhorn hợp với cluster 3–20 node bare-metal/edge/homelab. **Phản đề:** nếu cluster chạy trên cloud có managed disk (EKS EBS, GKE PD) → dùng cloud CSI driver đó thay vì Longhorn — đã tối ưu, không overhead replication software.

**Làm:**

```bash
# Bước 1: thêm Helm repo Longhorn
helm repo add longhorn https://charts.longhorn.io
helm repo update
```

```text
"longhorn" has been added to your repositories
Hang tight while we grab the latest from your chart repositories...
...Successfully got an update from the "longhorn" chart repository
Update Complete. Happy Helming!
```

```bash
# Bước 2: cài Longhorn
helm install longhorn longhorn/longhorn \
  --namespace longhorn-system \
  --create-namespace \
  --version 1.7.2 \
  --set defaultSettings.defaultReplicaCount=3
```

```text
NAME: longhorn
LAST DEPLOYED: Wed Aug 13 09:00:00 2026
NAMESPACE: longhorn-system
STATUS: deployed
REVISION: 1
NOTES:
Longhorn is now installed on the cluster!
...
```

```bash
# Bước 3: đợi tất cả pod Ready (~3-4 phút)
kubectl -n longhorn-system rollout status daemonset/longhorn-manager
kubectl -n longhorn-system get pods
```

```text
daemon set "longhorn-manager" successfully rolled out

NAME                                        READY   STATUS    RESTARTS   AGE
csi-attacher-7b89b7cd9f-6mnp2               1/1     Running   0          2m
csi-attacher-7b89b7cd9f-8xqt4               1/1     Running   0          2m
csi-attacher-7b89b7cd9f-vkl9s               1/1     Running   0          2m
csi-provisioner-7c4d9f8b6-2jglk             1/1     Running   0          2m
csi-provisioner-7c4d9f8b6-fmk7p             1/1     Running   0          2m
csi-provisioner-7c4d9f8b6-wqr3n             1/1     Running   0          2m
csi-resizer-6f8c79d5b-4hzt7                 1/1     Running   0          2m
csi-snapshotter-5c4b9d6f9-9kxvb             1/1     Running   0          2m
longhorn-csi-plugin-b9fmp                   3/3     Running   0          2m
longhorn-csi-plugin-k4xnt                   3/3     Running   0          2m
longhorn-csi-plugin-pzr7c                   3/3     Running   0          2m
longhorn-driver-deployer-6b7d4c8f9-wlqjr    1/1     Running   0          3m
longhorn-manager-6kht2                      2/2     Running   0          3m
longhorn-manager-fxn9w                      2/2     Running   0          3m
longhorn-manager-vp8qj                      2/2     Running   0          3m
longhorn-ui-5f9b84c6b-zq7kp                 1/1     Running   0          2m
```

→ **Verify:** `longhorn-manager` DaemonSet có 3 Pod (1 mỗi node), tất cả `2/2 Running`. Kiểm thêm StorageClass được tạo tự động:

```bash
kubectl get storageclass
```

```text
NAME                 PROVISIONER          RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION
longhorn (default)   driver.longhorn.io   Delete          Immediate              true
```

---

## 3. StorageClass + numberOfReplicas + dataLocality

**Chốt:** StorageClass `longhorn` mặc định tạo volume với 3 replica trải đều; `dataLocality` kiểm soát có nên đặt 1 replica cùng node với Pod hay không; PVC tạo từ SC này → Longhorn tự tạo PV và phân bổ replica.

- **`numberOfReplicas`** — tham số trong StorageClass parameters, quyết định bao nhiêu bản sao. Mặc định = 3. Số replica phải ≤ số node có disk Longhorn.
- **`dataLocality`**:
  - `disabled` (mặc định) — Longhorn đặt replica trên bất kỳ node nào; Engine đọc/ghi qua mạng.
  - `best-effort` — Longhorn cố đặt 1 replica trên cùng node với Pod đang dùng volume; nếu không có node phù hợp, fallback sang node khác (không block).
  - `strict-local` — **bắt buộc** 1 replica nằm trên node đang chạy Pod; volume không mount được nếu không thỏa điều kiện này.
- **Dynamic provisioning:** PVC tham chiếu StorageClass → Longhorn CSI `CreateVolume` → tạo PV tự động → replica được phân bổ trên các node.
- **`reclaimPolicy: Delete`** (mặc định) — xóa PVC là xóa luôn PV và tất cả replica trên disk.

**Vì sao:** `numberOfReplicas: 3` nghĩa là chịu được 2 node fault đồng thời (1 replica còn sống). `dataLocality: best-effort` giảm latency đọc/ghi vì Engine đọc local thay vì qua mạng — quan trọng với workload I/O cao.

**Cơ chế:** khi PVC bound, Longhorn Controller nhận yêu cầu tạo volume và chạy thuật toán placement:
1. Lọc node có đủ disk space và disk health OK.
2. Ưu tiên node ít replica nhất (cân bằng tải).
3. Với `best-effort`: nếu node đang schedule Pod thuộc tập eligible → đặt 1 replica ở đó.
4. Tạo `replica.longhorn.io` CRD object cho mỗi replica → longhorn-manager trên node tương ứng nhận lệnh, tạo process replica.

> 💡 **Ẩn dụ:** `numberOfReplicas: 3` như sao lưu RAID-1 phân tán — data ghi một lần, hệ thống tự nhân sang 3 node. `dataLocality: best-effort` như đặt 1 thùng hàng ngay ở kho sát văn phòng bạn — lấy nhanh hơn, còn 2 thùng ở kho xa để dự phòng.

| `dataLocality` | Ý nghĩa | Hợp với |
|---|---|---|
| `disabled` | Replica đặt tùy ý, đọc/ghi qua mạng | Cluster nhỏ, I/O không quá cao |
| `best-effort` | 1 replica cùng node nếu có thể | Stateful app cần latency thấp |
| `strict-local` | 1 replica **bắt buộc** cùng node | DB tự replicate (xem mục 5) |

**Dùng/KHÔNG:** `best-effort` hợp cho phần lớn workload stateful. **Phản đề:** `strict-local` có thể block schedule nếu node đang chạy Pod hết dung lượng disk Longhorn → Pod không mount được volume; dùng thận trọng và cần set resource request disk hợp lý.

**Làm:**

```bash
# xem params StorageClass longhorn mặc định
kubectl get storageclass longhorn -o yaml | grep -A20 'parameters:'
```

```yaml
parameters:
  dataLocality: disabled
  fromBackup: ""
  fsType: ext4
  numberOfReplicas: "3"
  staleReplicaTimeout: "2880"
```

```bash
# Bước 1: tạo PVC dùng StorageClass longhorn mặc định
cat > /tmp/longhorn-pvc.yml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: demo-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 2Gi
EOF
kubectl apply -f /tmp/longhorn-pvc.yml
kubectl get pvc demo-pvc
```

```text
NAME       STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
demo-pvc   Bound    pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b   2Gi        RWO            longhorn       8s
```

```bash
# Bước 2: xem replica đang chạy trên node nào
kubectl -n longhorn-system get replicas.longhorn.io \
  -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeID,STATE:.status.currentState' \
  | grep "pvc-4f8b2e1a"
```

```text
NAME                                                              NODE           STATE
pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b-r-4a2b1c3d            kind-worker    running
pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b-r-7e5f8a9b            kind-worker2   running
pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b-r-2c9d0e1f            kind-control-plane running
```

→ **Verify:** 3 replica trải đều trên 3 node. Kiểm nhanh volume object:

```bash
kubectl -n longhorn-system get volumes.longhorn.io
```

```text
NAME                                       STATE      ROBUSTNESS   SCHEDULED   SIZE         AGE
pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b detached   unknown      True        2147483648   30s
```

```bash
# Bước 3: tạo Pod dùng PVC → volume chuyển sang attached
cat > /tmp/demo-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: demo-app
spec:
  containers:
  - name: app
    image: alpine
    command: ["/bin/sh", "-c", "sleep 3600"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: demo-pvc
EOF
kubectl apply -f /tmp/demo-pod.yml
kubectl wait --for=condition=Ready pod/demo-app --timeout=120s
kubectl -n longhorn-system get volumes.longhorn.io
```

```text
NAME                                       STATE      ROBUSTNESS   SCHEDULED   SIZE         AGE
pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b attached   healthy      True        2147483648   90s
```

→ **Verify:** `STATE=attached`, `ROBUSTNESS=healthy` — 3 replica đang đồng bộ, volume ghi/đọc được.

---

## 4. Snapshot & backup

**Chốt:** Longhorn có hai lớp bảo vệ — **snapshot** (trong-cluster, nhanh, rollback về time-point) và **backup** (ra ngoài cluster vào S3 hoặc NFS, chống mất cả cluster); cả hai đều có thể tự động hóa bằng **RecurringJob**.

- **Snapshot** — lưu trữ dữ liệu tại một thời điểm ngay trong cluster. Nằm trên cùng disk với replica → mất node chứa replica là mất snapshot đó. Snapshot nhanh (copy-on-write), không tốn thêm disk cho block chưa thay đổi.
- **Backup** — Longhorn đọc snapshot và ghi ra **backupTarget** ngoài cluster (S3, NFS, SMB). Chống mất cả cluster; restore về cluster mới được.
- **RecurringJob** — Longhorn CRD lên lịch tự động chụp snapshot hoặc backup theo cron. Ví dụ: snapshot mỗi giờ, giữ 24 bản; backup ra S3 mỗi ngày, giữ 7 bản.
- **Restore** — tạo PVC mới từ backup → Longhorn kéo data từ S3 về → attach vào Pod thay thế.

**Vì sao:** snapshot tốt cho rollback nhanh khi upgrade DB schema sai; backup ra S3 tốt cho disaster recovery. Chỉ dùng snapshot mà không backup = vẫn mất hết nếu cluster cháy. Best practice: cả hai.

**Cơ chế — snapshot:** khi tạo snapshot, Longhorn Engine đánh dấu tất cả block hiện tại là "frozen", ghi mới đi vào block mới (copy-on-write). Snapshot = pointer tới tập block tại thời điểm đó. Rollback = Engine đọc block từ snapshot thay vì head. **Backup:** Longhorn đọc từng block của snapshot, nén (zstd), upload lên S3/NFS theo format riêng (không phải raw image — tiết kiệm bandwidth vì chỉ upload block thay đổi giữa các lần backup).

> 💡 **Ẩn dụ:** snapshot = chụp ảnh căn phòng ngay lúc này — nếu sau đó lộn xộn, bạn nhìn ảnh nhớ vị trí đồ đạc và dọn lại. Backup ra S3 = gửi ảnh đó sang email ngoài công ty — nhà cháy vẫn còn ảnh.

| | Snapshot | Backup |
|---|---|---|
| Nơi lưu | Disk của node trong cluster | S3 / NFS ngoài cluster |
| Tốc độ tạo | Nhanh (copy-on-write, giây) | Phụ thuộc network/disk |
| Bảo vệ khỏi | Ghi sai, corrupt data | Mất cluster, disk failure |
| Restore | Rollback volume tại chỗ | Tạo PVC mới từ backup |
| Tốn thêm disk | Chỉ block thay đổi | Không tốn disk cluster |

**Dùng/KHÔNG:** tạo snapshot trước mọi operation nguy hiểm (upgrade schema, migration). **Phản đề:** snapshot không thay thế backup — nếu disk node hỏng, snapshot trên disk đó mất theo replica; phải có backup ra ngoài cho data production quan trọng.

**Làm:**

```bash
# Bước 1: ghi dữ liệu vào volume
kubectl exec demo-app -- sh -c 'echo "version-1" > /data/state.txt && cat /data/state.txt'
```

```text
version-1
```

```bash
# Bước 2: tạo snapshot qua Longhorn CRD
VOLUME_NAME=$(kubectl -n longhorn-system get volumes.longhorn.io -o jsonpath='{.items[0].metadata.name}')
cat > /tmp/snapshot.yml <<EOF
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata:
  name: snap-v1
  namespace: longhorn-system
spec:
  volume: ${VOLUME_NAME}
EOF
kubectl apply -f /tmp/snapshot.yml
```

```bash
# xem snapshot đã tạo
kubectl -n longhorn-system get snapshots.longhorn.io
```

```text
NAME      VOLUME                                       READY   AGE
snap-v1   pvc-4f8b2e1a-7c3d-4a9e-b2f1-8d5c6e7a9f0b   true    5s
```

```bash
# Bước 3: RecurringJob — snapshot mỗi giờ, giữ 24 bản
cat > /tmp/recurring-snapshot.yml <<'EOF'
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata:
  name: hourly-snapshot
  namespace: longhorn-system
spec:
  cron: "0 * * * *"
  task: snapshot
  groups:
    - default
  retain: 24
  concurrency: 1
  labels:
    recurring-job: hourly-snapshot
EOF
kubectl apply -f /tmp/recurring-snapshot.yml
kubectl -n longhorn-system get recurringjobs.longhorn.io
```

```text
NAME               GROUPS    TASK       CRON        RETAIN   AGE
hourly-snapshot    default   snapshot   0 * * * *   24       3s
```

→ **Verify:** snapshot `snap-v1` `READY=true`; RecurringJob `hourly-snapshot` đã đăng ký — Longhorn sẽ tự chạy theo cron.

---

## 5. StorageClass replica-1 cho DB stateful

**Chốt — GOLDEN LESSON:** khi database tự replicate ở tầng ứng dụng (Postgres CNPG 3 instance, MySQL Group Replication…), nên dùng StorageClass Longhorn `numberOfReplicas: 1` + `dataLocality: strict-local` thay vì replica 3 mặc định — tránh **double-replication** lãng phí tài nguyên và giảm IOPS.

**Vì sao đây là GOLDEN LESSON:**

Nếu Postgres CNPG có 3 instance (primary + 2 standby) và mỗi instance dùng PVC Longhorn replica-3:
- Data thật được ghi: 1 bản.
- CNPG tự replicate: 3 bản ở tầng Postgres (WAL streaming).
- Longhorn replica-3 nhân thêm: mỗi 1 trong 3 bản Postgres → 3 bản Longhorn.
- **Tổng bản copy thật trên disk: 3 × 3 = 9 bản.**

Longhorn replica-1 + CNPG 3 instance:
- 3 bản Postgres, mỗi bản có 1 replica Longhorn = 3 bản tổng — đủ HA, không lãng phí.

**IOPS:** với replica-3, mỗi ghi phải xác nhận đủ 3 replica → latency ghi tăng. Replica-1 + `strict-local` = ghi local, không qua mạng → latency thấp nhất.

**Cơ chế:** `dataLocality: strict-local` đảm bảo Longhorn Engine và replica đều nằm trên cùng node với Pod → I/O không đi qua mạng cluster (không có iSCSI over TCP), gần như native disk performance.

> 💡 **Ẩn dụ:** CNPG 3 instance như 3 người mỗi người tự photocopy tài liệu. Nếu mỗi người lại nhờ thêm 2 người khác giữ bản sao → 9 bản total. Dùng Longhorn replica-1: mỗi người giữ 1 bản, tổng vẫn là 3 — đủ bền, không lãng phí.

| Cấu hình | Bản copy tổng | IOPS | Độ phức tạp |
|---|---|---|---|
| CNPG 3 + Longhorn replica-3 | 9 bản | Thấp (triple sync qua mạng) | Cao |
| CNPG 3 + Longhorn replica-1 strict-local | 3 bản | Cao (local I/O) | Thấp |
| CNPG 1 + Longhorn replica-3 | 3 bản | Trung bình | Trung bình |

**Dùng/KHÔNG:** replica-1 `strict-local` chỉ hợp khi **ứng dụng tự replicate** — CNPG, Vitess, TiKV, Kafka broker với replication-factor ≥ 2. **Phản đề:** đừng dùng replica-1 cho app đơn giản không tự replicate (single Redis, single Postgres không có standby) — node chết là mất data.

**Làm:**

```bash
# Tạo StorageClass longhorn-cnpg dành riêng cho CNPG lab 22
cat > /tmp/sc-longhorn-cnpg.yml <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: longhorn-cnpg
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: driver.longhorn.io
allowVolumeExpansion: true
reclaimPolicy: Delete
volumeBindingMode: Immediate
parameters:
  numberOfReplicas: "1"
  dataLocality: strict-local
  fsType: ext4
  staleReplicaTimeout: "2880"
EOF
kubectl apply -f /tmp/sc-longhorn-cnpg.yml
kubectl get storageclass
```

```text
NAME             PROVISIONER          RECLAIMPOLICY   VOLUMEBINDINGMODE   ALLOWVOLUMEEXPANSION
longhorn (default)   driver.longhorn.io   Delete       Immediate           true
longhorn-cnpg        driver.longhorn.io   Delete       Immediate           true
```

```bash
# Test PVC với StorageClass replica-1
cat > /tmp/cnpg-test-pvc.yml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cnpg-test-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn-cnpg
  resources:
    requests:
      storage: 5Gi
EOF
kubectl apply -f /tmp/cnpg-test-pvc.yml

# tạo pod test để trigger volume provision
cat > /tmp/cnpg-test-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: cnpg-test
spec:
  containers:
  - name: app
    image: alpine
    command: ["/bin/sh", "-c", "sleep 60"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: cnpg-test-pvc
EOF
kubectl apply -f /tmp/cnpg-test-pod.yml
kubectl wait --for=condition=Ready pod/cnpg-test --timeout=120s

# kiểm tra chỉ có 1 replica
CNPG_VOLUME=$(kubectl -n longhorn-system get volumes.longhorn.io \
  -o jsonpath='{range .items[?(@.metadata.name contains "cnpg")]}{.metadata.name}{"\n"}{end}' 2>/dev/null || \
  kubectl get pvc cnpg-test-pvc -o jsonpath='{.spec.volumeName}')
kubectl -n longhorn-system get replicas.longhorn.io \
  -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeID,STATE:.status.currentState' \
  | grep "${CNPG_VOLUME:0:20}"
```

```text
NAME                                                              NODE         STATE
pvc-9b3e7f2a-1d4c-5b8e-c3f2-0a7d8e9b1c2d-r-8f1e2a3b            kind-worker  running
```

→ **Verify:** chỉ **1 replica** (không phải 3) → xác nhận StorageClass `longhorn-cnpg` hoạt động đúng. Data locality: `strict-local` → replica nằm cùng node với Pod.

---

## 🧹 Dọn dẹp

```bash
# xóa pod và PVC test
kubectl delete pod demo-app cnpg-test --ignore-not-found
kubectl delete pvc demo-pvc cnpg-test-pvc --ignore-not-found

# xóa snapshot và recurring job
kubectl -n longhorn-system delete snapshot snap-v1 --ignore-not-found
kubectl -n longhorn-system delete recurringjob hourly-snapshot --ignore-not-found

# giữ lại StorageClass longhorn và longhorn-cnpg cho lab 22
# (uninstall Longhorn hoàn toàn nếu muốn dọn sạch)
# helm uninstall longhorn -n longhorn-system
```

---

## ✅ Đủ khi

① Giải thích Longhorn là gì, replica trải đều trên node hoạt động thế nào, khác hostPath ở điểm gì.
② Cài Longhorn qua Helm, biết cần prerequisite gì (open-iscsi).
③ Phân biệt `numberOfReplicas` và `dataLocality` (disabled / best-effort / strict-local) — khi nào dùng cái nào.
④ Tạo snapshot trong-cluster và giải thích snapshot khác backup ra S3 thế nào.
⑤ Giải thích GOLDEN LESSON double-replicate: CNPG 3 instance + Longhorn replica-3 = 9 bản, tại sao dùng replica-1 `strict-local` tốt hơn.

---

## 🧠 Recall

Tự trả lời trước, cuộn xuống xem Đáp án sau.

1. Longhorn khác hostPath/local-path ở điểm gì căn bản nhất?
2. longhorn-manager chạy loại K8s object nào? Trên bao nhiêu node?
3. `numberOfReplicas: 3` nghĩa là volume chịu được bao nhiêu node fault?
4. `dataLocality: best-effort` và `strict-local` khác nhau thế nào?
5. Snapshot trong Longhorn nằm ở đâu? Điểm yếu của nó so với backup?
6. Backup Longhorn khác snapshot ở điểm gì, dùng khi nào?
7. RecurringJob làm được gì? Khai báo bằng loại K8s object nào?
8. Tại sao CNPG 3 instance + Longhorn replica-3 tốn 9 bản copy?
9. StorageClass `longhorn-cnpg` dùng `strict-local` — rủi ro gì nếu node chứa Pod hết dung lượng disk Longhorn?
10. Longhorn có hỗ trợ `ReadWriteMany` (RWX) không? Thay thế bằng gì nếu cần RWX?

### Đáp án

1. hostPath/local-path gắn chặt 1 node — node chết là mất data. Longhorn có N replica trải nhiều node: node chết, volume vẫn hoạt động và tự heal replica mới. Có thêm snapshot, backup, UI.
2. DaemonSet — chạy 1 Pod trên **mỗi node** trong cluster (kể cả control-plane nếu không taint).
3. Chịu được **2 node fault** đồng thời (còn 1 replica sống là đủ để đọc/ghi, Longhorn sẽ tạo replica mới bù vào).
4. `best-effort`: cố đặt 1 replica cùng node với Pod, nếu không được thì fallback — không block mount. `strict-local`: **bắt buộc** 1 replica cùng node — nếu node hết chỗ, volume không mount được (block).
5. Snapshot nằm trên disk của node replica trong cluster. Điểm yếu: nếu node đó hỏng disk, snapshot mất theo; không bảo vệ khỏi mất cả cluster.
6. Backup đẩy data ra ngoài cluster (S3/NFS): chống mất cluster, restore về cluster mới được. Dùng khi cần disaster recovery. Snapshot chỉ là rollback nhanh trong-cluster.
7. RecurringJob tự động chụp snapshot hoặc tạo backup theo cron, giữ N bản gần nhất. Khai báo bằng CRD `recurringjob.longhorn.io` (Longhorn custom resource).
8. CNPG tạo 3 bản ở tầng Postgres (primary + 2 standby). Mỗi bản dùng PVC Longhorn replica-3 → mỗi bản Postgres có 3 bản Longhorn → tổng 3 × 3 = 9 bản trên disk.
9. `strict-local` bắt buộc replica nằm cùng node với Pod → nếu node hết dung lượng disk Longhorn, volume không attach được, Pod không start — volume bị block. Giải pháp: monitor disk usage Longhorn, set resource request disk.
10. Longhorn block volume chỉ hỗ trợ **RWO** (ReadWriteOnce). Cần RWX → dùng **CephFS** (Rook-Ceph) hoặc **NFS provisioner** hoặc **MinIO** (cho object storage, lab 21).

---

## Bắc cầu sang production

Trên cluster thật bare-metal, Longhorn thường là default storage: PVC của mọi stateful app (Postgres, Redis, Kafka, MinIO) đều đi qua Longhorn. Điều chỉnh theo workload:

- **Stateless app có state nhỏ** (Redis cache, session): dùng `longhorn` mặc định replica-3.
- **Database tự replicate** (CNPG, TiDB, Vitess): dùng `longhorn-cnpg` replica-1 `strict-local` — tiết kiệm disk 3×, tăng IOPS.
- **Monitoring stack** (Prometheus, Loki): replica-2 là đủ (balance giữa HA và disk cost).
- **Backup target**: cấu hình `backupTarget` trong Longhorn Settings trỏ vào MinIO S3 nội bộ (lab 21) hoặc S3 external — đây là lớp DR cuối cùng.

Snapshot trước mọi upgrade DB schema, backup ra S3 trước mọi maintenance cluster.

---

## 📎 Nguồn

- [Longhorn Documentation](https://longhorn.io/docs/) — cài đặt, CRD, backup, disaster recovery.
- [Longhorn GitHub](https://github.com/longhorn/longhorn) — release notes, known issues.
- [CNPG + Longhorn best practices](https://cloudnative-pg.io/documentation/) — xem mục Storage.
- [CSI Spec](https://github.com/container-storage-interface/spec) — hiểu interface giữa K8s và storage plugin.
