# 04 · ReplicaSet & Deployment — self-healing, scale, zero-downtime

Trước: [03 · Pod](../03-k8s-pod/k8s-pod.md) · kế tiếp: Service.

**Mục tiêu:** hiểu tại sao Deployment ra đời thay thế Pod trần; nắm chuỗi Deployment → ReplicaSet → Pod; tạo và quản Deployment bằng YAML + kubectl; tự tay scale, rolling update, rollout undo — thấy zero-downtime hoạt động thật.
**Nền:** lab Pod đã cho thấy "Pod trần xóa là mất" (`ownerReferences` rỗng). Lab này lắp controller vào để cái chết của 1 Pod không còn là vấn đề.
**⏱** 60–75 phút · **Sân:** host local (OrbStack Kubernetes).

> Mỗi mục: **Chốt → Vì sao → Cơ chế → Dùng/không → Làm → Kết quả** (output để đối chiếu). Đọc để *hiểu*, gõ để *thấy*.

## Tiền đề (1 lần)

```bash
kubectl config use-context orbstack
kubectl get nodes    # 1 node STATUS=Ready
```

---

## 1. ReplicaSet — "boss of the pods"

**Chốt:** ReplicaSet là controller liên tục so sánh số Pod *thực tế* với số *mong muốn* và tự bù đắp nếu lệch — đây là nguồn gốc của "self-healing".

- **Desired-state enforcement:** khai báo `replicas: N`, ReplicaSet đảm bảo *luôn* có đúng N Pod đang chạy.
- **Self-healing:** Pod chết → ReplicaSet nhận biết `current < desired` → tạo Pod mới thay thế tự động.
- **Horizontal scale:** tăng/giảm `replicas` là xong — không cần làm gì thêm.
- **Pod template + selector:** ReplicaSet dùng `spec.template` để biết *tạo Pod kiểu gì*, và dùng `selector.matchLabels` để *nhận ra* Pod nào thuộc mình.
- `ownerReferences` của mỗi Pod trỏ về ReplicaSet đó — phân biệt với Pod trần (ownerReferences rỗng → không ai tạo lại khi chết).

**Vì sao:** Pod trần chết là chết hẳn — bạn phải tự tạo lại bằng tay. Trong production, Node sập, OOM kill, process crash đều xảy ra. Không ai ngồi canh 24/7 để `kubectl apply` lại. ReplicaSet làm việc đó liên tục không nghỉ.

**Cơ chế:** ReplicaSet chạy một **reconciliation loop** — mỗi chu kỳ: đếm Pod hiện có khớp `selector.matchLabels`, so với `spec.replicas`, rồi:
- `current < desired` → gọi API tạo thêm Pod (dùng `spec.template`).
- `current > desired` → terminate bớt Pod (ưu tiên Pod không healthy trước).
- `current == desired` → không làm gì.

> **Ẩn dụ:** ReplicaSet như quản đốc ca sản xuất — anh ta không tự làm hàng, nhưng đếm đầu nhân công mỗi giờ, thiếu thì gọi người mới, thừa thì cho về.

| Tình huống | Điều gì xảy ra |
|---|---|
| `kubectl delete pod <tên>` | ReplicaSet tạo Pod mới thay thế |
| Node sập, Pod biến mất | ReplicaSet schedule Pod mới lên Node khác |
| `replicas: 2 → 4` | ReplicaSet tạo thêm 2 Pod |
| `replicas: 4 → 1` | ReplicaSet terminate 3 Pod |

**Dùng / KHÔNG:**
- Dùng khi muốn self-healing mà không cần rolling update/rollback — ví dụ stateless batch job đơn giản.
- **Phản đề:** trong thực tế bạn *hiếm khi tạo ReplicaSet trực tiếp* — Deployment là wrapper cao hơn, tạo và quản ReplicaSet thay bạn, đồng thời bổ sung rolling update + rollback. Tạo ReplicaSet thẳng = mất khả năng rollback.

