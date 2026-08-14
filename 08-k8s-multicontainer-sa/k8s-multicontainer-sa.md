# 08 · Multi-container Pod patterns & ServiceAccount

> **Chặng 2** — trước: Volume/PV/PVC/StorageClass · kế tiếp: Chiến lược deploy

**Mục tiêu:** Nắm 4 pattern multi-container Pod (init / sidecar / adapter / ambassador); hiểu cơ chế Pod chia sẻ network namespace + volume; tạo và gán ServiceAccount; hiểu luồng AuthN → AuthZ → RBAC cơ bản (Role / RoleBinding); biết token projected ≥1.24 khác gì token Secret cũ.
**Nền:** Đã làm lab Chặng 1 (Pod, probe, `kubectl` cơ bản). Init container và sidecar đều sống trong Pod — cần chắc khái niệm Pod-as-execution-environment trước.

## Tiền đề
```bash
kubectl config use-context orbstack
kubectl get nodes # 1 node STATUS=Ready
```

---

![[multicontainer-patterns.excalidraw]]

## 1. Pod Theory — tại sao multi-container?

**Chốt:** Pod là *execution environment* dùng chung — các container bên trong chia sẻ IP, port-space, network namespace (`localhost`) và volume; nguyên tắc thiết kế là **một container = một trách nhiệm**, ghép container "trợ lý" vào cùng Pod thay vì nhét logic phụ vào image chính.

- **Shared network namespace:** mọi container trong Pod cùng một IP, gọi nhau qua `localhost:<port>` — nhưng phải khác port, không được tranh.
- **Shared volume:** mount cùng `emptyDir` (hoặc PVC) vào nhiều container → truyền file mà không qua mạng.
- **Cùng node:** scheduler đặt toàn bộ Pod lên một node; các container không thể nằm ở node khác nhau.
- **Hai vòng đời:** *init container* — chạy trước, tuần tự, phải xong; *app/sidecar container* — chạy song song, dài hạn.

**Vì sao:** nhét logging, proxy, metrics transform vào image chính khiến image to, khó tái dùng, khó test riêng. Tách ra container riêng → image nhỏ, container reusable, team có thể phát triển độc lập.

**Cơ chế:** khi kubelet tạo Pod, kernel cấp cho Pod một *network namespace* và *mount namespace* riêng. Mỗi container join vào namespace đó → thấy cùng IP, cùng mount point nếu cùng `volumeMounts`. Container không thấy process của nhau (PID namespace cách ly, trừ khi `shareProcessNamespace: true`).

> **Ẩn dụ:** Pod = căn hộ chung; mỗi container = một người thuê. Họ dùng chung địa chỉ nhà và phòng khách (shared volume), nhưng phòng ngủ (filesystem riêng) vẫn tách.

**Dùng / không:** dùng multi-container khi container phụ thực sự cần chia sẻ `localhost`/volume với app chính. **Phản đề:** nếu hai service chỉ giao tiếp qua HTTP + có vòng đời độc lập → dùng hai Pod riêng, đừng ghép — ghép nhầm buộc chúng scale cùng nhau.

**Làm:**
```bash
# Xem spec Pod chia sẻ gì
kubectl explain pod.spec --recursive | grep -E 'initContainers|containers|volumes' | head -20
```
**Kết quả:**
```text
 initContainers <[]Container>
 containers <[]Container> -required-
 volumes <[]Volume>
 ...
```
→ **Verify:** thấy `initContainers` và `containers` là hai trường tách biệt.

---

## 2. Init Pattern

**Chốt:** init container khai báo trong `spec.initContainers[]` — chạy **tuần tự trước** mọi app container, phải `exit 0` mới tiếp; nếu fail Pod restart và chạy lại từ đầu nên code phải **idempotent**.

- Khai báo trong `spec.initContainers[]`, không phải `spec.containers[]`.
- **Tuần tự:** nhiều init container → từng cái chạy hết rồi mới sang cái tiếp.
- **Phải exit 0:** fail → Pod restart theo `restartPolicy`; toàn bộ chain init chạy lại từ đầu.
- **Idempotent:** vì chạy lại nhiều lần sau restart → kết quả phải nhất quán.

