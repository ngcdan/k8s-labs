# 25 · Observability + HA hardening + DR

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Deploy app tier · kế tiếp: (hết roadmap — tune theo tải thật) · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** cài metrics-server + kube-prometheus-stack; viết ServiceMonitor + alert rule; thiết lập resource requests/limits đúng hướng QoS; bảo vệ HA bằng PDB + ResourceQuota; nắm pipeline backup/DR 3 tầng và quy trình restore-test định kỳ.

**Nền:** đã qua lab 16 (etcd backup), lab 20 (Longhorn snapshot), lab 22 (CNPG WAL→S3), lab 17 (drain/upgrade node), lab 24 (app tier). Lab này tổng hợp tất cả thành một vòng vận hành: thấy (observability) → bảo vệ (HA hardening) → phục hồi (DR).

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

## ⚙️ Tiền đề

**1. Cụm kind 3-node đang chạy:**
```bash
kind get clusters          # kind-lab xuất hiện
kubectl get nodes          # 3 node STATUS=Ready
```

**2. Helm đã cài:**
```bash
helm version               # v3.x
helm repo list             # có hoặc chưa có repo, sẽ thêm ở mục 2
```

**3. Namespace lab:**
```bash
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace app        --dry-run=client -o yaml | kubectl apply -f -
```

**4. Kết nối internet** (Helm pull chart từ ghcr.io/prometheus-community, Docker pull từ registry).

**5. kubeconfig trỏ đúng context:**
```bash
kubectl config current-context    # kind-kind-lab
```

---

## 1. metrics-server + kubectl top

![[observability-dr.excalidraw]]

**Chốt:** metrics-server thu CPU/RAM realtime từ kubelet của mỗi node, phơi lên API `/apis/metrics.k8s.io/v1beta1` — đây là **nguồn dữ liệu của `kubectl top`**, HPA, VPA. Không có metrics-server, `kubectl top` báo lỗi và HPA không hoạt động.

- **metrics-server** = aggregated API server nhỏ gọn, scrape kubelet `/metrics/resource` mỗi 60s.
- `kubectl top nodes` — CPU/RAM của mỗi node (absolute + %).
- `kubectl top pods [-n <ns>] [--containers]` — CPU/RAM mỗi Pod / container.
- Dữ liệu chỉ là **window ngắn** (60–90s); để xem trend dài hạn cần Prometheus (mục 2).
- kind mặc định **không có** metrics-server — phải cài riêng.

**Vì sao:** không có metrics-server thì `kubectl top` báo `metrics not available`, HPA không tự scale, bạn mù quáng về tài nguyên. Trong kind-lab, đây là bước đầu tiên trước mọi việc tuning.

**Cơ chế:** kubelet trên mỗi node expose `/metrics/resource` (cAdvisor nhúng sẵn). metrics-server gọi endpoint đó qua kết nối TLS (bypass cert verify với flag `--kubelet-insecure-tls` khi dùng kind vì kubelet không có cert CA chính thức). metrics-server tổng hợp và đẩy vào API aggregation layer — `kubectl` đọc từ đó. Vòng lặp: kubelet → metrics-server → API aggregation → `kubectl top`.

> 💡 **Ẩn dụ:** metrics-server = đồng hồ năng lượng trên bảng điều khiển xe — chỉ mức tiêu thụ hiện tại (tốc độ tức thì), không phải hộp đen ghi lịch sử (Prometheus là hộp đen đó).

| So sánh | metrics-server | Prometheus |
|---|---|---|
| Độ trễ | ~60s (live) | ~15s–1m (có thể cấu hình) |
| Lịch sử | Không lưu | Lưu n ngày (retention) |
| Dùng cho | `kubectl top`, HPA, VPA | Dashboard, alert, trend dài hạn |
| Tốn RAM | ~30 MB | 500 MB+ (tuỳ cluster) |

**Dùng / không dùng:**
- Luôn cài metrics-server trên mọi cluster (điều kiện tiên quyết cho HPA).
- Không dùng metrics-server làm nguồn alert hoặc capacity planning dài hạn — chỉ dùng cho real-time và HPA.
- **Phản đề:** nhiều người cài metrics-server xong nghĩ đủ monitoring — thực ra metrics-server không lưu lịch sử, không alert, không dashboard. Mù alert = sự cố phát hiện muộn.

**Làm:**
```bash
# cài metrics-server cho kind (cần --kubelet-insecure-tls)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# patch để bỏ qua TLS kubelet (kind không có cert chính thức)
kubectl patch deployment metrics-server \
  -n kube-system \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

# đợi metrics-server Ready (~30s)
kubectl rollout status deployment/metrics-server -n kube-system

# xem CPU/RAM theo node
kubectl top nodes

# xem Pod tất cả namespace
kubectl top pods -A
```