**Làm** (chứng kiến self-healing — YAML tạo ở mục 2):
```bash
# Xem Pod đang chạy, ghi tên 1 Pod
kubectl get pods

# Xóa 1 Pod tay
kubectl delete pod <tên-pod>

# Xem Pod mới mọc lên
kubectl get pods -w    # Ctrl+C khi thấy Pod mới Running

# Xác nhận ownerReferences trỏ về ReplicaSet
kubectl get pod <tên-pod-mới> -o jsonpath='{.metadata.ownerReferences[0].kind}'; echo
```

**Kết quả:**
```text
$ kubectl delete pod my-nginx-7d8f9c5b4-xk2qp
pod "my-nginx-7d8f9c5b4-xk2qp" deleted

$ kubectl get pods -w
NAME                        READY   STATUS              RESTARTS   AGE
my-nginx-7d8f9c5b4-9mzlr    1/1     Running             0          2m
my-nginx-7d8f9c5b4-nw4vp    0/1     ContainerCreating   0          3s   ← Pod mới mọc
my-nginx-7d8f9c5b4-nw4vp    1/1     Running             0          5s

$ kubectl get pod my-nginx-7d8f9c5b4-nw4vp \
    -o jsonpath='{.metadata.ownerReferences[0].kind}'; echo
ReplicaSet    ← không phải rỗng như Pod trần
```
→ **Verify:** Pod bị xóa → Pod mới thay thế tự động trong vài giây; ownerReferences = `ReplicaSet`.

---

## 2. Deployment — wrapper quản ReplicaSet

**Chốt:** Deployment là tầng cao hơn ReplicaSet — nó không trực tiếp quản Pod, mà tạo và quản **ReplicaSet**; phần thêm giá trị là **rolling update zero-downtime** và **rollback** về version trước.

- Chuỗi 3 tầng: `Deployment → ReplicaSet → Pod(s)`.
- Deployment tạo ReplicaSet, ReplicaSet tạo Pod — Deployment không bao giờ chạm trực tiếp vào Pod.
- Đổi image → Deployment tạo ReplicaSet *mới* (version mới) song song với ReplicaSet cũ, lần lượt chuyển Pod.
- `kubectl rollout undo` → Deployment trỏ lại ReplicaSet cũ (vẫn còn đó, chỉ `replicas=0`).
- YAML Deployment và ReplicaSet gần như giống nhau, chỉ khác `kind: Deployment` vs `kind: ReplicaSet`.

**Vì sao:** ReplicaSet thuần đảm bảo *số lượng* Pod, nhưng không có cơ chế thay image an toàn — muốn update version mới là phải xóa hết Pod cũ (downtime) rồi tạo lại với image mới. Deployment giải quyết đúng bài toán đó.

**Cơ chế:** khi bạn `kubectl apply` với image mới, Deployment controller:
1. Tạo **ReplicaSet mới** (revision +1) với image mới, `replicas=0`.
2. Tăng dần `replicas` của RS mới, chờ Pod mới `Ready`.
3. Giảm dần `replicas` của RS cũ.
4. Lặp lại cho đến RS cũ `replicas=0`, RS mới = target.
5. RS cũ vẫn tồn tại (replicas=0) — đó là "checkpoint" để rollback.

> **Ẩn dụ:** Deployment như tổng công trình sư — ông ta không đặt gạch, nhưng điều phối đội thợ (ReplicaSet) theo bản thiết kế; nâng cấp tòa nhà mà không phải phá đi xây lại từ đầu.

**Dùng / KHÔNG:**
- Dùng cho hầu hết workload stateless — web app, API server, worker.
- **Phản đề:** workload stateful (database, Kafka, Zookeeper) cần thứ tự startup xác định và stable network identity → dùng `StatefulSet`, không phải Deployment.

**Làm** (tạo Deployment đầu tiên, quan sát chuỗi 3 tầng):
```bash
cat > /tmp/dep-nginx.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-nginx
  labels:
    app: my-nginx
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-nginx
  template:
    metadata:
      labels:
        app: my-nginx
    spec:
      containers:
      - name: web
        image: nginx:alpine
        ports:
        - containerPort: 80
        resources:
          limits:
            memory: "128Mi"
            cpu: "200m"
EOF

kubectl apply -f /tmp/dep-nginx.yml

# Quan sát chuỗi 3 tầng
kubectl get all
```

