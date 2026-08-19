# 20 · Distributed storage — Longhorn & Rook-Ceph

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Ingress stack production · kế tiếp: MinIO — S3 object storage · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** nắm hai lời giải hàng đầu cho block storage phân tán trên bare-metal/edge — **Longhorn** (đơn giản, K8s-native) và **Rook-Ceph** (unified block + file + object, scale enterprise). Với Longhorn: cài lên kind-lab 3-node, tạo StorageClass/PVC, kiểm replica trải đều, snapshot, và GOLDEN LESSON double-replicate. Với Rook-Ceph: hiểu kiến trúc Ceph (RADOS/MON/MGR/OSD/CRUSH), cài Rook operator + CephCluster, tạo RBD block (RWO) và CephFS (RWX — cái Longhorn không làm được), và biết **khi nào chọn cái nào**.

**Nền:** đã quen PV/PVC/StorageClass (lab 07), đã biết dynamic provisioning qua CSI. Lab này thay provisioner `rancher.io/local-path` bằng `driver.longhorn.io` / `rook-ceph.rbd.csi.ceph.com` và thấy volume tự nhân bản qua nhiều node.

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). Rook-Ceph nặng hơn Longhorn nhiều (nhiều daemon MON/MGR/OSD, cần raw block device cho OSD) — trên kind cần gắn thêm disk. **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

**Bản đồ module:**

| Phần | Mục | Học gì |
|---|---|---|
| I — Longhorn | 1–5 | Distributed block K8s-native, replica trải node, snapshot/backup, GOLDEN LESSON replica-1 cho DB |
| II — Rook-Ceph | 6–10 | Ceph là gì, cài Rook operator + CephCluster, RBD (RWO), CephFS (RWX), bảng quyết định Longhorn vs Ceph |

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

# Phần II — Rook-Ceph

## 6. Rook-Ceph là gì — Ceph chạy trong Kubernetes

**Chốt:** **Ceph** là hệ lưu trữ phân tán hợp nhất (unified) cung cấp cả **block, file và object** trên một nền RADOS duy nhất; **Rook** là Kubernetes operator biến Ceph thành CRD — bạn khai báo `CephCluster`/`CephBlockPool`/`CephFilesystem`, operator lo triển khai và vận hành các daemon Ceph.

- **RADOS** (*Reliable Autonomic Distributed Object Store*) — lớp nền của Ceph. Mọi thứ (RBD block, CephFS file, RGW object) đều lưu dưới dạng object trong RADOS. Không có single point of failure.
- **MON (Monitor)** — giữ **cluster map** (monmap, osdmap, pgmap, crushmap) và đồng thuận qua Paxos. Thường **3 MON** (số lẻ) để có quorum; MON quyết định "trạng thái đúng" của cluster, không lưu data.
- **MGR (Manager)** — thu thập metrics runtime, chạy dashboard, module (Prometheus, balancer, autoscaler PG). Thường 2 (active/standby).
- **OSD (Object Storage Daemon)** — **1 OSD ứng với 1 disk** (BlueStore quản lý raw block device trực tiếp, không qua filesystem). OSD lưu data thật, tự xử lý replication, recovery, rebalancing. Đây là nơi cần **raw device**.
- **MDS (Metadata Server)** — chỉ cần cho **CephFS**: lưu cây thư mục, inode, quyền POSIX. Không nằm trên đường data (data đi thẳng client ↔ OSD).
- **RGW (RADOS Gateway)** — cổng **S3/Swift** object, dựng object storage tương thích S3 (thay thế được MinIO ở lab 21 nếu muốn dùng chung Ceph).
- **CRUSH** — thuật toán placement không cần bảng tra cứu trung tâm: client tự tính object nằm ở OSD nào từ crushmap. **Failure domain** (host/rack/row) đảm bảo các bản sao không rơi cùng một điểm hỏng.
- **Pool + PG (Placement Group)** — pool là phân vùng logic của RADOS (có `size=3` replication hoặc erasure coding); PG là nhóm trung gian giữa object và OSD, giảm số mapping phải theo dõi.

**Vì sao:** Longhorn chỉ làm **block RWO**. Khi bạn cần **một** hệ lo cả ba — volume block cho DB (RBD), filesystem chia sẻ nhiều Pod (CephFS RWX), và S3 object (RGW) — thay vì ghép 3 công nghệ, Ceph gộp hết trên một cluster. Đổi lại: phức tạp hơn hẳn, cần hiểu MON/OSD/CRUSH và tốn RAM/CPU.