**Vì sao:** app container khởi động ngay nhưng DB chưa lên, schema chưa có → crash vòng lặp. Init container ngăn app container bắt đầu trước khi điều kiện tiên quyết đủ — đây là `liveness` ở cấp khởi động, không phải cấp runtime.

**Cơ chế:** kubelet chạy init container theo thứ tự khai báo. Mỗi cái chạy hết → kiểm tra exit code; `0` → tiếp; khác `0` → restart Pod. Trạng thái hiển thị `Init:0/2` (đang chạy cái đầu trong 2 init), `Init:1/2` (cái thứ nhất xong), `PodInitializing` (tất cả init xong, app container đang khởi), `Running` (xong hết).

Scheduling resources: scheduler tính `max(resources của tất cả init container, tổng resources của app container)` — không cộng tất cả init vào nhau. Tránh khai báo init request cao hơn app → lãng phí sau khi init xong.

> **Ẩn dụ:** init container = công nhân chuẩn bị công trường (dọn nền, đổ móng). Xây nhà (app container) chỉ bắt đầu sau khi công nhân xong và rời đi.

**Dùng / không:** clone Git, set permission file, chờ DNS service lên, chuẩn bị dataset. **Phản đề:** migration DB nặng / phức tạp cần domain knowledge → dùng Kubernetes Operator thay init container; init container không phù hợp tác vụ lâu và có logic nghiệp vụ.

**Làm:**
```bash
# Pod init đợi service "my-svc" lên DNS
cat > /tmp/init-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
 name: init-demo
spec:
 initContainers:
 # chờ DNS my-svc resolve được mới cho app container khởi động
 - name: wait-for-svc
 image: busybox
 command: ['sh', '-c', 'until nslookup my-svc; do echo "waiting"; sleep 1; done']
 containers:
 - name: app
 image: nginx:alpine
EOF
kubectl apply -f /tmp/init-pod.yml

# Quan sát: Init:0/1 = init chưa xong
kubectl get pod init-demo -w # Ctrl+C thoát

# Xem log của init container
kubectl logs init-demo -c wait-for-svc

# Tạo service để init unblock
kubectl create service clusterip my-svc --tcp=80:80
kubectl get pod init-demo # → Running

kubectl describe pod init-demo | grep -A5 "Init Containers"

kubectl delete pod init-demo
kubectl delete service my-svc
```
**Kết quả:**
```text
$ kubectl get pod init-demo -w
NAME READY STATUS RESTARTS AGE
init-demo 0/1 Init:0/1 0 5s ← init đang chạy, app chưa start
init-demo 0/1 PodInitializing 0 12s ← init xong, app đang khởi
init-demo 1/1 Running 0 14s ← app container Running

$ kubectl logs init-demo -c wait-for-svc
waiting
waiting
... ← loop cho đến khi my-svc lên DNS
```
→ **Verify:** thấy `Init:0/1` → `PodInitializing` → `Running` đúng trình tự.

---

## 3. Sidecar Pattern

**Chốt:** sidecar là **regular app container** trong `spec.containers[]` — chạy **song song, dài hạn** bên cạnh app chính, chia sẻ `localhost` và volume; không có gì đặc biệt về YAML.

- Nằm trong `spec.containers[]` cùng với app container chính.
- Chạy song song, không tuần tự, không đảm bảo thứ tự startup so với app chính.
- Chia sẻ network namespace (`localhost`) và volume (`emptyDir`).
- Use case điển hình: **git-sync** — liên tục kéo content từ Git về shared `emptyDir`, app NGINX serve ra; init container chỉ sync một lần, sidecar sync định kỳ → live update.

**Vì sao:** thay vì nhét cron job / log shipper / git pull vào image app → image phình to, không tái dùng được. Sidecar tách biệt trách nhiệm — app container chỉ serve, sidecar lo sync — mỗi bên thay thế độc lập.

