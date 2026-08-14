# 16 · etcd backup & restore — biến "mất cụm" thành "khôi phục vài phút"

> **Chặng 7 · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: kubeadm — dựng cụm HA · kế tiếp: Cluster upgrade · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu vì sao etcd là "não bộ" của cụm; thực hiện full cycle backup → off-cluster → phá thử nghiệm → restore → verify; nắm gotcha của cụm HA; và đặt được cron tự động hóa.

**Nền:** module 15 (kubeadm) đã dựng cụm multipass — các lệnh trong lab chạy trên master node đó. etcd là nơi apiserver ghi mọi object K8s; hỏng etcd = mọi workload "mất trí nhớ".

> ⚠ **Lưu ý:** dùng cụm multipass đã dựng ở **lab 15** (RAM/topology cho Mac Mini M4 24 GB + cấu hình gọn: xem "Lưu ý phần cứng" đầu lab 15). **Output trong lab là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify lại số/IP khi thao tác.**

## Tiền đề
Truy cập master node:

```bash
multipass shell master-1
```

Cài `etcdctl` khớp phiên bản etcd đang chạy:

```bash
# xem version etcd đang chạy
kubectl -n kube-system get pod etcd-master-1 -o jsonpath='{.spec.containers[0].image}'
# → registry.k8s.io/etcd:3.5.x-0

# tải etcdctl đúng version (ví dụ 3.5.9)
ETCD_VER=v3.5.9
curl -L https://github.com/etcd-io/etcd/releases/download/${ETCD_VER}/etcd-${ETCD_VER}-linux-amd64.tar.gz \
  -o /tmp/etcd.tar.gz
tar -xzf /tmp/etcd.tar.gz -C /tmp/
sudo mv /tmp/etcd-${ETCD_VER}-linux-amd64/etcdctl /usr/local/bin/
etcdctl version
```

Xuất biến môi trường để không phải gõ cert path mỗi lần:

```bash
export ETCDCTL_API=3
export ETCDCTL_ENDPOINTS=https://127.0.0.1:2379
export ETCDCTL_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCDCTL_CERT=/etc/kubernetes/pki/etcd/server.crt
export ETCDCTL_KEY=/etc/kubernetes/pki/etcd/server.key
```

Kiểm tra kết nối:

```bash
etcdctl member list --write-out=table
```

```text
+------------------+---------+----------+---------------------------+---------------------------+------------+
|        ID        | STATUS  |   NAME   |        PEER ADDRS         |       CLIENT ADDRS        | IS LEARNER |
+------------------+---------+----------+---------------------------+---------------------------+------------+
| 8e9e05c52164694d | started | master-1 | https://192.168.64.10:2380 | https://192.168.64.10:2379 | false      |
+------------------+---------+----------+---------------------------+---------------------------+------------+
```

→ **Verify:** thấy ít nhất 1 member `STATUS=started`. Nếu lỗi permission, thêm `sudo -E` trước `etcdctl`.

---

## 1. etcd là gì — nguồn sự thật của cụm

**Chốt:** etcd là **distributed key-value store** lưu toàn bộ trạng thái K8s — mọi Pod, Service, ConfigMap, Secret, RBAC… đều là entry trong etcd. apiserver là **client duy nhất** ghi/đọc etcd; các component khác (scheduler, controller manager) đọc qua apiserver. Mất etcd = mất cụm.

- **etcd** = embedded distributed database, dùng giao thức **Raft** để đảm bảo consistency trên nhiều node.
- Dữ liệu lưu tại `/var/lib/etcd` trên master (hoặc external etcd node nếu topology tách biệt).
- **Quorum** cần ≥ `floor(n/2)+1` node healthy — cụm 3 master cần ≥ 2 etcd live; cụm 5 master cần ≥ 3.
- etcd lưu theo **revision** — mỗi write tăng revision global; snapshot save chụp revision tại thời điểm đó.