**Cơ chế:** Rook operator (Deployment trong namespace `rook-ceph`) watch các CRD. Khi bạn apply `CephCluster`, operator: (1) sinh MON pods, chờ quorum; (2) chạy MGR; (3) discover disk theo `storage` spec → khởi động một OSD pod cho mỗi device; (4) cập nhật cluster map. Khi bạn apply `CephBlockPool`/`CephFilesystem`, operator tạo pool trong RADOS và (với CephFS) sinh MDS. Ceph-CSI driver (`rook-ceph.rbd.csi.ceph.com`, `rook-ceph.cephfs.csi.ceph.com`) map RBD image / mount CephFS vào Pod khi PVC bound.

> 💡 **Ẩn dụ:** Longhorn như một tủ đông chuyên giữ từng khối thịt (block) — đơn giản, một chức năng. Ceph như một tổng kho logistics: có khu pallet (block/RBD), khu kệ chung nhiều người lấy (file/CephFS), khu bưu kiện gửi đi (object/RGW) — cùng một hệ điều phối (RADOS + CRUSH). Mạnh và linh hoạt, nhưng vận hành phức tạp hơn nhiều cái tủ đông.

| Khái niệm Ceph | Vai trò | Số lượng điển hình |
|---|---|---|
| MON | Giữ cluster map, quorum Paxos | 3 (số lẻ) |
| MGR | Metrics, dashboard, module | 2 (active/standby) |
| OSD | Lưu data thật, 1/disk | = số disk (≥3 cho HA) |
| MDS | Metadata cho CephFS | 1 active (+ standby) |
| RGW | Cổng S3/Swift object | ≥1 |

**Dùng/KHÔNG:** chọn Rook-Ceph khi cần **unified storage** (block + file RWX + object) hoặc scale lớn (hàng trăm node, PB). **Phản đề:** cluster nhỏ chỉ cần block RWO cho vài stateful app → Longhorn nhẹ hơn, dễ vận hành hơn nhiều; đừng kéo cả Ceph vào homelab chỉ để chạy một Postgres.

![[rook-ceph-arch.excalidraw]]

---

## 7. Cài Rook operator + CephCluster

**Chốt:** cài **Rook operator** bằng Helm vào namespace `rook-ceph`; sau đó apply **CephCluster** CR để operator dựng MON/MGR/OSD; OSD cần **raw block device** (BlueStore) — trên kind phải gắn thêm disk cho mỗi node vì rootfs container không dùng làm OSD được.

- **Rook operator** — Deployment watch CRD, không tự lưu data; là "bộ não" triển khai Ceph.
- **CephCluster** — CR mô tả toàn cluster: số MON, node/device nào làm OSD, phiên bản Ceph image.
- **rook-ceph-tools** — Pod tiện ích chứa CLI `ceph`, `rbd`, `rados` để chẩn đoán (`ceph status`, `ceph osd tree`).
- **Raw device trên kind:** node kind là container, không có disk trống. Gắn thêm loop device / extra volume vào từng node rồi cho Rook `useAllDevices` hoặc `deviceFilter`.

**Vì sao:** BlueStore (OSD backend mặc định từ Ceph Luminous) quản lý **trực tiếp raw block device**, bỏ qua lớp filesystem để tối ưu I/O và checksum toàn phần. Vì vậy OSD cần device chưa format — khác Longhorn (dùng thư mục trên filesystem có sẵn `/var/lib/longhorn`). Đây là rào cản prerequisite lớn nhất khi thử Ceph trên kind/Docker.

**Cơ chế:** operator đọc `CephCluster.spec.storage` → với mỗi device khớp filter, sinh một `rook-ceph-osd-<id>` Deployment chạy `ceph-osd`. MON được đặt trên các node khác nhau (anti-affinity) để một node chết không mất quorum. `ceph-CSI` provisioner/attacher/nodeplugin được deploy để phục vụ PVC.

> 💡 **Ẩn dụ:** cài Rook operator như thuê một quản đốc kho biết dựng kho Ceph. Bạn đưa bản vẽ (`CephCluster`: "3 chốt bảo vệ MON, dùng mọi ổ cứng trống làm kệ OSD") — quản đốc tự gọi thợ dựng, không cần bạn chỉ tay từng viên gạch.

**Làm:**