**Kết quả:**
```text
$ kubectl top nodes
NAME                      CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%
kind-lab-control-plane    182m         9%     1124Mi          14%
kind-lab-worker           54m          2%     612Mi           7%
kind-lab-worker2          61m          3%     598Mi           7%

$ kubectl top pods -A
NAMESPACE     NAME                                          CPU(cores)   MEMORY(bytes)
kube-system   coredns-xxx-yyy                               3m           14Mi
kube-system   etcd-kind-lab-control-plane                   23m          58Mi
kube-system   kube-apiserver-kind-lab-control-plane         52m          312Mi
kube-system   metrics-server-xxx-yyy                        4m           22Mi
```
→ **Verify:** `kubectl top nodes` hiện 3 node với CPU% và MEMORY%; không còn lỗi `metrics not available`. CPU(cores) đơn vị `m` = millicores (1000m = 1 CPU).

---

## 2. Prometheus + Grafana (kube-prometheus-stack)

**Chốt:** `kube-prometheus-stack` (Helm chart của prometheus-community) cài trọn bộ: **Prometheus** (scrape + lưu metrics), **Alertmanager** (route alert), **Grafana** (dashboard), **kube-state-metrics** (metrics K8s objects), node-exporter (metrics OS). **ServiceMonitor** là CRD để khai báo target scrape theo label selector — không cần edit config Prometheus thủ công.

- **Prometheus** scrape `/metrics` từ các target, lưu time-series trong TSDB (mặc định 15 ngày).
- **ServiceMonitor** (CRD): khai báo "scrape tất cả Service có label `app=myapp` ở port `metrics`" → Prometheus tự discover.
- **Grafana** đọc Prometheus qua datasource, render dashboard theo PromQL.
- **Alertmanager** nhận alert từ Prometheus, route tới Slack/email/webhook.
- **kube-state-metrics** xuất metrics về K8s objects (Deployment replicas, Pod phase, PVC status…) — tách với metrics tài nguyên (metrics-server).

**Vì sao:** observability không chỉ là "máy còn sống không". Cần biết: latency endpoint tăng, disk pool sắp đầy, Pod restart nhiều lần, PVC sắp hết dung lượng — trước khi user gặp lỗi. kube-prometheus-stack là chuẩn công nghiệp, cài 1 lệnh Helm thay vì dựng tay từng component.

**Cơ chế:** Prometheus Operator (cài cùng chart) watch CRD `ServiceMonitor`/`PodMonitor`/`PrometheusRule` → tự động generate config scrape + reload Prometheus. Vòng scrape: Prometheus → gọi Service endpoint `/metrics` theo `ServiceMonitor` selector → lưu TSDB → Grafana query PromQL → hiển thị panel. Alert: PrometheusRule định nghĩa PromQL rule + threshold → Prometheus evaluate → fire → Alertmanager route.

> 💡 **Ẩn dụ:** Prometheus = camera an ninh quay liên tục (lưu lịch sử). Grafana = phòng điều phối nhiều màn hình. Alertmanager = hệ thống chuông báo động tự gọi điện khi phát hiện bất thường. ServiceMonitor = tờ đăng ký "camera số 12 quét khu vực này".

| Component | Vai trò | Tương đương |
|---|---|---|
| Prometheus | Scrape + lưu time-series | Database metrics |
| Grafana | Dashboard + visualization | Màn hình điều khiển |
| Alertmanager | Route + deduplicate alert | Trung tâm cảnh báo |
| kube-state-metrics | K8s object metrics | Trạng thái tầng orchestration |
| node-exporter | OS/hardware metrics | Trạng thái tầng máy chủ |

**Dùng / không dùng:**
- Cài kube-prometheus-stack sớm ngay khi cluster lên (trước khi có vấn đề, không phải sau).
- Không giữ retention quá dài (>30 ngày) trên kind-lab — TSDB sẽ to nhanh. Production: dùng Thanos/Cortex cho long-term storage.
- **Phản đề:** nhiều team cài xong không viết alert rule → Grafana chỉ dùng để "nhìn cho đẹp". Grafana không thay thế alert tự động; cần PrometheusRule + Alertmanager routing mới thật sự proactive.

**Làm:**
```bash
# thêm repo Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# cài kube-prometheus-stack
helm install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.adminPassword=admin123 \
  --set prometheus.prometheusSpec.retention=7d \
  --set alertmanager.enabled=true

# đợi tất cả Pod Ready (~2-3 phút)
kubectl rollout status deployment/monitoring-grafana -n monitoring
kubectl get pods -n monitoring
```

```bash
# port-forward Grafana để xem dashboard
kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80 &
# mở browser: http://localhost:3000  (admin / admin123)
```