**Vì sao:** apiserver là stateless — nó không giữ state trong RAM; mọi write được persist xuống etcd ngay lập tức. Khi master restart, apiserver đọc lại toàn bộ state từ etcd và cụm phục hồi. Nếu etcd mất dữ liệu, apiserver không biết có bao nhiêu Pod, Service nào đang sống — toàn bộ desired state biến mất.

**Cơ chế:** etcd expose gRPC API trên port `2379` (client) và `2380` (peer replication). apiserver kết nối với TLS mutual auth — cert ở `/etc/kubernetes/pki/etcd/`. Các controller watch API server qua `informer` (long-poll), không trực tiếp kết nối etcd. Dữ liệu trong etcd được mã hóa khi bật `encryption-config` (K8s secret encryption at rest).

> 💡 **Ẩn dụ:** etcd là **sổ đăng bộ** của cụm — mọi "giấy tờ" (Pod spec, Secret, Rule) đều nằm đó. apiserver là "cán bộ một cửa" — ai cần đọc/ghi phải qua anh ta. Mất sổ = mất toàn bộ hồ sơ dù nhà máy (worker node) vẫn đang chạy.

| Thành phần | Vai trò với etcd |
|---|---|
| kube-apiserver | Client duy nhất — ghi/đọc mọi object |
| etcd | Key-value store — lưu trữ toàn bộ state |
| etcdctl | CLI admin — backup, restore, member mgmt |
| /etc/kubernetes/pki/etcd/ | TLS cert cho mutual auth |

**Dùng / không dùng:**
- Truy cập trực tiếp etcd bằng etcdctl: chỉ để **admin** (backup, restore, debug) — không bao giờ ghi trực tiếp vào etcd bỏ qua apiserver.
- **Phản đề:** ghi trực tiếp vào etcd bypasses validation/admission webhook → tạo object invalid hoặc gây corruption. Mọi thay đổi state phải đi qua `kubectl` / apiserver.

**Làm:**

```bash
# xem key etcd lưu object nào (prefix-based)
etcdctl get / --prefix --keys-only | head -20
```

```text
/registry/apiextensions.k8s.io/customresourcedefinitions/...
/registry/configmaps/default/kube-root-ca.crt
/registry/namespaces/default
/registry/namespaces/kube-system
/registry/pods/kube-system/coredns-787d4945fb-2qx7p
/registry/pods/kube-system/etcd-master-1
/registry/secrets/kube-system/bootstrap-token-...
/registry/services/endpoints/default/kubernetes
```

→ **Verify:** thấy prefix `/registry/` với các sub-key theo kind (pods, secrets, namespaces…). Số lượng key phản ánh đúng số object đang có trong cụm (`kubectl get all -A` để đối chiếu).

![[etcd-backup-restore.excalidraw]]

---

## 2. snapshot save

**Chốt:** `etcdctl snapshot save` chụp toàn bộ database etcd tại một revision thành 1 file binary `.db`; file này chứa đủ dữ liệu để restore cụm về đúng trạng thái đó. `snapshot status` kiểm tra tính toàn vẹn.

- Snapshot là **point-in-time copy** — consistent tại 1 revision; không lock etcd trong quá trình save.
- File output thường 5–100 MB tùy số object trong cụm.
- `snapshot status` trả về hash, revision, số key, size — dùng để verify file không corrupt.
- Cần đủ 3 flag TLS: `--cacert`, `--cert`, `--key` (nếu đã export biến env ở trên thì không cần lặp).

**Vì sao:** không có snapshot → không có cách restore toàn bộ state mà không cần rebuild từ đầu (mất hàng ngày config, RBAC, Secret). Snapshot nhỏ (vài chục MB) nhưng giá trị bằng toàn bộ cụm.