**Cơ chế:** kubelet start tất cả container trong `spec.containers[]` cùng lúc (không thứ tự). Hai container chia sẻ network namespace → `curl localhost:<port>` của một container có thể reach port của container kia. Volume `emptyDir` được tạo khi Pod bắt đầu, xóa khi Pod xóa — cả hai container mount vào path riêng nhưng cùng trỏ về một thư mục.

Gotcha lịch sử: không có cách ép sidecar chạy *trước* app container (như init container). K8s ≥1.29 GA **native sidecar** — khai báo init container với `restartPolicy: Always` để vừa đảm bảo thứ tự startup, vừa giữ sống dài hạn.

> **Ẩn dụ:** app container = đầu bếp chính; sidecar = phụ bếp đứng cạnh liên tục bổ sung nguyên liệu. Họ dùng chung bàn làm việc (shared volume) và nói chuyện qua `localhost`.

**Dùng / không:** log shipping, metrics scrape, git sync liên tục, TLS termination cạnh app. **Phản đề:** nếu sidecar cần khởi động *trước* app chính và *tiếp tục chạy* → dùng native sidecar (K8s ≥1.29) hoặc init container; sidecar thông thường không đảm bảo thứ tự.

**Làm:**
```bash
cat > /tmp/sidecar-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
 name: sidecar-demo
spec:
 volumes:
 - name: shared-html
 emptyDir: {}
 containers:
 - name: nginx
 image: nginx:alpine
 volumeMounts:
 - name: shared-html
 mountPath: /usr/share/nginx/html
 - name: content-writer
 image: busybox
 # ghi timestamp 5 giây/lần vào shared volume
 command: ['sh', '-c', 'while true; do echo "<h1>$(date)</h1>" > /html/index.html; sleep 5; done']
 volumeMounts:
 - name: shared-html
 mountPath: /html
EOF
kubectl apply -f /tmp/sidecar-pod.yml

kubectl get pod sidecar-demo # 2/2 Running

# nginx serve file do content-writer ghi
kubectl exec sidecar-demo -c nginx -- wget -qO- localhost

kubectl delete pod sidecar-demo
```
**Kết quả:**
```text
$ kubectl get pod sidecar-demo
NAME READY STATUS RESTARTS AGE
sidecar-demo 2/2 Running 0 8s ← 2/2: cả hai container Running

$ kubectl exec sidecar-demo -c nginx -- wget -qO- localhost
<h1>Thu Aug 7 03:14:22 UTC 2026</h1> ← content do sidecar ghi, nginx serve
```
→ **Verify:** `2/2 Running`; wget từ nginx trả về timestamp của sidecar → shared volume hoạt động.

---

## 4. Adapter Pattern

**Chốt:** adapter là **dạng chuyên biệt của sidecar** làm nhiệm vụ **chuyển đổi định dạng output** của app chính sang format tool ngoài yêu cầu — app không cần biết format monitoring tồn tại.

- Là sidecar, nhưng chức năng cụ thể: đọc output app → transform → expose endpoint chuẩn.
- Ví dụ điển hình: app expose `/nginx_status` (format riêng) → adapter `nginx-prometheus-exporter` đọc qua `localhost:80`, transform, expose `/metrics` port 9113 → Prometheus scrape.
- Cả hai container cùng network namespace → gọi nhau qua `localhost`.

**Vì sao:** Prometheus yêu cầu format `metric_name{labels} value timestamp`; NGINX không nói ngôn ngữ đó. Thay vì fork NGINX để thêm exporter → gắn adapter container bên cạnh — app chính giữ nguyên, monitoring team tự lo adapter.

**Cơ chế:** adapter đọc `localhost:<port-của-app>` (hoặc shared volume) → parse → viết lại format → expose endpoint riêng. Pod shared namespace làm cho `localhost` đến đúng container trong cùng Pod, không cần Service.

**Dùng / không:** chuẩn hóa metrics (Prometheus), chuyển log format (JSON → Loki), transform health endpoint. **Phản đề:** nếu app có thể tự expose format chuẩn với chi phí thấp → không cần adapter, tránh thêm container nếu không cần.