```bash
# Bước 0 (kind-only): gắn raw disk cho mỗi node — OSD cần block device trống.
# Tạo file-backed loop device 10Gi trong từng node container.
for node in kind-control-plane kind-worker kind-worker2; do
  docker exec "$node" bash -c '
    dd if=/dev/zero of=/var/lib/ceph-osd.img bs=1M count=10240 2>/dev/null &&
    losetup -fP /var/lib/ceph-osd.img &&
    losetup -j /var/lib/ceph-osd.img'
done
```

```text
# mỗi node in ra loop device được cấp
/dev/loop8: [...] (/var/lib/ceph-osd.img)
```

> ⚠ Loop device không bền qua restart container. Trên bare-metal thật thì đây là ổ NVMe/SSD trống — bỏ hẳn Bước 0.

```bash
# Bước 1: cài Rook operator bằng Helm
helm repo add rook-release https://charts.rook.io/release
helm repo update
helm install rook-ceph rook-release/rook-ceph \
  --namespace rook-ceph --create-namespace \
  --version v1.15.6
kubectl -n rook-ceph rollout status deploy/rook-ceph-operator
```

```text
"rook-release" has been added to your repositories
NAME: rook-ceph
STATUS: deployed
deployment "rook-ceph-operator" successfully rolled out
```

```bash
# Bước 2: apply CephCluster — dùng mọi device trống (loop) làm OSD
cat > /tmp/ceph-cluster.yml <<'EOF'
apiVersion: ceph.rook.io/v1
kind: CephCluster
metadata:
  name: rook-ceph
  namespace: rook-ceph
spec:
  cephVersion:
    image: quay.io/ceph/ceph:v18.2.4
  dataDirHostPath: /var/lib/rook
  mon:
    count: 3
    allowMultiplePerNode: false
  mgr:
    count: 2
  dashboard:
    enabled: true
  storage:
    useAllNodes: true
    useAllDevices: false
    deviceFilter: "^loop8"
EOF
kubectl apply -f /tmp/ceph-cluster.yml
```

```text
cephcluster.ceph.rook.io/rook-ceph created
```

```bash
# Bước 3: đợi cluster hình thành (~5-8 phút — MON → MGR → OSD lần lượt Ready)
kubectl -n rook-ceph get pods
```

```text
NAME                                            READY   STATUS      RESTARTS   AGE
rook-ceph-mon-a-6c8fd9d7b8-x4k2p                1/1     Running     0          5m
rook-ceph-mon-b-7d9f4c6f9-9pl3q                 1/1     Running     0          4m
rook-ceph-mon-c-5f8b7d6c4-mz7rn                 1/1     Running     0          4m
rook-ceph-mgr-a-6b7d8f9c5-tk4wj                 1/1     Running     0          3m
rook-ceph-mgr-b-5c9d7e8f4-hb2xn                 1/1     Running     0          3m
rook-ceph-osd-0-7f8c9d6b5-qw3zk                 1/1     Running     0          2m
rook-ceph-osd-1-6d7e8f9c4-lm5vp                 1/1     Running     0          2m
rook-ceph-osd-2-8c9d7e6f5-nr8tj                 1/1     Running     0          2m
rook-ceph-osd-prepare-kind-worker-abc12         0/1     Completed   0          3m
csi-rbdplugin-provisioner-6f8c...               6/6     Running     0          4m
csi-cephfsplugin-provisioner-5d7...             6/6     Running     0          4m
```

```bash
# Bước 4: deploy toolbox rồi kiểm sức khỏe cluster
helm install rook-ceph-tools rook-release/rook-ceph-cluster \
  --namespace rook-ceph --set toolbox.enabled=true \
  --set cephClusterSpec.skipUpgradeChecks=true 2>/dev/null || \
kubectl -n rook-ceph apply -f https://raw.githubusercontent.com/rook/rook/release-1.15/deploy/examples/toolbox.yaml

kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status
```

```text
  cluster:
    id:     8f2a1b3c-4d5e-6f7a-8b9c-0d1e2f3a4b5c
    health: HEALTH_OK

  services:
    mon: 3 daemons, quorum a,b,c (age 5m)
    mgr: a(active, since 3m), standbys: b
    osd: 3 osds: 3 up (since 2m), 3 in (since 2m)

  data:
    pools:   1 pools, 1 pgs
    objects: 2 objects, 449 KiB
    usage:   80 MiB used, 30 GiB / 30 GiB avail
    pgs:     1 active+clean
```