**Kết quả:**
```text
$ kubectl get all
NAME                            READY   STATUS    RESTARTS   AGE
pod/my-nginx-7d8f9c5b4-9mzlr    1/1     Running   0          12s
pod/my-nginx-7d8f9c5b4-xk2qp    1/1     Running   0          12s

NAME                        READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/my-nginx    2/2     2            2           12s    ← tầng 1

NAME                                  DESIRED   CURRENT   READY   AGE
replicaset.apps/my-nginx-7d8f9c5b4    2         2         2       12s  ← tầng 2
                                                                        (Pod = tầng 3)
```
→ **Verify:** 1 Deployment → 1 ReplicaSet → 2 Pod. `READY 2/2` và `AVAILABLE 2`.

---

## 3. YAML Deployment — cấu trúc & selector

**Chốt:** YAML Deployment có hai khối `spec` lồng nhau; `selector.matchLabels` phải khớp chính xác với `template.metadata.labels` — sai là Deployment báo lỗi ngay.

- `spec` ngoài (của Deployment): chứa `replicas`, `selector`, `template`.
- `spec` trong (của Pod template, tức `spec.template.spec`): chứa `containers`.
- `selector.matchLabels` = cách Deployment/ReplicaSet *nhận ra* Pod nào thuộc mình — dùng để lọc qua `kubectl get -l <key>=<value>`.
- `resources.limits` (memory/cpu) nên luôn đặt — container không có limit có thể dùng hết tài nguyên node, kéo sập workload khác cùng node.

**Vì sao:** sai `selector` là lỗi thường gặp nhất khi viết YAML tay. K8s báo `selector does not match template labels` — hiểu cấu trúc lồng nhau giúp debug ngay thay vì đoán mò.

**Cơ chế:** khi Deployment tạo ReplicaSet, nó copy `selector` và `template` sang. ReplicaSet dùng `selector` để `list/watch` Pod qua API — chỉ những Pod có label khớp mới được tính vào `current`. Đây cũng là cách một Pod vô tình có cùng label có thể bị "nhận nuôi" bởi ReplicaSet của Deployment khác.

> **Ẩn dụ:** selector như tên họ — ReplicaSet chỉ "nhận" Pod nào cùng họ. Pod trùng họ từ gia đình khác sẽ bị gộp vào, dù không ai muốn vậy.

| Trường | Nằm ở | Mục đích |
|---|---|---|
| `spec.replicas` | Deployment spec | Số Pod mong muốn |
| `spec.selector.matchLabels` | Deployment spec | Bộ lọc nhận ra Pod thuộc Deployment |
| `spec.template.metadata.labels` | Pod template | Label gán vào từng Pod được tạo |
| `spec.template.spec.containers` | Pod template | Định nghĩa container bên trong Pod |

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-nginx
  labels:
    app: my-nginx        # label của chính Deployment (không bắt buộc khớp selector)
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-nginx      # ← phải khớp template.metadata.labels
  template:
    metadata:
      labels:
        app: my-nginx    # ← phải khớp selector.matchLabels
    spec:
      containers:
      - name: web
        image: nginx:alpine
        ports:
        - containerPort: 80
        resources:
          limits:
            memory: "128Mi"
            cpu: "200m"
```

**Dùng / KHÔNG:**
- Đặt `resources.limits` từ đầu — dễ quên khi prototype, khó nhớ lại khi lên production.
- **Phản đề:** trên môi trường dev local với 1 node, thiếu limit chỉ ảnh hưởng máy bạn — không gây tạm. Nhưng đừng để thói quen này lên staging/prod.

**Làm:**
```bash
# Xem selector Deployment đang dùng
kubectl get deployment my-nginx -o jsonpath='{.spec.selector}'; echo

# Lọc theo label
kubectl get pods -l app=my-nginx

# Xem tất cả label của Pod
kubectl get pods --show-labels
```

**Kết quả:**
```text
$ kubectl get deployment my-nginx -o jsonpath='{.spec.selector}'; echo
{"matchLabels":{"app":"my-nginx"}}

$ kubectl get pods -l app=my-nginx
NAME                        READY   STATUS    RESTARTS   AGE
my-nginx-7d8f9c5b4-9mzlr    1/1     Running   0          3m
my-nginx-7d8f9c5b4-xk2qp    1/1     Running   0          3m

