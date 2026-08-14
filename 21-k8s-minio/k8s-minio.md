# 21 · MinIO — S3 object storage phân tán

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Longhorn — block storage · kế tiếp: Operator/CRD + CloudNativePG · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu MinIO là gì và khác gì block/file storage; nắm cơ chế erasure coding; cài được MinIO distributed trên kind-lab 3-node bằng Helm; tạo bucket, dùng `mc` client để upload/download, quản access key; và hiểu tại sao **không đặt MinIO trên Longhorn**.
**Nền:** đã qua lab Longhorn (block storage, replica 3). MinIO là tầng tiếp theo — object storage, tự quản redundancy bằng erasure coding, dùng làm backend cho registry, backup, artifact store.

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

## ⚙️ Tiền đề

```bash
# 1. kind-lab 3-node đang Ready
kubectl get nodes
# NAME                 STATUS   ROLES           AGE
# kind-control-plane   Ready    control-plane   10m
# kind-worker          Ready    <none>          10m
# kind-worker2         Ready    <none>          10m

# 2. Helm đã cài
helm version --short    # v3.x.x

# 3. mc client (MinIO Client)
brew install minio/stable/mc         # macOS
# hoặc: curl -sSL https://dl.min.io/client/mc/release/linux-arm64/mc -o /usr/local/bin/mc && chmod +x /usr/local/bin/mc

# 4. Tạo namespace
kubectl create namespace minio-dev

# 5. Thêm Helm repo MinIO Operator
helm repo add minio-operator https://operator.min.io
helm repo update
```

---

## 1. MinIO là gì — S3-compatible object storage

**Chốt:** MinIO là **object storage tự host, S3-compatible** — lưu dữ liệu dưới dạng *object* (file + metadata + ID), trả về qua API HTTP giống hệt AWS S3. Dùng thay S3 cho workload on-prem, hoặc làm backend cho registry/backup/MLflow mà không cần AWS.

- **Object storage** = đơn vị là *object* (blob dữ liệu + metadata + key) trong *bucket*, không có khái niệm thư mục thật. Truy cập qua HTTP API (PUT/GET/DELETE).
- **Block storage** (Longhorn, PVC) = disk ảo, cần mount vào Pod, app đọc/ghi như file system — hợp DB.
- **File storage** (NFS, CephFS) = thư mục chia sẻ, mount qua network, nhiều client đọc cùng lúc — hợp media/home-dir.
- MinIO **triển khai API S3** — bất kỳ SDK/tool nào hỗ trợ S3 (`boto3`, `aws cli`, `rclone`, Velero, Harbor) đều dùng được với MinIO mà **không đổi code**.
- Dùng làm backend: artifact registry, Velero backup, MLflow, Loki, Thanos — đây là lý do MinIO phổ biến trong stack platform K8s.

**Vì sao:** không phải tổ chức nào cũng muốn trả tiền AWS S3 hoặc có thể đẩy data ra cloud (compliance, latency). MinIO cho object storage hiệu năng cao, on-prem, giữ API S3 nên không lock-in code.

**Cơ chế:** MinIO server expose HTTP endpoint. Object lưu vào disk dưới dạng file (key → đường dẫn vật lý). Metadata + bucket policy lưu trong `.minio.sys/` trên mỗi drive. Không cần external metadata DB — MinIO tự quản. Distributed mode: nhiều server + nhiều drive tham gia cùng erasure set, chia nhỏ object thành shard và phân phối.

> 💡 **Ẩn dụ:** Block storage = ổ cứng gắn vào máy (dùng 1 người). File storage = ổ mạng chia sẻ cả văn phòng (nhiều người mount cùng lúc). Object storage = kho chứa đồ hộp (mỗi hộp có mã vạch/ID, gửi/lấy qua quầy HTTP, không cần vào tận kho).

