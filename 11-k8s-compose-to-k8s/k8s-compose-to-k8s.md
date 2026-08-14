# 11 · Từ Docker Compose sang Kubernetes: Kompose, Skaffold, Kustomize, Helm, Argo CD

> **Chặng 4** — trước: Jobs/CronJob, HPA & troubleshoot · kế tiếp: Networking nâng cao (Chặng 6)

**Mục tiêu:** hiểu tại sao Compose không thay thế được K8s ở production; thạo mapping `service → Deployment+Service+ConfigMap+PVC`; dùng Kompose convert 1 compose.yml; hiểu vòng lặp phát triển cục bộ của Skaffold; nắm Kustomize overlay và Helm chart ở mức "đọc/chỉnh được"; hiểu Argo CD GitOps.  
**Nền:** đã qua Deployment, Service, ConfigMap, Secret, Storage (Chặng 1–3).  

## Tiền đề
```bash
kubectl config use-context orbstack
kubectl get nodes          # STATUS=Ready
# Cài Kompose (macOS)
brew install kompose
kompose version
# Cài Skaffold (macOS)
brew install skaffold
skaffold version
# Kustomize — có sẵn trong kubectl: kubectl kustomize --help
# Helm
brew install helm
helm version
```

---

## 1. Tại sao cần chuyển từ Docker Compose sang Kubernetes?

**Chốt:** Compose và K8s không phải thay thế nhau — Compose là **local dev + CI build**; K8s là **production-grade**. Chuyển không phải vì Compose "tệ", mà vì K8s giải quyết bài toán Compose không làm được.

- **Docker Compose** = một file YAML, `up/down/logs`, chạy trên một máy, nhẹ, tốc độ dev nhanh.
- **Kubernetes** = tự heal Pod, scale horizontal, rolling update không downtime, secret/config tách biệt, nhiều node — bài toán production thực sự.
- Compose container chết → **không ai mọc lại**. K8s Pod chết → Deployment tự tạo Pod mới.
- Compose không có health check native như K8s liveness/readiness probe.

**Vì sao:** team thường gặp lúc production rớt một service, ops vào SSH tay restart — vì Compose không tự heal. K8s controller loop liên tục so sánh *desired state* vs *actual state*, tự đưa về đúng.

**Cơ chế:** K8s dùng **control loop** (reconciliation): mỗi controller (Deployment Controller, ReplicaSet Controller) liên tục poll etcd, phát hiện sai lệch và tự sửa. Compose không có loop này — nó là công cụ chạy một lần (`up`), không giám sát sau đó.

> 💡 **Ẩn dụ:** Compose như bật bếp rồi ra ngoài — nếu bếp tắt không ai bật lại. K8s như thermostat — phòng lạnh hơn set point là máy sưởi tự bật, không cần người.

| | Docker Compose | Kubernetes |
|---|---|---|
| Tự heal | Không | Có (Deployment controller) |
| Scale horizontal | Giới hạn | `kubectl scale`, HPA tự động |
| Rolling update | Không built-in | Có, không downtime |
| Multi-node | Không | Mục tiêu thiết kế |
| Phù hợp | Local dev, CI | Production, staging |

**Dùng / không:** Compose cho dev local nhanh + CI build image. **Phản đề:** nhiều team cố dùng Compose lên production (Docker Swarm mode) — chấp nhận được với workload nhỏ, nhưng thiếu nhiều tính năng K8s khi scale.

**Làm** (tạo compose mẫu để dùng xuyên suốt bài):
```bash
mkdir -p /tmp/kompose-demo && cd /tmp/kompose-demo

cat > docker-compose.yml <<'EOF'
version: "3"
services:
  nginx:
    image: nginx:alpine
    ports:
      - "8080:80"
  node:
    image: node:18-alpine
    ports:
      - "3000:3000"
    environment:
      - NODE_ENV=production
EOF
```
**Kết quả:**
```text
$ cat docker-compose.yml
version: "3"
services:
  nginx:
    ...
  node:
    ...
```
→ **Verify:** file tồn tại, 2 service `nginx` và `node`.

---

## 2. Mapping Compose → K8s

**Chốt:** mỗi `service` trong Compose ánh xạ thành **nhiều** resource K8s. Compose gộp tất cả vào một block; K8s tách ra từng file có trách nhiệm riêng.