**Cơ chế:** etcdctl gọi gRPC `Snapshot()` API — etcd stream toàn bộ B-tree database ra file với checksum. Vì dùng **MVCC** (multi-version concurrency control), etcd có thể snapshot mà không block read/write đang diễn ra; snapshot chụp tại revision hiện tại khi lệnh bắt đầu.

> 💡 **Ẩn dụ:** snapshot save = chụp ảnh toàn bộ trang sổ đăng bộ tại thời điểm T — kể cả chữ đang được viết dở. Ảnh đó lưu vào USB (file `.db`) để sau này in lại.

| Flag | Bắt buộc | Ý nghĩa |
|---|---|---|
| `--endpoints` | Có | etcd endpoint (`https://127.0.0.1:2379`) |
| `--cacert` | Có | CA cert để verify server |
| `--cert` | Có | Client cert (identity) |
| `--key` | Có | Client private key |

**Dùng / không dùng:**
- Chạy `snapshot save` trên **master node chứa etcd**, không phải từ máy remote (cert path chỉ đúng trên master).
- **Phản đề:** chạy snapshot khi etcd đang bị áp lực cao (election, leader change) có thể tạo snapshot chậm; không hỏng dữ liệu nhưng nên tránh thời điểm cụm đang có incident.

**Làm:**

```bash
sudo -E etcdctl snapshot save /tmp/backup.db

# xem metadata của snapshot vừa tạo
sudo -E etcdctl snapshot status /tmp/backup.db --write-out=table
```

**Kết quả:**

```text
$ sudo -E etcdctl snapshot save /tmp/backup.db
{"level":"info","ts":"2026-08-13T10:15:22.341Z","caller":"snapshot/v3_snapshot.go:68","msg":"created temporary db file","path":"/tmp/backup.db.part"}
{"level":"info","ts":"2026-08-13T10:15:22.389Z","caller":"snapshot/v3_snapshot.go:79","msg":"saved","path":"/tmp/backup.db"}
Snapshot saved at /tmp/backup.db

$ sudo -E etcdctl snapshot status /tmp/backup.db --write-out=table
+----------+----------+------------+------------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
+----------+----------+------------+------------+
| 9a2d8f1c |     1842 |        412 |     2.7 MB |
+----------+----------+------------+------------+
```

→ **Verify:** dòng `Snapshot saved at /tmp/backup.db`; `snapshot status` trả về HASH, REVISION (khớp với `etcdctl endpoint status`), TOTAL KEYS > 0, TOTAL SIZE hợp lý. HASH không rỗng = file không corrupt.

---

## 3. Off-cluster — đẩy snapshot ra ngoài

**Chốt:** snapshot nằm trên cùng node etcd **không có giá trị** — node đó chết (đĩa hỏng, VM bay) thì backup cũng mất theo. Backup có nghĩa = phải **nằm ở nơi khác** so với dữ liệu gốc: S3, MinIO, NFS, hay ít nhất là máy khác qua scp.

- Rule tối thiểu: backup phải ở **ít nhất 1 location ngoài node etcd**.
- Rule 3-2-1: 3 bản, 2 medium khác nhau, 1 bản offsite.
- Cron job trên master node chạy snapshot + upload định kỳ (hàng giờ hoặc hàng đêm tùy RTO/RPO).
- `mc` = MinIO client — tương thích S3 API; cài 1 binary, không cần SDK.

**Vì sao:** "tôi có backup" mà backup nằm trên cùng node bị hỏng = không có backup. Đây là lỗi thường gặp nhất trong backup etcd trên homelab — snapshot cứu được khi etcd corrupt nhưng không cứu được khi node mất.

**Cơ chế:** `mc cp` gọi S3 `PutObject` API, upload binary file với multipart nếu >5 MB. MinIO có thể self-host trên VM riêng trong cùng mạng (không cần cloud). Ngoài ra, `scp` hoặc `rsync` sang máy khác trong cùng datacenter cũng đủ cho nhiều trường hợp.