```bash
# viết ServiceMonitor cho app namespace
cat > /tmp/servicemonitor.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: app-monitor
  namespace: monitoring
  labels:
    release: monitoring          # phải match label của Prometheus Operator
spec:
  namespaceSelector:
    matchNames:
    - app
  selector:
    matchLabels:
      app: myapp
  endpoints:
  - port: metrics
    interval: 15s
    path: /metrics
EOF
kubectl apply -f /tmp/servicemonitor.yaml
```

```bash
# viết PrometheusRule: alert khi disk pool Longhorn > 75%
cat > /tmp/alert-disk.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: longhorn-disk-alert
  namespace: monitoring
  labels:
    release: monitoring
spec:
  groups:
  - name: longhorn.rules
    rules:
    - alert: LonghornDiskUsageHigh
      expr: |
        longhorn_disk_usage_bytes / longhorn_disk_capacity_bytes > 0.75
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "Longhorn disk usage > 75% trên node {{ $labels.node }}"
        description: "Disk {{ $labels.disk }} dùng {{ $value | humanizePercentage }}."
EOF
kubectl apply -f /tmp/alert-disk.yaml
```

**Kết quả:**
```text
$ kubectl get pods -n monitoring
NAME                                                     READY   STATUS    RESTARTS   AGE
alertmanager-monitoring-kube-prometheus-alertmanager-0   2/2     Running   0          3m
monitoring-grafana-xxx                                   3/3     Running   0          3m
monitoring-kube-prometheus-operator-xxx                  1/1     Running   0          3m
monitoring-kube-state-metrics-xxx                        1/1     Running   0          3m
monitoring-prometheus-node-exporter-xxx (x3 nodes)      1/1     Running   0          3m
prometheus-monitoring-kube-prometheus-prometheus-0       2/2     Running   0          3m

$ kubectl get servicemonitor -n monitoring
NAME          AGE
app-monitor   12s

$ kubectl get prometheusrule -n monitoring
NAME                  AGE
longhorn-disk-alert   8s
```
→ **Verify:** 6 loại Pod Running trong namespace `monitoring`; `kubectl get servicemonitor` thấy `app-monitor`; trên Grafana (localhost:3000) → Dashboards → chọn "Kubernetes / Compute Resources / Cluster" thấy CPU/memory panels populated.

---

## 3. Resource requests/limits + QoS + tuning

**Chốt:** `requests` = lượng tài nguyên **được đảm bảo** (scheduler dùng để fit Pod lên node); `limits` = **trần không được vượt** (kernel kill nếu vượt memory limit; CPU bị throttle). Ba mức **QoS** quyết thứ tự evict khi node thiếu tài nguyên: Guaranteed > Burstable > BestEffort. **Tune bằng dữ liệu thật từ `kubectl top` + Prometheus**, không đoán.

- `requests.cpu` / `requests.memory`: Pod được đảm bảo ít nhất mức này — scheduler từ chối node không đủ.
- `limits.cpu` / `limits.memory`: trần tuyệt đối — vượt memory → OOMKilled; vượt CPU → throttle (không kill).
- **QoS Guaranteed**: requests = limits (cả cpu lẫn memory) → ưu tiên cao nhất, evicted cuối cùng.
- **QoS Burstable**: có requests < limits hoặc chỉ set một trong hai → mức trung.
- **QoS BestEffort**: không set requests lẫn limits → evicted đầu tiên khi node pressure.

**Vì sao:** không set requests → scheduler đặt Pod vào node không đủ RAM → node OOM → nhiều Pod chết cùng lúc (OOM cascade). Không set limits → 1 Pod hog toàn bộ CPU của node → các Pod khác chết đói. Nhưng set sai (quá thấp) → OOMKilled liên tục; set quá cao → lãng phí, bin-packing kém.

**Cơ chế:** scheduler tính `allocatable = node capacity – kube-reserved – system-reserved`. Mỗi Pod cộng requests vào "used". Khi node memory pressure, kubelet evict Pod theo thứ tự BestEffort → Burstable → Guaranteed. cgroups v2 enforce limits ở kernel level — `memory.limit_in_bytes` cho memory, CPU bandwidth controller cho CPU throttle.

> 💡 **Ẩn dụ:** requests = chỗ ngồi đặt trước trên máy bay (đảm bảo có chỗ), limits = hành lý xách tay tối đa (vượt là bị tịch thu). QoS = thứ tự ưu tiên thoát hiểm: Guaranteed (hạng thương gia) ra sau cùng; BestEffort (standby ticket) ra đầu tiên.