- Không có mapping 1-1: `service` → `Deployment` + `Service` + (tuỳ) `ConfigMap` / `PVC`.
- `environment` non-sensitive → `ConfigMap`; sensitive → `Secret`.
- `networks: bridge` → K8s không cần khai báo, tự cấp DNS nội bộ qua `ClusterIP` Service.

**Vì sao:** Compose ẩn complexity — một `ports` field làm hết nhiệm vụ expose. K8s tách rõ vì mỗi thứ có vòng đời khác nhau: Pod chết nhưng Service (endpoint) vẫn ổn định; ConfigMap đổi không cần rebuild image.

**Cơ chế:** K8s dùng **label selector** để Service tìm đúng Pod (thay vì container name như Compose). `spec.selector.matchLabels` trong Deployment phải khớp `spec.selector` trong Service — đây là điểm hay quên.

> 💡 **Ẩn dụ:** Compose như ngôi nhà "all-in-one" (điện/nước/gas đều đi cùng đường). K8s như hạ tầng tòa nhà hiện đại — điện, nước, mạng đi đường riêng, sửa 1 hệ không đụng hệ kia.

| Compose field | K8s resource |
|---|---|
| `image`, `ports`, `container_name` | `Deployment` → Pod template |
| `ports` (expose ra ngoài) | `Service` (ClusterIP / NodePort / LoadBalancer) |
| `volumes` (persistent) | `PersistentVolumeClaim` + `PersistentVolume` |
| `volumes` (scratch/config) | `emptyDir`, `hostPath`, hoặc `ConfigMap` |
| `environment` / `env_file` | `ConfigMap` (non-sensitive) · `Secret` (sensitive) |
| `networks: bridge` | K8s tự cấp DNS nội bộ cluster qua Service ClusterIP |
| `replicas` (Compose v3 deploy) | `spec.replicas` trong Deployment |

**Dùng / không:** mapping tay cho 1-2 service là bài học tốt. **Phản đề:** 10+ service thì tự viết tay cực khổ và dễ sai — đó là lý do Kompose ra đời.

**Làm** (tự viết tay Deployment + Service cho `nginx` từ compose trên):
```bash
cat > /tmp/web-deployment.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: web
        image: nginx:alpine
        ports:
        - containerPort: 80
        env:
        - name: APP_ENV
          value: development
---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector:
    app: web
  ports:
  - port: 80
    targetPort: 80
EOF

kubectl apply -f /tmp/web-deployment.yml
kubectl get deploy,svc web
kubectl delete -f /tmp/web-deployment.yml
```
**Kết quả:**
```text
$ kubectl apply -f /tmp/web-deployment.yml
deployment.apps/web created
service/web created

$ kubectl get deploy,svc web
NAME                  READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/web   1/1     1            1           8s

NAME          TYPE        CLUSTER-IP      PORT(S)   AGE
service/web   ClusterIP   10.96.147.201   80/TCP    8s
```
→ **Verify:** Deployment READY 1/1, Service có ClusterIP.

---

## 3. Kompose — tự động convert Compose sang K8s YAML

**Chốt:** Kompose đọc `docker-compose.yml` → sinh Deployment + Service (và PVC nếu cần) tương ứng trong vài giây. Output = YAML sẵn sàng `kubectl apply`. Đây là **điểm xuất phát**, không phải kết quả cuối.

- Mặc định sinh `Deployment` + `ClusterIP Service`.
- Override được: `DaemonSet`, `ReplicationController`, hoặc Helm chart (`--chart`).
- Output thường có nhiều label `kompose.io/...` — **cần dọn trước khi commit**.

**Vì sao:** viết tay mapping 5 service mất vài chục phút và dễ sai label selector. Kompose làm trong 1 giây, sau đó chỉnh tay phần cần.

**Cơ chế:** Kompose parse `docker-compose.yml` theo schema, map từng field theo bảng ở mục 2, rồi serialize ra YAML/JSON K8s. Với `volumes` có `driver: local` → sinh PVC; `tmpfs` → `emptyDir`. Với `env_file` → sinh `ConfigMap` riêng. Label `kompose.io/service.type` trong compose điều khiển loại Service sinh ra.

> 💡 **Ẩn dụ:** Kompose như Google Translate cho YAML — bản dịch đủ ý nhưng cần người bản ngữ (bạn) đọc lại và chỉnh cho tự nhiên trước khi dùng thật.