$ kubectl get pods --show-labels
NAME                        READY   STATUS    RESTARTS   AGE   LABELS
my-nginx-7d8f9c5b4-9mzlr    1/1     Running   0          3m    app=my-nginx,pod-template-hash=7d8f9c5b4
my-nginx-7d8f9c5b4-xk2qp    1/1     Running   0          3m    app=my-nginx,pod-template-hash=7d8f9c5b4
```
→ **Verify:** selector trả về đúng label; `pod-template-hash` là label K8s tự thêm để phân biệt Pod của các ReplicaSet khác nhau.

---

## 4. Scale — tăng/giảm số Pod

**Chốt:** scale Deployment = thay đổi `replicas`; có hai cách — imperative (lệnh nhanh) và declarative (sửa YAML + apply); ReplicaSet tự tạo thêm hoặc terminate bớt Pod.

- **Imperative:** `kubectl scale deployment <name> --replicas=N` — nhanh, phù hợp thử nghiệm tay.
- **Declarative:** sửa `replicas:` trong YAML rồi `kubectl apply -f` — idempotent, check vào Git, phù hợp production.
- Scale *xuống* cũng tương tự — ReplicaSet terminate Pod theo thứ tự Pod ít healthy nhất trước.

**Vì sao:** traffic tăng đột biến, cần thêm Pod ngay trong vài giây mà không deploy lại. Hoặc cuối ngày muốn scale xuống để tiết kiệm tài nguyên. Đây là thao tác không có downtime, không ảnh hưởng Pod đang chạy.

**Cơ chế:** `kubectl scale` chỉ patch trường `spec.replicas` trong object Deployment trên API server. Deployment controller nhận thông báo, cập nhật ReplicaSet. ReplicaSet controller nhận, bắt đầu tạo/terminate Pod. Toàn bộ là async — lệnh trả về ngay, Pod mọc/tắt sau đó vài giây.

> **Ẩn dụ:** scale như điều chỉnh số dây chuyền sản xuất — không cần dừng nhà máy, chỉ bật thêm dây chuyền mới hoặc tắt bớt dây cũ.

| Cách | Lệnh | Khi nào dùng |
|---|---|---|
| Imperative | `kubectl scale deployment my-nginx --replicas=4` | Debug nhanh, local test, on-call |
| Declarative | Sửa `replicas: 4` trong YAML → `kubectl apply -f` | Production, CI/CD, mọi thứ cần lặp lại |

**Dùng / KHÔNG:**
- Imperative ổn khi thử nghiệm hoặc xử lý sự cố — nhanh, không cần file.
- **Phản đề:** dùng imperative trên production mà không sync lại YAML → YAML trong Git "nói dối" (replicas=2 nhưng thực tế đang chạy 8). Lần apply tiếp theo sẽ scale về 2, gây bất ngờ.

**Làm:**
```bash
# Scale lên bằng lệnh imperative
kubectl scale deployment my-nginx --replicas=4
kubectl get pods -w    # Ctrl+C khi thấy 4 Pod Running

# Scale xuống bằng YAML declarative (sửa replicas: 2)
sed -i '' 's/replicas: 2/replicas: 2/' /tmp/dep-nginx.yml   # đã là 2, thực tế sửa thành 2
# Hoặc sửa tay: mở file, đổi replicas: 4 → replicas: 2
kubectl apply -f /tmp/dep-nginx.yml
kubectl get all
```

**Kết quả (sau scale lên 4):**
```text
$ kubectl get pods -w
NAME                        READY   STATUS              RESTARTS   AGE
my-nginx-7d8f9c5b4-9mzlr    1/1     Running             0          5m
my-nginx-7d8f9c5b4-xk2qp    1/1     Running             0          5m
my-nginx-7d8f9c5b4-r7tpl    0/1     ContainerCreating   0          2s
my-nginx-7d8f9c5b4-w2kqv    0/1     ContainerCreating   0          2s
my-nginx-7d8f9c5b4-r7tpl    1/1     Running             0          4s
my-nginx-7d8f9c5b4-w2kqv    1/1     Running             0          5s