→ **Verify:** `health: HEALTH_OK`, `mon: 3 daemons, quorum a,b,c`, `osd: 3 osds: 3 up ... 3 in`. Xem cây OSD:

```bash
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd tree
```

```text
ID  CLASS  WEIGHT   TYPE NAME                    STATUS  REWEIGHT
-1         0.02939  root default
-3         0.00980      host kind-control-plane
 2         0.00980          osd.2                    up   1.00000
-5         0.00980      host kind-worker
 0         0.00980          osd.0                    up   1.00000
-7         0.00980      host kind-worker2
 1         0.00980          osd.1                    up   1.00000
```

→ **Verify:** 3 OSD `up`, mỗi OSD nằm trên một **host** khác nhau → failure domain = host, CRUSH sẽ trải bản sao qua 3 node.

---

## 8. RBD block storage (RWO) — song song Longhorn

**Chốt:** tạo **CephBlockPool** (replication `size: 3`) và StorageClass provisioner `rook-ceph.rbd.csi.ceph.com`; PVC từ SC này cấp **RBD image** — block volume **RWO** giống Longhorn, nhưng bản sao được CRUSH trải theo failure domain thay vì round-robin đơn giản.

- **CephBlockPool** — pool RADOS dành cho RBD, `replicated.size: 3` = 3 bản mỗi object; `failureDomain: host` = 3 bản trên 3 host khác nhau.
- **RBD image** — mỗi PVC là một image thin-provisioned trong pool; kernel `rbd` map image thành `/dev/rbdX` trên node rồi mount vào Pod.
- **Access mode:** RBD filesystem chỉ **RWO** (giống Longhorn). RWX cần CephFS (mục 9).

**Vì sao:** khi so trực tiếp block-vs-block, RBD hơn Longhorn ở CRUSH-aware placement (failure domain, erasure coding tùy chọn), snapshot/clone nhanh ở tầng RADOS, và mirror sang cluster khác (RBD mirroring) cho DR. Đổi lại phải nuôi cả Ceph cluster.

**Cơ chế:** PVC bound → ceph-CSI provisioner gọi `rbd create` trong pool → tạo image. Khi Pod schedule, nodeplugin `rbd map` image → `/dev/rbd0`, format ext4/xfs, mount vào container. Ghi vào volume = ghi object vào pool, CRUSH phân tán 3 bản qua 3 OSD trên 3 host.

> 💡 **Ẩn dụ:** RBD image như một ổ đĩa ảo cắm vào đúng một máy (RWO). Ceph âm thầm xé ổ đó thành các mảnh object, rải 3 bản qua 3 kho theo bản đồ CRUSH — máy dùng vẫn thấy một ổ liền mạch.

**Làm:**

```bash
# Bước 1: tạo CephBlockPool + StorageClass RBD
cat > /tmp/ceph-rbd-sc.yml <<'EOF'
apiVersion: ceph.rook.io/v1
kind: CephBlockPool
metadata:
  name: replicapool
  namespace: rook-ceph
spec:
  failureDomain: host
  replicated:
    size: 3
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ceph-rbd
provisioner: rook-ceph.rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: replicapool
  imageFormat: "2"
  imageFeatures: layering
  csi.storage.k8s.io/fstype: ext4
allowVolumeExpansion: true
reclaimPolicy: Delete
EOF
kubectl apply -f /tmp/ceph-rbd-sc.yml
kubectl get storageclass
```

```text
cephblockpool.ceph.rook.io/replicapool created
storageclass.storage.k8s.io/ceph-rbd created

NAME                 PROVISIONER                     RECLAIMPOLICY   VOLUMEBINDINGMODE   ALLOWVOLUMEEXPANSION
ceph-rbd             rook-ceph.rbd.csi.ceph.com      Delete          Immediate           true
longhorn (default)   driver.longhorn.io              Delete          Immediate           true
longhorn-cnpg        driver.longhorn.io              Delete          Immediate           true
```

```bash
# Bước 2: PVC + Pod dùng ceph-rbd
cat > /tmp/rbd-demo.yml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: rbd-pvc
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: rbd-app
spec:
  containers:
  - name: app
    image: alpine
    command: ["/bin/sh", "-c", "sleep 3600"]
    volumeMounts:
    - { name: data, mountPath: /data }
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: rbd-pvc
EOF
kubectl apply -f /tmp/rbd-demo.yml
kubectl wait --for=condition=Ready pod/rbd-app --timeout=120s
kubectl get pvc rbd-pvc
```