| Loại | Đơn vị | Truy cập | Hợp cho |
|---|---|---|---|
| Block (Longhorn/PVC) | sector/block | mount vào fs | DB, stateful app |
| File (NFS/CephFS) | file/folder | network mount | media, home-dir, shared config |
| Object (MinIO/S3) | object/blob | HTTP API | backup, artifact, ML dataset, log |

**Dùng / không dùng:**
- Dùng MinIO khi cần lưu file lớn (binary, backup, video, model), truy cập qua HTTP, không cần fs mount.
- Không dùng thay PVC cho PostgreSQL/MySQL — DB cần POSIX filesystem, không phải HTTP object API.
- **Phản đề:** đừng nhét file nhỏ, nhiều transaction write ngẫu nhiên (IOPS cao) vào object storage — S3 API có latency per-request, không tối ưu như block disk; DB trên MinIO = sai design.

**Làm:**
```bash
# kiểm tra mc đã cài, xem version
mc --version

# về sau khi alias set (ở mục 4), xem tất cả bucket
mc ls minio-demo/
```

**Kết quả:**
```text
$ mc --version
mc version RELEASE.2024-11-21T17-21-54Z (commit-id=...) (linux; arm64)

$ mc ls minio-demo/           # (sau khi cài xong mục 3-4)
[2026-08-13 10:00:01 +07]     0B demo/
```
→ **Verify:** `mc --version` trả release ≥ 2024; `mc ls` liệt kê bucket `demo` sau khi tạo ở mục 4.

![[minio-erasure.excalidraw]]

---

## 2. Erasure coding & distributed mode

**Chốt:** MinIO dùng **erasure coding** để chịu lỗi — object bị chia thành `data` + `parity` shard, rải qua ≥4 drive. Mất tối đa `parity` drive mà vẫn reconstruct được. Usable capacity ≈ raw × data/(data+parity). Cần **ít nhất 4 drive** để có đủ quorum.

- **Erasure coding** (Reed-Solomon): object → `d` data shard + `p` parity shard = `d+p` shard tổng. Mất bất kỳ tối đa `p` shard → reconstruct từ `d` shard còn lại.
- MinIO mặc định: `d = p = (n_drives / 2)` — ví dụ 4 drive → 2 data + 2 parity; 8 drive → 4+4.
- **Usable capacity**: `raw × d/(d+p)` — 4 drive × 100 GB = 400 GB raw → 200 GB usable (50% overhead khi 2+2).
- Cần **≥4 drive** vì cần ít nhất `d+p ≥ 4` để erasure set có ý nghĩa (1 data + 1 parity là trivial, không chịu đủ lỗi). MinIO từ chối khởi động nếu < 4 drive.
- **Distributed mode**: drive trải qua nhiều server/node — mất cả node mà vẫn OK miễn còn đủ `d` shard trên các node khác.

**Vì sao:** RAID-5/6 truyền thống chịu lỗi theo drive vật lý trong 1 máy; erasure coding của MinIO chịu lỗi theo drive **xuyên node** — phù hợp môi trường phân tán. Đồng thời throughput read cao hơn (đọc song song từ nhiều drive).

**Cơ chế:** khi PUT object, MinIO server điều phối encode: chia object thành `d` chunk data bằng nhau, tính `p` chunk parity (Reed-Solomon polynomial), ghi song song vào `d+p` drive. Mỗi drive nhận 1 shard (1/4 ~ 1/8 kích thước object tùy config). GET: đọc bất kỳ `d` shard nào còn sống → decode → trả object. Nếu 1 drive lỗi và được thay: MinIO tự heal bằng cách đọc `d` shard còn lại, tính lại parity, ghi vào drive mới.

> 💡 **Ẩn dụ:** Erasure coding = QR code — xé rách tới 30% vẫn quét được. Object = bức ảnh. 4 shard = 4 mảnh ảnh rải 4 nơi. Mất 2 mảnh → 2 mảnh còn lại đủ khôi phục toàn bộ ảnh (như QR code chịu lỗi). RAID-1 (mirror) = chụp 3 bản giống hệt → tốn hơn, không linh hoạt.