$ kubectl get all
NAME                        READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/my-nginx    4/4     4            4           6m

NAME                                  DESIRED   CURRENT   READY
replicaset.apps/my-nginx-7d8f9c5b4    4         4         4
```
→ **Verify:** Deployment `READY 4/4`; sau apply lại với replicas=2, 2 Pod chuyển sang `Terminating` rồi biến mất.

---

## 5. Rolling update & zero-downtime

![[deploy-rollout.excalidraw]]

**Chốt:** khi đổi image và `kubectl apply`, Deployment tạo ReplicaSet *mới* song song với ReplicaSet cũ, lần lượt chuyển Pod — không có khoảng trắng downtime vì lúc nào cũng có Pod đang phục vụ.

- Deployment tạo ReplicaSet mới (revision +1), tăng Pod mới dần dần.
- Chờ mỗi Pod mới `Ready` (readiness probe pass) trước khi terminate Pod cũ tương ứng.
- Kiểu update mặc định: `RollingUpdate` (ngược lại là `Recreate` — xóa hết rồi tạo lại, có downtime).
- `minReadySeconds` (tuỳ chọn): Pod mới phải sống ổn định N giây trước khi được tính `Ready` — buffer tránh traffic vào Pod đang khởi động chưa ổn định.
- `kubectl rollout undo` → Deployment trỏ lại ReplicaSet cũ (vẫn tồn tại với `replicas=0`).

**Vì sao:** deploy lúc 2 giờ sáng để tránh người dùng là công nghệ cũ. Rolling update cho phép deploy *bất kỳ lúc nào* vì không có downtime. Rollback trong vài giây khi phát hiện bug — không cần hotfix khẩn.

**Cơ chế:** Deployment controller theo dõi `maxSurge` (tối đa bao nhiêu Pod *thêm* được tạo trong lúc update) và `maxUnavailable` (tối đa bao nhiêu Pod cũ được terminate trước khi Pod mới Ready). Mặc định cả hai là `25%` — với 4 Pod: tạo tối đa 1 Pod mới cùng lúc, terminate tối đa 1 Pod cũ.

> **Ẩn dụ:** thay thủy thủ trên tàu đang chạy — mỗi lần chỉ cho 1 người xuống tàu cứu hộ và 1 người mới lên. Tàu không bao giờ dừng, không bao giờ thiếu người lái.

**Dùng / KHÔNG:**
- `RollingUpdate` cho hầu hết workload stateless.
- **Phản đề:** rolling update chạy *hai version* song song trong thời gian ngắn — nếu code v2 không tương thích ngược với schema DB hay API của v1, sẽ có lỗi trong giai đoạn chuyển tiếp. Giải pháp: backward-compatible migration, hoặc dùng `Recreate` (chấp nhận downtime ngắn) khi version không tương thích.

**Làm:**
```bash
# Đổi image → trigger rolling update
kubectl set image deployment/my-nginx web=nginx:1.25-alpine

# Theo dõi tiến độ real-time
kubectl rollout status deployment/my-nginx

# Xem lịch sử revision
kubectl rollout history deployment/my-nginx

# Rollback về revision trước
kubectl rollout undo deployment/my-nginx
kubectl rollout status deployment/my-nginx

# Xem hai ReplicaSet (cũ replicas=0, mới replicas=2)
kubectl get replicaset
```

**Kết quả:**
```text
$ kubectl rollout status deployment/my-nginx
Waiting for deployment "my-nginx" rollout to finish: 1 out of 2 new replicas have been updated...
Waiting for deployment "my-nginx" rollout to finish: 1 old replicas are pending termination...
deployment "my-nginx" successfully rolled out

$ kubectl rollout history deployment/my-nginx
REVISION  CHANGE-CAUSE
1         <none>
2         <none>    ← revision 2 = image nginx:1.25-alpine

$ kubectl rollout undo deployment/my-nginx
deployment.apps/my-nginx rolled back

$ kubectl rollout status deployment/my-nginx
deployment "my-nginx" successfully rolled out