| Tool | Vai trò | Khi nào dùng |
|---|---|---|
| **Kompose** | Convert compose → K8s YAML (1 lần) | Khởi điểm nhanh, sau đó chỉnh tay |
| **Skaffold** | Dev loop: watch → rebuild → redeploy | Đang code lặp nhanh với K8s |
| **Kustomize** | Overlay env (dev/prod) không template | Quản lý nhiều môi trường, không Go template |
| **Helm** | Package + versioning + rollback | Phân phối chart, quản lý release |

**Dùng / không:** Kompose cho kickstart nhanh khi migrate project Compose sang K8s. **Phản đề:** đừng dùng output Kompose nguyên xi lên production — thiếu resource limits, liveness probe, namespace, security context. Label `kompose.io/*` là noise. Kompose chỉ là điểm xuất phát; YAML prod cần review kỹ.

**Làm:**
```bash
cd /tmp/kompose-demo   # đã có docker-compose.yml từ mục 1

# Convert cơ bản — sinh file vào cùng thư mục
kompose convert
ls -1

# Convert vào thư mục riêng
mkdir -p k8s
kompose convert -o k8s/
ls k8s/

# Xem YAML trước khi ghi file
kompose convert --stdout

# Chỉ định số replicas
kompose convert -o k8s/ --replicas 2

# Xem 1 file sinh ra
cat k8s/nginx-deployment.yaml

# Apply và dọn
kubectl apply -f k8s/
kubectl get deploy,svc
kubectl delete -f k8s/
```
**Kết quả — `kompose convert`:**
```text
$ kompose convert
INFO Kubernetes file "nginx-service.yaml" created
INFO Kubernetes file "node-service.yaml" created
INFO Kubernetes file "nginx-deployment.yaml" created
INFO Kubernetes file "node-deployment.yaml" created
```
**`kubectl apply -f k8s/`:**
```text
deployment.apps/nginx created
deployment.apps/node created
service/nginx created
service/node created
```
**`kubectl get deploy,svc`:**
```text
NAME                    READY   UP-TO-DATE   AVAILABLE
deployment.apps/nginx   1/1     1            1
deployment.apps/node    1/1     1            1

NAME            TYPE        CLUSTER-IP
service/nginx   ClusterIP   10.96.12.1
service/node    ClusterIP   10.96.45.7
```
→ **Verify:** 4 file sinh ra (2 Deployment + 2 Service), apply thành công, 2 Deployment READY 1/1.

Flags hữu ích:

| Flag | Tác dụng |
|---|---|
| `-f <file>` | chỉ định compose file khác tên mặc định |
| `-o <dir\|file>` | output dir hoặc gộp 1 file |
| `--stdout` | in ra console, không ghi file |
| `--replicas N` | set số replicas trong Deployment |
| `--chart` | sinh Helm chart thay vì YAML thuần |
| `--volumes emptyDir\|hostPath\|configMap` | ghi đè kiểu volume |

---

## 4. Skaffold — vòng lặp dev liên tục lên K8s

**Chốt:** Skaffold = `docker compose up` nhưng target là cụm K8s. Luồng: **watch source → rebuild image → redeploy vào K8s → tail logs** — tự động khi bạn lưu file.

- `skaffold dev` — watch + rebuild + redeploy liên tục, tail logs, Ctrl+C tự `kubectl delete` dọn sạch.
- `skaffold run` — build + deploy 1 lần, dùng khi test môi trường gần production.
- `skaffold init --compose-file` — sinh `skaffold.yml` + YAML K8s từ compose.

**Vì sao:** không có Skaffold, mỗi lần sửa code dev phải: `docker build` → `docker push` → `kubectl set image` → `kubectl rollout status` — 4 bước tay mỗi lần save file. Skaffold tự động hoá vòng lặp này.

**Cơ chế:** `skaffold.yml` khai báo hai phần: `build` (artifact = Dockerfile nào, image name gì) và `deploy` (manifest YAML ở đâu). Khi `dev`, Skaffold dùng file watcher, detect thay đổi source, trigger build → push registry cục bộ (OrbStack có registry nội bộ) → `kubectl apply` diff. Có tính năng `sync` để chỉ copy file đổi vào container (bỏ qua rebuild) — dùng với static HTML, Python script không cần compile.

> 💡 **Ẩn dụ:** Skaffold như `nodemon` cho Kubernetes — save file là app tự restart trong cluster, không cần làm gì thêm.