| Config | Data shard | Parity shard | Chịu mất | Usable (400 GB raw) |
|---|---|---|---|---|
| 4 drive (2+2) | 2 | 2 | 2 drive | 200 GB (50%) |
| 8 drive (4+4) | 4 | 4 | 4 drive | 200 GB (50%) |
| 8 drive (6+2) | 6 | 2 | 2 drive | 300 GB (75%) |
| 16 drive (8+8) | 8 | 8 | 8 drive | 400 GB (50%) |

**Dùng / không dùng:**
- Dùng 4+4 hoặc 8+8 khi cần balance giữa redundancy và capacity.
- Dùng 6+2 khi capacity quan trọng hơn redundancy (ít parity hơn → ít overhead nhưng chỉ chịu mất 2 drive).
- **Phản đề:** đừng nhầm "usable = raw" — MinIO luôn tiêu tốn `p/(d+p)` cho parity. Với 4 drive 2+2, mua 400 GB raw chỉ dùng được 200 GB. Phải tính trước khi mua disk.

**Làm:**
```bash
# xem erasure set info sau khi deploy (mục 3)
mc admin info minio-demo/

# xem thông tin chi tiết từng drive
mc admin info minio-demo/ --json | jq '.info.servers[].drives[] | {path, state, totalSpace, usedSpace}'
```

**Kết quả:**
```text
$ mc admin info minio-demo/
●  minio-0.minio-svc.minio-dev.svc.cluster.local:9000
   Uptime: 2 minutes
   Version: 2024-11-21T17:21:54Z
   Network: 4/4 OK
   Drives: 4/4 OK

4 drives online, 0 drives offline
Erasure sets: 1 × 4 drives
Data/Parity: 2/2
Status:  Healthy

$ mc admin info minio-demo/ --json | jq '.info.servers[].drives[] | {path, state}'
{"path":"/data0","state":"ok"}
{"path":"/data1","state":"ok"}
{"path":"/data0","state":"ok"}
{"path":"/data1","state":"ok"}
```
→ **Verify:** `4/4 OK`, `Erasure sets: 1 × 4 drives`, `Data/Parity: 2/2`, `Status: Healthy`.

---

## 3. Cài đặt distributed (Helm + Tenant)

**Chốt:** MinIO distributed trên K8s dùng **MinIO Operator** (quản Tenant CRD) hoặc chart **bitnami/minio** (StatefulSet standalone). Lab dùng bitnami chart cho đơn giản — 4 replica × 2 PVC mỗi replica = 8 drive ảo; trải qua 3 node kind-lab. Expose qua Service `ClusterIP` + `port-forward` để test.

- **MinIO Operator + Tenant**: production-grade, có console UI, auto-cert, multi-tenant. Cần nhiều resource hơn.
- **bitnami/minio chart (distributed)**: StatefulSet `replicas=4`, `drivesPerNode=2` → 8 drive tổng. Đơn giản, hợp lab.
- **StatefulSet**: đảm bảo Pod có tên stable (`minio-0`..`minio-3`), PVC gắn theo tên Pod — cần thiết vì MinIO nhận biết drive theo hostname.
- **Service**: `minio` (port 9000, API) + `minio-console` (port 9001, Web UI).

**Vì sao:** MinIO phải nhận biết được địa chỉ ổn định của từng drive xuyên restart — StatefulSet cung cấp điều đó qua headless service (`minio-{0..3}.minio-svc`). Nếu dùng Deployment (Pod name thay đổi sau restart), MinIO sẽ treat mỗi lần restart như drive mới.

**Cơ chế:** Helm chart tạo StatefulSet 4 replica, mỗi Pod mount 2 PVC (`data-0`, `data-1`) vào `/data0` và `/data1`. MinIO server khởi động với arg `http://minio-{0...3}.minio-svc.minio-dev.svc.cluster.local:9000/data{0...1}` — đây là erasure set declaration. Sau khi đủ 4 server ready, MinIO bầu chủ và bắt đầu nhận request.

