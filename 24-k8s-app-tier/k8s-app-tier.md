# 24 · Deploy app tier — Harbor + SonarQube + Jenkins (external DB + S3)

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Argo CD / GitOps · kế tiếp: Observability + HA hardening + DR · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** deploy 3 công cụ nền tảng (Harbor registry, SonarQube SAST, Jenkins CI) trên kind-lab theo đúng nguyên tắc production — external DB (CNPG), object storage (MinIO S3), persistent volume (Longhorn), TLS ingress, credential trong Secret; hiểu các GOLDEN LESSON (Harbor blob retention, SonarQube prepared-statement gotcha, sysctl Elasticsearch, Jenkins single-controller).
**Nền:** bạn đã có Longhorn PVC (20) · MinIO bucket (21) · CNPG cluster (22) · cert-manager + Ingress (19) — lab này kết nối tất cả lại thành app tier đầy đủ.

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15–18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

## ⚙️ Tiền đề

**1. kind-lab 3-node running:**
```bash
kind get clusters          # phải thấy tên cluster (vd kind-lab)
kubectl get nodes          # 3 nodes STATUS=Ready
```

**2. Helm repos đã thêm:**
```bash
helm repo add harbor https://helm.goharbor.io
helm repo add sonarqube https://SonarSource.github.io/helm-chart-sonarqube
helm repo add jenkins https://charts.jenkins.io
helm repo update
```

**3. Namespace chuẩn bị:**
```bash
kubectl create namespace harbor   --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace sonarqube --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace jenkins  --dry-run=client -o yaml | kubectl apply -f -
```

**4. CNPG DB đã khởi tạo (lab 22):**
```bash
kubectl get cluster -A          # phải thấy cluster CNPG STATUS=Cluster in healthy state
# mỗi app cần DB riêng
kubectl get database -A         # harbor-db, sonarqube-db (tạo ở mục 2, 3)
```

**5. MinIO bucket đã tạo (lab 21):**
```bash
# bucket harbor-registry, harbor-chartmuseum
mc ls minio/                    # thấy harbor-registry, harbor-chartmuseum
```

---

## 1. Pattern chung — externalize DB + object storage

**Chốt:** ứng dụng production-grade tuân theo **12-factor app** — DB và object storage tách khỏi Pod; Pod chỉ chứa runtime stateless, dễ scale và upgrade. Trong kind-lab: **Longhorn PVC** cho block storage (config/plugin state), **MinIO S3** cho blob (image layer, artifact), **CNPG** cho SQL — ba tầng không lẫn nhau.

- **Longhorn PVC (block):** mount vào Pod dưới dạng filesystem — dùng cho state nhỏ, structured (JENKINS_HOME, SonarQube plugin). ReadWriteOnce, chỉ 1 Pod mount tại 1 thời điểm.
- **MinIO S3 (blob):** HTTP API — dùng cho large binary (container image layer, chart archive). Không mount vào filesystem; app gọi S3 API. Không giới hạn số app dùng đồng thời.
- **CNPG SQL (relational):** metadata, tag, project, user, analysis result. CNPG cấp endpoint `-rw` (primary, đọc/ghi) và `-ro` (replica, chỉ đọc). Mỗi app dùng DB riêng, user riêng.
- **Pod stateless:** khi Pod crash/restart/reschedule → không mất dữ liệu vì dữ liệu không nằm trong Pod.

**Vì sao:** Pod là ephemeral (lab 03 — chết là mất). Nhét dữ liệu quan trọng vào Pod là khai tử dữ liệu. Tách ra ngoài thì có thể: (a) upgrade app mà không downtime dữ liệu, (b) scale Pod khi cần, (c) backup DB/S3 riêng không cần chụp cả Pod.

**Cơ chế:** Helm chart của mỗi app có section `database.external` (hoặc `jdbcUrl`) và `persistence.imageChartStorage` (hoặc `sonarProperties`) để khai báo external endpoint. App đọc credential từ K8s Secret qua `envFrom` hoặc `secretKeyRef`. Longhorn StorageClass tự provision PV khi PVC được tạo.

| Loại dữ liệu | Lưu ở đâu | Protocol | Mount vào Pod? |
|---|---|---|---|
| Image layer (Harbor) | MinIO S3 | S3 API | Không |
| Chart artifact (Harbor) | MinIO S3 | S3 API | Không |
| Project/tag metadata (Harbor) | CNPG `harbor` DB | PostgreSQL | Không |
| Analysis result (SonarQube) | CNPG `sonarqube` DB | JDBC | Không |
| SonarQube plugin/index | Longhorn PVC 10Gi | Filesystem | Có (ReadWriteOnce) |
| Jenkins config + jobs | Longhorn PVC 50Gi | Filesystem | Có (ReadWriteOnce) |

> 💡 **Ẩn dụ:** Pod như nhân viên làm việc tại quán cà phê công cộng — không được giữ file quan trọng trên laptop của quán. File quan trọng phải nằm trên Google Drive (S3), DB công ty (CNPG), và ổ cứng riêng có gắn tên (Longhorn PVC). Nhân viên có thể đổi bàn (reschedule) hay thay người (restart) mà không mất gì.

**Dùng / không dùng:**
- Dùng external DB + S3 khi app có volume dữ liệu đáng kể, cần HA/backup riêng.
- Dùng Longhorn PVC khi app cần filesystem truyền thống (plugin dir, workspace nhỏ).
- **Phản đề:** đừng đẩy mọi thứ ra external chỉ để "đúng pattern" — SQLite embedded đủ cho tool nội bộ rất nhỏ. External DB = thêm network hop, thêm thứ phải vận hành. Chọn khi benefit rõ ràng.

