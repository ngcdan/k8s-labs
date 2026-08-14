# 09 · Chiến lược triển khai: Rolling · Canary · Blue-Green + Rollback

> **Chặng 3** — trước: Multi-container & ServiceAccount · kế tiếp: Jobs/CronJob, HPA & troubleshoot

**Mục tiêu:** hiểu 4 chiến lược triển khai (Rolling, Recreate, Canary, Blue-Green); thạo lệnh `rollout status/history/undo`; mô phỏng canary và blue-green bằng label + Service selector trên OrbStack; biết khi nào chọn chiến lược nào.
**Nền:** đã làm lab Deployment/ReplicaSet — biết `kubectl apply`, label, selector, Service LoadBalancer.

## Tiền đề
```bash
kubectl config use-context orbstack
kubectl get nodes # 1 node STATUS=Ready
```

---

## 1. Rolling Update — zero-downtime mặc định

**Chốt:** Rolling Update là chiến lược mặc định của Deployment — thay từng Pod một, không cắt traffic, không downtime.

- **Chiến lược mặc định** — không cần khai báo `strategy.type` nếu muốn dùng.
- **Cơ chế "từng Pod":** tạo Pod mới → đợi sẵn sàng (readiness probe) → xóa Pod cũ → lặp.
- **ReplicaSet đứng sau:** K8s tạo RS mới tăng dần, RS cũ giảm dần — 2 RS tồn tại song song trong quá trình.
- **Hai tham số kiểm soát tốc độ** (default 25%, có thể dùng số tuyệt đối):
 - `maxSurge` — số Pod được phép vượt quá `replicas` trong lúc update.
 - `maxUnavailable` — số Pod cũ được phép unavailable cùng lúc.

**Vì sao:** nếu không có rolling update, deploy = tắt hết Pod cũ → bật Pod mới → có khoảng tối downtime. Với `replicas: 4, maxSurge: 1, maxUnavailable: 1`: tối đa 5 Pod tồn tại, tối thiểu 3 Pod phục vụ traffic bất kỳ lúc nào — user không thấy gián đoạn.

**Cơ chế sâu:**

K8s tạo RS thứ hai với image mới. Vòng lặp: scale RS mới +1 → đợi Pod `Ready` → scale RS cũ -1. Hai tham số `maxSurge`/`maxUnavailable` quyết định bao nhiêu Pod được "in flight" cùng lúc. `minReadySeconds` thêm một lớp đệm: Pod phải ổn định N giây mới được coi là available (tránh race condition readiness probe chậm). `progressDeadlineSeconds` bắt lỗi stuck — nếu rollout không tiến sau X giây, Deployment báo `ProgressDeadlineExceeded`.

> **Ẩn dụ:** relay marathon — trao gậy từng người một; không bao giờ thả gậy trước khi người kế cầm chắc.

| | Rolling Update | Recreate |
|---|---|---|
| Downtime | Không | Có (ngắn) |
| v1+v2 song song | Có (tạm) | Không bao giờ |
| Tài nguyên | 1x + surge nhỏ | 1x (xóa hết rồi tạo) |
| Rollback | `rollout undo` | `rollout undo` |

Trường liên quan trong YAML:
```yaml
spec:
 replicas: 4
 minReadySeconds: 10
 progressDeadlineSeconds: 60
 revisionHistoryLimit: 5
 strategy:
 type: RollingUpdate
 rollingUpdate:
 maxSurge: 1
 maxUnavailable: 1
```

**Dùng / KHÔNG:**
- Dùng: phần lớn trường hợp — service API, frontend stateless.
- KHÔNG: schema DB không tương thích ngược (v1 ghi column, v2 đọc column đó bị crash khi 2 version chạy song song) → dùng **Recreate** để đảm bảo chỉ một version tồn tại. **Phản đề:** `maxUnavailable: 0, maxSurge: 1` = an toàn nhất nhưng chậm nhất (thêm từng Pod một). Với `replicas: 100` và `maxSurge: 25%`, quá trình kéo dài — cân nhắc tăng `maxSurge` nếu cluster còn tài nguyên.