> 💡 **Ẩn dụ:** StatefulSet + headless service = sổ bộ tên hộ gia đình — `minio-0` luôn là `minio-0` dù restart bao nhiêu lần, như số nhà không đổi dù chủ nhà thay. Deployment = người thuê nhà tạm — tên đổi sau mỗi lần chuyển, MinIO không nhận ra drive cũ.

| Cài theo | Pros | Cons |
|---|---|---|
| bitnami/minio (chart) | Đơn giản, 1 helm install | Ít feature hơn Operator |
| MinIO Operator + Tenant | Production, multi-tenant, auto-TLS | Phức tạp hơn, cần CRD |

**Dùng / không dùng:**
- Lab/dev: bitnami chart đủ dùng.
- Production multi-tenant: dùng MinIO Operator.
- **Phản đề:** đừng dùng MinIO `mode=standalone` (1 replica) cho bất kỳ thứ gì quan trọng — không có erasure coding, mất drive = mất data.

**Làm:**
```bash
# thêm repo bitnami
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# cài MinIO distributed: 4 server, 2 drive/server, 1 Gi mỗi PVC (kind dùng hostPath)
helm install minio bitnami/minio \
  --namespace minio-dev \
  --set mode=distributed \
  --set statefulset.replicaCount=4 \
  --set statefulset.drivesPerNode=2 \
  --set persistence.size=1Gi \
  --set auth.rootUser=minioadmin \
  --set auth.rootPassword=minioadmin \
  --set resources.requests.memory=256Mi \
  --set resources.requests.cpu=100m \
  --wait --timeout=5m

# xem Pods
kubectl get pods -n minio-dev -o wide

# xem Services
kubectl get svc -n minio-dev
```

**Kết quả:**
```text
$ kubectl get pods -n minio-dev -o wide
NAME      READY   STATUS    RESTARTS   AGE   IP           NODE
minio-0   1/1     Running   0          90s   10.244.1.5   kind-worker
minio-1   1/1     Running   0          85s   10.244.2.7   kind-worker2
minio-2   1/1     Running   0          80s   10.244.1.6   kind-worker
minio-3   1/1     Running   0          75s   10.244.2.8   kind-worker2

$ kubectl get svc -n minio-dev
NAME            TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)             AGE
minio           ClusterIP   10.96.45.12     <none>        9000/TCP,9001/TCP   2m
minio-headless  ClusterIP   None            <none>        9000/TCP,9001/TCP   2m
```
→ **Verify:** 4 Pod `Running`, phân bổ qua ít nhất 2 node; Service `minio` ClusterIP port 9000 + 9001; Service `minio-headless` ClusterIP `None` (headless).

---

## 4. Bucket + mc client + access key

**Chốt:** `mc` là CLI client cho MinIO/S3. Cấu hình *alias* trỏ vào server, rồi dùng `mc mb` tạo bucket, `mc cp` upload/download, `mc ls` liệt kê. Access key (Access Key ID + Secret Key) dùng để xác thực — lưu vào K8s Secret, cấp cho app.

- `mc alias set <name> <url> <access-key> <secret-key>` — đăng ký server, lưu vào `~/.mc/config.json`.
- `mc mb <alias>/<bucket>` — tạo bucket.
- `mc cp <local-file> <alias>/<bucket>/` — upload object.
- `mc ls <alias>/<bucket>/` — liệt kê object trong bucket.
- **Access key**: tạo bằng `mc admin user add` + policy; hoặc dùng root credential (chỉ dev/lab). Production: tạo user riêng, cấp policy `readwrite` theo bucket cụ thể.
- **Bucket policy**: JSON tương tự IAM S3 — kiểm soát ai được PutObject/GetObject/DeleteObject.