```text
persistentvolumeclaim/rbd-pvc created
pod/rbd-app created
pod/rbd-app condition met

NAME      STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
rbd-pvc   Bound    pvc-2a7c9e1f-3b4d-5c6e-7f8a-9b0c1d2e3f4a   2Gi        RWO            ceph-rbd       15s
```

```bash
# Bước 3: xác nhận image nằm trong pool và replication size=3
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- rbd ls replicapool
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd pool get replicapool size
```

```text
csi-vol-2a7c9e1f-3b4d-5c6e-7f8a-9b0c1d2e3f4a
size: 3
```

→ **Verify:** RBD image tồn tại trong `replicapool`, `size: 3` → mỗi block ghi 3 bản, CRUSH trải qua 3 host. Access mode `RWO` — đúng bản chất block.

---

## 9. CephFS — filesystem RWX (cái Longhorn không làm được)

**Chốt:** tạo **CephFilesystem** (metadata pool + data pool + MDS) và StorageClass provisioner `rook-ceph.cephfs.csi.ceph.com`; PVC từ SC này cho volume **RWX** — **nhiều Pod trên nhiều node cùng mount đọc/ghi** một filesystem. Đây là năng lực Longhorn (chỉ RWO) không có.

- **CephFilesystem** — khai báo một filesystem POSIX: 1 metadata pool (MDS quản lý cây thư mục) + ≥1 data pool (lưu nội dung file).
- **RWX (ReadWriteMany)** — nhiều Pod ghi song song vào cùng volume, kể cả khác node. Hợp cho shared upload, web assets, CI artifact, WordPress `wp-content`.
- **MDS** — điều phối lock metadata để nhiều client ghi không đạp nhau; data đi thẳng client ↔ OSD, MDS không nghẽn.

**Vì sao:** rất nhiều workload cần shared filesystem: cụm web nhiều replica đọc/ghi chung thư mục media, pipeline ML chia sẻ dataset, Jenkins/GitLab artifact. Với Longhorn phải dựng thêm NFS server phía trên; Ceph có sẵn CephFS RWX native, nhất quán mạnh (không phải NFS eventual).

**Cơ chế:** PVC RWX bound → ceph-CSI tạo một **subvolume** trong CephFS. Mỗi Pod mount volume qua kernel `ceph` client; MDS cấp capability (lock) cho từng inode để nhiều client ghi an toàn. Data ghi thành object vào data pool, CRUSH trải 3 bản như RBD.

> 💡 **Ẩn dụ:** RBD như một ổ USB — chỉ cắm được vào một máy tại một thời điểm (RWO). CephFS như một ổ mạng phòng ban — cả team cùng mở, cùng lưu file vào (RWX), có "thủ thư" MDS điều phối để hai người sửa cùng thư mục không loạn.

| | RBD (mục 8) | CephFS (mục 9) |
|---|---|---|
| Kiểu | Block device | POSIX filesystem |
| Access mode | RWO | **RWX** (+ RWO/ROX) |
| Daemon riêng | Không | Cần **MDS** |
| Dùng cho | DB, volume 1-Pod | Shared media, artifact, nhiều Pod |
| Tương đương Longhorn | Có (Longhorn = RWO) | **Longhorn không có** |

**Làm:**

```bash
# Bước 1: tạo CephFilesystem + StorageClass CephFS
cat > /tmp/cephfs-sc.yml <<'EOF'
apiVersion: ceph.rook.io/v1
kind: CephFilesystem
metadata:
  name: myfs
  namespace: rook-ceph
spec:
  metadataPool:
    replicated:
      size: 3
  dataPools:
    - name: data0
      replicated:
        size: 3
  metadataServer:
    activeCount: 1
    activeStandby: true
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ceph-fs
provisioner: rook-ceph.cephfs.csi.ceph.com
parameters:
  clusterID: rook-ceph
  fsName: myfs
  pool: myfs-data0
reclaimPolicy: Delete
allowVolumeExpansion: true
EOF
kubectl apply -f /tmp/cephfs-sc.yml
kubectl -n rook-ceph get cephfilesystem
```

```text
cephfilesystem.ceph.rook.io/myfs created
storageclass.storage.k8s.io/ceph-fs created

NAME   ACTIVEMDS   AGE   PHASE
myfs   1           40s   Ready
```