**Làm:**
```bash
# kiểm tra Longhorn StorageClass đã có
kubectl get storageclass
# kiểm MinIO endpoint từ lab 21
kubectl get svc -n minio
# kiểm CNPG cluster ready
kubectl get cluster -n cnpg-system
```

**Kết quả:**
```text
$ kubectl get storageclass
NAME                 PROVISIONER          RECLAIMPOLICY   VOLUMEBINDINGMODE
longhorn (default)   driver.longhorn.io   Delete          Immediate

$ kubectl get svc -n minio
NAME            TYPE        CLUSTER-IP     PORT(S)
minio           ClusterIP   10.96.45.12    9000/TCP
minio-console   ClusterIP   10.96.45.13    9001/TCP

$ kubectl get cluster -n cnpg-system
NAME        AGE   INSTANCES   READY   STATUS
pg-main     2d    3           3       Cluster in healthy state
```
→ **Verify:** Longhorn là default StorageClass; MinIO svc port 9000; CNPG cluster healthy với 3 instances. Thiếu bất kỳ điều kiện nào → hoàn thành lab tiên quyết trước.

![[app-tier-wiring.excalidraw]]

---

## 2. Harbor — registry (external DB + S3)

**Chốt:** Harbor là container registry + Helm chart registry có UI, RBAC, vulnerability scan. Deploy Helm với `database.external` trỏ CNPG `harbor` DB, `persistence.imageChartStorage.type: s3` trỏ MinIO — **không dùng PVC cho image layer**. **GOLDEN LESSON: bật GC + tag retention** từ đầu, không thì blob MinIO phình vô hạn.

- **Komponen chính:** core, portal, registry, jobservice, trivy (scan), notary (optional).
- **External DB:** CNPG tạo DB `harbor` + user `harbor` với password quản bằng Secret CNPG.
- **S3 backend:** image layer → MinIO bucket `harbor-registry`; chart → `harbor-chartmuseum`. Harbor dùng S3 API (endpoint, accessKey, secretKey).
- **GC (Garbage Collection):** xóa orphan blob khỏi MinIO sau khi tag/manifest bị delete. Phải bật schedule, không chạy mặc định.
- **Tag Retention:** tự động xóa tag cũ theo rule (giữ N latest, hoặc giữ image push trong 30 ngày gần nhất).

**Vì sao:** không bật GC, mỗi lần `docker push` thêm layer vào MinIO mà không bao giờ dọn → MinIO hết disk sau vài tuần CI. Tag retention giữ registry gọn; GC dọn blob thực tế sau khi manifest xóa. Đây là lý do nhiều team thấy MinIO phình không rõ nguyên nhân.

**Cơ chế:** Harbor registry component lưu manifest + config blob vào S3 bucket trực tiếp qua distribution/registry library. Database lưu repository name, tag, project, RBAC. Khi `docker push`, registry ghi layer vào S3 và metadata vào DB. Khi `docker pull`, registry đọc metadata từ DB, redirect client đến S3 presigned URL (hoặc proxy). GC query DB tìm manifest không có reference → xóa blob tương ứng trong S3.

| Thành phần | Kết nối | Credential |
|---|---|---|
| harbor-core | CNPG `harbor-db:5432` | Secret `harbor-db-secret` |
| harbor-registry | MinIO `minio:9000/harbor-registry` | Secret `harbor-s3-secret` |
| harbor-chartmuseum | MinIO `minio:9000/harbor-chartmuseum` | Secret `harbor-s3-secret` |
| Ingress | cert-manager TLS | ClusterIssuer `letsencrypt` |

> 💡 **Ẩn dụ:** Harbor như kho hàng lớn — kệ hàng (MinIO S3) để chứa thùng (image layer), sổ kho (CNPG DB) ghi tên và vị trí từng thùng, bảo vệ (RBAC) kiểm ai được lấy thùng nào. Không dọn kho (GC) → kệ đầy thùng vô chủ.

**Dùng / không dùng:**
- Dùng external DB + S3 cho Harbor production — không dùng embedded PVC storage cho image (giới hạn 1 node, không HA).
- **Phản đề:** Harbor khá nặng (6+ Pod) — với team nhỏ, Docker Hub free tier hoặc ghcr.io đủ dùng. Self-host Harbor có nghĩa là bạn phải vận hành GC, backup DB, backup MinIO.

**Làm:**

Bước 1 — Tạo DB `harbor` trên CNPG:
```yaml
# harbor-db.yaml
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata:
  name: harbor-db
  namespace: cnpg-system
spec:
  name: harbor
  cluster:
    name: pg-main
  owner: harbor
```
```bash
kubectl apply -f harbor-db.yaml
# lấy password CNPG tự sinh (secret tên: pg-main-app hoặc theo cluster)
kubectl get secret -n cnpg-system pg-main-harbor -o jsonpath='{.data.password}' | base64 -d
```

Bước 2 — Tạo Secret cho Harbor:
```bash
# credential DB (thay <PASSWORD> bằng giá trị thật lúc cài)
kubectl create secret generic harbor-db-secret \
  -n harbor \
  --from-literal=password=<HARBOR_DB_PASSWORD>

# credential MinIO S3
kubectl create secret generic harbor-s3-secret \
  -n harbor \
  --from-literal=accessKey=minioadmin \
  --from-literal=secretKey=minioadmin123
```