**Vì sao:** bucket tổ chức object như namespace; access key tách biệt xác thực khỏi root credential — app không cần biết `minioadmin`. K8s Secret giữ key an toàn, inject qua env var vào Pod.

**Cơ chế:** `mc` gửi HTTP request tới MinIO server (S3 API). Auth dùng AWS Signature Version 4 (SigV4) — HMAC-SHA256 ký header, không gửi password plain-text. MinIO verify signature bằng secret key tương ứng access key ID.

> 💡 **Ẩn dụ:** Bucket = ngăn tủ trong kho. Access key = chìa khoá ngăn tủ đó — cắt 1 chìa mới cho mỗi app, không đưa chìa tổng (`minioadmin`) cho ai. `mc` = nhân viên kho cầm chìa, làm theo lệnh.

| Lệnh | Tác dụng |
|---|---|
| `mc alias set` | Đăng ký server vào mc |
| `mc mb` | Tạo bucket |
| `mc cp <src> <dst>` | Copy object (upload hoặc download) |
| `mc ls` | Liệt kê bucket/object |
| `mc rm` | Xoá object |
| `mc admin user add` | Tạo user (access key) |
| `mc admin policy attach` | Gán policy cho user |

**Dùng / không dùng:**
- Dùng access key riêng cho từng app — không dùng root `minioadmin` cho workload production.
- Dùng policy giới hạn bucket cụ thể — không cấp `admin` cho app bình thường.
- **Phản đề:** đừng hardcode access key vào Dockerfile hoặc YAML ConfigMap — dùng K8s Secret (type `Opaque`), inject qua env `valueFrom.secretKeyRef`. Nếu lộ key thì rotate ngay (xoá + tạo key mới).

**Làm:**
```bash
# port-forward API MinIO (terminal 1)
kubectl port-forward -n minio-dev svc/minio 9000:9000 &

# đăng ký alias
mc alias set minio-demo http://localhost:9000 minioadmin minioadmin

# xem cluster info
mc admin info minio-demo/

# tạo bucket "demo"
mc mb minio-demo/demo

# upload file test
echo "hello from minio lab" > /tmp/test.txt
mc cp /tmp/test.txt minio-demo/demo/

# liệt kê object
mc ls minio-demo/demo/

# download về
mc cp minio-demo/demo/test.txt /tmp/test-dl.txt
cat /tmp/test-dl.txt

# tạo access key mới cho app (không dùng root)
mc admin user add minio-demo appuser appsecret123

# gán policy readwrite cho bucket demo
mc admin policy create minio-demo demo-rw /dev/stdin <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
    "Resource": ["arn:aws:s3:::demo","arn:aws:s3:::demo/*"]
  }]
}
EOF

mc admin policy attach minio-demo demo-rw --user appuser

# lưu access key vào K8s Secret
kubectl create secret generic minio-app-credentials \
  --namespace minio-dev \
  --from-literal=accessKey=appuser \
  --from-literal=secretKey=appsecret123

kubectl get secret minio-app-credentials -n minio-dev -o jsonpath='{.data.accessKey}' | base64 -d; echo
```

**Kết quả:**
```text
$ mc alias set minio-demo http://localhost:9000 minioadmin minioadmin
Added `minio-demo` successfully.

$ mc mb minio-demo/demo
Bucket created successfully `minio-demo/demo`.

$ mc cp /tmp/test.txt minio-demo/demo/
/tmp/test.txt: 21 B / 21 B ━━━━━━━━━━━━━━━━━━━━━━━ 100% 1.2 KiB/s

$ mc ls minio-demo/demo/
[2026-08-13 10:05:42 +07]    21B STANDARD test.txt

$ cat /tmp/test-dl.txt
hello from minio lab

$ mc admin user add minio-demo appuser appsecret123
Added user `appuser` successfully.

$ mc admin policy attach minio-demo demo-rw --user appuser
Successfully attached policy `demo-rw` to user `appuser`

$ kubectl get secret minio-app-credentials -n minio-dev -o jsonpath='{.data.accessKey}' | base64 -d; echo
appuser
```
→ **Verify:** `Bucket created successfully`; `mc ls` thấy `test.txt` 21 B; `cat` file download đúng nội dung; `mc admin user add` thành công; Secret tồn tại và decode được `appuser`.