**Làm:**
```bash
cat > /tmp/adapter-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
 name: adapter-demo
spec:
 containers:
 - name: web
 image: nginx:alpine
 ports:
 - containerPort: 80
 - name: metrics-adapter
 image: busybox
 # mô phỏng: poll localhost:80, ghi metrics format Prometheus
 command: ['sh', '-c', 'while true; do echo "nginx_up 1" > /tmp/metrics; sleep 10; done']
EOF
kubectl apply -f /tmp/adapter-pod.yml

# Container web gọi localhost:80 — cùng network namespace
kubectl exec adapter-demo -c web -- wget -qO- localhost | head -3

kubectl delete pod adapter-demo
```
**Kết quả:**
```text
$ kubectl exec adapter-demo -c web -- wget -qO- localhost | head -3
<!DOCTYPE html>
<html>
<head><title>Welcome to nginx!</title> ← web thấy chính nó qua localhost:80
```
→ **Verify:** container `web` gọi được `localhost:80` trong cùng Pod → network namespace shared xác nhận.

---

## 5. Ambassador Pattern

**Chốt:** ambassador là **dạng chuyên biệt của sidecar** làm nhiệm vụ **proxy/broker kết nối ra ngoài** — app chính chỉ gọi `localhost:<port>`, ambassador nhận và forward tới external service kèm auth/TLS/retry; app không biết địa chỉ thật ở đâu.

- Là sidecar, nhưng chức năng cụ thể: nhận request từ app qua `localhost` → forward ra ngoài.
- Ví dụ: main app gọi `localhost:8001`, ambassador chạy `kubectl proxy` nhận và forward tới Kubernetes API server kèm SA token.
- Khi external API đổi địa chỉ/cert → chỉ đổi config ambassador, app chính không chạm tới.

**Vì sao:** hardcode URL API ngoài vào app → deploy khác môi trường phải rebuild image. Ambassador trừu tượng hóa kết nối → app luôn gọi `localhost`, ambassador lo routing — như service mesh nhẹ nhàng tại cấp Pod.

**Cơ chế:** ambassador lắng nghe `localhost:<port>` trong shared network namespace → forward request (thêm header auth, xử lý retry, terminate TLS) → external endpoint. App container không cần biết gì về auth hay địa chỉ thật. Điểm nổi bật so với adapter: adapter xử lý **output đến** (scrape app, transform, expose); ambassador xử lý **request đi** (nhận từ app, proxy ra ngoài).

> **Ẩn dụ:** ambassador = phiên dịch viên đứng cạnh. Bạn nói tiếng Việt vào tai anh ta (`localhost:9000`), anh ta dịch và nói với đối tác nước ngoài (external API). Bạn không cần biết đối tác ở đâu, nói tiếng gì.

**Dùng / không:** proxy đến Kubernetes API server, proxy đến external service cần auth phức tạp, service mesh thủ công tại cấp Pod. **Phản đề:** nếu cluster đã có service mesh (Istio/Linkerd) → ambassador manual là dư thừa, dùng mesh thay.

**Làm:**
```bash
cat > /tmp/ambassador-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
 name: ambassador-demo
spec:
 containers:
 - name: main-app
 image: curlimages/curl:latest
 command: ['sh', '-c', 'sleep 3600']
 - name: ambassador
 image: busybox
 # mô phỏng: nhận request localhost:9000, đóng vai proxy
 command: ['sh', '-c', 'while true; do echo "ambassador ready on :9000"; sleep 5; done']
EOF
kubectl apply -f /tmp/ambassador-pod.yml

kubectl exec ambassador-demo -c main-app -- sh -c 'echo "calling external via ambassador on :9000 (simulated)"'

kubectl delete pod ambassador-demo
```
**Kết quả:**
```text
$ kubectl exec ambassador-demo -c main-app -- sh -c 'echo "calling external via ambassador on :9000 (simulated)"'
calling external via ambassador on :9000 (simulated)
```
→ **Verify:** `2/2 Running`; main-app và ambassador cùng network namespace, sẵn sàng giao tiếp qua localhost.