| QoS class | Điều kiện | Evict order | Khi nào dùng |
|---|---|---|---|
| Guaranteed | requests = limits (cả cpu+mem) | Cuối cùng | DB, critical stateful app |
| Burstable | requests < limits hoặc partial | Giữa | Web app, API server |
| BestEffort | Không set gì | Đầu tiên | Batch job, dev/test pod |

**Dùng / không dùng:**
- Luôn set requests cho mọi Pod production. Limits tùy: DB nên Guaranteed; web app thường Burstable hợp lý hơn.
- **Tune workflow:** chạy 1–2 tuần với `kubectl top pods --containers` + Prometheus metrics → lấy p95 CPU/RAM → set requests = p50, limits = p95–p99.
- Không đoán: "app này cần 256Mi" mà không có dữ liệu → thường sai, dẫn đến OOMKill hoặc lãng phí.
- **Phản đề:** set limits.cpu quá thấp (vd 100m cho app Java) → CPU throttle nặng → latency tăng mà Pod vẫn Running — rất khó debug nếu không có Prometheus metric `container_cpu_throttled_seconds_total`.

**Làm:**
```bash
# deploy app mẫu không có requests/limits (BestEffort)
cat > /tmp/app-besteffort.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
  namespace: app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: myapp
  template:
    metadata:
      labels:
        app: myapp
    spec:
      containers:
      - name: web
        image: nginx:alpine
EOF
kubectl apply -f /tmp/app-besteffort.yaml

# xem QoS class = BestEffort
kubectl get pod -n app -l app=myapp -o jsonpath='{range .items[*]}{.metadata.name}: {.status.qosClass}{"\n"}{end}'

# xem top pods để lấy baseline
kubectl top pods -n app --containers
```

```bash
# sau khi quan sát 1-2 phút, patch với requests/limits hợp lý
cat > /tmp/app-burstable.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
  namespace: app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: myapp
  template:
    metadata:
      labels:
        app: myapp
    spec:
      containers:
      - name: web
        image: nginx:alpine
        resources:
          requests:
            cpu: "50m"
            memory: "64Mi"
          limits:
            cpu: "200m"
            memory: "128Mi"
EOF
kubectl apply -f /tmp/app-burstable.yaml
kubectl rollout status deployment/web-app -n app

# confirm QoS Burstable
kubectl get pod -n app -l app=myapp -o jsonpath='{range .items[*]}{.metadata.name}: {.status.qosClass}{"\n"}{end}'
```

**Kết quả:**
```text
# trước khi set requests/limits
$ kubectl get pod -n app -l app=myapp -o jsonpath='{range .items[*]}{.metadata.name}: {.status.qosClass}{"\n"}{end}'
web-app-aaa-111: BestEffort
web-app-aaa-222: BestEffort

# sau khi apply với requests/limits
$ kubectl get pod -n app -l app=myapp -o jsonpath='{range .items[*]}{.metadata.name}: {.status.qosClass}{"\n"}{end}'
web-app-bbb-333: Burstable
web-app-bbb-444: Burstable

$ kubectl top pods -n app --containers
POD                   NAME   CPU(cores)   MEMORY(bytes)
web-app-bbb-333       web    2m           5Mi
web-app-bbb-444       web    1m           5Mi
```
→ **Verify:** QoS class đổi từ BestEffort → Burstable; `kubectl top` hiện CPU và MEMORY thực tế — so với requests (50m/64Mi) để biết còn head-room bao nhiêu.

---

## 4. PodDisruptionBudget + ResourceQuota/LimitRange

**Chốt:** **PDB** (`minAvailable` hoặc `maxUnavailable`) bảo vệ HA khi có **voluntary disruption** (drain node, rolling update, Cluster Autoscaler scale-down) — kubelet từ chối evict Pod nếu vi phạm PDB. **ResourceQuota** giới hạn tổng tài nguyên của cả namespace (tránh 1 team hog cluster). **LimitRange** đặt default request/limit cho Pod không khai báo, tránh BestEffort không kiểm soát.

- **PDB** `minAvailable: N` → phải có ít nhất N Pod healthy trước khi evict. `maxUnavailable: N` → được phép có tối đa N Pod unavailable cùng lúc.
- PDB **chỉ chặn voluntary disruption** (drain, Cluster Autoscaler) — không chặn node crash (involuntary).
- **ResourceQuota**: giới hạn tổng `requests.cpu`, `requests.memory`, `limits.cpu`, `limits.memory`, `count/pods`, v.v. trong 1 namespace.
- **LimitRange**: set default + min/max cho containers/pods — Pod không set resources sẽ nhận default từ LimitRange thay vì BestEffort.