```bash
# Bước 2: một PVC RWX, hai Pod trên (khả năng) hai node cùng mount
cat > /tmp/cephfs-rwx.yml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: shared-pvc
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ceph-fs
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shared-writer
spec:
  replicas: 2
  selector:
    matchLabels: { app: shared-writer }
  template:
    metadata:
      labels: { app: shared-writer }
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchLabels: { app: shared-writer }
              topologyKey: kubernetes.io/hostname
      containers:
      - name: w
        image: alpine
        command: ["/bin/sh","-c","while true; do echo \"$(hostname) $(date)\" >> /shared/log.txt; sleep 5; done"]
        volumeMounts:
        - { name: shared, mountPath: /shared }
      volumes:
      - name: shared
        persistentVolumeClaim:
          claimName: shared-pvc
EOF
kubectl apply -f /tmp/cephfs-rwx.yml
kubectl rollout status deploy/shared-writer
kubectl get pvc shared-pvc -o wide
```

```text
persistentvolumeclaim/shared-pvc created
deployment.apps/shared-writer created
deployment "shared-writer" successfully rolled out

NAME         STATUS   VOLUME         CAPACITY   ACCESS MODES   STORAGECLASS   AGE
shared-pvc   Bound    pvc-...        1Gi        RWX            ceph-fs        25s
```

```bash
# Bước 3: chứng minh RWX — hai Pod KHÁC NHAU cùng ghi vào một file
kubectl exec deploy/shared-writer -- sh -c 'sort -u /shared/log.txt | cut -d" " -f1 | sort -u'
```

```text
shared-writer-6c9d7f8b5-4k2xp
shared-writer-6c9d7f8b5-9pl3q
```

→ **Verify:** file `/shared/log.txt` chứa dòng từ **cả hai** Pod (hai hostname khác nhau) → nhiều Pod trên nhiều node ghi đồng thời vào **một** volume. `ACCESS MODES = RWX`. Thử làm điều này với Longhorn sẽ fail ở bước mount thứ hai (`Multi-Attach error`).

---

## 10. Longhorn vs Rook-Ceph — chọn cái nào

**Chốt:** không có cái "tốt hơn" tuyệt đối — **Longhorn tối ưu cho đơn giản + block RWO**; **Rook-Ceph tối ưu cho unified (block+file+object) + scale lớn**. Chọn theo nhu cầu thật, đừng theo hào quang.

**Vì sao:** đây là quyết định kiến trúc tốn kém để đảo ngược (migrate storage backend = di chuyển toàn bộ data). Chọn sai về phía Ceph = gánh vận hành nặng không cần thiết; chọn sai về phía Longhorn = đụng trần khi cần RWX/object/scale rồi phải thay giữa chừng.

| Tiêu chí | Longhorn | Rook-Ceph |
|---|---|---|
| Access mode | RWO (block) | RWO (RBD) + **RWX (CephFS)** + object (RGW S3) |
| Disk yêu cầu | Filesystem có sẵn (`/var/lib/longhorn`) | **Raw block device** cho OSD (BlueStore) |
| Data placement | Replica round-robin đơn giản | **CRUSH** — failure domain, erasure coding |
| Overhead (RAM/CPU) | Nhẹ | Nặng (MON/MGR/OSD/MDS nhiều daemon) |
| Scale thực dụng | ≤ ~20 node | Hàng trăm node, PB |
| Object storage (S3) | Không | Có (RGW) |
| Vận hành / học | Dễ, UI trực quan | Dốc, cần hiểu Ceph internals |
| Snapshot/backup | Tích hợp, backup ra S3/NFS | RBD snapshot/clone, RBD mirror (DR) |
| Phù hợp | Homelab, edge, cluster nhỏ-vừa | Enterprise, unified storage, đa dịch vụ |

**Quy tắc quyết định nhanh:**

- Chỉ cần **block RWO** cho vài stateful app, cluster nhỏ, muốn vận hành nhẹ → **Longhorn**.
- Cần **RWX** (shared filesystem nhiều Pod) hoặc **S3 object** trong cùng cluster, hoặc scale lớn → **Rook-Ceph**.
- Cần cả hai kiểu workload nhưng ngại nuôi Ceph → chạy **Longhorn cho block + MinIO cho object** (lab 21) là combo nhẹ, phổ biến ở cluster nhỏ-vừa.

**Dùng/KHÔNG:** đừng chạy Rook-Ceph trên < 3 node có raw disk (mất HA, mất ý nghĩa CRUSH). **Phản đề:** đừng ép Longhorn làm RWX bằng cách chồng NFS server tự dựng cho workload production quan trọng — CephFS làm việc đó native và nhất quán mạnh hơn.