**Làm:**
```bash
# Tạo Deployment v1
cat > /tmp/rolling.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
 name: my-nginx
spec:
 replicas: 4
 revisionHistoryLimit: 5
 strategy:
 type: RollingUpdate
 rollingUpdate: { maxSurge: 1, maxUnavailable: 1 }
 selector:
 matchLabels: { app: nginx }
 template:
 metadata:
 labels: { app: nginx }
 spec:
 containers:
 - name: web
 image: nginx:1.16.1-alpine
 ports: [{ containerPort: 80 }]
EOF
kubectl apply -f /tmp/rolling.yml --save-config

# Xem trạng thái ban đầu
kubectl rollout status deployment/my-nginx

# Cập nhật image → trigger rolling update
kubectl set image deployment/my-nginx web=nginx:1.17.8-alpine

# Theo dõi tiến độ — chạy ngay sau lệnh trên
kubectl rollout status deployment/my-nginx

# Giữa chừng: thấy 5 Pod (surge=1), mix cũ + mới
kubectl get pods
kubectl get rs
```

**Kết quả:**
```text
$ kubectl rollout status deployment/my-nginx
Waiting for deployment "my-nginx" rollout to finish: 1 out of 4 new replicas have been updated...
Waiting for deployment "my-nginx" rollout to finish: 2 out of 4 new replicas have been updated...
Waiting for deployment "my-nginx" rollout to finish: 3 out of 4 new replicas have been updated...
Waiting for deployment "my-nginx" rollout to finish: 3 of 4 updated replicas are available...
deployment "my-nginx" successfully rolled out

$ kubectl get rs
NAME DESIRED CURRENT READY
my-nginx-6d4cf56db6 4 4 4 ← RS mới (1.17) — đang active
my-nginx-7848d4b9f7 0 0 0 ← RS cũ (1.16) — còn giữ để rollback
```
→ **Verify:** 2 RS tồn tại — RS mới DESIRED=4, RS cũ DESIRED=0 (giữ cho rollback).

---

## 2. Rollback — quay lui khi có sự cố

**Chốt:** mỗi `kubectl apply` tạo một **revision**; rollback = K8s scale RS cũ trở lại — không cần build lại image.

- `revisionHistoryLimit` quyết định giữ bao nhiêu RS cũ (mặc định 10).
- Sau `rollout undo`, revision cũ được "xoay vòng" thành revision mới nhất trong history.
- Có thể nhảy về revision cụ thể bằng `--to-revision=N`.

**Vì sao:** deploy lỗi là tình huống thực tế — cần quay lui nhanh mà không cần CI build lại. K8s giữ RS cũ chính là "undo stack" cho Deployment. Không hiểu rollback = không dám deploy.

**Cơ chế:** `rollout undo` chỉ đổi `spec.template` của Deployment về template của revision trước → K8s coi đó là deploy mới → tạo RS mới (thực ra là RS cũ được reuse) → rolling update ngược lại. RS không thực sự bị tạo lại — K8s nhận ra template giống RS cũ và scale nó lên thay vì tạo RS thứ ba.

| Lệnh | Tác dụng |
|---|---|
| `kubectl rollout status deployment/<name>` | Trạng thái rollout hiện tại |
| `kubectl rollout history deployment/<name>` | Danh sách revision |
| `kubectl rollout history deployment/<name> --revision=2` | Chi tiết revision 2 |
| `kubectl rollout undo deployment/<name>` | Quay về revision trước |
| `kubectl rollout undo deployment/<name> --to-revision=2` | Nhảy về revision cụ thể |

**Dùng / KHÔNG:**
- Dùng: deploy lỗi → rollback ngay. Deploy 2 lần liên tiếp đều lỗi → `--to-revision=N` nhảy thẳng về bản ổn.
- KHÔNG tự làm rollback bằng tay (xóa Pod, scale RS) — để `rollout undo` làm; tự tay dễ để lại trạng thái không nhất quán. **Phản đề:** trong GitOps (Argo CD), `rollout undo` thủ công sẽ bị Argo sync ghi đè — đúng hơn là revert commit trong git (xem Bắc cầu).

