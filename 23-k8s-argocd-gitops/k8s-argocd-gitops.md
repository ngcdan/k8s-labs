# 23 · Argo CD / GitOps — App-of-Apps, HA, drift

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Operator/CRD + CloudNativePG · kế tiếp: Deploy app tier · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu GitOps là gì và tại sao Git là nguồn sự thật; cài Argo CD và viết Application CRD; thấy tận mắt drift detection và self-heal; triển khai App-of-Apps pattern để quản toàn bộ platform từ một điểm; biết cấu trúc Argo CD HA và khi nào cần.  
**Nền:** đã qua Helm + Kustomize (lab 11); biết Deployment/Service/ConfigMap; có cụm kind-lab 3-node.  

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

## ⚙️ Tiền đề

```bash
# 1. Tạo cụm kind 3-node nếu chưa có
cat > /tmp/kind-3node.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
EOF
kind create cluster --name kind-lab --config /tmp/kind-3node.yaml
kubectl config use-context kind-kind-lab

# 2. Verify cụm sẵn sàng
kubectl get nodes
# NAME                     STATUS   ROLES           AGE
# kind-lab-control-plane   Ready    control-plane   90s
# kind-lab-worker          Ready    <none>          75s
# kind-lab-worker2         Ready    <none>          75s

# 3. Cài Argo CD CLI (macOS)
brew install argocd
argocd version --client
# argocd: v2.12.x

# 4. Chuẩn bị namespace argocd (sẽ dùng xuyên lab)
kubectl create namespace argocd

# 5. Chuẩn bị repo Git demo (fork hoặc clone sẵn)
# Lab này dùng repo public mẫu của Argo CD team:
#   https://github.com/argoproj/argocd-example-apps
# Bạn có thể fork về https://github.com/<your-user>/argocd-example-apps
# để thử push + auto-sync (xem mục 3)
```

**✅ Đủ khi:** 3 node STATUS=Ready; `argocd version --client` trả version ≥2.10.

---

## 1. GitOps là gì — Git là nguồn sự thật

**Chốt:** GitOps là phương pháp vận hành trong đó **toàn bộ trạng thái mong muốn của hệ thống được lưu dưới dạng file khai báo (declarative) trong Git**. Một controller trong cluster liên tục **reconcile** cluster ↔ Git — phát hiện lệch là tự sửa. Audit trail, rollback, review đều qua git history, không cần công cụ ngoài.

- **Declarative desired-state trong Git**: YAML Deployment, Service, HelmRelease… sống trong repo. Không ai `kubectl apply` tay lên production — mọi thay đổi đi qua commit.
- **Controller reconcile vòng kín**: controller (Argo CD hoặc Flux) liên tục so Git vs cluster; phát hiện sai lệch → apply diff về đúng Git.
- **Audit/rollback = git history**: `git log` biết ai thay đổi gì, khi nào; rollback = `git revert` rồi push, không cần nhớ `kubectl rollout undo`.
- **Khác `kubectl apply` tay**: apply tay là push một chiều, không ai canh sau đó. GitOps là vòng lặp liên tục — cluster luôn được kéo về Git.

**Vì sao:** không có GitOps, cluster là "hộp đen" — ops SSH vào apply tay, tháng sau không ai nhớ ai làm gì. Incident xảy ra, rollback phải đoán. Với GitOps: mọi thay đổi có commit kèm author, reviewer, CI gate. Rollback là `git revert`, không phải nhớ lệnh `kubectl` nào.

**Cơ chế:** Argo CD chạy **application-controller** trong cluster, poll Git repo định kỳ (mặc định 3 phút) hoặc nhận webhook push ngay lập tức. So sánh manifest Git vs trạng thái live bằng **server-side diff** (gọi API `/api/v1/resources/...` của cluster). Kết quả là `SyncStatus` (Synced/OutOfSync) và `HealthStatus` (Healthy/Progressing/Degraded). Khi phát hiện OutOfSync, nếu `autoSync` bật → apply diff (không apply lại toàn bộ, chỉ diff).