**Vì sao:** không có PDB, `kubectl drain` (lab 17) có thể hạ toàn bộ Deployment xuống 0 replica trong khoảnh khắc → downtime. Không có ResourceQuota → một Deployment lỗi tạo vô số Pod → ăn hết resource cluster → mọi team bị ảnh hưởng. Không có LimitRange → dev quên set requests → Pod BestEffort → bị evict ưu tiên khi node pressure.

**Cơ chế:** PDB là object trong etcd. Khi `kubectl drain` gửi eviction request, API server check PDB của Pod đó — nếu evict sẽ vi phạm `minAvailable` → trả `429 Too Many Requests` (Disruption Budget exceeded) → drain block/retry. ResourceQuota: admission controller check tổng usage trước khi tạo Pod; nếu vượt quota → `403 Forbidden`. LimitRange: admission controller inject default vào Pod spec trước khi lưu etcd.

> 💡 **Ẩn dụ:** PDB = quy định "phòng ICU phải còn ít nhất 2 giường trống trước khi chuyển bệnh nhân đi nơi khác". ResourceQuota = ngân sách phòng ban (không được xài quá X đồng). LimitRange = quy tắc mặc định "nếu không khai báo, nhân viên mới được cấp laptop 8GB RAM" — không để ai dùng máy không giới hạn.

| Resource | Scope | Chặn cái gì | Ví dụ |
|---|---|---|---|
| PDB | Per-workload | Evict voluntary | drain node, Cluster Autoscaler |
| ResourceQuota | Per-namespace | Tạo mới vượt ngân sách | Pod, CPU, Memory tổng |
| LimitRange | Per-namespace | Pod không set resources | inject default requests/limits |

**Dùng / không dùng:**
- PDB: viết cho mọi Deployment production có ≥2 replica. `minAvailable: 1` là tối thiểu.
- ResourceQuota: nên có ở mọi namespace non-system trong shared cluster.
- LimitRange: bổ sung ResourceQuota, không thay thế — cả hai cùng tồn tại.
- **Phản đề:** `minAvailable` bằng số replica thực tế → drain luôn bị block, không drain được. PDB phải hợp lý: với 3 replica thì `minAvailable: 2` (chấp nhận 1 down) hoặc `maxUnavailable: 1`.

**Làm:**
```bash
# tạo PDB cho web-app: luôn giữ ít nhất 1 Pod
cat > /tmp/pdb.yaml <<'EOF'
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web-app-pdb
  namespace: app
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: myapp
EOF
kubectl apply -f /tmp/pdb.yaml

# xem trạng thái PDB
kubectl get pdb -n app
```

```bash
# thử drain 1 node (sẽ block khi vi phạm nếu chỉ còn 1 Pod)
# scale down trước để chỉ còn 1 replica
kubectl scale deployment/web-app -n app --replicas=1

# drain sẽ evict Pod → vi phạm PDB (minAvailable=1, hiện chỉ có 1) → block
kubectl drain kind-lab-worker --ignore-daemonsets --delete-emptydir-data --dry-run
```

```bash
# ResourceQuota cho namespace app
cat > /tmp/quota.yaml <<'EOF'
apiVersion: v1
kind: ResourceQuota
metadata:
  name: app-quota
  namespace: app
spec:
  hard:
    requests.cpu: "2"
    requests.memory: "2Gi"
    limits.cpu: "4"
    limits.memory: "4Gi"
    count/pods: "20"
EOF
kubectl apply -f /tmp/quota.yaml
kubectl describe quota app-quota -n app
```

```bash
# LimitRange: default request/limit cho Pod không khai báo
cat > /tmp/limitrange.yaml <<'EOF'
apiVersion: v1
kind: LimitRange
metadata:
  name: app-defaults
  namespace: app
spec:
  limits:
  - type: Container
    default:
      cpu: "100m"
      memory: "128Mi"
    defaultRequest:
      cpu: "50m"
      memory: "64Mi"
    max:
      cpu: "1"
      memory: "1Gi"
    min:
      cpu: "10m"
      memory: "16Mi"
EOF
kubectl apply -f /tmp/limitrange.yaml
kubectl describe limitrange app-defaults -n app
```

**Kết quả:**
```text
$ kubectl get pdb -n app
NAME          MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE
web-app-pdb   1               N/A               0                     15s

# ALLOWED DISRUPTIONS = 0 vì chỉ còn 1 replica = minAvailable
# khi scale lên 2+ replica → ALLOWED DISRUPTIONS = 1

$ kubectl describe quota app-quota -n app
Name:            app-quota
Namespace:       app
Resource         Used    Hard
--------         ----    ----
count/pods       1       20
limits.cpu       200m    4
limits.memory    128Mi   4Gi
requests.cpu     50m     2
requests.memory  64Mi    2Gi

$ kubectl describe limitrange app-defaults -n app
Name:       app-defaults
Namespace:  app
Type        Resource  Min   Max  Default Request  Default Limit  Max Limit/Request Ratio
----        --------  ---   ---  ---------------  -------------  -----------------------
Container   cpu       10m   1    50m              100m           -
Container   memory    16Mi  1Gi  64Mi             128Mi          -
```
→ **Verify:** `kubectl get pdb` hiện `ALLOWED DISRUPTIONS`; `kubectl describe quota` hiện Used vs Hard; `kubectl drain --dry-run` với 1 replica báo `Cannot evict pod as it would violate the pod's disruption budget`.