**Làm** (tiếp theo lab Rolling):
```bash
# Xem lịch sử revision
kubectl rollout history deployment/my-nginx

# Chi tiết revision 2
kubectl rollout history deployment/my-nginx --revision=2

# Rollback về revision trước (1.16 lại)
kubectl rollout undo deployment/my-nginx
kubectl rollout status deployment/my-nginx

# Xác nhận image đã quay về
kubectl describe deployment my-nginx | grep Image

# Lịch sử sau undo — revision cũ trở thành revision mới nhất
kubectl rollout history deployment/my-nginx

# Dọn
kubectl delete deployment my-nginx
```

**Kết quả:**
```text
$ kubectl rollout history deployment/my-nginx
REVISION CHANGE-CAUSE
1 <none>
2 <none>

$ kubectl rollout history deployment/my-nginx --revision=2
Pod Template:
 Containers:
 web:
 Image: nginx:1.17.8-alpine

$ kubectl rollout undo deployment/my-nginx
deployment.apps/my-nginx rolled back

$ kubectl describe deployment my-nginx | grep Image
 Image: nginx:1.16.1-alpine ← đã quay về 1.16

$ kubectl rollout history deployment/my-nginx
REVISION CHANGE-CAUSE
2 <none>
3 <none> ← revision cũ (1.16) xoay thành revision 3
```
→ **Verify:** `Image: nginx:1.16.1-alpine` — đúng. History: revision 2 (1.17) vẫn còn, revision 3 là 1.16 (rollback).

---

## 3. Canary Deployment — thử nghiệm với một phần traffic

**Chốt:** Canary = triển khai phiên bản mới cho **một phần nhỏ user thật** trong khi phần lớn vẫn chạy stable — dò lỗi trước khi rollout toàn bộ.

- **2 Deployment cùng label chung** → cùng 1 Service → Service phân phối traffic theo ratio replicas.
- Label `track: stable/canary` là tùy chọn — giúp lọc; Service chỉ quan tâm label chung (`app: myapp`).
- Tỉ lệ traffic = tỉ lệ replicas: 1 canary / 5 tổng Pod = ~20% traffic hit canary.
- Promote: scale canary lên, xóa stable. Rollback: xóa canary Deployment.

**Vì sao:** Rolling Update thay hết — nếu lỗi, cả 4 Pod mới đều lỗi. Canary giới hạn rủi ro: chỉ 1/5 user gặp lỗi, phần còn lại vẫn ổn. Cho phép quan sát metric (latency, error rate) trên traffic thật trước khi commit.

**Cơ chế:**

K8s Service dùng `iptables`/`ipvs` — không có round-robin cứng, phân phối ngẫu nhiên theo xác suất tỉ lệ Pod. Với 4 stable + 1 canary, mỗi request có 20% khả năng hit canary. Không có "session affinity theo version" trừ khi dùng Istio `VirtualService` với `weight`. Cách K8s thuần kiểm soát tỉ lệ: thay `replicas` của mỗi Deployment.

> **Ẩn dụ:** mở quầy thử nghiệm trong siêu thị bên cạnh quầy chính — khách ngẫu nhiên vào quầy nào; quầy thử chiếm 1/5 diện tích thì xác suất ghé ~20%.

**Dùng / KHÔNG:**
- Dùng: muốn thử nghiệm tính năng mới với real traffic, giám sát metric (error rate, p99 latency) trước khi promote.
- KHÔNG: khi user **không được thấy hai phiên bản** (A/B test có tác động UI khác nhau, schema DB không tương thích ngược) → dùng Blue-Green. **Phản đề:** canary K8s thuần không kiểm soát traffic chính xác theo phần trăm — chỉ xấp xỉ qua replicas; cần kiểm soát chính xác (vd 1%) → Istio VirtualService hoặc Argo Rollouts.