> 💡 **Ẩn dụ:** Longhorn là xe bán tải — gọn, dễ lái, chở đủ việc nhà. Rook-Ceph là đoàn xe tải + kho logistics — chở được mọi loại hàng ở mọi quy mô, nhưng cần tài xế chuyên và chi phí vận hành. Nhà nhỏ mua đoàn xe tải là lãng phí; công ty logistics đi xe bán tải là nghẽn.

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

```bash
# Rook-Ceph: xóa workload test trước
kubectl delete pod rbd-app --ignore-not-found
kubectl delete deploy shared-writer --ignore-not-found
kubectl delete pvc rbd-pvc shared-pvc --ignore-not-found

# xóa CephFilesystem, CephBlockPool rồi CephCluster (đúng thứ tự — CR phụ trước)
kubectl -n rook-ceph delete cephfilesystem myfs --ignore-not-found
kubectl -n rook-ceph delete cephblockpool replicapool --ignore-not-found
kubectl -n rook-ceph delete cephcluster rook-ceph --ignore-not-found

# gỡ operator + CRD (uninstall hẳn Rook)
# helm uninstall rook-ceph-tools rook-ceph -n rook-ceph
# kubectl delete ns rook-ceph

# kind-only: tháo loop device đã gắn ở mục 7
# for n in kind-control-plane kind-worker kind-worker2; do
#   docker exec "$n" bash -c 'losetup -D; rm -f /var/lib/ceph-osd.img'
# done
```

> ⚠ Xóa `CephCluster` không tự wipe raw device — nếu dựng lại Ceph trên cùng disk, phải `sgdisk --zap-all` device trước, nếu không OSD prepare sẽ bỏ qua "disk đã có dữ liệu Ceph".

---

## ✅ Đủ khi

① Giải thích Longhorn là gì, replica trải đều trên node hoạt động thế nào, khác hostPath ở điểm gì.
② Cài Longhorn qua Helm, biết cần prerequisite gì (open-iscsi).
③ Phân biệt `numberOfReplicas` và `dataLocality` (disabled / best-effort / strict-local) — khi nào dùng cái nào.
④ Tạo snapshot trong-cluster và giải thích snapshot khác backup ra S3 thế nào.
⑤ Giải thích GOLDEN LESSON double-replicate: CNPG 3 instance + Longhorn replica-3 = 9 bản, tại sao dùng replica-1 `strict-local` tốt hơn.
⑥ Kể tên và vai trò các daemon Ceph (MON/MGR/OSD/MDS/RGW) và biết OSD cần raw block device, khác Longhorn dùng filesystem có sẵn.
⑦ Cài Rook operator + CephCluster, đọc `ceph status`/`ceph osd tree` để xác nhận HEALTH_OK và OSD trải theo failure domain host.
⑧ Tạo RBD StorageClass (RWO) và CephFS StorageClass (RWX); chứng minh nhiều Pod cùng ghi một CephFS volume — điều Longhorn không làm được.
⑨ Ra quyết định Longhorn vs Rook-Ceph theo tiêu chí (access mode, disk, scale, overhead) thay vì theo cảm tính.

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
11. RADOS là gì trong Ceph? Ba dịch vụ (block/file/object) quan hệ với nó thế nào?
12. Vai trò MON, MGR, OSD, MDS khác nhau ra sao? MDS cần cho dịch vụ nào?
13. Vì sao thường triển khai **3** MON (số lẻ)?
14. OSD yêu cầu gì về disk mà Longhorn không cần? Vì sao (BlueStore)?
15. CRUSH giải quyết vấn đề gì so với bảng lookup trung tâm? "Failure domain" nghĩa là gì?
16. RBD và CephFS khác nhau ở access mode thế nào? Cái nào cho RWX?
17. Provisioner của StorageClass RBD và CephFS trong Rook là gì?
18. Khi nào chọn Longhorn, khi nào chọn Rook-Ceph? Combo nhẹ thay cho Ceph khi cần cả block lẫn object là gì?

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
11. **RADOS** (Reliable Autonomic Distributed Object Store) là lớp nền lưu mọi thứ dưới dạng object, không có single point of failure. RBD (block), CephFS (file), RGW (object S3) đều là các "mặt tiền" dựng **trên** RADOS.
12. **MON** giữ cluster map + quorum (không lưu data). **MGR** lo metrics/dashboard/module. **OSD** lưu data thật (1/disk), xử lý replication/recovery. **MDS** giữ metadata filesystem — **chỉ cần cho CephFS**.
13. Số lẻ để đạt **quorum** đa số (Paxos): 3 MON chịu được mất 1 mà vẫn > 50% đồng thuận. Số chẵn không tăng khả năng chịu lỗi mà còn dễ split-brain.
14. OSD cần **raw block device chưa format**; Longhorn chỉ cần thư mục trên filesystem có sẵn. Vì **BlueStore** quản lý trực tiếp block device (bỏ lớp filesystem) để tối ưu I/O và checksum toàn phần.
15. **CRUSH** cho client **tự tính** vị trí object từ crushmap, không cần tra bảng trung tâm → không nghẽn, không SPOF khi scale. **Failure domain** = mức cô lập lỗi (host/rack/row) mà CRUSH bảo đảm các bản sao không rơi cùng một điểm — ví dụ `failureDomain: host` = 3 bản trên 3 host khác nhau.
16. **RBD** = block, chỉ **RWO** (như Longhorn). **CephFS** = POSIX filesystem, hỗ trợ **RWX** (nhiều Pod/nhiều node ghi cùng lúc). Cần RWX → CephFS.
17. RBD: `rook-ceph.rbd.csi.ceph.com`. CephFS: `rook-ceph.cephfs.csi.ceph.com`.
18. Longhorn khi chỉ cần block RWO, cluster nhỏ, vận hành nhẹ. Rook-Ceph khi cần RWX / object S3 / scale lớn / unified storage. Combo nhẹ khi cần cả block lẫn object mà ngại nuôi Ceph: **Longhorn (block) + MinIO (object S3, lab 21)**.