---

## 5. Backup/DR đa tầng + restore-test

**Chốt:** DR trong K8s không có một giải pháp duy nhất — cần **3 tầng độc lập**: (1) **etcd snapshot** = toàn bộ cluster state, (2) **Database WAL/dump** (CNPG→S3) = dữ liệu ứng dụng, (3) **PVC snapshot** (Longhorn) = data persistent volume. RTO/RPO khác nhau per tầng. **GOLDEN LESSON: backup chưa test-restore = chưa có backup.**

- **Tầng 1 — etcd snapshot** (lab 16): `etcdctl snapshot save` → S3/offsite. Restore: khởi động lại control-plane từ snapshot. RPO = tần suất snapshot (vd mỗi 6h). Restore toàn bộ cluster objects nhưng không có data trong PVC.
- **Tầng 2 — Database WAL/continuous backup** (lab 22): CNPG streaming WAL → object storage (S3/MinIO). Point-in-time recovery (PITR) tới bất kỳ giây nào trong retention window. RPO gần 0 (WAL ship real-time).
- **Tầng 3 — PVC snapshot** (lab 20): Longhorn `VolumeSnapshot` → snapshot local hoặc replicate offsite. Restore: tạo PVC từ snapshot. RPO = tần suất snapshot (vd mỗi 1h).

**Vì sao:** node crash không mất data (Longhorn replicate 3 bản). Nhưng nếu ai đó `kubectl delete --all` nhầm → etcd snapshot cứu cluster objects. Nếu DB bị corrupted → CNPG WAL PITR cứu data. Nếu app ghi nhầm vào volume → Longhorn snapshot cho phép rollback volume về thời điểm trước. Ba tầng phủ ba loại thảm họa khác nhau — thiếu một tầng là có lỗ hổng.

**Cơ chế:** etcd snapshot là binary file chứa toàn bộ key-value của cluster (YAML, secret, configmap…). CNPG WAL ship từng transaction log tới object storage, Barman đứng giữa xử lý. Longhorn snapshot dùng copy-on-write trên replica — snapshot tức thì, không cần dừng app. Restore yêu cầu: etcd → stop API server, restore binary, start lại. CNPG → tạo cluster mới với `bootstrap.recovery`. Longhorn → tạo PVC từ `VolumeSnapshot`.

> 💡 **Ẩn dụ:** etcd snapshot = ảnh chụp toàn bộ sơ đồ tổ chức + quy trình công ty (không gồm hồ sơ nhân viên thật). CNPG WAL = máy quay phim mọi giao dịch NH liên tục (replay được từng giây). Longhorn snapshot = ảnh chụp ổ cứng tại thời điểm T. Ba cái bảo vệ ba loại mất mát khác nhau: mất kiến trúc, mất giao dịch, mất file.

| Tầng | Công nghệ | RPO | RTO | Phục hồi cái gì |
|---|---|---|---|---|
| etcd snapshot | etcdctl + CronJob | Theo lịch (vd 6h) | 15–30 phút | Cluster objects (YAML, secrets…) |
| DB WAL (CNPG) | Barman + S3 | ~0 (real-time) | 5–15 phút | Dữ liệu app trong PostgreSQL |
| PVC snapshot (Longhorn) | VolumeSnapshot | Theo lịch (vd 1h) | 5–10 phút | Data trong persistent volume |

**Dùng / không dùng:**
- Cả 3 tầng cần thiết — không tầng nào thay thế tầng kia.
- **Lịch restore-test bắt buộc**: etcd quarterly, CNPG monthly, Longhorn monthly — đưa vào calendar, gắn owner.
- **Phản đề:** nhiều team có backup nhưng chưa bao giờ chạy restore → khi sự cố thật mới phát hiện restore guide bị lỗi, lệnh outdated, credential hết hạn → RTO thực tế 10× dự kiến.

**Làm — etcd snapshot (manual):**
```bash
# chạy từ control-plane node (kind exec vào)
docker exec -it kind-lab-control-plane bash

# trong control-plane container
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /tmp/etcd-snapshot-$(date +%Y%m%d-%H%M%S).db

# verify snapshot
ETCDCTL_API=3 etcdctl snapshot status /tmp/etcd-snapshot-*.db --write-out=table
exit
```

