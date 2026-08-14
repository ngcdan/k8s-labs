# 10 · Job, CronJob, HPA & Troubleshooting — chạy-tới-hoàn-thành, lịch tự động, tự scale, chẩn đoán Pod lỗi

> **Chặng 3** — trước: Chiến lược deploy · kế tiếp: Compose → Kubernetes

**Mục tiêu:** biết khi nào dùng Job thay vì Deployment; viết YAML cho Job (completions/parallelism/backoffLimit) và CronJob (schedule cron); hiểu HPA tự scale theo CPU; thạo luồng chẩn đoán vàng `describe → logs --previous → get events`; đọc được Prometheus/Grafana dashboard cơ bản.
**Nền:** đã thạo Deployment/ReplicaSet (chặng 2) — Job và CronJob đặt trên nền Pod template giống hệt, chỉ khác controller.

## Tiền đề
```bash
kubectl config use-context orbstack
kubectl get nodes   # 1 node STATUS=Ready
```

---

## 1. Job — chạy-tới-hoàn-thành

**Chốt:** Deployment cố giữ Pod *luôn sống*; Job ngược lại — tạo một hoặc nhiều Pod, chạy task/batch, rồi **kết thúc**. Pod **không tự xoá** sau khi Job done; chỉ mất khi `kubectl delete job`.

- **Job** = controller `batch/v1` tạo Pod, theo dõi trạng thái, đánh dấu thành công khi đủ số Pod hoàn thành.
- **completions** = số Pod phải succeed để Job = done (mặc định 1).
- **parallelism** = số Pod chạy song song cùng lúc (mặc định 1).
- **backoffLimit** = số lần retry tối đa trước khi Job fail (mặc định 6).
- **restartPolicy** = chỉ được `Never` hoặc `OnFailure` — không dùng `Always` với Job.

**Vì sao:** batch/backup/migration không cần Pod chạy liên tục — Deployment sẽ restart mãi kể cả khi task đã xong, gây tốn tài nguyên và khó biết khi nào "hoàn thành". Job giải quyết đúng bài toán "chạy 1 lần đến khi xong thì thôi".

**Cơ chế:** Job controller poll trạng thái Pod liên tục. Mỗi khi Pod succeed (`Completed`), bộ đếm `succeeded` tăng 1; khi đủ `completions` → Job `Complete`. Nếu Pod fail và vượt `backoffLimit` → Job `Failed`. `activeDeadlineSeconds` đặt trần thời gian toàn bộ Job — quá hạn, mọi Pod đang chạy bị kill ngay cả khi chưa đủ completions.

> 💡 **Ẩn dụ:** Job như dây chuyền lắp ráp có mục tiêu rõ — "lắp đủ 4 sản phẩm rồi tắt máy"; Deployment như máy chạy 24/7 không bao giờ tắt.

| Field | Ý nghĩa | Mặc định |
|---|---|---|
| `completions` | Số Pod phải succeed để Job done | 1 |
| `parallelism` | Số Pod chạy song song | 1 |
| `backoffLimit` | Số lần retry tối đa trước khi fail | 6 |
| `activeDeadlineSeconds` | Thời gian tối đa Job được chạy | không giới hạn |
| `restartPolicy` | `Never` hoặc `OnFailure` | — (bắt buộc khai báo) |

**Dùng / không:** batch xử lý dữ liệu, DB migration, gửi email hàng loạt, backup snapshot. **Phản đề:** không dùng Job cho service cần chạy liên tục (API, worker queue always-on) — đó là việc của Deployment. Job không phù hợp workload cần restart liên tục theo demand — đó là HPA + Deployment.