---

## 5. GOLDEN LESSON — MinIO KHÔNG đặt trên Longhorn

**Chốt:** MinIO **đã tự erasure-code** data trên disk — nếu drive của MinIO lại là PVC Longhorn (replica 3), tức là mỗi shard đã được parity bởi MinIO lại còn được nhân 3 bản bởi Longhorn. Đây là **double redundancy** vô nghĩa: tốn dung lượng gấp bội, giảm hiệu năng I/O, không tăng thêm bảo vệ. MinIO phải dùng **local disk / local PV trực tiếp**.

- MinIO erasure coding tự xử lý redundancy ở tầng application.
- Longhorn replica-3 là redundancy ở tầng storage — chúng **không cộng hưởng**, chúng **chồng lên nhau**.
- Ví dụ số: 4 drive MinIO (2+2 erasure) trên Longhorn replica-3:
  - Raw data 100 GB → MinIO ghi 200 GB (50% parity overhead) → Longhorn nhân 3 bản → **600 GB disk thực tế tiêu tốn** để lưu 100 GB data.
  - Thay bằng local PV: 200 GB thực tế (chỉ MinIO parity, không Longhorn overhead).
- I/O path: `app → MinIO API → MinIO encode → Longhorn ISCSI/NFS → replica 3 node` — quá nhiều tầng, latency cao.
- **Quy tắc:** object storage cần raw block device hoặc local PV (hostPath, local-path-provisioner). Không dùng distributed/replicated storage làm backend cho distributed storage.

**Vì sao:** thiết kế đúng là mỗi tầng chịu trách nhiệm 1 việc. MinIO đảm nhiệm erasure coding; disk bên dưới cứ là disk đơn giản nhất (local). Nếu node chết và disk mất: MinIO tự reconstruct từ parity shard trên các node khác — không cần Longhorn replica.

**Cơ chế:** MinIO khi ghi object: tính `n` shard, gửi đồng thời tới `n` drive trên `n` server. Nếu drive là Longhorn PVC, Longhorn lại tiếp tục replicate shard đó qua mạng tới 2 node khác. Tổng I/O network: `n × 3` thay vì `n`. Với throughput lớn (backup 100 GB), đây là bottleneck rõ ràng.

> 💡 **Ẩn dụ:** Dùng MinIO trên Longhorn = mua bảo hiểm xe, rồi mua thêm 3 cái bảo hiểm cho cùng 1 chiếc xe từ 3 công ty khác nhau — tai nạn thì chỉ 1 bảo hiểm đền, 3 cái phí đều đã trả. MinIO erasure = bảo hiểm đã đủ; local disk = đừng mua thêm.

| Setup | Tầng redundancy | Disk thực tế cho 100 GB data | Đúng không? |
|---|---|---|---|
| MinIO erasure 2+2 trên local PV | MinIO parity (1 tầng) | 200 GB | ✅ |
| MinIO erasure 2+2 trên Longhorn replica-3 | MinIO parity + Longhorn replica (2 tầng) | 600 GB | ❌ |
| MinIO standalone trên Longhorn replica-3 | Longhorn replica (1 tầng) | 300 GB | ❌ (thiếu erasure) |

**Dùng / không dùng:**
- Dùng `local-path-provisioner` (kind mặc định) hoặc `local` StorageClass (hostPath) cho MinIO PVC trong lab.
- Production: cấp local NVMe trực tiếp (bare-metal) hoặc persistent disk cloud gắn 1:1 vào node, không qua Ceph/Longhorn/Portworx.
- **Phản đề:** "nhưng Longhorn dễ quản lý hơn local disk" — đúng, nhưng MinIO đã có console để monitor drive health. Dùng `mc admin info` + `mc admin heal` thay vì dựa vào Longhorn làm điều MinIO đã làm.