> 💡 **Ẩn dụ:** backup trên cùng node = chụp ảnh sổ đăng bộ rồi kẹp vào quyển sổ đó — cháy quyển sổ là mất cả ảnh. Off-cluster = scan và lưu lên cloud drive khác tòa nhà.

| Phương pháp | Ưu | Nhược |
|---|---|---|
| `mc cp` → S3/MinIO | Tự động hóa dễ, retention policy | Cần S3 endpoint |
| `scp` → máy khác | Không cần infra thêm | Thủ công hoặc cần ssh key |
| NFS mount | Đơn giản | Single point of failure nếu NFS chết |

**Dùng / không dùng:**
- Cụm production: bắt buộc S3 hoặc object storage có versioning.
- **Phản đề:** đừng dùng cùng cụm K8s chứa MinIO để lưu backup etcd của chính cụm đó — khi cụm chết, MinIO cũng chết theo.

**Làm:**

Cài MinIO client (một lần):

```bash
# trên master node
curl -O https://dl.min.io/client/mc/release/linux-amd64/mc
chmod +x mc && sudo mv mc /usr/local/bin/
mc --version
```

Giả sử đã có MinIO endpoint tại `http://10.0.0.5:9000` (máy riêng):

```bash
# cấu hình alias
mc alias set myminio http://10.0.0.5:9000 minioadmin minioadmin

# upload snapshot
mc cp /tmp/backup.db myminio/etcd-backups/$(date +%Y%m%d-%H%M%S)-backup.db

# verify file đã lên
mc ls myminio/etcd-backups/
```

```text
$ mc cp /tmp/backup.db myminio/etcd-backups/20260813-101522-backup.db
...backup.db: 2.7 MiB / 2.7 MiB  ████████████  100%

$ mc ls myminio/etcd-backups/
[2026-08-13 10:15:30 UTC]  2.7MiB STANDARD 20260813-101522-backup.db
```

Nếu chỉ có `scp` (không có MinIO):

```bash
scp /tmp/backup.db user@backup-server:/backups/etcd/$(date +%Y%m%d-%H%M%S)-backup.db
```

Đặt cron tự động trên master node (`crontab -e`):

```bash
# backup etcd lúc 2h sáng mỗi ngày, giữ 7 ngày
0 2 * * * export ETCDCTL_API=3; etcdctl snapshot save /tmp/etcd-$(date +\%Y\%m\%d).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key && \
  mc cp /tmp/etcd-$(date +\%Y\%m\%d).db myminio/etcd-backups/ && \
  find /tmp -name 'etcd-*.db' -mtime +7 -delete
```

→ **Verify:** `mc ls myminio/etcd-backups/` thấy file đúng timestamp và size. Sau khi đặt cron, kiểm tra bằng `crontab -l` thấy entry.

---

## 4. snapshot restore

**Chốt:** `etcdctl snapshot restore` tạo ra một **data directory mới** từ snapshot — nó không ghi đè `/var/lib/etcd` tại chỗ. Sau khi restore, phải sửa static pod manifest `/etc/kubernetes/manifests/etcd.yaml` để etcd dùng data dir mới, rồi kubelet tự restart etcd.

- `snapshot restore` tạo `/var/lib/etcd-restore/` (hoặc path tùy chọn) chứa data directory mới.
- **Không tự động** thay thế etcd đang chạy — phải sửa `--data-dir` trong `etcd.yaml` thủ công.
- kubelet watch `/etc/kubernetes/manifests/` — khi file YAML thay đổi, kubelet tự restart static pod trong vài giây.
- **Gotcha cụm HA:** restore phải chạy trên **MỌI master** và phải **dừng apiserver** trên tất cả trước — nếu không, apiserver tiếp tục ghi vào etcd node khác và tạo split-brain.

**Vì sao:** restore tạo data dir mới (không ghi đè) là an toàn — nếu restore lỗi, `/var/lib/etcd` cũ vẫn còn, chỉ cần revert `etcd.yaml`. Ghi đè trực tiếp là bất hồi phục nếu có sự cố.