$ kubectl get replicaset
NAME                        DESIRED   CURRENT   READY   AGE
my-nginx-7d8f9c5b4          2         2         2       12m   ← RS cũ (revision 1) active lại
my-nginx-6c9f8b7a3          0         0         0       4m    ← RS bị rollback (replicas=0)
```
→ **Verify:** `rollout status` in `successfully rolled out`; sau undo, RS cũ active (`DESIRED 2`), RS mới về `0`; Pod `RESTARTS` vẫn bình thường.

> **Thực chạy — undo tạo revision MỚI, nhưng tái dùng RS cũ.** `rollout history` đổi từ `1, 2` sang **`2, 3`**: `undo` được coi là một thay đổi mới nên cấp revision kế tiếp (3), *không* tái dùng số 1. Nhưng ReplicaSet thì **được tái dùng** — K8s thấy template rollback khớp hash RS đã có (`769df8fff`, cùng AGE với RS gốc) → bật lại đúng RS đó thay vì đúc mới. Hai RS chỉ **đổi vai** `replicas` qua lại, không cái nào bị xoá.

---

## 6. Imperative vs declarative — chọn theo ngữ cảnh

**Chốt:** imperative (lệnh trực tiếp) nhanh nhưng không lưu vết; declarative (YAML + apply) idempotent và traceable — production luôn chọn declarative.

- **Imperative:** `kubectl create`, `kubectl scale`, `kubectl set image` — không cần file, kết quả tức thì.
- **Declarative:** `kubectl apply -f <yaml>` — tạo-hoặc-cập-nhật, chạy lại nhiều lần không lỗi (idempotent).
- `kubectl create` báo lỗi nếu resource đã tồn tại — thêm `--save-config` khi dùng `create` lần đầu để `apply` sau có thể diff đúng.
- `apply` lưu trạng thái vào annotation `kubectl.kubernetes.io/last-applied-configuration` — dùng để tính diff lần apply tiếp.

**Vì sao:** imperative trên production = "ai đó làm gì đó lúc 3 giờ sáng, không ai biết, YAML trong Git không phản ánh thực tế". Khi cluster bị reset hoặc apply lại từ Git, config đó mất. Declarative = Git là nguồn sự thật duy nhất.

**Cơ chế:** `kubectl apply` so sánh 3 thứ: (1) trạng thái object *trên cluster hiện tại*, (2) *last-applied-configuration* (lần apply trước), (3) *YAML mới*. Từ đó tính diff và patch chính xác — không ghi đè những trường do controller/user khác quản lý.

> **Ẩn dụ:** imperative như dùng phím tắt sửa trực tiếp trên server; declarative như commit lên Git rồi CI/CD deploy — lần sau mọi người đều thấy đúng trạng thái.

| | Imperative | Declarative |
|---|---|---|
| Lệnh | `kubectl create`, `kubectl scale`, `kubectl set image` | `kubectl apply -f` |
| Khi resource đã tồn tại | Báo lỗi | Cập nhật (patch diff) |
| Idempotent | Không | Có |
| Dùng khi | Debug nhanh, local test, on-call | Production, CI/CD, mọi thứ lặp lại |

**Dùng / KHÔNG:**
- Imperative tốt khi học, thử nhanh, xử lý sự cố ngay lập tức.
- **Phản đề:** đừng trộn hai cách trên cùng resource trong production — imperative scale lên 8, declarative apply về 2 → surprise. Chọn 1 cách và nhất quán.

**Làm:**
```bash
# Xem annotation last-applied-configuration (biết apply đã lưu gì)
kubectl get deployment my-nginx \
  -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
  | python3 -m json.tool

# Imperative create với --save-config (để apply sau diff đúng)
kubectl delete deployment my-nginx
kubectl create -f /tmp/dep-nginx.yml --save-config   # lần đầu dùng create
kubectl apply -f /tmp/dep-nginx.yml                  # lần sau: idempotent update
```

**Kết quả:**
```text
$ kubectl get deployment my-nginx \
    -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
    | python3 -m json.tool
{
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": "my-nginx",
        ...
    },
    "spec": {
        "replicas": 2,
        ...
    }
}