---

## Bắc cầu sang production

Trên cluster thật bare-metal, Longhorn thường là default storage: PVC của mọi stateful app (Postgres, Redis, Kafka, MinIO) đều đi qua Longhorn. Điều chỉnh theo workload:

- **Stateless app có state nhỏ** (Redis cache, session): dùng `longhorn` mặc định replica-3.
- **Database tự replicate** (CNPG, TiDB, Vitess): dùng `longhorn-cnpg` replica-1 `strict-local` — tiết kiệm disk 3×, tăng IOPS.
- **Monitoring stack** (Prometheus, Loki): replica-2 là đủ (balance giữa HA và disk cost).
- **Backup target**: cấu hình `backupTarget` trong Longhorn Settings trỏ vào MinIO S3 nội bộ (lab 21) hoặc S3 external — đây là lớp DR cuối cùng.

Snapshot trước mọi upgrade DB schema, backup ra S3 trước mọi maintenance cluster.

Khi nào leo lên **Rook-Ceph**: khi một Longhorn không còn đủ — cần **RWX** cho nhiều Pod dùng chung filesystem (media, artifact, ML dataset), cần **object S3 nội bộ** đặt cạnh block/file trên cùng cluster, hoặc scale vượt ~20 node. Điều kiện tiên quyết trên bare-metal: mỗi node góp OSD phải có **raw disk riêng** (NVMe/SSD trống), tối thiểu 3 node để CRUSH failure-domain = host có nghĩa. Đổi lại phải nuôi đội daemon MON/MGR/OSD/MDS và có người hiểu Ceph để xử lý `HEALTH_WARN`, rebalance, near-full OSD. Nhiều team giữ **Longhorn cho block + MinIO cho object** để tránh gánh nặng Ceph khi chưa thật cần unified storage.

---

## 📎 Nguồn

- [Longhorn Documentation](https://longhorn.io/docs/) — cài đặt, CRD, backup, disaster recovery.
- [Longhorn GitHub](https://github.com/longhorn/longhorn) — release notes, known issues.
- [CNPG + Longhorn best practices](https://cloudnative-pg.io/documentation/) — xem mục Storage.
- [CSI Spec](https://github.com/container-storage-interface/spec) — hiểu interface giữa K8s và storage plugin.
- [Rook Documentation](https://rook.io/docs/rook/latest/) — operator, CephCluster, CephBlockPool, CephFilesystem, Ceph-CSI.
- [Ceph Documentation](https://docs.ceph.com/en/latest/) — kiến trúc RADOS, CRUSH, BlueStore, MON/OSD/MDS.
- [Rook examples (GitHub)](https://github.com/rook/rook/tree/master/deploy/examples) — `cluster.yaml`, `storageclass.yaml`, `filesystem.yaml`, `toolbox.yaml`.