**Cơ chế:** `snapshot restore` ghi lại B-tree từ snapshot binary vào data dir mới, đặt lại cluster ID và member ID mới (tránh xung đột với cluster cũ nếu chạy song song). Sau khi sửa `etcd.yaml`, kubelet detect file change qua inotify → stop container etcd cũ → start container etcd mới với `--data-dir` mới → etcd đọc data từ snapshot.

> 💡 **Ẩn dụ:** restore = in lại sổ đăng bộ từ bản scan — tạo ra quyển sổ mới ở ngăn bên (`/var/lib/etcd-restore`). Sau đó dán biển "sổ chính thức" vào quyển mới (sửa `etcd.yaml`) và đuổi người giữ sổ cũ về nhà (kubelet restart etcd).

| Bước | Lệnh / Hành động |
|---|---|
| 1. Tạo data dir mới | `etcdctl snapshot restore backup.db --data-dir=/var/lib/etcd-restore` |
| 2. Sửa manifest | `etcd.yaml`: `--data-dir=/var/lib/etcd-restore` + `hostPath.path=/var/lib/etcd-restore` |
| 3. Restart kubelet | `systemctl restart kubelet` |
| 4. Verify | `kubectl get nodes`, `etcdctl member list` |

**Dùng / không dùng:**
- Restore: chỉ khi etcd data corrupt hoặc cluster mất state — không phải cho rolling upgrade.
- **Phản đề:** đừng restore trên 1 master của cụm HA trong khi 2 master kia vẫn đang chạy etcd bình thường — sẽ tạo quorum mới xung đột quorum cũ → split-brain → cluster ngừng nhận write.

**Làm — thí nghiệm đầy đủ (tạo → backup → phá → restore → verify):**

**Bước A — tạo object thử nghiệm:**

```bash
kubectl create configmap test-restore \
  --from-literal=message="nếu bạn thấy cái này sau restore, nó đã thành công"
kubectl get configmap test-restore
```

```text
NAME           DATA   AGE
test-restore   1      3s
```

**Bước B — backup:**

```bash
sudo -E etcdctl snapshot save /tmp/before-delete.db
```

```text
Snapshot saved at /tmp/before-delete.db
```

**Bước C — xoá object (mô phỏng "mất state"):**

```bash
kubectl delete configmap test-restore
kubectl get configmap test-restore   # phải báo không tìm thấy
```

```text
Error from server (NotFound): configmaps "test-restore" not found
```

**Bước D — restore:**

```bash
# dừng apiserver trước (di chuyển manifest ra ngoài thư mục watch)
sudo mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/

# restore snapshot vào data dir mới
sudo -E etcdctl snapshot restore /tmp/before-delete.db \
  --data-dir=/var/lib/etcd-restore

# sửa etcd.yaml trỏ data-dir mới
sudo sed -i 's|/var/lib/etcd|/var/lib/etcd-restore|g' \
  /etc/kubernetes/manifests/etcd.yaml

# kubelet tự restart etcd (watch manifest change)
# đợi etcd up (~10s), kiểm tra:
sudo -E etcdctl member list

# đưa apiserver về lại
sudo mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/
```

```text
$ sudo -E etcdctl snapshot restore /tmp/before-delete.db \
    --data-dir=/var/lib/etcd-restore
{"level":"info","ts":"2026-08-13T10:30:01.123Z","msg":"restoring snapshot","path":"/tmp/before-delete.db","wal-dir":"/var/lib/etcd-restore/member/wal","data-dir":"/var/lib/etcd-restore"}
{"level":"info","ts":"2026-08-13T10:30:01.891Z","msg":"finished restoring snapshot","path":"/tmp/before-delete.db","wal-dir":"/var/lib/etcd-restore/member/wal","data-dir":"/var/lib/etcd-restore"}
```