---

## 6. Kubernetes AuthN và AuthZ — luồng tổng quan

**Chốt:** mọi hành động trên cluster đi qua **API server** theo luồng `AuthN → AuthZ → Admission Control → thực thi`; **AuthN** = "mày là ai?" (xác minh identity), **AuthZ** = "mày được làm gì?" (kiểm tra quyền).

- **AuthN (Authentication):** xác minh certificate (user dùng `~/.kube/config`) hoặc JWT token (Pod dùng ServiceAccount). Sau bước này biết *ai* đang gọi.
- **AuthZ (Authorization):** RBAC kiểm tra: identity này có quyền thực hiện *verb* (`get`/`list`/`create`...) trên *resource* (`pods`/`services`...) trong *namespace* này không?
- **Hai loại caller:** Human users (cert, xác thực ngoài K8s) và Pod processes (ServiceAccount token).
- **Deny by default:** nếu không có rule nào allow → từ chối (403 Forbidden).

**Vì sao:** không có AuthN/AuthZ, bất kỳ process nào trong cluster có thể đọc Secret, xóa Deployment, leo thang đặc quyền. Tách AuthN và AuthZ cho phép mở rộng từng layer độc lập (thêm OIDC cho user không ảnh hưởng RBAC).

**Cơ chế:**

```
Request (HTTPS) → API server
 → AuthN: certificate hoặc JWT hợp lệ? → identity (username/SA)
 → AuthZ: RBAC có rule allow verb+resource+namespace cho identity này?
 → Admission Control: webhook, policy (OPA, Kyverno...)
 → etcd: persist / thực thi
```

AuthN vs AuthZ — hay nhầm:

| | AuthN | AuthZ |
|---|---|---|
| Hỏi | "Mày là ai?" | "Mày được làm gì?" |
| Cơ chế | cert, JWT token | RBAC rule |
| Khi fail | 401 Unauthorized | 403 Forbidden |
| Caller type | user (cert) hoặc Pod (SA token) | user/SA + verb + resource + namespace |

**Dùng / không:** luôn phải hiểu đúng để debug. **Phản đề** hay gặp: nhầm `401` với `403` — 401 = token hết hạn / sai SA; 403 = SA đúng nhưng thiếu RoleBinding.

**Làm:**
```bash
# Xem user identity trong kubeconfig (AuthN của bạn)
kubectl config view --minify -o jsonpath='{.users[0].name}'; echo

# Kiểm tra quyền: bạn có list pods không?
kubectl auth can-i list pods # → yes/no

# Kiểm tra quyền: bạn có delete secrets không?
kubectl auth can-i delete secrets # → yes/no

# Kiểm tra quyền của SA cụ thể
kubectl auth can-i list services --as=system:serviceaccount:default:default # → yes/no
```
**Kết quả:**
```text
$ kubectl auth can-i list pods
yes

$ kubectl auth can-i delete secrets
yes ← admin context có toàn quyền

$ kubectl auth can-i list services --as=system:serviceaccount:default:default
no ← SA default không có RoleBinding → deny
```
→ **Verify:** `can-i` trả về `yes`/`no` tương ứng với quyền thực tế.

---

## 7. ServiceAccount — danh tính của Pod

**Chốt:** ServiceAccount (SA) là **API object** namespace-scoped đại diện cho danh tính Pod khi gọi API server; Pod không khai báo `serviceAccountName` → admission controller tự gán SA `default`; từ K8s 1.24 token là **projected volume** (ngắn hạn, tự rotate) thay vì Secret cũ.

- **Namespace-scoped:** mỗi namespace có sẵn 1 SA tên `default`.
- **Admission controller** tự gán SA `default` cho Pod không khai báo `spec.serviceAccountName`.
- Nhiều Pod có thể dùng chung 1 SA.
- Token được mount vào container tại `/var/run/secrets/kubernetes.io/serviceaccount/` gồm `token`, `ca.crt`, `namespace`.