File `skaffold.yml` điển hình:
```yaml
apiVersion: skaffold/v4beta6
kind: Config
metadata:
  name: my-app
build:
  artifacts:
  - image: my-nginx
    context: .
    docker:
      dockerfile: nginx.dockerfile
deploy:
  kubectl:
    manifests:
    - k8s/*.yaml
```

**Dùng / không:** dev loop K8s local. `dev` khi đang code; `run` khi CI test lần cuối. **Phản đề:** nếu build chạy `npm install` (Angular/React), mỗi save file là build lại toàn bundle — rất chậm. Dùng `sync` cho file không cần compile, hoặc xem lại có cần K8s local hay Compose tạm thời vẫn đủ.

**Làm:**
```bash
mkdir -p /tmp/skaffold-demo && cd /tmp/skaffold-demo

cat > docker-compose.yml <<'EOF'
version: "3"
services:
  web:
    build: .
    ports:
      - "8080:80"
EOF

cat > Dockerfile <<'EOF'
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/
EOF

echo "<h1>Hello Skaffold</h1>" > index.html

# Init từ compose file → sinh k8s/*.yaml + skaffold.yml
skaffold init --compose-file docker-compose.yml -o skaffold.yml
ls          # skaffold.yml, k8s/

# Deploy 1 lần
skaffold run
kubectl get deploy,svc

# Dev loop (watch) — lưu ý: Ctrl+C để dừng
# skaffold dev
# Sửa index.html → Skaffold tự rebuild + redeploy

# Dọn
skaffold delete
```
**Kết quả — `skaffold run`:**
```text
Generating tags...
 - my-nginx -> my-nginx:abc1234
Building [my-nginx]...
...
Deploy complete
```
**`kubectl get deploy,svc`:**
```text
NAME                  READY   UP-TO-DATE   AVAILABLE
deployment.apps/web   1/1     1            1

NAME          TYPE           CLUSTER-IP     EXTERNAL-IP
service/web   LoadBalancer   10.96.99.10    localhost
```
→ **Verify:** Deployment READY 1/1, `skaffold delete` dọn sạch.

---

## 5. Kustomize — overlay không template (bổ sung theo roadmap)

**Chốt:** Kustomize = quản lý nhiều môi trường (dev/staging/prod) bằng **base + overlay**, không dùng template engine (`{{ }}`). Overlay chỉ khai báo **phần khác biệt**. Built-in `kubectl` từ 1.14.

- **Base** = YAML gốc, đủ để chạy.
- **Overlay** = patch chứa chỉ field cần override (replicas, image tag, env…).
- `kubectl kustomize <dir>` — render YAML (không apply).
- `kubectl apply -k <dir>` — render + apply.

**Vì sao:** không có Kustomize, mỗi env phải copy toàn bộ YAML và sửa tay → base thay đổi phải đồng bộ manual mọi bản copy. Với overlay, base thay đổi → mọi overlay tự hưởng lợi, chỉ giữ lại phần khác biệt.

**Cơ chế:** `kustomization.yaml` là file chỉ mục. Kustomize đọc `bases` → load YAML gốc → apply `patchesStrategicMerge` (strategic merge patch: merge thông minh theo schema K8s, không ghi đè toàn bộ field) hoặc `patchesJson6902` (JSON Patch). Không có template engine — không cần học Go template.

> 💡 **Ẩn dụ:** base = bản nhạc gốc; overlay = phần hướng dẫn cho từng nhạc cụ chơi khác — bản full vẫn từ gốc, chỉ ghi thêm chỗ cần biến tấu.

Cấu trúc điển hình:
```
k8s/
├── base/
│   ├── deployment.yaml
│   ├── service.yaml
│   └── kustomization.yaml     ← liệt kê resource
└── overlays/
    ├── dev/
    │   ├── kustomization.yaml  ← bases + patches
    │   └── patch-replicas.yaml
    └── prod/
        ├── kustomization.yaml
        └── patch-replicas.yaml
```

**Dùng / không:** quản lý nhiều env, không muốn Go template. **Phản đề:** nếu diff giữa các env quá lớn (thay đổi toàn cấu trúc, không chỉ vài field), Kustomize patch phức tạp hơn Helm — cân nhắc Helm. Kustomize tốt khi các env **gần nhau**, chỉ khác replicas/image tag/resource limits.