**Bước E — verify:**

```bash
# đợi apiserver sẵn sàng (có thể mất 30-60s)
kubectl get nodes

kubectl get configmap test-restore
kubectl get configmap test-restore -o jsonpath='{.data.message}'
```

**Kết quả:**

```text
$ kubectl get nodes
NAME       STATUS   ROLES           AGE   VERSION
master-1   Ready    control-plane   2d    v1.28.0

$ kubectl get configmap test-restore
NAME           DATA   AGE
test-restore   1      8m    ← configmap đã xoá giờ hiện lại

$ kubectl get configmap test-restore -o jsonpath='{.data.message}'
nếu bạn thấy cái này sau restore, nó đã thành công
```

→ **Verify:** `test-restore` configmap xuất hiện lại sau restore — state đã quay về thời điểm backup. `kubectl get nodes` STATUS=Ready xác nhận apiserver đã kết nối etcd mới.

---

## 5. Verify restore + test-restore định kỳ

**Chốt:** backup chưa được test-restore = chưa có backup thật. File `.db` có thể corrupt, restore process có thể fail vì sai flag, data dir có thể bị permission sai — chỉ khi restore thành công và object xuất hiện lại thì backup mới có giá trị.

- Sau restore, **object bị xoá sau thời điểm backup sẽ biến mất** (bình thường — đây là điểm-in-time restore).
- Object tồn tại trước backup **sẽ hiện lại** — đây là verify chính.
- Test-restore nên chạy trên **cụm staging riêng biệt** (không làm trên prod để tránh downtime).
- Lịch: test-restore ít nhất 1 lần/tháng; sau mỗi upgrade K8s; sau mỗi thay đổi lớn về config.

**Vì sao:** backup thường bị phát hiện là hỏng đúng lúc cần restore nhất — có thể do: cert hết hạn (không kết nối được etcd), snapshot corrupt (network lỗi khi upload), data-dir permission sai (etcd không đọc được). Test-restore định kỳ phát hiện sớm tất cả các vấn đề này.

**Cơ chế:** test-restore trên staging = chạy `etcdctl snapshot restore` + sửa manifest + verify một số object đặc trưng (namespace count, secret count khớp, một số workload cụ thể xuất hiện). Không cần cụm staging giống hệt prod — chỉ cần đủ để verify quy trình hoạt động và file `.db` không corrupt.

> 💡 **Ẩn dụ:** bảo hiểm hỏa hoạn không có giá trị gì nếu chưa bao giờ diễn tập thực tế. "Drill" backup restore = diễn tập chữa cháy — phát hiện bình cứu hỏa hết gas *trước* khi cháy thật.

| Kiểm tra sau restore | Lệnh | Kết quả kỳ vọng |
|---|---|---|
| Cluster health | `etcdctl endpoint health` | `healthy` |
| Member list | `etcdctl member list` | Đủ số member |
| Namespace count | `kubectl get ns \| wc -l` | Khớp trước restore |
| Object marker | `kubectl get configmap test-restore` | Có (nếu tạo trước backup) |
| Node status | `kubectl get nodes` | Ready |

**Dùng / không dùng:**
- Test-restore trên staging: **bắt buộc** với cluster production.
- **Phản đề:** "restore sẽ làm khi cần" — không có drill thì khi cần thực sự sẽ mất 4-8 tiếng thay vì 15 phút vì gặp lỗi chưa từng thấy (cert hết hạn, data dir permission, sai path).

**Làm — verify đầy đủ sau restore:**

```bash
# kiểm tra etcd health
sudo -E etcdctl endpoint health --write-out=table

# đếm key trong etcd (so sánh với trước restore)
sudo -E etcdctl get / --prefix --keys-only | wc -l

# kiểm tra object marker
kubectl get configmap test-restore -o yaml | grep message

# kiểm tra workload hệ thống vẫn chạy
kubectl -n kube-system get pods
```

**Kết quả:**