**Làm — Longhorn VolumeSnapshot:**
```bash
cat > /tmp/vs.yaml <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: app-data-snap-01
  namespace: app
spec:
  volumeSnapshotClassName: longhorn-snapshot-vsc
  source:
    persistentVolumeClaimName: app-data-pvc
EOF
kubectl apply -f /tmp/vs.yaml

# check status
kubectl get volumesnapshot -n app
kubectl describe volumesnapshot app-data-snap-01 -n app
```

**Làm — restore-test script (mẫu, chạy định kỳ):**
```bash
# restore-test CNPG: tạo cluster từ backup trên staging namespace
cat > /tmp/cnpg-restore-test.yaml <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: db-restore-test
  namespace: staging
spec:
  instances: 1
  storage:
    size: 5Gi
  bootstrap:
    recovery:
      source: db-main
  externalClusters:
  - name: db-main
    barmanObjectStore:
      destinationPath: s3://k8s-backup/cnpg/db-main
      s3Credentials:
        accessKeyId:
          name: backup-creds
          key: ACCESS_KEY_ID
        secretAccessKey:
          name: backup-creds
          key: SECRET_ACCESS_KEY
EOF
# kubectl apply -f /tmp/cnpg-restore-test.yaml  # uncomment khi có CNPG + S3
```

**Kết quả:**
```text
# etcd snapshot
$ ETCDCTL_API=3 etcdctl snapshot status /tmp/etcd-snapshot-20260813-143022.db --write-out=table
+----------+----------+------------+------------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
+----------+----------+------------+------------+
| 4f3a8b2c |     4821 |       1247 |     6.1 MB |
+----------+----------+------------+------------+

# Longhorn VolumeSnapshot
$ kubectl get volumesnapshot -n app
NAME                READYTOUSE   SOURCEPVC       RESTORESIZE   SNAPSHOTCONTENT     AGE
app-data-snap-01    true         app-data-pvc    5Gi           snapcontent-xxx     45s

# Longhorn restore từ snapshot → PVC mới
$ kubectl describe volumesnapshot app-data-snap-01 -n app
...
Status:
  Ready To Use: true
  Restore Size:  5Gi
  Creation Time: 2026-08-13T14:32:10Z
```
→ **Verify:** etcd snapshot status hiện TOTAL KEYS > 0 (cluster không rỗng) và TOTAL SIZE > 0; Longhorn VolumeSnapshot `READYTOUSE=true`. Khi làm restore-test thật (monthly): tạo namespace `restore-test`, apply PVC từ snapshot, mount vào Pod, đọc data, confirm row count khớp production → ghi kết quả vào DR runbook.

---

## 🧹 Dọn dẹp