**Projected token ≥1.24 — khác gì trước:**

| | Trước 1.24 | Từ 1.24 |
|---|---|---|
| Hình thức | Secret `type: kubernetes.io/service-account-token` | Projected volume (không tạo Secret) |
| Thời hạn | Không hết hạn (tồn tại vĩnh viễn) | Ngắn hạn (~1 giờ), tự rotate |
| Bảo mật | Kém hơn (nếu lộ token sống mãi) | Tốt hơn (token hết hạn tự động) |
| Nhận biết | `kubectl get secret` thấy `default-token-xxxxx` | Không thấy Secret đó |

**Vì sao:** SA không có = anonymous; anonymous bị từ chối hầu hết mọi thứ. SA là bước đầu để cấp quyền có kiểm soát — tạo SA riêng cho mỗi workload thay vì dùng SA `default` (nguyên tắc least privilege).

**Cơ chế:** kubelet request token từ API server (TokenRequest API) với audience và thời hạn cụ thể → mount vào `/var/run/secrets/kubernetes.io/serviceaccount/token` dưới dạng projected volume. Token này là JWT với `iss`, `aud`, `exp` — API server verify chữ ký và expiry trước khi accept.

**Dùng / không:** tạo SA riêng cho mỗi app workload, gán quyền tối thiểu. **Phản đề:** dùng SA `default` cho tất cả → nếu 1 workload bị compromise có thể lạm dụng quyền mọi workload khác trong namespace.

**Làm:**
```bash
# Xem SA mặc định của namespace default
kubectl get serviceaccount default -o yaml

# Chạy Pod bình thường — tự nhận SA "default"
kubectl run sa-demo --image=nginx:alpine

# Kiểm tra SA được gán
kubectl get pod sa-demo -o jsonpath='{.spec.serviceAccountName}'; echo # → default

# Xem token được mount (projected volume)
kubectl exec sa-demo -- ls /var/run/secrets/kubernetes.io/serviceaccount/

# Xem 60 ký tự đầu của JWT token
kubectl exec sa-demo -- cat /var/run/secrets/kubernetes.io/serviceaccount/token | cut -c1-60

kubectl delete pod sa-demo
```
**Kết quả:**
```text
$ kubectl get pod sa-demo -o jsonpath='{.spec.serviceAccountName}'; echo
default ← admission controller gán tự động

$ kubectl exec sa-demo -- ls /var/run/secrets/kubernetes.io/serviceaccount/
ca.crt namespace token ← 3 file: cert, namespace name, JWT token

$ kubectl exec sa-demo -- cat /var/run/secrets/kubernetes.io/serviceaccount/token | cut -c1-60
eyJhbGciOiJSUzI1NiIsImtpZCI6InVERzJxRlFSMW5OQjlWd ← JWT ngắn hạn (projected)
```
→ **Verify:** SA `default` tự gán; `/var/run/secrets/kubernetes.io/serviceaccount/` có đủ 3 file.

---

## 8. RBAC cơ bản + ServiceAccount thực chiến

**Chốt:** RBAC dùng **Role + RoleBinding** để cấp quyền cho SA; **deny by default** — mọi quyền phải grant tường minh; `kubectl auth can-i` để verify quyền trước khi debug.

- **Role:** tập verb (`get`/`list`/`watch`/`create`...) trên resource (`pods`/`services`...) trong **1 namespace**.
- **ClusterRole:** như Role nhưng scope toàn cluster (và resource không namespaced như `nodes`).
- **RoleBinding:** gán Role cho user / group / **ServiceAccount** trong 1 namespace.
- **ClusterRoleBinding:** gán ClusterRole toàn cluster.
- SA mới tạo = không có quyền gì → phải RoleBinding tường minh.

**Vì sao:** không có RBAC, mọi SA có thể đọc Secret, xóa Deployment, escalate privilege. RBAC = tường lửa bên trong cluster — dù bị compromise một workload, attacker bị giới hạn đúng quyền SA đó.