```text
$ sudo -E etcdctl endpoint health --write-out=table
+---------------------------+--------+-------------+-------+
|         ENDPOINT          | HEALTH |    TOOK     | ERROR |
+---------------------------+--------+-------------+-------+
| https://127.0.0.1:2379    |   true | 3.127699ms  |       |
+---------------------------+--------+-------------+-------+

$ sudo -E etcdctl get / --prefix --keys-only | wc -l
412

$ kubectl get configmap test-restore -o yaml | grep message
    message: nếu bạn thấy cái này sau restore, nó đã thành công

$ kubectl -n kube-system get pods
NAME                               READY   STATUS    RESTARTS   AGE
coredns-787d4945fb-2qx7p           1/1     Running   0          2d
etcd-master-1                      1/1     Running   1          2d
kube-apiserver-master-1            1/1     Running   1          2d
kube-controller-manager-master-1   1/1     Running   1          2d
kube-proxy-8xvn2                   1/1     Running   0          2d
kube-scheduler-master-1            1/1     Running   1          2d
```

→ **Verify:** `HEALTH=true`; key count 412 khớp snapshot status; configmap test-restore có value đúng; tất cả system pod `Running`. RESTARTS=1 ở apiserver/etcd là bình thường (do bị restart khi restore).

---

## 🧹 Dọn dẹp

```bash
# xoá configmap test
kubectl delete configmap test-restore --ignore-not-found

# xoá snapshot tạm (đã đẩy off-cluster rồi mới xoá)
sudo rm -f /tmp/backup.db /tmp/before-delete.db

# nếu đã restore và muốn quay về data dir gốc:
# sudo sed -i 's|/var/lib/etcd-restore|/var/lib/etcd|g' \
#   /etc/kubernetes/manifests/etcd.yaml
# sudo systemctl restart kubelet
```

---

## ✅ Đủ khi

① Giải thích được etcd lưu gì và vì sao apiserver là client duy nhất; nêu được hậu quả khi etcd mất dữ liệu.
② Chạy `etcdctl snapshot save` với đúng 3 flag cert và `snapshot status` để verify file.
③ Giải thích tại sao snapshot trên cùng node = vô nghĩa và nêu ít nhất 1 phương án off-cluster.
④ Thực hiện full cycle: tạo object → backup → xoá object → restore → thấy lại object; giải thích từng bước.
⑤ Nêu được 2 gotcha của cụm HA khi restore (dừng apiserver trước; restore trên MỌI master).

---

## 🧠 Recall

1. etcd lưu loại dữ liệu gì? Ai là client được phép ghi vào etcd?
2. etcd chạy trên port nào? TLS cert lấy từ đâu trên master kubeadm?
3. `etcdctl snapshot save` tạo ra file gì? Dùng `snapshot status` để kiểm tra những thông tin nào?
4. Vì sao backup snapshot mà đặt trên cùng node etcd thì coi như không có backup?
5. `etcdctl snapshot restore` làm gì? Nó có ghi đè `/var/lib/etcd` không?
6. Sau khi restore tạo data dir mới, phải làm thêm bước nào để etcd thật sự dùng data đó?
7. Kubelet detect thay đổi `etcd.yaml` bằng cơ chế nào và làm gì tiếp theo?
8. Cụm HA 3 master: trước khi restore phải làm gì? Và restore trên bao nhiêu master?
9. Sau restore, object nào sẽ hiện lại? Object nào sẽ biến mất?
10. "Backup chưa test-restore = chưa có backup" — nêu 3 loại lỗi mà chỉ test-restore mới phát hiện được.

### Đáp án