**Làm:**
```bash
# xem StorageClass mà MinIO PVC đang dùng (trong lab kind = standard/local-path)
kubectl get pvc -n minio-dev

# xem StorageClass
kubectl get storageclass

# kiểm tra PV của minio-0 dùng local hay distributed storage
kubectl get pv $(kubectl get pvc -n minio-dev -o jsonpath='{.items[0].spec.volumeName}') -o jsonpath='{.spec.storageClassName}'; echo
```

**Kết quả:**
```text
$ kubectl get pvc -n minio-dev
NAME            STATUS   VOLUME          CAPACITY   ACCESS MODES   STORAGECLASS   AGE
data-0-minio-0  Bound    pvc-abc1...     1Gi        RWO            standard       5m
data-1-minio-0  Bound    pvc-abc2...     1Gi        RWO            standard       5m
data-0-minio-1  Bound    pvc-abc3...     1Gi        RWO            standard       5m
data-1-minio-1  Bound    pvc-abc4...     1Gi        RWO            standard       5m
data-0-minio-2  Bound    pvc-abc5...     1Gi        RWO            standard       5m
data-1-minio-2  Bound    pvc-abc6...     1Gi        RWO            standard       5m
data-0-minio-3  Bound    pvc-abc7...     1Gi        RWO            standard       5m
data-1-minio-3  Bound    pvc-abc8...     1Gi        RWO            standard       5m

$ kubectl get storageclass
NAME                 PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      AGE
standard (default)   rancher.io/local-path   Delete          WaitForFirstConsumer   15m

$ kubectl get pv pvc-abc1... -o jsonpath='{.spec.storageClassName}'; echo
standard
```
→ **Verify:** 8 PVC Bound (4 Pod × 2 drive); StorageClass `standard` = `local-path` (hostPath, không replicated); **không phải Longhorn** — đúng thiết kế.

---

## 🧹 Dọn dẹp

```bash
# xoá Helm release
helm uninstall minio -n minio-dev

# xoá PVC (không tự xoá cùng StatefulSet)
kubectl delete pvc -n minio-dev --all

# xoá namespace
kubectl delete namespace minio-dev

# dừng port-forward nếu còn
kill $(lsof -ti tcp:9000) 2>/dev/null || true

# xoá alias mc
mc alias remove minio-demo
```

---

## ✅ Đủ khi

① MinIO là gì và khác block/file storage ở điểm nào (đơn vị, truy cập, dùng khi nào) · ② erasure coding chia object thành data+parity shard, cần ≥4 drive, usable = raw × d/(d+p) · ③ cài distributed bằng bitnami chart: StatefulSet 4 replica, headless service, mỗi Pod 2 PVC · ④ tạo bucket, dùng `mc cp` upload/download, tạo access key + policy + K8s Secret · ⑤ tại sao MinIO KHÔNG dùng Longhorn làm backend (double redundancy = tốn 3× dung lượng, không tăng bảo vệ).

---

## Recall
1. MinIO khác block storage (Longhorn) và file storage (NFS) ở điểm gì? Cho ví dụ use case phù hợp mỗi loại.
2. Erasure coding hoạt động thế nào? `data shard` và `parity shard` là gì?
3. Với 4 drive cấu hình 2+2, MinIO chịu mất tối đa bao nhiêu drive? Usable capacity là bao nhiêu % raw?
4. Tại sao MinIO cần ≥4 drive để chạy distributed?
5. Vì sao MinIO phải dùng StatefulSet thay vì Deployment?
6. Lệnh nào đăng ký server MinIO vào `mc`? Lệnh nào tạo bucket?
7. Access key MinIO khác root credential (`minioadmin`) thế nào? Tại sao không dùng root cho app?
8. Khi app cần access MinIO, lưu key vào K8s resource nào? Inject vào Pod bằng cách nào?
9. Tại sao KHÔNG đặt MinIO drive trên Longhorn PVC replica-3?
10. Với 100 GB data thực tế, MinIO erasure 2+2 trên Longhorn replica-3 tốn bao nhiêu GB disk vật lý?