**Cơ chế:**

```
API server nhận request từ Pod (SA token)
 → AuthN: verify JWT → identity = system:serviceaccount:<ns>:<sa-name>
 → AuthZ RBAC: có RoleBinding nào gán Role có rule allow verb+resource+ns cho identity này?
 → có → allow
 → không → 403 Forbidden
```

Pattern SA + RBAC + Ambassador: main container gọi `localhost:8001` → ambassador (`kubectl proxy`) forward tới API server kèm SA token → RBAC kiểm tra → allow hoặc Forbidden.

**Dùng / không:** tạo SA + Role + RoleBinding riêng cho mỗi workload cần gọi API server (CI/CD runner, operator, monitoring agent). **Phản đề:** cấp `ClusterRole admin` cho SA → vi phạm least privilege; `kubectl proxy` tắt AuthZ nếu không cẩn thận — chỉ dùng trong Pod có kiểm soát.

**Làm:**
```bash
# 1. Tạo ServiceAccount riêng
kubectl create serviceaccount service-reader

# 2. Tạo Role + RoleBinding: được list/get/watch services
cat > /tmp/rbac.yml <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
 name: service-reader-role
 namespace: default
rules:
- apiGroups: [""]
 resources: ["services"]
 verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
 name: service-reader-binding
 namespace: default
subjects:
- kind: ServiceAccount
 name: service-reader
 namespace: default
roleRef:
 kind: Role
 name: service-reader-role
 apiGroup: rbac.authorization.k8s.io
EOF
kubectl apply -f /tmp/rbac.yml

# Verify quyền trước khi tạo Pod
kubectl auth can-i list services --as=system:serviceaccount:default:service-reader # → yes
kubectl auth can-i list pods --as=system:serviceaccount:default:service-reader # → no

# 3. Pod dùng SA + ambassador (kubectl proxy)
cat > /tmp/sa-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
 name: sa-rbac-demo
spec:
 serviceAccountName: service-reader
 containers:
 - name: main
 image: curlimages/curl:latest
 command: ['sh', '-c', 'sleep 3600']
 - name: ambassador
 image: bitnami/kubectl:latest
 command: ['kubectl', 'proxy', '--port=8001']
EOF
kubectl apply -f /tmp/sa-pod.yml
kubectl get pod sa-rbac-demo # 2/2 Running

# 4. Từ main container, list services qua ambassador
kubectl exec sa-rbac-demo -c main -- \
 curl -s http://localhost:8001/api/v1/namespaces/default/services | head -20

# 5. Thử list pods → Forbidden
kubectl exec sa-rbac-demo -c main -- \
 curl -s http://localhost:8001/api/v1/namespaces/default/pods | grep -i forbidden

kubectl delete pod sa-rbac-demo
kubectl delete -f /tmp/rbac.yml
kubectl delete serviceaccount service-reader
```
**Kết quả:**
```text
$ kubectl auth can-i list services --as=system:serviceaccount:default:service-reader
yes ← RoleBinding đã grant

$ kubectl auth can-i list pods --as=system:serviceaccount:default:service-reader
no ← không có rule → deny

$ kubectl get pod sa-rbac-demo
NAME READY STATUS RESTARTS AGE
sa-rbac-demo 2/2 Running 0 12s

# list services qua ambassador → trả về JSON services list (trích)
$ kubectl exec sa-rbac-demo -c main -- curl -s http://localhost:8001/api/v1/namespaces/default/services | head -5
{
 "kind": "ServiceList",
 "apiVersion": "v1",
 ... ← services trả về

# list pods qua ambassador → Forbidden
$ kubectl exec sa-rbac-demo -c main -- curl -s http://localhost:8001/api/v1/namespaces/default/pods | grep -i forbidden
 "message": "pods is forbidden: User \"system:serviceaccount:default:service-reader\" cannot list resource \"pods\"..."
```
→ **Verify:** `can-i` yes/no khớp với kết quả curl; services trả về, pods Forbidden → RBAC hoạt động đúng.