**Làm:**
```bash
mkdir -p /tmp/kustomize-demo/k8s/base \
         /tmp/kustomize-demo/k8s/overlays/prod
cd /tmp/kustomize-demo

# Base deployment
cat > k8s/base/deployment.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: web
        image: nginx:alpine
        ports:
        - containerPort: 80
EOF

cat > k8s/base/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- deployment.yaml
EOF

# Overlay prod — tăng replicas lên 3
cat > k8s/overlays/prod/patch-replicas.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3
EOF

cat > k8s/overlays/prod/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
bases:
- ../../base
patchesStrategicMerge:
- patch-replicas.yaml
EOF

# Xem YAML render (không apply)
kubectl kustomize k8s/base
kubectl kustomize k8s/overlays/prod     # replicas=3

# Apply overlay prod
kubectl apply -k k8s/overlays/prod
kubectl get deploy web
kubectl delete -k k8s/overlays/prod
```
**Kết quả — `kubectl kustomize k8s/overlays/prod` (trích):**
```text
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3     ← patch đã merge vào
  ...
```
**`kubectl apply -k k8s/overlays/prod`:**
```text
deployment.apps/web created
```
**`kubectl get deploy web`:**
```text
NAME   READY   UP-TO-DATE   AVAILABLE
web    3/3     3            3          ← replicas=3 từ overlay prod
```
→ **Verify:** base có replicas=1; overlay prod render ra replicas=3; apply thành công READY 3/3.

---

## 6. Helm — package manager cho K8s (bổ sung theo roadmap)

**Chốt:** Helm = package manager cho Kubernetes (tương tự apt/brew nhưng cho K8s resource). Đơn vị = **chart** (YAML template + metadata + default values). Khác Kustomize: Helm có versioning, release tracking, rollback.

- **Chart** = tập hợp template YAML + metadata.
- **values.yaml** = giá trị mặc định (image tag, replicas, ingress host…).
- **Release** = 1 lần install chart vào cluster — Helm theo dõi history.
- Template dùng Go template `{{ }}` để inject giá trị từ `values.yaml` hoặc `--set`.

**Vì sao:** Kustomize tốt khi tự quản lý YAML; Helm tốt khi **phân phối chart cho người khác dùng** (như npm package) hoặc cần rollback về revision cụ thể (Helm lưu history trong cluster). Nginx Ingress, cert-manager, Prometheus đều phân phối qua Helm chart.

**Cơ chế:** `helm install` render template (Go template engine) với values, gửi kết quả YAML vào K8s API, lưu release manifest vào Secret trong namespace `kube-system` (hoặc namespace target). `helm upgrade` tạo revision mới; `helm rollback` apply lại manifest của revision cũ.

> 💡 **Ẩn dụ:** Helm như npm — `helm install` = `npm install`, chart = package, values.yaml = `package.json` defaults, `--set` = env override lúc runtime.

| | Kustomize | Helm |
|---|---|---|
| Template engine | Không (strategic merge) | Có (Go template) |
| Versioning/rollback | Qua git | Built-in (`helm rollback`) |
| Phân phối | Git repo | Chart repo (Artifact Hub) |
| Dùng tốt khi | Quản lý env nội bộ | Phân phối chart, cài 3rd-party |

**Dùng / không:** cài chart 3rd-party (nginx-ingress, cert-manager), hoặc phân phối app cho nhiều team. **Phản đề:** Go template `{{ }}` trong YAML phức tạp rất khó đọc và debug — Kustomize thường dễ hơn khi chỉ quản lý internal env. Đừng dùng Helm chỉ vì nghe tên nhiều.

**Làm:**
```bash
# Tạo chart scaffold
helm create my-chart
ls my-chart/

# Render template không apply (dry-run)
helm template my-chart ./my-chart

# Install vào cluster
helm install my-release ./my-chart
kubectl get deploy,svc

# Override values khi install
helm upgrade my-release ./my-chart --set replicaCount=3

# Xem release list và history
helm list
helm history my-release

# Rollback về revision 1
helm rollback my-release 1
kubectl get deploy

# Xoá release
helm uninstall my-release

# Tìm chart public
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm search repo ingress-nginx
```
**Kết quả — `helm install my-release ./my-chart`:**
```text
NAME: my-release
LAST DEPLOYED: Thu Aug  7 09:00:00 2026
NAMESPACE: default
STATUS: deployed
REVISION: 1
```
**`helm list`:**
```text
NAME        NAMESPACE  REVISION  STATUS    CHART
my-release  default    1         deployed  my-chart-0.1.0
```
**`helm rollback my-release 1`:**
```text
Rollback was a success! Happy Helming!
```
→ **Verify:** `helm list` thấy STATUS=deployed; rollback thành công; `helm uninstall` xoá sạch release.