Bước 3 — Helm values:
```yaml
# harbor-values.yaml
expose:
  type: ingress
  tls:
    enabled: true
    certSource: secret
    secret:
      secretName: harbor-tls
  ingress:
    hosts:
      core: harbor.lab.local
      notary: notary.lab.local
    annotations:
      kubernetes.io/ingress.class: nginx
      cert-manager.io/cluster-issuer: letsencrypt-staging

externalURL: https://harbor.lab.local

database:
  type: external
  external:
    host: pg-main-rw.cnpg-system.svc.cluster.local
    port: 5432
    username: harbor
    password: ""          # kosong — dùng existingSecret
    coreDatabase: harbor
    existingSecret: harbor-db-secret
    existingSecretKey: password

persistence:
  enabled: true
  resourcePolicy: keep
  persistentVolumeClaim:
    registry:
      storageClass: longhorn
      size: 5Gi            # nhỏ — chỉ metadata; blob ở S3
    jobservice:
      storageClass: longhorn
      size: 1Gi
    database:
      storageClass: ""     # external DB → không cần
    redis:
      storageClass: longhorn
      size: 1Gi
  imageChartStorage:
    disableredirect: true   # MinIO không hỗ trợ redirect S3
    type: s3
    s3:
      region: us-east-1
      bucket: harbor-registry
      accesskey: ""         # dùng existingSecret
      secretkey: ""
      regionendpoint: http://minio.minio.svc.cluster.local:9000
      encrypt: false
      secure: false
      existingSecret: harbor-s3-secret

# chart storage
chartmuseum:
  enabled: true
  persistence:
    storageClass: longhorn
    size: 5Gi
  env:
    open:
      STORAGE: amazon
      STORAGE_AMAZON_BUCKET: harbor-chartmuseum
      STORAGE_AMAZON_ENDPOINT: http://minio.minio.svc.cluster.local:9000
      STORAGE_AMAZON_REGION: us-east-1

# scan
trivy:
  enabled: true

# tắt built-in PG/Redis nếu dùng external
postgresql:
  enabled: false

redis:
  type: internal   # giữ internal Redis cho đơn giản; external nếu muốn HA

# resource nhẹ cho kind-lab
core:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
portal:
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
```

Bước 4 — Install:
```bash
helm install harbor harbor/harbor \
  -n harbor \
  -f harbor-values.yaml \
  --version 1.14.2 \
  --wait --timeout 5m
```

Bước 5 — Push/pull test:
```bash
# login
docker login harbor.lab.local -u admin -p Harbor12345

# tag và push
docker pull nginx:alpine
docker tag nginx:alpine harbor.lab.local/library/nginx:1.0
docker push harbor.lab.local/library/nginx:1.0

# pull về
docker pull harbor.lab.local/library/nginx:1.0
```

Bước 6 — Bật GC schedule (qua UI hoặc API):
```bash
# Harbor API: bật GC chạy hàng ngày lúc 2:00 AM
curl -X POST https://harbor.lab.local/api/v2.0/system/gc/schedule \
  -u admin:Harbor12345 \
  -H 'Content-Type: application/json' \
  -d '{"schedule":{"type":"Custom","cron":"0 2 * * *"}}'

# Tag retention: giữ 10 tag mới nhất (qua UI: Projects → library → Tag Retention)
curl -X POST https://harbor.lab.local/api/v2.0/projects/1/immutabletagrules \
  -u admin:Harbor12345 \
  -H 'Content-Type: application/json' \
  -d '{"selector_repository":{"kind":"doublestar","decoration":"repoMatches","pattern":"**"},
       "selector_tag":{"kind":"latestPushedK","decoration":"latestPushedK","n":10}}'
```

**Kết quả:**
```text
$ helm install harbor harbor/harbor -n harbor -f harbor-values.yaml --wait --timeout 5m
NAME: harbor
LAST DEPLOYED: ...
STATUS: deployed
NOTES: ...

$ kubectl get pods -n harbor
NAME                                    READY   STATUS    RESTARTS   AGE
harbor-core-7d9f8c6b4-xk2pq            1/1     Running   0          3m
harbor-jobservice-6c8d9f7b5-mn4rs      1/1     Running   0          3m
harbor-portal-5b7c8d9f6-qw3rt          1/1     Running   0          3m
harbor-registry-8f9b6c5d4-lk7mn       1/1     Running   0          3m
harbor-trivy-0                          1/1     Running   0          3m
harbor-redis-0                          1/1     Running   0          3m

$ docker push harbor.lab.local/library/nginx:1.0
The push refers to repository [harbor.lab.local/library/nginx]
1.0: digest: sha256:a1b2c3d4e5f6... size: 1234
```
→ **Verify:** `kubectl get pods -n harbor` — tất cả Running; `docker push` thành công có digest; vào Harbor UI `https://harbor.lab.local` → Projects → library → thấy `nginx:1.0`; kiểm MinIO bucket `harbor-registry` có object mới (`mc ls minio/harbor-registry`).

---

## 3. SonarQube — external DB + sysctl

**Chốt:** SonarQube Community Edition dùng embedded Elasticsearch (bundled) cần `vm.max_map_count=524288` (kernel tuning) — thiếu sẽ crash. Dùng CNPG `sonarqube` DB với `jdbcUrl` trỏ thẳng `-rw` endpoint. **GOLDEN LESSON: KHÔNG nối qua PgBouncer transaction mode** — SonarQube dùng prepared statement, PgBouncer transaction mode phá vỡ chúng. CE single-instance — không scale replica.