> 💡 **Ẩn dụ:** Thermostat. Cluster = nhiệt độ phòng. Git = nhiệt độ set point. Controller = bộ điều nhiệt — phòng lạnh hơn set point thì bật sưởi, không cần người. Ai mở cửa sổ (kubectl edit tay) → thermostat bù ngay.

| | `kubectl apply` tay | GitOps (Argo CD) |
|---|---|---|
| Ai thay đổi | Không rõ | Git commit + author |
| Rollback | `kubectl rollout undo` (limited) | `git revert` + push |
| Drift detection | Không có | Argo CD báo OutOfSync |
| Review | Không | PR/MR trước khi merge |
| Audit trail | Không | Git log đầy đủ |

**Dùng / không:**
- GitOps **luôn hợp lý** khi từ 2 người trở lên chạm production, hoặc cần compliance audit.
- **Phản đề:** team 1 người, app prototype, còn thay đổi hàng giờ → `kubectl apply` tay nhanh hơn, Argo CD là overhead. Đừng setup GitOps cho sandbox học thử rồi bỏ.

**Làm** (tạo sơ đồ khái niệm bằng lệnh — xem diagram embed bên dưới):

```bash
# Confirm namespace argocd tồn tại
kubectl get namespace argocd
```

```text
NAME     STATUS   AGE
argocd   Active   2m
```

![[argocd-gitops.excalidraw]]

→ **Verify:** namespace `argocd` Active. Diagram trên hiển thị vòng GitOps: commit → Argo CD detect → reconcile → cluster.

---

## 2. Cài Argo CD + Application CRD

**Chốt:** Argo CD cài bằng một manifest YAML chính thức vào namespace `argocd`. Sau cài, cluster có thêm **Application** CRD — đây là đơn vị khai báo "sync repo này vào cluster kia". Tương tác qua UI (port-forward 8080) hoặc CLI `argocd`.

- **Cài vào namespace `argocd`**: apply stable manifest từ GitHub; sinh ~20 Deployment/Service/CRD.
- **`kind: Application`**: CRD trung tâm — khai báo `repoURL`, `path`, `targetRevision` (source) và `server`, `namespace` (destination).
- **Sync manual vs auto**: `syncPolicy.automated` tắt → phải `argocd app sync <name>` hoặc click Sync trong UI. Bật → mỗi commit Git tự apply.
- **UI**: port-forward `svc/argocd-server` 8080:443, truy cập `https://localhost:8080`.
- **CLI `argocd app`**: `list`, `sync`, `get`, `diff`, `delete` — workflow hàng ngày.

**Vì sao:** Application CRD là "hợp đồng" giữa Git và cluster. Thay vì dùng script CI push vào cluster (push model, cần credential cluster trong CI), Argo CD dùng **pull model**: controller trong cluster tự kéo từ Git — credential Git ở trong cluster, CI không cần biết cluster. Bảo mật và đơn giản hơn.

**Cơ chế:** `kubectl apply -f install.yaml` đăng ký các CRD (`Application`, `AppProject`, `ApplicationSet`) và deploy controller pods. `argocd-repo-server` clone Git, render manifest (Helm/Kustomize/plain YAML). `application-controller` nhận rendered manifest, diff với cluster API, báo SyncStatus. `argocd-server` là API/UI gateway.

> 💡 **Ẩn dụ:** Trung tâm điều vận. `argocd-repo-server` = kho tài liệu (lấy spec từ Git). `application-controller` = đội kiểm tra (so spec vs thực tế). `argocd-server` = bảng điều khiển tổng (bạn nhìn vào). Không ai deploy thủ công — trung tâm tự điều phối.