---

## 7. Argo CD — GitOps: commit = deploy (bổ sung theo roadmap)

![[gitops-flow.excalidraw]]

**Chốt:** Argo CD = GitOps controller trong cluster. Git repo là **source of truth** — commit YAML (hoặc Helm/Kustomize), Argo CD tự detect và sync vào cluster. Không ai `kubectl apply` tay lên production.

- **Application** (CRD của Argo CD): khai báo `source` (Git repo + path + revision) và `destination` (cluster + namespace).
- **Sync**: Argo CD so trạng thái Git vs cluster, phát hiện lệch → apply diff.
- **Auto-sync**: bật → mỗi push Git là tự deploy. Tắt → cần click "Sync" hoặc `argocd app sync`.
- **selfHeal**: nếu ai `kubectl edit` tay → Argo CD tự ghi đè về đúng Git.
- **Health status**: Progressing / Healthy / Degraded — Argo CD monitor liên tục.

**Vì sao:** không có GitOps, lịch sử cluster là bí ẩn — ai apply gì, khi nào, không rõ. Với Argo CD, mọi thay đổi đều có git commit kèm author + timestamp → audit trail đầy đủ. Rollback = `git revert` rồi push.

**Cơ chế:** Argo CD chạy controller trong cluster, poll Git repo (mặc định mỗi 3 phút hoặc webhook push). So sánh manifest Git với object K8s hiện tại dùng server-side diff. Phát hiện OutOfSync → (nếu auto-sync) apply. `prune: true` = xoá resource không còn trong Git. `selfHeal: true` = ghi đè thay đổi thủ công.

> 💡 **Ẩn dụ:** Argo CD như thermostat (lại): cluster là nhiệt độ phòng, Git là nhiệt độ set point. Chênh lệch → controller tự chỉnh. Ai mở cửa sổ (kubectl edit) → thermostat bù lại ngay.

| | Truyền thống | GitOps (Argo CD) |
|---|---|---|
| Deploy | `kubectl apply` tay | Commit → Argo CD sync |
| Audit | Không rõ ai làm gì | Git log đầy đủ |
| Rollback | `kubectl rollout undo` | `git revert` + push |
| Drift detection | Không có | Argo CD báo OutOfSync |

**Dùng / không:** production + staging với nhiều người, cần traceability. **Phản đề:** đội 1-2 người, app đơn giản, `kubectl apply` tay vẫn ổn — Argo CD thêm complexity (cài, quản lý). Đừng overkill khi team nhỏ mới bắt đầu.

**Làm** (cài Argo CD vào OrbStack local):
```bash
# Cài Argo CD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait --for=condition=available deploy/argocd-server -n argocd --timeout=120s

# Port-forward UI
kubectl port-forward svc/argocd-server -n argocd 8080:443 &
# Truy cập https://localhost:8080

# Lấy password admin
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d; echo

# Login CLI
argocd login localhost:8080 --username admin --insecure

# Tạo Application từ manifest
cat > /tmp/argo-app.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: HEAD
    path: guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: guestbook
  syncPolicy:
    syncOptions:
    - CreateNamespace=true
EOF
kubectl apply -f /tmp/argo-app.yaml

# Xem trạng thái
argocd app list
kubectl get application -n argocd
argocd app sync demo-app

kubectl get all -n guestbook

# Dọn
argocd app delete demo-app --yes
kubectl delete namespace guestbook
```

Manifest Application prod với Git + Kustomize:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-app
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://git.example.local/manifests/k8s-manifests.git
    targetRevision: main
    path: overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: prod
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

**Kết quả — `kubectl get application -n argocd`:**
```text
NAME       SYNC STATUS   HEALTH STATUS
demo-app   Synced        Healthy
```
**`argocd app sync demo-app`:**
```text
Name:               argocd/demo-app
Sync Status:        Synced
Health Status:      Healthy
...
GROUP  KIND        NAMESPACE  NAME               STATUS  HEALTH
apps   Deployment  guestbook  guestbook-ui        Synced  Healthy
       Service     guestbook  guestbook-ui        Synced  Healthy
```
→ **Verify:** Application STATUS=Synced, HEALTH=Healthy; `kubectl get all -n guestbook` thấy Pod running.