- **Embedded Elasticsearch:** SonarQube CE bundle Elasticsearch để index code. ES yêu cầu `vm.max_map_count ≥ 524288` trên node chạy Pod — thiếu thì SonarQube khởi động fail với lỗi `max virtual memory areas vm.max_map_count [65530] is too low`.
- **initContainer sysctl:** vì Pod không tự sửa kernel param, dùng initContainer `privileged: true` chạy `sysctl -w vm.max_map_count=524288` trên node.
- **PgBouncer transaction mode:** PgBouncer pooling mode `transaction` multiplex connection — sau mỗi transaction, connection có thể về tay client khác. Prepared statement (server-side) bị reset → SonarQube crash khi execute. Chỉ dùng PgBouncer `session` mode hoặc nối thẳng CNPG `-rw`.
- **CE single-instance:** SonarQube CE không support cluster mode. ReadWriteOnce PVC — không mount chung cho 2 Pod.

**Vì sao:** Elasticsearch embedded là điểm đặc biệt — nhiều app K8s khác không cần kernel tuning. Hiểu rõ để không mất 30 phút debug lỗi `max_map_count`. PgBouncer gotcha thường gặp khi team cố tối ưu DB connection trước khi hiểu app yêu cầu gì.

**Cơ chế:** initContainer chạy trước main container, dùng `hostIPC`/`privileged` để gọi `sysctl`. Sau khi initContainer exit 0, kubelet mới khởi động SonarQube container. `sysctl -w` chỉ áp dụng cho node đang chạy Pod; nếu Pod reschedule sang node khác, sysctl phải chạy lại (initContainer đảm bảo điều này tự động).

| Tham số | Giá trị | Lý do |
|---|---|---|
| `vm.max_map_count` | 524288 | Elasticsearch yêu cầu tối thiểu |
| CNPG endpoint | `pg-main-rw.cnpg-system.svc.cluster.local` | `-rw` = primary, đọc/ghi |
| PgBouncer | Không dùng | Prepared statement conflict |
| replicas | 1 | CE không hỗ trợ cluster mode |

> 💡 **Ẩn dụ:** SonarQube như văn phòng cần điện 3 pha — trước khi nhân viên (SonarQube) vào làm, thợ điện (initContainer) phải vào đấu dây (sysctl) trước. PgBouncer transaction mode như tổng đài điện thoại cắt ngang giữa câu — người hỏi câu trước (prepared statement step 1) bị nối với người khác khi đến câu trả lời (step 2).

**Dùng / không dùng:**
- Dùng SonarQube CE cho team nhỏ ≤ 5 projects — đủ tính năng SAST cơ bản.
- SonarQube EE/DC nếu cần cluster mode, branch analysis, portfolio.
- **Phản đề:** SonarQube nặng (~2–3 GB RAM), khởi động chậm (2–3 phút), chỉ scan được khi có CI pipeline gọi scanner. Nếu chỉ có 1–2 repo nhỏ, SonarCloud (SaaS) rẻ hơn nhiều.

**Làm:**

Bước 1 — Tạo DB `sonarqube` trên CNPG:
```yaml
# sonarqube-db.yaml
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata:
  name: sonarqube-db
  namespace: cnpg-system
spec:
  name: sonarqube
  cluster:
    name: pg-main
  owner: sonarqube
```
```bash
kubectl apply -f sonarqube-db.yaml
kubectl get secret -n cnpg-system pg-main-sonarqube -o jsonpath='{.data.password}' | base64 -d
```

Bước 2 — Secret:
```bash
kubectl create secret generic sonarqube-db-secret \
  -n sonarqube \
  --from-literal=password=<SONAR_DB_PASSWORD>
```

Bước 3 — Helm values:
```yaml
# sonarqube-values.yaml
image:
  tag: 10.4-community

# external DB — trỏ thẳng CNPG -rw, KHÔNG qua PgBouncer transaction mode
jdbcUrlOverride: "jdbc:postgresql://pg-main-rw.cnpg-system.svc.cluster.local:5432/sonarqube"
postgresql:
  enabled: false            # tắt built-in PG
  existingSecret: sonarqube-db-secret

env:
  - name: SONAR_JDBC_USERNAME
    value: sonarqube
  - name: SONAR_JDBC_PASSWORD
    valueFrom:
      secretKeyRef:
        name: sonarqube-db-secret
        key: password

# sysctl initContainer — bắt buộc cho embedded Elasticsearch
initSysctl:
  enabled: true
  vmMaxMapCount: 524288
  securityContext:
    privileged: true

# Longhorn PVC
persistence:
  enabled: true
  storageClass: longhorn
  size: 10Gi
  accessMode: ReadWriteOnce

# CE — không scale
replicaCount: 1

# Ingress
ingress:
  enabled: true
  ingressClassName: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-staging
  hosts:
    - name: sonarqube.lab.local
      path: /
  tls:
    - secretName: sonarqube-tls
      hosts:
        - sonarqube.lab.local

# resource cho kind-lab
resources:
  requests:
    cpu: 200m
    memory: 2Gi
  limits:
    cpu: "2"
    memory: 4Gi

# sonar.properties tùy chỉnh (nếu cần)
sonarProperties:
  sonar.forceAuthentication: "true"
```

Bước 4 — Install:
```bash
helm install sonarqube sonarqube/sonarqube \
  -n sonarqube \
  -f sonarqube-values.yaml \
  --version 10.4.1+2389 \
  --wait --timeout 10m
```