**Làm:**
```bash
# Stable Deployment (4 replicas)
cat > /tmp/stable.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
 name: app-stable
spec:
 replicas: 4
 selector:
 matchLabels: { app: myapp }
 template:
 metadata:
 labels: { app: myapp, track: stable }
 spec:
 containers:
 - name: web
 image: nginx:1.16.1-alpine
 ports: [{ containerPort: 80 }]
EOF

# Canary Deployment (1 replica — ~20% traffic)
cat > /tmp/canary.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
 name: app-canary
spec:
 replicas: 1
 selector:
 matchLabels: { app: myapp }
 template:
 metadata:
 labels: { app: myapp, track: canary }
 spec:
 containers:
 - name: web
 image: nginx:1.17.8-alpine
 ports: [{ containerPort: 80 }]
EOF

# Service chung — selector chỉ dùng label chung
cat > /tmp/svc.yml <<'EOF'
apiVersion: v1
kind: Service
metadata:
 name: myapp-svc
spec:
 type: LoadBalancer
 selector: { app: myapp }
 ports: [{ port: 80, targetPort: 80 }]
EOF

kubectl apply -f /tmp/stable.yml -f /tmp/canary.yml -f /tmp/svc.yml

# Xem column track
kubectl get pods -L track

# Mô phỏng traffic: canary xuất hiện ~20%
for i in $(seq 1 20); do
 curl -s http://localhost | grep -o 'nginx/[0-9.]*'
done

# Canary OK → promote
kubectl scale deployment app-canary --replicas=4
kubectl delete deployment app-stable

# Dọn
kubectl delete deploy app-stable app-canary --ignore-not-found
kubectl delete svc myapp-svc
```

**Kết quả:**
```text
$ kubectl get pods -L track
NAME READY STATUS TRACK
app-stable-7848d4b9f7-4xkp2 1/1 Running stable
app-stable-7848d4b9f7-8bn7t 1/1 Running stable
app-stable-7848d4b9f7-cd9qr 1/1 Running stable
app-stable-7848d4b9f7-lmn2p 1/1 Running stable
app-canary-6d4cf56db6-wqr3s 1/1 Running canary

$ for i in $(seq 1 20); do curl -s http://localhost | grep -o 'nginx/[0-9.]*'; done
nginx/1.16.1
nginx/1.16.1
nginx/1.17.8 ← canary xuất hiện
nginx/1.16.1
nginx/1.16.1
nginx/1.16.1
nginx/1.17.8 ← canary lại
nginx/1.16.1
... ← stable thắng, canary rải rác ~20%
```
→ **Verify:** `track=canary` chỉ 1 Pod; stable xuất hiện nhiều hơn trong curl loop. Sau promote: `kubectl get deploy` chỉ còn `app-canary` với `READY 4/4`.

---

## 4. Blue-Green Deployment — cắt traffic tức thì, không mixing

**Chốt:** Blue-Green = 2 môi trường đầy đủ chạy song song, user chỉ thấy một — chuyển bằng cách đổi selector của public Service.

- **Blue** = production hiện tại; **Green** = phiên bản mới đã kiểm tra xong.
- User **không bao giờ hit cả hai** version — khác hoàn toàn với canary.
- Cutover tức thì: K8s cập nhật endpoints ngay khi Service selector đổi.
- Chi phí: phải có tài nguyên cluster cho ~2x Deployment cùng lúc.

**Vì sao:** canary cho user thật thấy cả hai version — với tính năng thay đổi UI lớn hoặc schema DB mới, điều đó không chấp nhận được. Blue-green cô lập: green chạy song song nhưng chỉ dev thấy qua test Service riêng (port 9001), user thật vẫn thấy blue qua public Service (port 80). Khi xác nhận xong: đổi selector → cutover tức thì.

**Cơ chế:**

```
Public Service (port 80) ──selector: role=blue── Blue Deployment (2 Pod)
 Green Deployment (2 Pod) ── Test Service (port 9001)
```
Sau khi đổi selector:
```
Public Service (port 80) ──selector: role=green── Green Deployment (2 Pod)
```
K8s cập nhật Endpoints object tức thì — iptables rule trỏ sang Pod mới. Không có grace period ở tầng Service selector. Rollback: đổi selector về `role=blue` (Blue Deployment vẫn còn).