| Component | Vai trò |
|---|---|
| `argocd-repo-server` | Clone Git, render Helm/Kustomize → YAML |
| `application-controller` | Diff YAML vs live cluster, trigger sync |
| `argocd-server` | API + UI + CLI gateway |
| `argocd-redis` | Cache manifest (tránh pull Git liên tục) |
| `argocd-dex-server` | SSO/OIDC auth |

**Dùng / không:**
- Luôn pin version (`v2.12.x`) trong install.yaml — đừng dùng `stable` alias trong production (có thể nhảy major version).
- **Phản đề:** install.yaml mặc định dùng `argocd-redis` single instance — fine cho dev, nhưng HA cần `redis-ha` (xem mục 5).

**Làm:**

```bash
# Cài Argo CD vào namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Đợi server sẵn sàng (~2-3 phút)
kubectl wait --for=condition=available deploy/argocd-server \
  -n argocd --timeout=180s

# Xem pods
kubectl get pods -n argocd
```

```text
NAME                                                READY   STATUS    RESTARTS   AGE
argocd-application-controller-0                     1/1     Running   0          2m
argocd-dex-server-6b7b4f9d6-xk8q2                  1/1     Running   0          2m
argocd-notifications-controller-5c9f6bdbd5-w9r4t    1/1     Running   0          2m
argocd-redis-7b98b44777-mn2xp                       1/1     Running   0          2m
argocd-repo-server-69f74c9f96-j6q2p                 1/1     Running   0          2m
argocd-server-7d44b67d55-lk8wm                      1/1     Running   0          2m
```

```bash
# Port-forward UI
kubectl port-forward svc/argocd-server -n argocd 8080:443 &

# Lấy password admin ban đầu
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d; echo
# → in ra password ngẫu nhiên, vd: X7kPqR2mNzLw

# Login CLI
argocd login localhost:8080 --username admin --insecure
# "admin:login" logged in successfully

# Tạo Application đầu tiên (plain YAML, repo public)
cat > /tmp/app-guestbook.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: guestbook
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

kubectl apply -f /tmp/app-guestbook.yaml

# Sync thủ công lần đầu
argocd app sync guestbook

# Xem trạng thái
argocd app list
kubectl get application -n argocd
```

```text
$ argocd app list
NAME              CLUSTER                         NAMESPACE  PROJECT  STATUS  HEALTH   SYNCPOLICY  CONDITIONS
argocd/guestbook  https://kubernetes.default.svc  guestbook  default  Synced  Healthy  Manual      <none>

$ kubectl get application -n argocd
NAME        SYNC STATUS   HEALTH STATUS
guestbook   Synced        Healthy
```

→ **Verify:** `SYNC STATUS=Synced`, `HEALTH STATUS=Healthy`. Kiểm tra thêm `kubectl get all -n guestbook` — thấy Deployment + Service running.

---

## 3. Sync, drift detection, self-heal

**Chốt:** Argo CD liên tục so trạng thái cluster với Git. Khi cluster lệch Git (ai `kubectl edit` tay, ai scale trực tiếp...) → trạng thái chuyển sang **OutOfSync**. Bật `selfHeal: true` → Argo CD tự apply lại Git về cluster trong vài giây, không cần người can thiệp. `prune: true` xoá resource không còn trong Git. **Sync waves** kiểm soát thứ tự apply giữa các resource.

- **OutOfSync**: cluster lệch Git — có thể do edit tay, hoặc Git vừa được commit mới.
- **selfHeal: true**: Argo CD detect OutOfSync → tự apply Git về cluster; "ai sửa lén production thì controller hoàn trả về".
- **prune: true**: resource xoá khỏi Git → Argo CD xoá khỏi cluster. Không bật → resource "mồ côi" còn sót lại.
- **Sync waves**: annotation `argocd.argoproj.io/sync-wave: "0"` (thấp hơn apply trước) — dùng để đảm bảo CRD apply trước CR, namespace trước Deployment.