---

## 🧹 Dọn dẹp
```bash
kubectl delete -f /tmp/web-deployment.yml --ignore-not-found
kubectl delete -f /tmp/kompose-demo/k8s/ --ignore-not-found
kubectl delete -k /tmp/kustomize-demo/k8s/overlays/prod --ignore-not-found
helm uninstall my-release 2>/dev/null || true
kubectl delete namespace guestbook argocd --ignore-not-found
```

---

## Đủ khi
① Compose vs K8s — mỗi thứ giải quyết vấn đề gì, không phải thay thế nhau · ② 1 `service` Compose → những K8s resource nào, vì sao tách · ③ `kompose convert` sinh gì, cần làm gì trước khi commit YAML đó lên Git · ④ Skaffold `dev` vs `run` khác gì, vì sao `dev` chậm với Angular/React · ⑤ Kustomize base/overlay — overlay chỉ chứa phần khác biệt, base thay đổi overlay tự hưởng lợi · ⑥ Helm chart/values/release — `install`, `upgrade`, `rollback`, khác Kustomize điểm nào · ⑦ Argo CD Application source/destination, auto-sync + selfHeal là gì.

## Recall
Tự trả lời trước khi cuộn xuống Đáp án.

1. Compose container chết khác gì K8s Pod chết?
2. Một `service` Docker Compose ánh xạ tối thiểu thành những K8s resource nào?
3. `kompose convert` xong, việc đầu tiên phải làm trước khi commit là gì?
4. Cờ `--volumes emptyDir` trong Kompose có tác dụng gì?
5. `skaffold dev` khác `skaffold run` ở điểm nào? Khi nào dùng cái nào?
6. Kustomize overlay chứa gì? Tại sao không cần copy toàn bộ base YAML?
7. Helm release là gì? `helm rollback` rollback về đâu?
8. Argo CD `selfHeal: true` có nghĩa là gì trong thực tế vận hành?
9. Kompose vs Kustomize vs Helm — mỗi tool giải quyết vấn đề gì khác nhau?

### Đáp án

1. Compose container chết → **không ai khởi động lại**, phải ops vào tay. K8s Pod chết → Deployment controller phát hiện actual < desired, **tự tạo Pod mới** trong vài giây.
2. Tối thiểu: **Deployment** (Pod template, image, port, env) + **Service** (ClusterIP). Tuỳ compose file còn có PVC (volume), ConfigMap (env_file).
3. **Review và dọn label `kompose.io/*`**; thêm resource limits, liveness/readiness probe, namespace; kiểm tra volume type đúng chưa. Đừng commit nguyên xi.
4. Volume mount trong Compose được chuyển thành `emptyDir` thay vì PVC — dùng khi chỉ cần scratch space tạm, không cần persistent.
5. `dev` = watch code → rebuild image → redeploy liên tục + tail logs, Ctrl+C dọn sạch. `run` = build+deploy 1 lần rồi dừng. `dev` khi đang code; `run` khi CI test final.
6. Overlay chứa chỉ **phần khác biệt** (field cần override). Kustomize tự merge patch vào base — khi base thay đổi, overlay tự hưởng lợi, không cần sync tay.
7. Release = 1 lần install chart vào cluster (có tên, có lịch sử revision). `helm rollback my-release 1` → apply lại manifest của revision 1.
8. Nếu ai `kubectl edit` tay làm lệch trạng thái cluster vs Git, Argo CD **tự ghi đè lại** về đúng Git trong vài phút. Không ai "sửa lén" production được lâu.
9. **Kompose** = convert compose→K8s YAML 1 lần (kickstart). **Kustomize** = quản lý nhiều env nội bộ (base+overlay, no template). **Helm** = package + version + rollback (phân phối chart, cài 3rd-party).

## 📎 Nguồn & xem lại
- Docs: [kompose.io](https://kompose.io) · [skaffold.dev](https://skaffold.dev) · [kubectl.docs.kubernetes.io/references/kustomize](https://kubectl.docs.kubernetes.io/references/kustomize/) · [helm.sh/docs](https://helm.sh/docs) · [argo-cd.readthedocs.io](https://argo-cd.readthedocs.io).