Hai cách đổi selector:
- **Declarative:** sửa YAML `selector.role: green` rồi `kubectl apply` — có audit trail trong git.
- **Imperative:** `kubectl set selector svc/<name> role=green` — nhanh hơn, dùng khi emergency.

**Dùng / KHÔNG:**
- Dùng: cần kiểm tra kỹ trước khi user thấy, cần instant cutover và instant rollback, môi trường production có compliance yêu cầu không mixing.
- KHÔNG: cluster nhỏ, không đủ tài nguyên cho 2x Pod cùng lúc. **Phản đề chi phí:** `replicas: 50` → cần 100 Pod slot trong lúc switch — tốn tiền/quota. Giải pháp: scale green lên dần rồi scale blue xuống (hybrid với rolling), hoặc dùng Argo Rollouts blue-green tối ưu hơn.

**Làm:**
```bash
# Blue Deployment
cat > /tmp/blue.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
 name: nginx-blue
spec:
 replicas: 2
 selector:
 matchLabels: { app: nginx, role: blue }
 template:
 metadata:
 labels: { app: nginx, role: blue }
 spec:
 containers:
 - name: web
 image: nginx:1.16.1-alpine
 ports: [{ containerPort: 80 }]
EOF

# Public Service — trỏ blue
cat > /tmp/public-svc.yml <<'EOF'
apiVersion: v1
kind: Service
metadata:
 name: nginx-public
spec:
 type: LoadBalancer
 selector: { app: nginx, role: blue }
 ports: [{ port: 80, targetPort: 80 }]
EOF

kubectl apply -f /tmp/blue.yml -f /tmp/public-svc.yml
kubectl get pods -L role
curl -s http://localhost | grep -o 'nginx/[0-9.]*'

# Green Deployment (version mới — chạy song song)
cat > /tmp/green.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
 name: nginx-green
spec:
 replicas: 2
 selector:
 matchLabels: { app: nginx, role: green }
 template:
 metadata:
 labels: { app: nginx, role: green }
 spec:
 containers:
 - name: web
 image: nginx:1.17.8-alpine
 ports: [{ containerPort: 80 }]
EOF

# Test Service — chỉ dev thấy (port 9001)
cat > /tmp/green-test.yml <<'EOF'
apiVersion: v1
kind: Service
metadata:
 name: nginx-green-test
spec:
 type: LoadBalancer
 selector: { app: nginx, role: green }
 ports: [{ port: 9001, targetPort: 80 }]
EOF

kubectl apply -f /tmp/green.yml -f /tmp/green-test.yml
kubectl get pods -L role

# Test green qua port riêng — user thật không thấy
curl -s http://localhost:9001 | grep -o 'nginx/[0-9.]*'

# Switch public traffic → green (imperative — nhanh nhất)
kubectl set selector svc/nginx-public role=green

# Xác nhận
kubectl describe svc nginx-public | grep Selector
curl -s http://localhost | grep -o 'nginx/[0-9.]*'

# Xóa blue và test service
kubectl delete deploy nginx-blue
kubectl delete svc nginx-green-test

# Dọn
kubectl delete deploy nginx-blue nginx-green --ignore-not-found
kubectl delete svc nginx-public nginx-green-test --ignore-not-found
```