**Vì sao:** không có self-heal, GitOps chỉ là "deploy từ Git" chứ không phải "Git là nguồn sự thật duy nhất". Ai có quyền `kubectl` vào cluster vẫn có thể sửa lén và Git không phản ánh thực tế. Self-heal đóng vòng kiểm soát: cluster **luôn** bằng Git.

**Cơ chế:** `application-controller` poll cluster mỗi 3 phút (tuỳ chỉnh `--app-resync-period`). Khi phát hiện diff → set `SyncStatus=OutOfSync`. Nếu `autoSync` bật và `selfHeal: true` → gọi `kubectl apply` với manifest từ Git (qua `repo-server`). Nếu chỉ `autoSync` không `selfHeal` → chỉ sync khi Git có commit mới, không xử lý drift do edit tay.

> 💡 **Ẩn dụ:** Nhân viên giám sát bảo tàng. Git = bản gốc hiện vật. Cluster = phòng trưng bày. `selfHeal` = nhân viên thấy ai dịch chuyển hiện vật → lập tức đặt lại đúng vị trí bản gốc. Không có nhân viên → ai muốn dịch chuyển thì dịch, không ai biết.

| SyncPolicy field | Ý nghĩa | Mặc định |
|---|---|---|
| `automated.prune` | Xoá resource không còn trong Git | false |
| `automated.selfHeal` | Ghi đè drift do edit tay | false |
| `syncOptions: CreateNamespace=true` | Tạo namespace nếu chưa có | false |
| `retry.limit` | Thử lại sync bao nhiêu lần nếu fail | 0 |

**Dùng / không:**
- Production: bật cả `prune: true` + `selfHeal: true` để Git thật sự là nguồn sự thật.
- **Phản đề:** bật `selfHeal` mà không có cơ chế review Git → dev commit sai vào main → Argo CD apply sai lên production ngay lập tức. Cần branch protection + PR review trên repo Git manifest trước khi bật auto-sync + selfHeal cho production.

**Làm** (cập nhật guestbook app sang auto-sync + selfHeal, rồi thử gây drift):

```bash
# Cập nhật Application thêm auto-sync + selfHeal + prune
kubectl patch application guestbook -n argocd --type merge -p '{
  "spec": {
    "syncPolicy": {
      "automated": {
        "prune": true,
        "selfHeal": true
      }
    }
  }
}'

# Xem app đã có auto-sync
argocd app get guestbook | grep -E "Sync Policy|Self Heal|Prune"
```

```text
Sync Policy:            Automated
  Prune:                true
  Self Heal:            true
```

```bash
# Gây drift: scale deployment thủ công (lệch Git đang là replicas=1)
kubectl scale deployment guestbook-ui -n guestbook --replicas=3
kubectl get deployment guestbook-ui -n guestbook
# NAME           READY   UP-TO-DATE   AVAILABLE
# guestbook-ui   3/3     3            3

# Đợi ~30s, Argo CD tự phát hiện và sửa về replicas=1
sleep 30
kubectl get deployment guestbook-ui -n guestbook
```

```text
$ kubectl get deployment guestbook-ui -n guestbook
NAME           READY   UP-TO-DATE   AVAILABLE   AGE
guestbook-ui   1/1     1            1           5m   ← tự trở về 1
```

```bash
# Xem lịch sử sync trong Argo CD
argocd app history guestbook
```

```text
ID  DATE                           REVISION
0   2026-08-13 10:00:00 +0700 +07  HEAD (a1b2c3d)
1   2026-08-13 10:05:32 +0700 +07  HEAD (a1b2c3d)  ← sync tự động sau drift
```

→ **Verify:** scale lên 3 → Argo CD kéo về 1 trong ~30s; `argocd app history` thấy thêm 1 sync event.

---

## 4. App-of-Apps pattern

**Chốt:** App-of-Apps = một Application "root" (cha) trỏ vào thư mục chứa nhiều file `Application` con. Khi root sync → Argo CD tạo tất cả Application con → mỗi con tự sync workload riêng. Kết quả: **toàn bộ platform bootstrap từ 1 điểm** — apply 1 file YAML root là xong.