Bước 5 — Verify sau khi khởi động (~2–3 phút):
```bash
# xem initContainer sysctl đã chạy
kubectl describe pod -n sonarqube -l app=sonarqube | grep -A10 "Init Containers"

# theo dõi log khởi động
kubectl logs -n sonarqube -l app=sonarqube -f | grep -E "SonarQube is up|ERROR"
```

**Kết quả:**
```text
$ kubectl get pods -n sonarqube
NAME                         READY   STATUS    RESTARTS   AGE
sonarqube-7d8f9c6b5-xk2mn   1/1     Running   0          4m

$ kubectl describe pod -n sonarqube -l app=sonarqube | grep -A10 "Init Containers"
Init Containers:
  init-sysctl:
    Image: busybox:1.36
    Command: sysctl -w vm.max_map_count=524288
    State: Terminated
      Reason: Completed
      Exit Code: 0

$ kubectl logs -n sonarqube -l app=sonarqube | grep "SonarQube is up"
2026.08.13 05:12:33 INFO  app[][o.s.a.SchedulerImpl] SonarQube is up
```
→ **Verify:** Pod Running; initContainer `init-sysctl` exit 0; log `SonarQube is up`; vào `https://sonarqube.lab.local` → login `admin/admin` → đổi mật khẩu → thấy dashboard. Nếu thấy lỗi `prepared statement` trong log → kiểm tra đang dùng CNPG `-rw` trực tiếp, không qua PgBouncer.

---

## 4. Jenkins — controller + dynamic agent (không DB)

**Chốt:** Jenkins không dùng external DB — toàn bộ state nằm trong `JENKINS_HOME` (PVC Longhorn 50Gi). Kubernetes plugin tự động spawn agent Pod ephemeral khi có build job — agent chạy xong tự xóa. **GOLDEN LESSON: chỉ 1 controller, không mount PVC cho 2 controller đồng thời** (ReadWriteOnce). Backup `JENKINS_HOME` định kỳ.

- **JENKINS_HOME:** thư mục chứa toàn bộ config (jobs, plugins, credentials, build history). Được mount từ Longhorn PVC 50Gi.
- **Kubernetes plugin:** khi có pipeline job, Jenkins controller yêu cầu K8s API tạo agent Pod (theo `podTemplate`). Agent pull source, chạy build, push artifact, rồi Pod tự xóa. Controller không tham gia vào bước build nặng.
- **Dynamic agent vs static agent:** dynamic agent (ephemeral Pod) không cần quản lý máy thêm — scale tự động, cách ly giữa các build, không nhiễm dependency. Static agent (SSH/JNLP vào máy cố định) = legacy, tránh dùng với K8s.
- **Single controller:** ReadWriteOnce PVC không thể mount vào 2 Pod cùng lúc → chỉ chạy 1 controller. Nếu controller Pod crash, PVC giữ nguyên → controller mới mount lên — không mất config.

**Vì sao:** Jenkins state trong PVC = đơn giản nhất, không cần setup external DB. Tradeoff: controller là SPOF (single point of failure), nhưng với kind-lab thì chấp nhận được. Dynamic agent Pod giải quyết vấn đề "không đủ máy" và "build nhiễm nhau" mà static agent hay gặp.

**Cơ chế:** Kubernetes plugin dùng ServiceAccount của Jenkins controller để gọi K8s API (`pods/create`, `pods/delete`, `pods/log`). Agent Pod được tạo trong namespace `jenkins` (hoặc namespace tùy chỉnh). Agent connect ngược lại controller qua JNLP port (50000). Sau khi `currentBuild.result` có kết quả, controller xóa agent Pod.

| Thành phần | Lưu trữ | Ghi chú |
|---|---|---|
| Jenkins controller | Longhorn PVC 50Gi (JENKINS_HOME) | ReadWriteOnce — chỉ 1 controller |
| Agent Pod | Ephemeral (trong Pod) | Tự xóa sau build |
| Build artifact | Tùy pipeline (đẩy vào Harbor/MinIO) | Không lưu trong JENKINS_HOME |

> 💡 **Ẩn dụ:** Jenkins controller như quản đốc ngồi văn phòng (PVC) — biết hết job/config. Khi có việc, quản đốc thuê công nhân tạm (agent Pod) làm đúng việc đó, xong trả về. Không phải quản đốc làm thay — và chỉ có 1 quản đốc trên 1 bàn làm việc (ReadWriteOnce PVC).

**Dùng / không dùng:**
- Dùng Kubernetes plugin cho build CI scale-out mà không cần static slave.
- Backup JENKINS_HOME thường xuyên (Velero + Longhorn snapshot, hoặc script tar + gửi S3).
- **Phản đề:** Jenkins ngày càng bị thay thế bởi Argo Workflows, Tekton, GitHub Actions trong cloud-native stack. Nếu bắt đầu project mới, cân nhắc Argo Workflows (lab 23 ecosystem). Jenkins hợp khi migration từ legacy CI hoặc team đã quen.

**Làm:**

Bước 1 — RBAC cho Kubernetes plugin:
```yaml
# jenkins-rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: jenkins
  namespace: jenkins
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: jenkins-agent-role
  namespace: jenkins
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/exec", "pods/log", "persistentvolumeclaims", "events"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: jenkins-agent-rolebinding
  namespace: jenkins
subjects:
  - kind: ServiceAccount
    name: jenkins
    namespace: jenkins
roleRef:
  kind: Role
  name: jenkins-agent-role
  apiGroup: rbac.authorization.k8s.io
```
```bash
kubectl apply -f jenkins-rbac.yaml
```