```bash
# xóa resources lab (giữ cụm kind)
kubectl delete -f /tmp/pdb.yaml --ignore-not-found
kubectl delete -f /tmp/quota.yaml --ignore-not-found
kubectl delete -f /tmp/limitrange.yaml --ignore-not-found
kubectl delete -f /tmp/servicemonitor.yaml --ignore-not-found
kubectl delete -f /tmp/alert-disk.yaml --ignore-not-found
kubectl delete deployment/web-app -n app --ignore-not-found
kubectl delete volumesnapshot app-data-snap-01 -n app --ignore-not-found

# gỡ kube-prometheus-stack (nếu muốn giải phóng RAM)
helm uninstall monitoring -n monitoring
kubectl delete namespace monitoring --ignore-not-found

# gỡ metrics-server (optional)
kubectl delete -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

---

## ✅ Đủ khi

① Giải thích metrics-server làm gì, tại sao cần `--kubelet-insecure-tls` trên kind, và `kubectl top` đọc từ đâu.

② Nói được 5 component của kube-prometheus-stack và vai trò của ServiceMonitor trong auto-discovery target scrape.

③ Phân biệt 3 QoS class và thứ tự evict; giải thích tại sao tune bằng dữ liệu thật, không đoán.

④ Giải thích PDB chặn cái gì (voluntary disruption, không phải node crash); tại sao `minAvailable = tổng replica` là anti-pattern.

⑤ Mô tả 3 tầng backup/DR (etcd / CNPG WAL / Longhorn snapshot), RPO/RTO mỗi tầng, và tại sao "backup chưa test-restore = chưa có backup".

---

## Recall
1. metrics-server scrape từ đâu? Tại sao cần `--kubelet-insecure-tls` trên kind?
2. `kubectl top nodes` hiện đơn vị CPU là gì? 500m nghĩa là gì?
3. ServiceMonitor là gì? Nó giải quyết vấn đề nào so với cách cấu hình Prometheus thủ công?
4. Prometheus và metrics-server khác nhau thế nào về lưu trữ lịch sử?
5. Ba mức QoS là gì? Điều kiện để Pod được xếp vào Guaranteed?
6. Tại sao set `limits.cpu` quá thấp không làm Pod OOMKill nhưng vẫn gây vấn đề?
7. PDB chặn được loại disruption nào? Node crash thì sao?
8. `ALLOWED DISRUPTIONS = 0` trong `kubectl get pdb` nghĩa là gì?
9. Ba tầng backup/DR trong K8s bảo vệ cái gì, và tầng nào có RPO gần 0?
10. Tại sao restore-test bắt buộc phải làm định kỳ, không chỉ backup là đủ?

### Đáp án

1. Scrape kubelet `/metrics/resource` (cAdvisor nhúng trong kubelet). kind dùng TLS tự ký không có CA cluster-trust → metrics-server cần bỏ qua xác thực cert bằng `--kubelet-insecure-tls`.
2. Millicores — 1000m = 1 CPU core. 500m = 0.5 CPU core.
3. ServiceMonitor là CRD cho phép khai báo target scrape bằng label selector. Không cần edit ConfigMap Prometheus thủ công; Prometheus Operator watch ServiceMonitor và tự reload config.
4. metrics-server không lưu lịch sử — chỉ window ~60s, dùng cho `kubectl top` và HPA. Prometheus lưu time-series nhiều ngày, dùng cho dashboard, alert, trend analysis.
5. BestEffort (không set gì), Burstable (partial hoặc requests < limits), Guaranteed (requests = limits cho cả cpu và memory). Guaranteed: tất cả container trong Pod phải có requests = limits.
6. Vượt `limits.cpu` → CPU throttle (kernel giới hạn bandwidth, không kill). Pod vẫn Running nhưng latency tăng, throughput giảm — dễ nhầm là bug app. Phát hiện qua metric `container_cpu_throttled_seconds_total` trên Prometheus.
7. Voluntary disruption: `kubectl drain`, Cluster Autoscaler scale-down, rolling update. Node crash là involuntary disruption — PDB không chặn được, đó là việc của Longhorn replica + etcd HA.
8. Số Pod có thể evict thêm mà không vi phạm PDB = 0. Tức là evict bất kỳ Pod nào sẽ vi phạm `minAvailable`. Thường xảy ra khi số replica = minAvailable.
9. Tầng 1 (etcd snapshot): cluster objects — RPO theo lịch (vd 6h). Tầng 2 (CNPG WAL): DB data — RPO gần 0 (real-time WAL ship). Tầng 3 (Longhorn snapshot): PVC data — RPO theo lịch (vd 1h).
10. Restore guide có thể lỗi, lệnh outdated, credential hết hạn, file snapshot bị corrupt — những lỗi này chỉ phát hiện khi thật sự restore. Backup chưa test = giả định chưa được xác minh. Khi sự cố thật xảy ra, áp lực cao, không phải lúc debug guide lần đầu.

---

## Bắc cầu sang production

Trên cluster production, observability + HA hardening + DR là **vòng vận hành liên tục**, không phải cài một lần xong. Sau khi deploy kube-prometheus-stack, bước tiếp theo là: (1) wire Alertmanager tới kênh thực tế (Slack/PagerDuty/webhook), (2) tune alert threshold theo baseline thật của hệ thống (không dùng default), (3) cài Vertical Pod Autoscaler (VPA) để tự đề xuất requests/limits dựa trên lịch sử Prometheus, (4) thiết lập lịch etcd snapshot + CNPG backup verification + Longhorn snapshot replication ra offsite, (5) viết DR runbook và đưa restore-test vào lịch quarterly/monthly với owner cụ thể.

Resource requests/limits không nên set một lần mãi mãi — review mỗi quý khi traffic thay đổi. PDB cần review mỗi lần thay đổi replica count. Backup retention + offsite replication là chi phí cần cân nhắc rõ (S3 storage cost, bandwidth) nhưng rẻ hơn nhiều so với downtime thật.

---

## 📎 Nguồn

- [metrics-server GitHub](https://github.com/kubernetes-sigs/metrics-server)
- [kube-prometheus-stack Helm chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)
- [Kubernetes QoS classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)
- [PodDisruptionBudget](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [ResourceQuota](https://kubernetes.io/docs/concepts/policy/resource-quotas/)
- [LimitRange](https://kubernetes.io/docs/concepts/policy/limit-range/)
- [Longhorn VolumeSnapshot](https://longhorn.io/docs/latest/snapshots-and-backups/volume-snapshotting-and-restoring/)
- [CloudNativePG Backup](https://cloudnative-pg.io/documentation/current/backup/)