- **Root Application**: `kind: Application`, `path` trỏ vào thư mục `apps/` trong repo Git.
- **Application con**: mỗi file YAML trong `apps/` là 1 `kind: Application` (monitoring, ingress, app-web, app-api…).
- **Bootstrap toàn cụm**: disaster recovery → apply 1 file root → Argo CD tự kéo lại toàn bộ; không cần script dài.
- **Tách repo**: root và children có thể ở cùng repo hoặc khác repo — linh hoạt cho nhiều team.

**Vì sao:** quản lý 10+ app thì không thể apply từng Application YAML một. App-of-Apps cho phép "định nghĩa platform như code" — toàn bộ cluster là 1 Git tree. Thêm app mới = thêm file vào `apps/`, không cần chạy lệnh tay.

**Cơ chế:** root Application trỏ path `apps/` → `repo-server` render các file YAML trong đó → controller nhận, thấy `kind: Application` → tạo Application resource trong Argo CD. Mỗi Application con sau đó tự reconcile độc lập. Root chỉ quản "tập hợp Application", không quản workload trực tiếp.

> 💡 **Ẩn dụ:** Sơ đồ tổ chức công ty. Root Application = CEO (quản danh sách phòng ban). Mỗi Application con = trưởng phòng (quản workload của phòng mình). CEO không trực tiếp quản nhân viên — chỉ quyết định "có bao nhiêu phòng và phòng nào tồn tại".

```
repo: https://github.com/<user>/platform-gitops
├── root-app.yaml          ← apply 1 lần duy nhất (bootstrap)
└── apps/
    ├── monitoring.yaml    ← Application: cài kube-prometheus-stack
    ├── ingress.yaml       ← Application: cài ingress-nginx
    ├── app-web.yaml       ← Application: deploy app frontend
    └── app-api.yaml       ← Application: deploy app backend
```

**Dùng / không:**
- Rất hợp khi có ≥3 app/service trên cụm; disaster recovery cần nhanh.
- **Phản đề:** với 1-2 app, App-of-Apps là overkill — thêm 1 tầng indirection không cần thiết. Bắt đầu simple rồi migrate lên khi cần.

**Làm** (mô phỏng App-of-Apps trên kind-lab):

```bash
# Tạo cấu trúc repo local (giả lập git repo)
mkdir -p /tmp/platform-gitops/apps

# Application con 1: guestbook (đã có)
cat > /tmp/platform-gitops/apps/guestbook.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: guestbook
  namespace: argocd
  finalizers:
  - resources-finalizer.argocd.argoproj.io
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
    automated:
      prune: true
      selfHeal: true
    syncOptions:
    - CreateNamespace=true
EOF

# Application con 2: helm-guestbook (dùng Helm chart)
cat > /tmp/platform-gitops/apps/helm-guestbook.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: helm-guestbook
  namespace: argocd
  finalizers:
  - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: HEAD
    path: helm-guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: helm-guestbook
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
    - CreateNamespace=true
EOF

# Root Application — trỏ vào thư mục apps/ trên repo Git
# Vì lab dùng file local, ta apply trực tiếp các app con thay vì root thật
# (root thật cần push /tmp/platform-gitops lên Git public)
# Demo root Application YAML (tham khảo):
cat > /tmp/platform-gitops/root-app.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: platform-root
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/<your-user>/platform-gitops.git
    targetRevision: main
    path: apps
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
EOF

# Apply các app con (mô phỏng root đã sync)
kubectl apply -f /tmp/platform-gitops/apps/

# Xem tất cả Application
argocd app list
kubectl get applications -n argocd
```