1. etcd lưu toàn bộ state K8s: Pod, Service, ConfigMap, Secret, RBAC, Node, Namespace… dưới dạng key-value với prefix `/registry/`. **kube-apiserver** là client duy nhất được phép ghi — các component khác đọc/ghi qua apiserver.
2. Port `2379` (client) và `2380` (peer). Cert ở `/etc/kubernetes/pki/etcd/`: `ca.crt`, `server.crt`, `server.key`.
3. Tạo file binary `.db` (snapshot database). `snapshot status` trả về: HASH (checksum tính toàn vẹn), REVISION (thời điểm chụp), TOTAL KEYS (số object), TOTAL SIZE.
4. Vì node hỏng (đĩa lỗi, VM mất) thì cả etcd data lẫn backup file đều biến mất cùng lúc — backup không còn giá trị. Backup phải nằm ở location khác hoàn toàn.
5. `snapshot restore` tạo ra **data directory mới** tại path chỉ định (vd `/var/lib/etcd-restore`). **Không** ghi đè `/var/lib/etcd` — data cũ vẫn còn nguyên.
6. Sửa `/etc/kubernetes/manifests/etcd.yaml`: thay `--data-dir` và `hostPath.path` trỏ về path mới. Sau đó restart kubelet để apply.
7. kubelet dùng **inotify** watch thư mục `/etc/kubernetes/manifests/` — khi file thay đổi, kubelet stop static pod cũ (container etcd) và start lại với config mới.
8. Trước khi restore: **dừng apiserver** trên tất cả master (di chuyển `kube-apiserver.yaml` ra khỏi manifests). Restore phải chạy trên **tất cả** master trong cụm HA — mỗi master một lần `etcdctl snapshot restore` với `--data-dir` mới.
9. Object tồn tại **trước thời điểm backup** sẽ hiện lại. Object tạo **sau backup và trước lúc mất** sẽ biến mất (đây là bình thường — point-in-time restore). Object bị xoá **trước backup** sẽ không hiện lại.
10. Ba loại lỗi chỉ test-restore mới phát hiện: (1) **cert hết hạn** — không kết nối được etcd khi restore; (2) **file corrupt** — upload S3 bị ngắt giữa chừng, hash mismatch khi restore; (3) **permission data dir** — etcd container không có quyền đọc `/var/lib/etcd-restore` → pod CrashLoopBackOff.

---

## Bắc cầu sang production

Trên cụm thật, backup etcd là SLA tối thiểu trước khi onboard bất kỳ workload nào:

- **Cron snapshot + đẩy S3:** mỗi giờ hoặc mỗi 6 tiếng tùy RPO; dùng S3 lifecycle để xoá bản cũ hơn 30 ngày tự động.
- **Restore-test định kỳ:** ít nhất mỗi quý trên staging; sau mỗi K8s upgrade; ghi kết quả vào runbook.
- **Cụm HA restore — thứ tự chuẩn:** (1) dừng apiserver tất cả master, (2) `snapshot restore` trên master-1 với `--name master-1 --initial-cluster ...` (phải khai báo initial cluster đầy đủ), (3) lặp lại cho master-2, master-3, (4) sửa `etcd.yaml` trên từng master, (5) start apiserver lần lượt, (6) `etcdctl member list` verify quorum.
- **Encrypt snapshot:** nếu snapshot chứa Secret (không encrypt at rest), cân nhắc `gpg --symmetric` trước khi upload S3.
- **Alerting:** monitor `etcd_server_leader_changes_seen_total` (leader churn cao) và `etcd_disk_wal_fsync_duration_seconds` (I/O latency) — hai metric này cảnh báo etcd sắp có vấn đề trước khi corrupt.

---

## 📎 Nguồn & xem lại

- [course-catalog](../../wiki/notebook/k8s/course-catalog.md) — vị trí module trong lộ trình
- [Backing up an etcd cluster](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/#backing-up-an-etcd-cluster) — kubernetes.io/docs
- [Restoring an etcd cluster](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/#restoring-an-etcd-cluster) — kubernetes.io/docs
- [etcd disaster recovery](https://etcd.io/docs/v3.5/op-guide/recovery/) — etcd.io/docs