**Làm:**
```bash
cat > /tmp/pi-job.yml <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: pi-counter
spec:
  completions: 4
  parallelism: 2
  backoffLimit: 3
  activeDeadlineSeconds: 240
  template:
    metadata:
      name: pi-counter
    spec:
      restartPolicy: Never
      containers:
      - name: pi
        image: alpine
        command: ["sh", "-c", "echo 'scale=500; 4*a(1)' | bc -l && sleep 2"]
EOF
kubectl apply -f /tmp/pi-job.yml

# Theo dõi tiến độ: completions tăng 1/4 → 2/4 → ... → 4/4
kubectl get jobs -w   # Ctrl+C khi Done

# Xem Pod sinh ra (không tự xoá)
kubectl get pods -l job-name=pi-counter

# Đọc log Pod đã hoàn thành
kubectl logs <pod-name-ở-trên>

# Xem chi tiết Job
kubectl describe job pi-counter
kubectl get job pi-counter -o yaml | grep -A5 status

# Dọn: xoá Job → Pod bị xoá theo
kubectl delete job pi-counter
```
**Kết quả:**
```text
$ kubectl get jobs -w
NAME         COMPLETIONS   DURATION   AGE
pi-counter   0/4           3s         3s
pi-counter   2/4           8s         8s
pi-counter   4/4           14s        14s    ← 4 Pod succeed, Job done

$ kubectl get pods -l job-name=pi-counter
NAME               READY   STATUS      RESTARTS   AGE
pi-counter-4xkqz   0/1     Completed   0          30s
pi-counter-7mngp   0/1     Completed   0          30s
pi-counter-k9v2t   0/1     Completed   0          22s
pi-counter-xrwbt   0/1     Completed   0          22s    ← vẫn còn, không tự xoá

$ kubectl describe job pi-counter | grep -A3 "^Status"
# hoặc nhìn dòng: Succeeded: 4
```
→ **Verify:** `COMPLETIONS = 4/4`; Pod trạng thái `Completed`; `kubectl logs <pod>` in chữ số pi.

---

## 2. CronJob — lịch tự động

**Chốt:** CronJob là wrapper của Job, thêm trường `schedule` theo **định dạng cron 5 trường** — tự động sinh Job theo lịch, không cần trigger thủ công.

- **CronJob** = `batch/v1` controller bao ngoài Job, chứa `jobTemplate` và `schedule`.
- **schedule** = chuỗi cron 5 trường xác định lịch chạy.
- **concurrencyPolicy** = xử lý khi Job trước chưa xong mà lịch mới đến.
- Tên CronJob phải ≤ 52 ký tự — K8s ghép suffix tự động khi tạo Job từ CronJob.

**Vì sao:** không có CronJob, phải dùng cronjob hệ điều hành trên node (gắn chặt 1 máy, chết node là chết job) hoặc script ngoài cluster. CronJob chạy native trong K8s — lifecycle, log, retry đều quản lý được bằng `kubectl`; Job sinh ra tự động đúng giờ.

**Cơ chế:** CronJob controller tính thời điểm kế tiếp từ `schedule`, tạo Job object đúng lúc đó. Job controller lấy từ đó và chạy Pod như bình thường. `concurrencyPolicy` quyết định điều gì xảy ra khi lịch đến mà Job cũ chưa xong:

| `concurrencyPolicy` | Hành vi |
|---|---|
| `Allow` (mặc định) | Tạo Job mới song song với Job cũ đang chạy |
| `Forbid` | Bỏ qua lịch mới nếu Job cũ còn chạy |
| `Replace` | Kill Job cũ, tạo Job mới — dùng khi Job cũ bị treo |

**Bảng cron 5 trường:**
```
* * * * *
│ │ │ │ └── day of week (0=Sun … 6=Sat, 7=Sun)
│ │ │ └──── month (1-12 hoặc jan-dec)
│ │ └────── day of month (1-31)
│ └──────── hour (0-23)
└────────── minute (0-59)
```
Ví dụ nhanh: `0 22 * * 1` = 22:00 mỗi thứ Hai · `1 0 1 * *` = 00:01 ngày đầu mỗi tháng · `*/5 * * * *` = mỗi 5 phút · `@daily` = tương đương `0 0 * * *`.