```text
$ argocd app list
NAME                    CLUSTER                         NAMESPACE       PROJECT  STATUS  HEALTH   SYNCPOLICY
argocd/guestbook        https://kubernetes.default.svc  guestbook       default  Synced  Healthy  Auto-Prune-SelfHeal
argocd/helm-guestbook   https://kubernetes.default.svc  helm-guestbook  default  Synced  Healthy  Auto-Prune-SelfHeal

$ kubectl get applications -n argocd
NAME             SYNC STATUS   HEALTH STATUS
guestbook        Synced        Healthy
helm-guestbook   Synced        Healthy
```

→ **Verify:** cả 2 Application SYNC=Synced HEALTH=Healthy. Trong mô hình thật: apply `root-app.yaml` lên Argo CD → root sync → tạo children → children tự sync — toàn bộ platform up từ 1 lệnh.

---

## 5. HA mode

**Chốt:** Argo CD mặc định chạy single-instance mỗi component — đủ cho dev/staging. Production cần **HA mode**: `argocd-repo-server` ×2, `argocd-server` ×2, `application-controller` sharded, `redis-ha` ×3 (Redis Sentinel). HA đảm bảo Argo CD không thành single point of failure của toàn bộ deployment pipeline.

- **argocd-repo-server ×2**: clone Git và render manifest — nếu 1 pod crash, pod còn lại tiếp tục; có thể bị quá tải khi nhiều Application sync cùng lúc.
- **application-controller**: stateful (biết diff state), chạy StatefulSet; K8s ≥2.12 hỗ trợ **sharding** — mỗi shard quản một tập Application, không phải 1 controller quản tất cả.
- **argocd-server ×2**: API/UI gateway — stateless, scale ngang tự do; cần `LoadBalancer` hoặc Ingress phân tải.
- **redis-ha ×3**: Redis Sentinel — 1 primary + 2 replica; Sentinel bầu primary mới khi primary chết. Cache manifest và session.
- **PodDisruptionBudget (PDB)**: đảm bảo rolling update node không kill hết pod cùng lúc.

**Vì sao:** nếu Argo CD down thì không ai sync được → incident deployment bị chặn. Với GitOps, Argo CD là hạ tầng cốt lõi của pipeline, quan trọng như ingress controller hay etcd. HA không phải luxury — là yêu cầu cho production có SLA.

**Cơ chế:** Argo CD có manifest riêng `install-ha.yaml` (install manifests/ha/install.yaml). Với HA: `argocd-server` và `argocd-repo-server` được deploy dạng Deployment nhiều replica + HPA tuỳ chọn. `application-controller` là StatefulSet với env `ARGOCD_CONTROLLER_REPLICAS` điều khiển số shard. `redis-ha` dùng Helm chart `redis-ha` của DandyDeveloper.

> 💡 **Ẩn dụ:** Nhà máy điện. Single instance = 1 tổ máy phát — hỏng là mất điện toàn khu. HA = nhiều tổ máy + lưới điện dự phòng — 1 tổ bảo trì, tổ khác chạy bù, không ai mất điện.

| Component | Single instance | HA mode | Ghi chú |
|---|---|---|---|
| `argocd-server` | Deployment, 1 replica | Deployment, ≥2 replica | Stateless, scale ngang |
| `argocd-repo-server` | Deployment, 1 replica | Deployment, ≥2 replica | Stateless, scale ngang |
| `application-controller` | StatefulSet, 1 shard | StatefulSet, N shard | `ARGOCD_CONTROLLER_REPLICAS=N` |
| `argocd-redis` | Deployment, 1 replica | Helm `redis-ha`, 3 pod | Sentinel: 1 primary + 2 replica |
| `argocd-dex-server` | Deployment, 1 replica | Deployment, 1 replica | Thường không cần HA |

**PDB đi kèm HA:**
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: argocd-server-pdb
  namespace: argocd
spec:
  minAvailable: 1       # luôn có ít nhất 1 pod argocd-server
  selector:
    matchLabels:
      app.kubernetes.io/name: argocd-server