**Kết quả:**
```text
$ kubectl get pods -L role
NAME READY STATUS ROLE
nginx-blue-7848d4b9f7-4xkp2 1/1 Running blue
nginx-blue-7848d4b9f7-8bn7t 1/1 Running blue
nginx-green-6d4cf56db6-wqr3s 1/1 Running green
nginx-green-6d4cf56db6-xp9mn 1/1 Running green

$ curl -s http://localhost | grep -o 'nginx/[0-9.]*'
nginx/1.16.1 ← public vẫn thấy blue

$ curl -s http://localhost:9001 | grep -o 'nginx/[0-9.]*'
nginx/1.17.8 ← green test OK — chỉ dev thấy

$ kubectl set selector svc/nginx-public role=green
service/nginx-public selector updated

$ kubectl describe svc nginx-public | grep Selector
Selector: app=nginx,role=green

$ curl -s http://localhost | grep -o 'nginx/[0-9.]*'
nginx/1.17.8 ← cutover tức thì, user thấy green
```
→ **Verify:** trước switch: `localhost` = 1.16 (blue), `localhost:9001` = 1.17 (green). Sau `set selector`: `localhost` = 1.17, tức thì, không mixing.

---

## 5. So sánh — khi nào dùng chiến lược nào

![[strategies.excalidraw]]

| Chiến lược | Traffic mixing | Downtime | Tài nguyên | Rollback | Dùng khi |
|---|---|---|---|---|---|
| **Rolling Update** | Có (v1+v2 tạm) | Không | 1x + surge nhỏ | `rollout undo` | Mặc định — phần lớn trường hợp |
| **Recreate** | Không | Có (ngắn) | 1x | `rollout undo` | Schema DB không tương thích ngược |
| **Canary** | Có (kiểm soát tỉ lệ) | Không | ~1.25x | Xóa canary Deployment | Thử nghiệm real traffic, quan sát metric |
| **Blue-Green** | Không | Không | ~2x | Đổi selector về blue | Cần kiểm tra kỹ trước user thấy, instant cutover |

---

## Dọn dẹp
```bash
kubectl delete deploy my-nginx app-stable app-canary nginx-blue nginx-green --ignore-not-found
kubectl delete svc myapp-svc nginx-public nginx-green-test --ignore-not-found
```

---

## Đủ khi
① rolling update hoạt động thế nào, maxSurge/maxUnavailable kiểm soát gì · ② rollback bằng lệnh nào, revision history là gì · ③ canary chia traffic bằng cơ chế nào (label + replicas ratio) · ④ blue-green khác canary chỗ nào (user không thấy cả hai, chi phí 2x) · ⑤ khi nào dùng Recreate thay Rolling.

## Recall
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

**Danh sách A — khái niệm:**
1. Rolling Update mặc định là gì? Nó đảm bảo zero-downtime bằng cách nào?
2. `maxSurge: 1` với `replicas: 4` nghĩa là gì?
3. `revisionHistoryLimit` dùng để làm gì?
4. Canary chia traffic bằng cơ chế gì trong K8s thuần (không Istio)?
5. Blue-green và canary khác nhau điểm gì về trải nghiệm user?

**Danh sách B — lệnh:**
6. Lệnh xem tiến độ rollout đang chạy?
7. Lệnh liệt kê lịch sử các revision?
8. Lệnh rollback về revision trước?
9. Lệnh đổi selector của Service tức thì (imperative)?
10. Lệnh xem chi tiết revision 2?

### Đáp án

1. `RollingUpdate` — thay từng Pod một: tạo Pod mới → đợi sẵn sàng → xóa Pod cũ. Luôn có Pod phục vụ traffic trong suốt quá trình.
2. Tối đa 5 Pod tồn tại cùng lúc trong lúc update (4 + 1 surge).
3. Số RS cũ được K8s giữ lại — giới hạn bao nhiêu revision có thể rollback. Default 10.
4. Hai Deployment cùng label `app=myapp` → cùng 1 Service. Tỉ lệ traffic = tỉ lệ replicas (1 canary / 5 tổng = 20%).
5. Canary: user thật có thể hit cả stable lẫn canary (mixing). Blue-green: user chỉ thấy một môi trường tại 1 thời điểm — traffic cutover tức thì qua Service selector.
6. `kubectl rollout status deployment/<name>`
7. `kubectl rollout history deployment/<name>`
8. `kubectl rollout undo deployment/<name>`
9. `kubectl set selector svc/<name> <key>=<value>`
10. `kubectl rollout history deployment/<name> --revision=2`