> 💡 **Ẩn dụ:** CronJob = đồng hồ báo thức gắn với dây chuyền (Job) — báo thức reo thì dây chuyền khởi động một lần, xong thì nghỉ, hôm sau báo thức reo lại.

**Dùng / không:** backup DB đêm, gửi report định kỳ, dọn file log cũ, sync dữ liệu theo giờ. **Phản đề:** không dùng CronJob cho việc cần phản hồi real-time hoặc trigger theo event — đó là message queue (Kafka, NATS) + Deployment consumer. Nếu interval < 1 phút, cân nhắc worker trong Deployment vòng lặp sleep thay vì CronJob (K8s CronJob không hỗ trợ sub-minute).

**Làm:**
```bash
cat > /tmp/pi-cronjob.yml <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: pi-cron
spec:
  schedule: "*/1 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: pi
            image: alpine
            command: ["sh", "-c", "echo 'scale=100; 4*a(1)' | bc -l"]
EOF
kubectl apply -f /tmp/pi-cronjob.yml

# Xem trạng thái CronJob
kubectl get cj   # cột LAST SCHEDULE, ACTIVE

# Chờ ~1 phút rồi xem Job sinh ra
kubectl get jobs
kubectl get pods

# Xem log Pod vừa chạy
kubectl logs <pod-name>

# Đổi lịch sang mỗi 30 phút — sửa schedule trong file rồi apply lại
kubectl apply -f /tmp/pi-cronjob.yml
kubectl describe cj pi-cron | grep Schedule   # xác nhận schedule mới

# Dọn: xoá CronJob → Job + Pod bị xoá theo
kubectl delete cj pi-cron
```
**Kết quả:**
```text
$ kubectl get cj
NAME      SCHEDULE    SUSPEND   ACTIVE   LAST SCHEDULE   AGE
pi-cron   */1 * * *   False     0        15s             75s

$ kubectl get jobs
NAME               COMPLETIONS   DURATION   AGE
pi-cron-28991760   1/1           4s         62s    ← Job tự sinh, tên = <cronjob>-<unix-minute>

$ kubectl get pods
NAME                         READY   STATUS      RESTARTS   AGE
pi-cron-28991760-r5xk7       0/1     Completed   0          65s

$ kubectl logs pi-cron-28991760-r5xk7
3.14159265358979323846...    ← output pi đúng
```
→ **Verify:** `LAST SCHEDULE` cập nhật mỗi phút; Job name có suffix timestamp; Pod `Completed`.

---

## 3. HPA — Horizontal Pod Autoscaler *(bổ sung theo roadmap)*

> HPA bổ sung theo roadmap 2026 để hoàn chỉnh chặng 3.

**Chốt:** HPA tự động tăng/giảm số replica của Deployment dựa trên **metrics thực tế** (mặc định: CPU utilization) — không cần tay scale. Yêu cầu **Metrics Server** phải được cài trong cluster.

- **HPA** = controller `autoscaling/v2` điều chỉnh `spec.replicas` của Deployment/StatefulSet.
- **scaleTargetRef** = trỏ đến Deployment/StatefulSet cần scale.
- **minReplicas / maxReplicas** = giới hạn dưới và trên số Pod.
- **averageUtilization** = % CPU mục tiêu — HPA giữ CPU gần ngưỡng này.
- Không có **Metrics Server** → HPA không lấy được metrics → không hoạt động.

**Vì sao:** scale thủ công phản ứng chậm (phát hiện lag → quyết định → chạy lệnh → Pod ready = vài phút); HPA phản ứng tự động trong vài chục giây. Không HPA → hoặc over-provision (lãng phí) hoặc under-provision (chịu lag khi spike).

**Cơ chế:** Metrics Server thu CPU/memory từ kubelet mỗi 15 giây. HPA controller poll Metrics Server mỗi 15 giây, tính:

```
desiredReplicas = ceil(currentReplicas × currentMetric / targetMetric)
```

Sau đó cập nhật `spec.replicas` của Deployment → ReplicaSet tạo/xoá Pod. Scale out nhanh (phát hiện → ~30s); scale in chậm hơn (cooldown mặc định 5 phút) để tránh flapping.

> 💡 **Ẩn dụ:** HPA như người điều phối ca làm việc — khi đơn hàng tăng, gọi thêm người; khi vắng, cho về; nhưng phải có bảng giờ (Metrics Server) mới biết đơn hàng nhiều hay ít.

| Field | Ý nghĩa |
|---|---|
| `scaleTargetRef` | Deployment/StatefulSet cần scale |
| `minReplicas` | Không scale in xuống dưới mức này |
| `maxReplicas` | Không scale out vượt mức này |
| `averageUtilization` | % CPU mục tiêu (vd: 50 = giữ CPU ~50%) |

**Dùng / không:** API/service có traffic biến động theo giờ, batch worker có spike bất thường. **Phản đề:** HPA không phù hợp workload stateful phức tạp (DB cần quorum, shard rebalance) — scale StatefulSet cần cẩn thận hơn. Workload fixed-size (cần chính xác N replica vì giấy phép phần mềm) → không dùng HPA.

**Làm:**
```bash
# Kiểm tra Metrics Server
kubectl top nodes   # nếu báo lỗi → cài metrics-server trước

# Tạo Deployment (có resource request — bắt buộc để HPA tính %)
cat > /tmp/web-deploy.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels: { app: web }
  template:
    metadata:
      labels: { app: web }
    spec:
      containers:
      - name: web
        image: nginx:alpine
        resources:
          requests:
            cpu: "100m"
          limits:
            cpu: "200m"
EOF
kubectl apply -f /tmp/web-deploy.yml

# Tạo HPA qua kubectl
kubectl autoscale deployment web --cpu-percent=50 --min=1 --max=5

# Hoặc dùng YAML
cat > /tmp/web-hpa.yml <<'EOF'
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
  minReplicas: 1
  maxReplicas: 5
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 50
EOF
kubectl apply -f /tmp/web-hpa.yml

# Xem trạng thái HPA
kubectl get hpa   # cột TARGETS, MINPODS, MAXPODS, REPLICAS

# Tạo tải để xem scale out
kubectl run load --image=busybox --rm -it -- sh -c \
  "while true; do wget -q -O- http://web; done"
# Mở tab khác: kubectl get hpa -w   (xem REPLICAS tăng)

# Dọn
kubectl delete hpa web-hpa
kubectl delete deploy web
```
**Kết quả:**
```text
$ kubectl get hpa
NAME      REFERENCE        TARGETS   MINPODS   MAXPODS   REPLICAS   AGE
web-hpa   Deployment/web   12%/50%   1         5         1          30s

# Sau khi tạo tải (tab khác):
$ kubectl get hpa -w
NAME      REFERENCE        TARGETS    MINPODS   MAXPODS   REPLICAS
web-hpa   Deployment/web   12%/50%    1         5         1
web-hpa   Deployment/web   68%/50%    1         5         1
web-hpa   Deployment/web   68%/50%    1         5         2    ← scale out
web-hpa   Deployment/web   55%/50%    1         5         3
```
→ **Verify:** `TARGETS` vượt ngưỡng → `REPLICAS` tăng; dừng tải → replica giảm về 1 sau ~5 phút.

---

## 4. Monitoring — Metrics Server, Prometheus, Grafana

**Chốt:** ba tầng monitoring bổ sung nhau — Metrics Server cho `kubectl top` và HPA; Prometheus thu metrics dạng time-series; Grafana dựng dashboard trực quan từ Prometheus.