```

**Dùng / không:**
- HA mode cho production có SLA hoặc nhiều team phụ thuộc Argo CD để deploy.
- **Phản đề:** HA mode tốn resource gấp 3-4x so với single instance. Trên kind-lab 3-node RAM thấp thì `redis-ha` (3 pod) sẽ strain resource đáng kể. Chỉ bật HA trên cụm production thật — dev/staging dùng single instance cho nhẹ.

**Làm** (cài HA mode trên kind-lab — chú ý: resource-heavy, chỉ làm nếu còn ≥6 GB RAM):

```bash
# Cài HA manifest (thay thế install.yaml thường)
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/ha/install.yaml

# Đợi tất cả pod sẵn sàng (~4-5 phút)
kubectl wait --for=condition=available deploy/argocd-server \
  -n argocd --timeout=300s

# Xem số replicas
kubectl get deploy -n argocd
kubectl get statefulset -n argocd
```

```text
$ kubectl get deploy -n argocd
NAME                               READY   UP-TO-DATE   AVAILABLE   AGE
argocd-applicationset-controller   1/1     1            1           4m
argocd-dex-server                  1/1     1            1           4m
argocd-notifications-controller    1/1     1            1           4m
argocd-redis-ha-haproxy            3/3     3            3           4m    ← HA: 3 HAProxy
argocd-repo-server                 2/2     2            2           4m    ← HA: 2 replicas
argocd-server                      2/2     2            2           4m    ← HA: 2 replicas

$ kubectl get statefulset -n argocd
NAME                            READY   AGE
argocd-application-controller   1/1     4m
argocd-redis-ha                 3/3     4m    ← HA Redis: 3 pod (1 primary + 2 replica)
```

```bash
# Xem PDB đã tạo sẵn
kubectl get pdb -n argocd
```

```text
NAME                        MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE
argocd-redis-ha             2               N/A               1                     4m
argocd-repo-server          1               N/A               1                     4m
argocd-server               1               N/A               1                     4m
```

→ **Verify:** `argocd-server` READY 2/2, `argocd-repo-server` READY 2/2, `argocd-redis-ha` READY 3/3. PDB đã có cho 3 component chính — rolling update node không kill hết pod cùng lúc.

---

## 🧹 Dọn dẹp

```bash
# Xoá Application con trước
argocd app delete guestbook --yes
argocd app delete helm-guestbook --yes

# Xoá namespace workload
kubectl delete namespace guestbook helm-guestbook --ignore-not-found

# Xoá Argo CD
kubectl delete namespace argocd --ignore-not-found