---

## Dọn dẹp
```bash
kubectl delete pod init-demo sidecar-demo adapter-demo ambassador-demo sa-demo sa-rbac-demo \
 --ignore-not-found
kubectl delete service my-svc --ignore-not-found
kubectl delete -f /tmp/rbac.yml --ignore-not-found
kubectl delete serviceaccount service-reader --ignore-not-found
```

---

## Đủ khi
① Pod chia sẻ gì giữa các container — localhost và volume, tại sao không phải PID · ② Init container khác sidecar: khai báo ở đâu, vòng đời thế nào, vì sao code phải idempotent · ③ Adapter vs ambassador — cái nào đọc output của app, cái nào proxy request ra ngoài · ④ AuthN vs AuthZ — câu hỏi khác nhau, error code khác nhau · ⑤ SA là gì, ai tạo cho Pod nếu mình không khai báo, token mount ở đâu · ⑥ Token SA từ 1.24 thay đổi gì — projected vs Secret · ⑦ Role + RoleBinding — khai báo cái gì, gán cho ai, scope ra sao.

## Recall
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

**Vòng 1 — Multi-container**
1. Init container và sidecar container: cái nào trong `initContainers[]`, cái nào trong `containers[]`? Vòng đời khác gì?
2. Nếu Pod có 3 init container và init thứ 2 fail, K8s làm gì?
3. Hai container trong cùng Pod muốn chia sẻ file, dùng gì?
4. Adapter pattern giải quyết vấn đề gì? Cho ví dụ tool cụ thể.
5. Ambassador pattern: app container gọi tới đâu, ambassador làm gì tiếp?

**Vòng 2 — AuthN/AuthZ + ServiceAccount**
6. AuthN vs AuthZ — câu hỏi khác nhau thế nào? Error code khác nhau thế nào?
7. Pod không khai báo `serviceAccountName` thì nhận SA nào, do đâu?
8. Token SA được mount vào container ở đường dẫn nào?
9. K8s 1.24+ thay đổi gì về cách tạo SA token so với trước?
10. Role vs ClusterRole: khác gì về scope? RBAC deny-by-default nghĩa là gì?

### Đáp án

1. Init container → `spec.initContainers[]`; app/sidecar → `spec.containers[]`. Init: chạy trước, tuần tự, phải exit 0, dừng hẳn. Sidecar: chạy song song với app, dài hạn.
2. Pod restart từ đầu — toàn bộ init container chạy lại từ cái đầu tiên (nên code phải idempotent).
3. `emptyDir` volume — mount vào cả hai container; tồn tại trong suốt vòng đời Pod.
4. Chuyển đổi format output của app sang format tool ngoài yêu cầu. Ví dụ: `nginx-prometheus-exporter` đọc `/nginx_status`, expose `/metrics` port 9113 cho Prometheus scrape.
5. App gọi `localhost:<port>` (vd 9000). Ambassador lắng nghe port đó, proxy/forward ra external service thật kèm auth/TLS — app chính không biết địa chỉ thật.
6. AuthN hỏi "mày là ai?" → fail = 401 Unauthorized (token hết hạn, sai SA). AuthZ hỏi "mày được làm gì?" → fail = 403 Forbidden (SA đúng nhưng thiếu RoleBinding).
7. SA `default` của namespace — do admission controller ServiceAccount tự gán khi thấy Pod không có `serviceAccountName`.
8. `/var/run/secrets/kubernetes.io/serviceaccount/` — gồm `token`, `ca.crt`, `namespace`.
9. Trước 1.24: token là Secret không có thời hạn, tự động tạo. Từ 1.24: projected volume, ngắn hạn (~1 giờ), tự rotate — không tạo Secret riêng, bảo mật hơn.
10. `Role` scope 1 namespace; `ClusterRole` scope toàn cluster (kể cả resource không namespaced như `nodes`). Deny-by-default: SA mới không có quyền gì — phải RoleBinding tường minh, verb/resource không được grant → 403.