- **Metrics Server** = thu CPU/memory của node và Pod từ kubelet; cho phép `kubectl top node/pod`; **bắt buộc cho HPA**.
- **kube-state-metrics** = kết nối API server, sinh metrics về trạng thái object K8s (Deployment, Pod, Node…); tập trung vào *health of objects*, không phải health của K8s components.
- **Prometheus** = open-source (dự án thứ 2 vào CNCF sau K8s); scrape metrics từ kube-state-metrics + endpoint khác; hỗ trợ alerting rules.
- **Grafana** = dashboard trực quan trên dữ liệu Prometheus; biến raw metrics thành biểu đồ + alert.

**Vì sao:** `kubectl get pods` chỉ thấy trạng thái tại thời điểm hỏi — không thấy CPU đã spike 10 phút trước như thế nào, không phát hiện trend OOM dần. Prometheus lưu time-series → alert trước khi Pod crash; Grafana hiện dashboard → ops team thấy liền mà không phải chạy lệnh.

**Cơ chế:** Metrics Server ←scrape kubelet mỗi 15s → expose API `/metrics.k8s.io`; HPA và `kubectl top` đọc API này. Prometheus ←scrape kube-state-metrics + Metrics Server endpoint định kỳ → lưu vào storage time-series riêng → Grafana query PromQL → vẽ đồ thị. Hai pipeline độc lập: Metrics Server (real-time, ngắn hạn) và Prometheus (lưu lâu dài, alerting).

> 💡 **Ẩn dụ:** Metrics Server như đồng hồ dashboard xe (nhìn tốc độ hiện tại); Prometheus như hộp đen máy bay (ghi lại mọi thứ theo thời gian); Grafana như màn hình phân tích sau chuyến bay.

**Dùng / không:** mọi cluster production cần cả ba. **Phản đề:** lab nhỏ/học thì chỉ Metrics Server đủ; Prometheus + Grafana tốn thêm RAM/CPU và cần cấu hình alerting — không nên dựng nếu chưa có nhu cầu alert thực sự.

**Làm:**
```bash
# Xem CPU/memory của node và Pod (cần Metrics Server)
kubectl top nodes
kubectl top pods
kubectl top pods --all-namespaces   # cả cluster

# Prometheus + Grafana cục bộ (namespace monitoring):
kubectl create namespace monitoring
kubectl create -f <prometheus-folder>/ -R   # YAML từ kube-prometheus-stack Helm
# Mở Prometheus: localhost:30000
# Mở Grafana: localhost:30001 (hoặc port-forward)
kubectl port-forward -n monitoring svc/grafana 3000:3000
```
**Kết quả:**
```text
$ kubectl top nodes
NAME             CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%
orbstack         231m         5%     1892Mi          23%

$ kubectl top pods
NAME                    CPU(cores)   MEMORY(bytes)
web-6d7f8b9c4-xkp2n    2m           5Mi
```
→ **Verify:** `kubectl top nodes` in CPU/memory; nếu báo `error: Metrics API not available` → Metrics Server chưa cài.

---

## 5. Troubleshooting — luồng chẩn đoán vàng

![[diagnostic-flow.excalidraw]]

**Chốt:** khi Pod/Deployment lỗi, đi theo thứ tự `get pods → describe → logs --previous → get events` — dừng lại ở bước nào tìm manh mối thì xử lý ngay, không nhảy cóc.

- Luồng 5 bước: `get pods` (STATUS) → `describe pod` (Events) → `logs` (app log) → `logs --previous` (log lần crash trước) → `get events` (toàn cluster).
- `describe pod` → phần **Events** cuối output thường chứa lý do lỗi rõ nhất.
- `logs --previous` = log của lần chạy trước Pod bị restart — **thiếu lệnh này là bế tắc khi CrashLoopBackOff**.
- `exec -it -- sh` = vào shell container đang chạy để test DNS, curl nội bộ.