Bước 2 — Helm values:
```yaml
# jenkins-values.yaml
controller:
  serviceAccount:
    create: false
    name: jenkins

  ingress:
    enabled: true
    ingressClassName: nginx
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-staging
    hostName: jenkins.lab.local
    tls:
      - secretName: jenkins-tls
        hosts:
          - jenkins.lab.local

  # plugins mặc định + kubernetes plugin
  installPlugins:
    - kubernetes:latest
    - workflow-aggregator:latest
    - git:latest
    - blueocean:latest
    - harbor:latest

  resources:
    requests:
      cpu: 200m
      memory: 512Mi
    limits:
      cpu: "2"
      memory: 2Gi

  # admin password từ Secret (không hardcode)
  adminSecret: true
  adminPassword: ""   # Helm tự sinh, lưu vào Secret jenkins

persistence:
  enabled: true
  storageClass: longhorn
  size: 50Gi
  accessMode: ReadWriteOnce

agent:
  # podTemplate mặc định — Kubernetes plugin dùng làm base
  image: jenkins/inbound-agent
  tag: latest
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: "1"
      memory: 1Gi

# không cần backup built-in — dùng Longhorn snapshot riêng
backup:
  enabled: false
```

Bước 3 — Install:
```bash
helm install jenkins jenkins/jenkins \
  -n jenkins \
  -f jenkins-values.yaml \
  --version 5.3.3 \
  --wait --timeout 5m

# lấy admin password
kubectl get secret -n jenkins jenkins -o jsonpath='{.data.jenkins-admin-password}' | base64 -d && echo
```

Bước 4 — Kích build để thấy dynamic agent:
```bash
# tạo Freestyle job qua CLI (JCasC hoặc groovy seed)
# hoặc vào UI → New Item → Pipeline → script:
cat << 'GROOVY'
pipeline {
  agent {
    kubernetes {
      yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: main
    image: alpine:3.19
    command: ['cat']
    tty: true
"""
    }
  }
  stages {
    stage('Hello') {
      steps {
        container('main') {
          sh 'echo "Build on $(hostname)" && uname -a'
        }
      }
    }
  }
}
GROOVY
```

**Kết quả:**
```text
$ kubectl get pods -n jenkins
NAME                       READY   STATUS    RESTARTS   AGE
jenkins-0                  2/2     Running   0          3m

$ kubectl get secret -n jenkins jenkins -o jsonpath='{.data.jenkins-admin-password}' | base64 -d && echo
xK9mR2pQwN4vL7jT          ← mật khẩu ngẫu nhiên Helm sinh

# trong lúc build chạy:
$ kubectl get pods -n jenkins
NAME                           READY   STATUS    RESTARTS   AGE
jenkins-0                      2/2     Running   0          10m
jenkins-agent-abc123-xyz456    1/1     Running   0          8s    ← agent Pod ephemeral

# sau khi build xong:
$ kubectl get pods -n jenkins
NAME          READY   STATUS    RESTARTS   AGE
jenkins-0     2/2     Running   0          12m
                                                              ← agent Pod đã tự xóa
```
→ **Verify:** `jenkins-0` Running; lấy được admin password từ Secret (không hardcode); trong lúc build thấy thêm `jenkins-agent-*` Pod; sau khi build Pod agent biến mất; vào `https://jenkins.lab.local` login được với password trên.

---

## 5. Secrets + TLS + thứ tự deploy

**Chốt:** credential phải nằm trong K8s Secret (hoặc External Secrets Operator kéo từ Vault/AWS SM) — **không bao giờ hardcode vào Helm values commit lên Git**. TLS cho mọi ingress qua cert-manager. Thứ tự deploy đúng: Longhorn → MinIO → CNPG → Harbor → SonarQube → Jenkins.

- **K8s Secret:** base64-encoded, lưu trong etcd. Đủ tốt cho kind-lab và môi trường nhỏ nếu etcd được mã hóa at-rest.
- **External Secrets Operator (ESO):** sync credential từ secret store ngoài (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager) vào K8s Secret. Dùng ở production khi cần audit trail, rotation tự động.
- **Helm values với existingSecret:** mọi Helm chart tốt đều có tham số `existingSecret` — trỏ tên Secret K8s thay vì nhận plain-text password trong `values.yaml`. Cách này `values.yaml` commit Git an toàn.
- **TLS ingress:** cert-manager + ClusterIssuer `letsencrypt-staging` (test) / `letsencrypt` (prod) tự issue cert. Kind-lab dùng self-signed hoặc letsencrypt staging để test luồng.
- **Thứ tự deploy:** phụ thuộc tầng nào dùng tầng nào.

**Vì sao:** secret bị leak trong Git là incident bảo mật phổ biến nhất. `git log` giữ credential mãi mãi dù bạn xóa commit sau. Dùng `existingSecret` ngăn credential vào Git từ đầu. Thứ tự deploy đúng tránh app khởi động trước khi dependency ready → CrashLoopBackOff rồi tự phục hồi (tốn thời gian, log nhiễu).

**Cơ chế:** ESO có `SecretStore` (kết nối Vault/ASM) và `ExternalSecret` (định nghĩa key nào sync thành K8s Secret nào). Khi ExternalSecret được apply, ESO operator kéo credential về, tạo/cập nhật K8s Secret. App mount Secret qua `envFrom` hoặc `secretKeyRef`. Rotation: Vault rotation trigger ESO resync → K8s Secret update → app reload env (cần restart Pod hoặc dùng Reloader).