### Đáp án

1. Block storage = disk gắn vào 1 Pod (mount fs), dùng cho DB. File storage = thư mục chia sẻ mount qua network, nhiều Pod đọc cùng. Object storage = blob lưu theo key, truy cập HTTP API — dùng cho backup, artifact, ML dataset. MinIO = object.
2. Object chia thành `d` data shard (dữ liệu thật) + `p` parity shard (tính từ polynomial Reed-Solomon). Khi mất ≤ `p` shard, reconstruct từ `d` shard còn lại.
3. Chịu mất tối đa 2 drive. Usable = raw × 2/(2+2) = 50% raw.
4. Cần tối thiểu `d+p ≥ 4` để tạo erasure set có ý nghĩa — 1 data + 1 parity không đủ phân tán, và MinIO từ chối khởi động nếu drive count < 4.
5. StatefulSet cho Pod tên stable (`minio-0`..`minio-3`) và PVC gắn theo tên — MinIO nhận biết drive theo hostname. Deployment đổi tên Pod sau restart → MinIO treat như drive mới.
6. `mc alias set <name> <url> <access-key> <secret-key>` — đăng ký server. `mc mb <alias>/<bucket>` — tạo bucket.
7. Access key được tạo per-user với policy giới hạn bucket cụ thể. Root credential có quyền admin toàn bộ — lộ ra là mất toàn bộ cluster. App chỉ cần quyền đọc/ghi bucket của mình, không cần admin.
8. Lưu vào K8s Secret (`type: Opaque`). Inject vào Pod qua `env[].valueFrom.secretKeyRef` (biến môi trường `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` hoặc tên tương đương SDK).
9. MinIO đã tự erasure-code (1 tầng redundancy). Longhorn replica-3 thêm 1 tầng nữa → double redundancy: không tăng bảo vệ nhưng mỗi shard MinIO bị nhân 3 bản → tốn gấp 3× dung lượng Longhorn, và I/O path dài hơn (MinIO → Longhorn → replica qua mạng).
10. MinIO erasure 2+2: 100 GB data → 200 GB ghi xuống disk. Longhorn replica-3: 200 GB × 3 = **600 GB** disk vật lý thực tế tiêu tốn.

---

## Bắc cầu sang production

MinIO distributed trên K8s production thường dùng **MinIO Operator + Tenant CRD** thay vì bitnami chart — Operator quản auto-cert TLS, multi-tenant, upgrade rolling, console UI tích hợp. Drive production cần local NVMe (bare-metal node) hoặc cloud disk gắn 1:1 (không replica ở tầng storage). Bucket versioning (`mc version enable`) + object locking (S3 object lock API) cần bật nếu dùng làm backend backup Velero. Access policy tuân AWS IAM JSON syntax — có thể tái dùng policy đã viết cho S3 thật. Monitor qua `mc admin prometheus generate` → scrape Prometheus — metric key: `minio_disk_storage_used_bytes`, `minio_heal_objects_total`, `minio_cluster_nodes_offline_total`.

---

## 📎 Nguồn

- [MinIO Distributed Mode](https://min.io/docs/minio/linux/operations/install-deploy-manage/deploy-minio-multi-node-multi-drive.html)
- [MinIO Erasure Coding](https://min.io/docs/minio/linux/operations/concepts/erasure-coding.html)
- [bitnami/minio Helm chart](https://github.com/bitnami/charts/tree/main/bitnami/minio)
- [mc Client Quickstart](https://min.io/docs/minio/linux/reference/minio-mc.html)
- [MinIO Operator](https://operator.min.io/)