**Vì sao:** không có quy trình → mỗi lần debug mò từ đầu, mất 10–30 phút cho thứ 5 phút là tìm ra. Luồng này bắt 90% lỗi phổ biến theo thứ tự từ nhanh → chậm, từ ngoài vào trong.

**Cơ chế:** K8s ghi Events cho mọi object (Pod, Node, PVC…) trong 1 giờ. `describe` tổng hợp Events của đúng Pod đó. `logs` đọc stdout/stderr của container process (ghi vào log driver). `--previous` đọc log của container **đã chết**, lưu tạm trên node (mất sau khi node restart hoặc Pod bị evict). `exec` mở pseudoterminal trực tiếp vào namespace của container — test DNS/curl từ đây phản ánh đúng network view của Pod.

> 💡 **Ẩn dụ:** `get pods` = nhìn đèn báo lỗi trên xe; `describe` = đọc mã lỗi OBD; `logs` = nghe tiếng động cơ; `logs --previous` = xem camera hành trình lúc tai nạn; `exec` = chui xuống gầm xe kiểm tra trực tiếp.

**Các STATUS thường gặp và hướng xử lý:**

| STATUS | Nguyên nhân phổ biến | Lệnh tiếp theo |
|---|---|---|
| `ErrImagePull` / `ImagePullBackOff` | Image tag sai, registry không tồn tại, không có quyền pull | `describe pod` → xem Events message; kiểm tra tag image |
| `CrashLoopBackOff` | App bên trong crash; liên tục restart | `logs --previous` để xem log lần chạy trước |
| `Pending` | Không tìm được node phù hợp (resource không đủ, taints, PVC chưa bound) | `describe pod` → xem Events "Insufficient cpu/memory" |
| `OOMKilled` | Container vượt memory limit | Tăng `resources.limits.memory` hoặc tối ưu app |
| `Terminating` mãi | Finalizer chưa xử lý xong | `describe` xem finalizers; có thể cần force delete |

**Dùng / không:** luồng này áp dụng mọi khi Pod không ở trạng thái `Running`/`Completed`. **Phản đề:** nếu Pod `Running` nhưng app trả lỗi logic → luồng này ít giúp ích; cần đọc `logs` của Pod đang chạy và trace code.

**Làm:**
```bash
# Lỗi 1: image tag sai → ErrImagePull
kubectl run bad-image --image=nginx:nonexistent-tag
kubectl get pod bad-image          # ErrImagePull
kubectl describe pod bad-image     # Events: Failed to pull image "nginx:nonexistent-tag"
kubectl delete pod bad-image

# Lỗi 2: app crash → CrashLoopBackOff
kubectl run crash --image=alpine -- sh -c "exit 1"
kubectl get pod crash              # CrashLoopBackOff
kubectl logs --previous crash      # log lần chạy trước
kubectl describe pod crash         # Last State: Terminated, exit code 1
kubectl delete pod crash

# Lỗi 3: lỗi DNS trong app — vào shell kiểm tra
kubectl run debug --image=busybox -it --rm -- sh
# Trong shell: wget -qO- http://ten-service-dung
# Nếu không thấy → nslookup ten-service-dung

# Xem tất cả events mới nhất
kubectl get events --sort-by=.lastTimestamp | tail -20
```
**Kết quả — lỗi 1 (ErrImagePull):**
```text
$ kubectl get pod bad-image
NAME        READY   STATUS         RESTARTS   AGE
bad-image   0/1     ErrImagePull   0          5s

$ kubectl describe pod bad-image
...
Events:
  Type     Reason     Age  Message
  ----     ------     ---  -------
  Normal   Pulling    8s   Pulling image "nginx:nonexistent-tag"
  Warning  Failed     5s   Failed to pull image "nginx:nonexistent-tag": ... not found
  Warning  Failed     5s   Error: ErrImagePull
```
**Kết quả — lỗi 2 (CrashLoopBackOff):**
```text
$ kubectl get pod crash
NAME    READY   STATUS             RESTARTS   AGE
crash   0/1     CrashLoopBackOff   3          45s

$ kubectl logs --previous crash
(không có output — exit 1 không in gì)

$ kubectl describe pod crash | grep -A5 "Last State"
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
      Started:      Thu, 07 Aug 2026 10:00:01 +0700
      Finished:     Thu, 07 Aug 2026 10:00:01 +0700
```
→ **Verify:** `ErrImagePull` → Events nói rõ tag không tồn tại; `CrashLoopBackOff` → `--previous` + `describe` cho exit code.