| Thứ tự | App | Phụ thuộc |
|---|---|---|
| 1 | Longhorn | Bare K8s nodes |
| 2 | MinIO | Longhorn PVC (data) |
| 3 | CNPG | Longhorn PVC (WAL/data) |
| 4 | Harbor | CNPG DB `harbor` + MinIO bucket |
| 5 | SonarQube | CNPG DB `sonarqube` + Longhorn PVC |
| 6 | Jenkins | Longhorn PVC |

> 💡 **Ẩn dụ:** credential trong Git như ghi mật khẩu vào mặt sau danh thiếp — ai cầm danh thiếp cũng biết. `existingSecret` như nói "hỏi bảo vệ ở cổng" — danh thiếp (values.yaml) không ghi gì nhạy cảm.

**Dùng / không dùng:**
- K8s Secret cho kind-lab và môi trường nhỏ với etcd encryption at-rest.
- External Secrets Operator cho production multi-team, cần audit và rotation.
- **Phản đề:** ESO thêm một component cần vận hành (operator + SecretStore). Với team nhỏ, K8s Secret đủ nếu etcd encrypted. Đừng thêm ESO chỉ vì nghe "production phải có" — có chi phí vận hành thật.

**Làm:**
```bash
# pattern existingSecret — tạo Secret trước, Helm values trỏ vào
kubectl create secret generic my-app-secret \
  -n my-namespace \
  --from-literal=db-password='s3cr3t' \
  --from-literal=s3-secret-key='minio123'

# kiểm tra Secret đã tạo (không thấy plain-text — chỉ thấy key names)
kubectl get secret -n my-namespace my-app-secret -o jsonpath='{.data}' | python3 -m json.tool
```

Cấu trúc values.yaml an toàn (không có plain-text):
```yaml
# values.yaml — có thể commit Git
database:
  existingSecret: my-app-secret
  existingSecretKey: db-password
  # password: ""   ← KHÔNG điền, để trống

objectStorage:
  existingSecret: my-app-secret
  existingSecretKey: s3-secret-key
  # secretKey: ""  ← KHÔNG điền
```

Kiểm tra thứ tự deploy và trạng thái:
```bash
# kiểm tra tất cả app tier đã up
for ns in harbor sonarqube jenkins; do
  echo "=== $ns ===" && kubectl get pods -n $ns
done
```

**Kết quả:**
```text
$ kubectl get secret -n my-namespace my-app-secret -o jsonpath='{.data}' | python3 -m json.tool
{
    "db-password": "czNjcjN0",    ← base64, không phải plain-text
    "s3-secret-key": "bWluaW8xMjM="
}

$ for ns in harbor sonarqube jenkins; do echo "=== $ns ===" && kubectl get pods -n $ns; done
=== harbor ===
NAME                                    READY   STATUS    RESTARTS   AGE
harbor-core-7d9f8c6b4-xk2pq            1/1     Running   0          15m
harbor-jobservice-6c8d9f7b5-mn4rs      1/1     Running   0          15m
harbor-portal-5b7c8d9f6-qw3rt          1/1     Running   0          15m
harbor-registry-8f9b6c5d4-lk7mn       1/1     Running   0          15m
harbor-trivy-0                          1/1     Running   0          15m
harbor-redis-0                          1/1     Running   0          15m
=== sonarqube ===
NAME                         READY   STATUS    RESTARTS   AGE
sonarqube-7d8f9c6b5-xk2mn   1/1     Running   0          10m
=== jenkins ===
NAME          READY   STATUS    RESTARTS   AGE
jenkins-0     2/2     Running   0          8m
```
→ **Verify:** tất cả namespace không có Pod `CrashLoopBackOff`; Secret chỉ lưu base64; `values.yaml` không chứa plain-text credential nào. Kiểm tra Git diff: file values.yaml không có password.

---

## 🧹 Dọn dẹp
```bash
helm uninstall jenkins  -n jenkins   --wait
helm uninstall sonarqube -n sonarqube --wait
helm uninstall harbor   -n harbor    --wait

# xóa PVC (chứa data — cân nhắc kỹ trước khi xóa)
kubectl delete pvc -n jenkins   --all
kubectl delete pvc -n sonarqube --all
kubectl delete pvc -n harbor    --all

# xóa namespace
kubectl delete namespace harbor sonarqube jenkins

# xóa DB CNPG
kubectl delete database harbor-db sonarqube-db -n cnpg-system
```

---

## ✅ Đủ khi

① Giải thích được tại sao DB + S3 tách khỏi Pod (12-factor stateless Pod) và ánh xạ vào Longhorn/MinIO/CNPG.
② Deploy được Harbor với external CNPG DB + MinIO S3; push/pull image thành công.
③ Hiểu tại sao bật GC + tag retention ngay từ đầu với Harbor.
④ Deploy được SonarQube; biết sysctl `vm.max_map_count` là gì và tại sao cần initContainer.
⑤ Biết tại sao KHÔNG dùng PgBouncer transaction mode cho SonarQube.
⑥ Deploy được Jenkins; thấy agent Pod spawn và tự xóa sau build.
⑦ Biết tại sao JENKINS_HOME PVC là ReadWriteOnce — chỉ 1 controller.
⑧ Dùng `existingSecret` thay plain-text trong values.yaml; không bao giờ commit credential.
⑨ Biết thứ tự deploy đúng và lý do.

---