# Xoá cụm kind nếu muốn
kind delete cluster --name kind-lab
```

---

## Đủ khi
① GitOps là gì — Git là nguồn sự thật, controller reconcile vòng kín, audit/rollback qua git history · ② Application CRD khai báo gì (source, destination, syncPolicy); sync manual vs auto · ③ drift detection: OutOfSync là gì, selfHeal làm gì, prune làm gì, khi nào không nên bật auto-sync + selfHeal · ④ App-of-Apps: root Application trỏ thư mục chứa nhiều Application con, bootstrap toàn cluster từ 1 file · ⑤ HA mode: component nào cần HA, vì sao redis-ha cần 3 pod (Sentinel), PDB bảo vệ gì.

---

## Recall
1. GitOps khác `kubectl apply` tay ở điểm gì cốt lõi nhất?
2. Argo CD Application CRD có những field chính nào? `source` và `destination` chứa gì?
3. `autoSync` bật thì Argo CD làm gì khi phát hiện OutOfSync?
4. `selfHeal: true` và `autoSync` khác nhau thế nào?
5. `prune: true` nghĩa là gì? Nếu không bật thì sao?
6. Sync waves là gì? Dùng khi nào?
7. App-of-Apps: root Application trỏ vào gì? Children Application quản gì?
8. HA mode: component nào stateless (scale ngang), component nào stateful?
9. Redis HA dùng 3 pod với cơ chế nào? Tại sao cần số lẻ?
10. Khi nào KHÔNG nên bật `selfHeal` trên production?

### Đáp án

1. `kubectl apply` tay là push 1 chiều, không ai canh sau đó. GitOps là **vòng kín liên tục** — controller so Git vs cluster, tự sửa lệch; mọi thay đổi đi qua Git commit có author + timestamp.
2. `source`: `repoURL`, `path`, `targetRevision`. `destination`: `server` (K8s API endpoint), `namespace`. Thêm: `syncPolicy` (automated/manual, prune, selfHeal).
3. Argo CD **apply diff** từ Git vào cluster — chỉ apply phần lệch, không apply lại toàn bộ.
4. `autoSync` sync khi Git có **commit mới**. `selfHeal` sync khi cluster bị **edit tay** (drift) dù Git không đổi. Cần bật cả 2 để Git thật sự là nguồn sự thật duy nhất.
5. `prune: true`: resource bị **xoá khỏi Git** → Argo CD xoá khỏi cluster. Không bật → resource "mồ côi" còn sót; cluster bẩn dần theo thời gian.
6. Sync waves dùng annotation `argocd.argoproj.io/sync-wave: "N"` (N nhỏ apply trước). Dùng khi resource phụ thuộc nhau: CRD phải apply trước CR; namespace phải tồn tại trước Deployment.
7. Root Application trỏ vào **thư mục chứa các file Application YAML** (ví dụ `apps/`). Children Application tự quản workload của mình (Helm chart, Kustomize overlay, plain YAML).
8. **Stateless** (scale ngang tự do): `argocd-server`, `argocd-repo-server`. **Stateful**: `application-controller` (biết diff state, chạy StatefulSet với sharding), `argocd-redis` (lưu cache/session).
9. **Redis Sentinel** — 1 primary + 2 replica; Sentinel process bầu primary mới khi primary chết. Cần số lẻ để quorum (đa số) khi bầu chọn: 3 pod = quorum 2/3, tránh split-brain.
10. Không bật `selfHeal` khi **repo Git manifest không có branch protection + PR review** — ai commit sai lên main là Argo CD apply ngay lên production. Phải có gate review Git trước khi trust selfHeal.

---

## Bắc cầu sang production

Trên cụm thật nhiều team, Argo CD thường deploy kèm:
- **ApplicationSet**: sinh nhiều Application tự động từ generator (cluster list, Git directory, Pull Request) — không phải tay tạo từng Application.
- **AppProject**: phân quyền — team A chỉ sync vào namespace `team-a`, không chạm namespace khác; repo Git được phép; cluster được phép.
- **Notifications**: webhook Slack/PagerDuty khi Application OutOfSync hoặc Degraded.
- **Image Updater**: bot tự commit tag image mới vào Git khi registry có build mới — kết nối CI và GitOps.
- **RBAC** Argo CD level: tách `admin` (cấu hình Argo CD) vs `developer` (chỉ sync app của team mình).

Mô hình Git thường gặp: **mono-repo manifest** (tất cả YAML platform trong 1 repo, nhiều path) hoặc **multi-repo** (mỗi app có repo manifest riêng, root repo chứa App-of-Apps). Chọn theo quy mô team và tốc độ thay đổi.

---

## 📎 Nguồn

- [argo-cd.readthedocs.io](https://argo-cd.readthedocs.io) — tài liệu chính thức đầy đủ nhất.
- [argoproj/argocd-example-apps](https://github.com/argoproj/argocd-example-apps) — repo mẫu dùng trong lab.
- [Argo CD Best Practices](https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/) — App-of-Apps, repo structure, project RBAC.
- [redis-ha Helm chart](https://github.com/DandyDeveloper/charts/tree/master/charts/redis-ha) — cài Redis Sentinel cho Argo CD HA.
- [ApplicationSet docs](https://argo-cd.readthedocs.io/en/stable/user-guide/application-set/) — tự động sinh Application từ template.