---

## 🧹 Dọn dẹp
```bash
kubectl delete job pi-counter --ignore-not-found
kubectl delete cj pi-cron --ignore-not-found
kubectl delete hpa web-hpa --ignore-not-found
kubectl delete deploy web --ignore-not-found
kubectl delete pod bad-image crash debug --ignore-not-found
```

---

## Đủ khi
① Job vs Deployment khác gì về mục đích · ② 3 field chính của Job (completions/parallelism/backoffLimit) và restartPolicy · ③ 5 trường cron + 3 giá trị concurrencyPolicy · ④ HPA cần Metrics Server, công thức tính desiredReplicas · ⑤ 3 tầng monitoring làm gì khác nhau · ⑥ luồng chẩn đoán 5 bước theo thứ tự · ⑦ ErrImagePull và CrashLoopBackOff → lệnh tiếp theo là gì.

## Recall
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

1. Job và Deployment khác gì nhau về mục đích sử dụng?
2. `completions: 4, parallelism: 2` có nghĩa là gì?
3. Chuỗi cron `0 3 * * 1-5` chạy lúc nào?
4. `concurrencyPolicy: Replace` làm gì khác so với `Forbid`?
5. HPA cần component nào bắt buộc phải có trong cluster để hoạt động?
6. `kubectl top pod` không chạy được → nguyên nhân đầu tiên cần kiểm tra?
7. Pod ở trạng thái `CrashLoopBackOff` → lệnh đầu tiên cần chạy?
8. Sự khác biệt giữa `kubectl logs <pod>` và `kubectl logs --previous <pod>`?
9. Prometheus và Grafana phối hợp với nhau như thế nào?
10. Khi Pod `Pending` mãi, `kubectl describe pod` thường báo gì trong phần Events?

### Đáp án

1. Deployment cố giữ Pod *luôn chạy*; Job chạy task rồi **kết thúc** — dùng cho batch/backup/migration, không cần Pod chạy liên tục.
2. Bốn Pod phải succeed để Job done; tại bất kỳ thời điểm nào tối đa 2 Pod chạy song song.
3. 3:00 sáng mỗi ngày trong tuần (thứ Hai đến thứ Sáu).
4. `Forbid` = bỏ qua lịch chạy mới nếu Job cũ còn đang chạy; `Replace` = kill Job cũ và bắt đầu Job mới.
5. **Metrics Server** — không có Metrics Server, HPA không lấy được CPU metrics để tính desiredReplicas.
6. Metrics Server chưa cài hoặc chưa Ready trong cluster.
7. `kubectl logs --previous <pod>` — xem log của lần chạy trước, vì lần hiện tại đã crash và Pod đang restart.
8. `logs <pod>` = log của container *đang chạy*; `logs --previous` = log của *lần chạy trước* — cần thiết khi container vừa crash và restart.
9. Prometheus scrape metrics (từ kube-state-metrics, Metrics Server, app…) và lưu dạng time-series; Grafana kết nối Prometheus làm data source, dựng dashboard/biểu đồ và alert rule.
10. "Insufficient cpu" / "Insufficient memory" (không đủ tài nguyên node) hoặc "no nodes are available that match all of the following predicates" (taint/toleration mismatch, PVC không bound…).

## 📎 Nguồn & xem lại
- **HPA docs:** kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
- **crontab.guru** — test cron expression online.