## Recall
1. Tại sao Pod nên stateless? Kể 3 loại storage được dùng trong lab này và mỗi loại dùng cho dữ liệu gì?
2. Harbor dùng MinIO như thế nào? Protocol gì? Mount vào Pod không?
3. GC trong Harbor là gì? Không bật GC thì hậu quả gì?
4. SonarQube cần kernel param gì? Tại sao cần initContainer để set?
5. Tại sao KHÔNG dùng PgBouncer transaction mode cho SonarQube? Nên nối vào endpoint nào của CNPG?
6. Jenkins lưu state ở đâu? Tại sao không thể chạy 2 controller cùng lúc?
7. Agent Pod trong Jenkins Kubernetes plugin khác gì static agent? Lifecycle của agent Pod?
8. `existingSecret` trong Helm values giải quyết vấn đề gì so với hardcode password?
9. Thứ tự deploy 6 component theo phụ thuộc? Tại sao CNPG phải trước Harbor?
10. External Secrets Operator khác K8s Secret thuần ở điểm nào? Khi nào nên dùng ESO?

### Đáp án

1. Pod ephemeral — chết/reschedule là mất dữ liệu trong Pod. Ba loại: **Longhorn PVC** (block filesystem — config/plugin/JENKINS_HOME), **MinIO S3** (blob — image layer, chart artifact), **CNPG** (SQL — metadata, tag, project, analysis result).
2. Harbor dùng MinIO qua **S3 API** (HTTP, không mount vào filesystem). Registry component ghi layer blob vào MinIO bucket `harbor-registry`; chartmuseum ghi chart vào `harbor-chartmuseum`. Client không truy cập MinIO trực tiếp — qua Harbor làm proxy.
3. GC (Garbage Collection) xóa orphan blob khỏi MinIO sau khi tag/manifest bị delete. Không bật GC → manifest xóa nhưng blob vẫn nằm trong MinIO → MinIO dần hết dung lượng; không có tín hiệu cảnh báo rõ ràng.
4. Cần `vm.max_map_count=524288` vì Elasticsearch bundled trong SonarQube yêu cầu tối thiểu giá trị này để tạo virtual memory mapping. Cần initContainer privileged vì Pod thường không được sửa kernel parameter của host; initContainer `privileged: true` chạy `sysctl -w` trên node trước khi main container start.
5. PgBouncer transaction mode multiplex connection — sau mỗi transaction, connection server-side có thể về tay client khác, phá vỡ prepared statement (server-side state bị reset). SonarQube dùng prepared statement nên crash. Nên nối thẳng CNPG **`pg-main-rw`** (endpoint `-rw` = primary, đọc/ghi, không qua pooler transaction mode).
6. Jenkins lưu state trong `JENKINS_HOME` trên Longhorn PVC 50Gi. Không chạy 2 controller vì PVC `ReadWriteOnce` — chỉ 1 Pod mount tại 1 thời điểm; mount 2 Pod cùng lúc sẽ fail hoặc corrupt data.
7. Static agent: máy cố định, luôn online, có thể bị nhiễm dependency giữa các build. Dynamic agent (Kubernetes plugin): Pod ephemeral tạo khi có build, tự xóa sau khi xong — cách ly hoàn toàn, scale tự nhiên. Lifecycle: controller gọi K8s API tạo agent Pod → agent connect ngược JNLP → chạy build → build xong → controller xóa Pod.
8. `existingSecret` cho phép Helm values.yaml không chứa plain-text credential — chỉ chứa tên Secret và key. File values.yaml có thể commit Git an toàn. Nếu hardcode, `git log` giữ password mãi mãi dù xóa commit sau.
9. Longhorn (1) → MinIO (2, dùng Longhorn PVC) → CNPG (3, dùng Longhorn PVC) → Harbor (4, cần CNPG `harbor` DB + MinIO bucket sẵn) → SonarQube (5, cần CNPG `sonarqube` DB) → Jenkins (6, chỉ cần Longhorn). CNPG phải trước Harbor vì Harbor khi khởi động kết nối DB ngay; DB chưa có sẽ crash.
10. K8s Secret: manual create, không auto-rotate, etcd lưu base64. ESO: sync từ secret store ngoài (Vault/ASM/GSM) → audit trail, auto-rotate khi credential xoay. Dùng ESO khi: prod multi-team, cần rotation tự động, cần audit trail ai đọc secret. K8s Secret đủ khi: cluster nhỏ, ít team, etcd đã encrypt at-rest.

---

## Bắc cầu sang production

Ba app vừa deploy đại diện cho lớp **platform tooling** — infrastructure CI/CD và quality gate. Trên cụm thật với domain thật, chỉ thay đổi ở: (a) `externalURL` và ingress host thật, (b) cert-manager dùng `letsencrypt` production (không staging), (c) External Secrets Operator thay K8s Secret thuần, (d) CNPG cluster nhiều replica hơn với backup S3. Pattern externalize DB + S3 giữ nguyên.

Tiếp theo (lab 25 — Observability + HA hardening + DR): bổ sung Prometheus/Grafana monitor 3 app này, cấu hình Velero backup PVC + CNPG, và drill test failover để đảm bảo `JENKINS_HOME` không mất khi node chết.

---

## 📎 Nguồn

- Harbor Helm chart docs: https://goharbor.io/docs/latest/install-config/harbor-ha-helm/
- Harbor GC: https://goharbor.io/docs/latest/administration/garbage-collection/
- SonarQube on Kubernetes: https://docs.sonarsource.com/sonarqube/latest/setup-and-upgrade/deploy-on-kubernetes/
- SonarQube + PgBouncer issue: https://community.sonarsource.com/t/pgbouncer-transaction-mode/
- Jenkins Kubernetes plugin: https://plugins.jenkins.io/kubernetes/
- CloudNativePG docs: https://cloudnative-pg.io/docs/
- External Secrets Operator: https://external-secrets.io/