$ kubectl apply -f /tmp/dep-nginx.yml
deployment.apps/my-nginx unchanged    ← idempotent: không thay gì vì YAML khớp cluster
```
→ **Verify:** annotation có nội dung YAML; apply lại cùng file ra `unchanged`.

---

## Dọn dẹp

```bash
kubectl delete deployment my-nginx --ignore-not-found
kubectl get all    # xác nhận sạch (chỉ còn kubernetes service)
```

---

## Đủ khi (nói trơn bằng lời mình)

① ReplicaSet làm gì khi Pod chết, vì sao nó là nguồn của "self-healing" · ② Chuỗi 3 tầng Deployment → ReplicaSet → Pod và vai trò từng tầng · ③ Viết YAML Deployment từ đầu, giải thích vì sao `selector.matchLabels` phải khớp `template.metadata.labels` · ④ Scale cả hai cách (imperative + declarative) và biết khi nào dùng cái nào · ⑤ Mô tả rolling update xảy ra thế nào bước-từng-bước · ⑥ Dùng `rollout status`, `rollout history`, `rollout undo` đúng lúc · ⑦ Giải thích vì sao production luôn chọn declarative.

## Recall — tự kiểm (cuối buổi)

Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

1. ReplicaSet làm gì khi 1 Pod trong nhóm nó quản bị xóa?
2. `ownerReferences` của Pod do ReplicaSet tạo trỏ về đối tượng gì? Khác Pod trần thế nào?
3. Deployment và ReplicaSet khác nhau ở điểm nào cốt lõi nhất?
4. Trong YAML Deployment, `selector.matchLabels` phải khớp với trường nào? Sai thì sao?
5. Scale imperative dùng lệnh gì? Scale declarative làm thế nào?
6. Rolling update xảy ra theo trình tự nào (3 bước)?
7. `rollout status` dùng để làm gì? `rollout undo` dùng khi nào?
8. `kubectl create` vs `kubectl apply` — sự khác biệt khi resource đã tồn tại?
9. `minReadySeconds` có tác dụng gì trong rolling update?
10. Vì sao nên đặt `resources.limits` (memory/cpu) cho container?

### Đáp án

1. ReplicaSet phát hiện `current < desired` → tự tạo Pod mới thay thế trong vài giây. Tự động, không cần can thiệp tay.
2. Trỏ về `ReplicaSet` (kind: ReplicaSet, name: tên-replicaset). Pod trần: `ownerReferences` rỗng → không ai tạo lại khi chết.
3. Deployment là wrapper cao hơn: quản ReplicaSet (không trực tiếp quản Pod), và hỗ trợ rolling update zero-downtime + rollback. ReplicaSet chỉ đảm bảo số lượng Pod.
4. Phải khớp với `spec.template.metadata.labels`. Sai → K8s báo lỗi `selector does not match template labels` ngay khi apply.
5. Imperative: `kubectl scale deployment <name> --replicas=N`. Declarative: sửa `replicas:` trong YAML rồi `kubectl apply -f`.
6. ① Tạo ReplicaSet mới + Pod mới (image mới), chờ Pod Ready → ② Terminate 1 Pod cũ → ③ Lặp lại đến khi toàn bộ chuyển sang version mới.
7. `rollout status` — theo dõi tiến độ rolling update real-time. `rollout undo` — khi phát hiện version mới có vấn đề, quay về ReplicaSet/revision trước.
8. `create` báo lỗi nếu resource đã tồn tại. `apply` tạo-hoặc-cập-nhật (idempotent) — phù hợp workflow apply lại nhiều lần.
9. Yêu cầu Pod mới không crash trong N giây trước khi được tính `Ready` — tránh traffic vào Pod đang khởi động chưa ổn định.
10. Container không có limits có thể dùng hết RAM/CPU của node, kéo sập toàn bộ workload khác trên cùng node.

---

## Bắc cầu sang Kubernetes production

`kubectl -n <namespace> get pods` — mỗi Pod có `ownerReferences → ReplicaSet → Deployment`. Lần sau thấy Pod tự restart không phải bug — ReplicaSet đang reconcile. Rolling update chính là cơ chế `kubectl rollout` bạn dùng khi deploy phiên bản mới mà không cần downtime maintenance window. `kubectl rollout undo` là công cụ rollback nhanh khi hotfix chưa kịp build.

---

