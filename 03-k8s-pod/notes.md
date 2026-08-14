# Kubernetes — Pod: đơn vị nhỏ nhất, soi/debug, health probe

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [k8s-pod.md](k8s-pod.md).

## Pod là gì

<details>
<summary>1. Pod là gì? Nhiều container trong cùng một Pod chia sẻ những gì, và bị buộc phải khác nhau ở đâu?</summary>

Pod là **đơn vị nhỏ nhất K8s tạo/deploy** — nó *bọc* 1+ container tightly-coupled chạy cùng trên
**một node**. Container cùng Pod **chung**: IP (một `podIP` duy nhất), network namespace/`localhost`,
volume mount. Bị buộc **khác port** (chung network namespace nên không ai được trùng port). Pod
**không span node** và nhận **cluster IP** (chỉ truy cập nội bộ cụm).

Ẩn dụ: Pod = căn hộ, container = phòng — chung địa chỉ & hành lang, nhưng mỗi phòng làm việc riêng.
</details>

<details>
<summary>2. Vì sao cần khái niệm Pod, sao không schedule thẳng container?</summary>

Container đơn lẻ không đủ để K8s quản (scheduling, probe, policy, cấp IP). Pod là đơn vị tối thiểu
để **scheduler** đặt vào node, **kubelet** theo dõi health, **network plugin** cấp IP. kubelet dùng
một **pause container** (sandbox) giữ network namespace → các container trong Pod thật sự chia chung
1 network interface, gọi nhau qua `localhost:port` **không qua mạng**.
</details>

## Imperative vs Declarative

<details>
<summary>3. <code>kubectl run</code> vs <code>kubectl apply -f</code> — khác gì, khi nào dùng cái nào?</summary>

`run` = **imperative**: nhanh, hợp thử nghịch/debug, **không lưu ý định** (xóa là mất config).
`apply -f <yaml>` = **declarative**: YAML check vào Git, tái tạo được, idempotent → cách của
production. Thử nhanh dùng `run`; bất kỳ thứ gì đụng staging/prod hoặc >1 người quản → **YAML + apply**.
</details>

<details>
<summary>4. <code>apply</code> vs <code>create</code> khác gì? Vì sao apply lần 2 không báo lỗi?</summary>

`apply` = **tạo-hoặc-cập-nhật** (idempotent); `create`/`run` báo lỗi `AlreadyExists` nếu resource đã
tồn tại (lab thật: chạy lại `kubectl run my-nginx` → `Error ... AlreadyExists`). apply lần 2 trả
`pod/web unchanged` vì K8s lưu snapshot YAML lần trước vào annotation
`kubectl.kubernetes.io/last-applied-configuration`, rồi **diff** với snapshot đó — thấy khớp → báo
`unchanged`, không tạo trùng. Đây là thứ `run`/`create` không có.
</details>

## kubectl soi & debug

<details>
<summary>5. 4 lệnh để soi/debug một Pod lỗi, theo thứ tự nên dùng?</summary>

`describe pod` (đọc **Events** — lý do lỗi thường ở đây) → `logs [--previous]` (`--previous` = lần
chạy trước nếu vừa restart) → `exec -it -- sh` (vào shell container) → `get -o wide/yaml` (IP/node +
field K8s tự thêm). `port-forward <pod> 8080:80` để test tạm qua tunnel — **không cần Service**,
không mở NodePort; chỉ dùng debug, không cho traffic production.
</details>

<details>
<summary>6. Soi một Pod cũ mà <code>describe</code> hiện <code>Events: &lt;none&gt;</code> — lỗi hay bình thường?</summary>

**Bình thường.** Events trong K8s có **TTL ~1 giờ** rồi bị xóa khỏi etcd. Lab thật: Pod `my-nginx`
tuổi `15h`, `web` tuổi `14h` → cả hai đều `Events: <none>` vì sự kiện khai sinh
`Scheduled→Pulled→Created→Started` đã hết hạn. Muốn thấy chuỗi này phải soi Pod **mới tạo**.
`describe … Events` chỉ hữu ích cho sự kiện gần đây.
</details>

<details>
<summary>7. Trong <code>get -o yaml</code>, <code>hostIP</code> khác <code>podIP</code> nói lên điều gì?</summary>

Lab thật: `hostIP: 192.168.139.2` (IP của node) ≠ `podIP: 192.168.194.11` (IP riêng của Pod). Pod có
IP riêng, khác IP node — và `podIP` **chỉ truy cập được nội bộ cụm**. Đó là lý do phải `port-forward`
để `curl` từ máy host. Sau khi container restart, nếu Pod object không đổi thì `podIP` **giữ nguyên**
(Pod mới mới đổi IP).
</details>

## Pod trần chết là mất

<details>
<summary>8. Vì sao Pod trần xóa là mất, còn Pod của Deployment thì mọc lại?</summary>

Pod trần (`run`/`apply` Pod trực tiếp) có `ownerReferences` **rỗng** → không controller nào
reconcile → xóa/chết là **mất vĩnh viễn** (lab thật: `kubectl delete pod web` → `get pods` không còn
`web`). Pod của Deployment có `ownerReferences` trỏ **ReplicaSet**; ReplicaSet controller liên tục
`watch`, thấy `replicas hiện tại < desired` → tạo Pod mới trong vài giây. **Self-healing đến từ
controller loop, không phải từ Pod.**
</details>

## Health probe

<details>
<summary>9. Ai chạy probe, và với <code>httpGet</code> thì probe đi tới đâu?</summary>

**kubelet** trên node chạy probe (không phải controller/API server). Với `httpGet`, cứ mỗi
`periodSeconds` kubelet tự mở HTTP request **thẳng vào podIP:port** (`http://<podIP>:80/index.html`) —
không qua Service, không qua DNS. 200–399 = ok. Kết quả probe quyết định **kubelet action**, controller
không liên quan.
</details>

<details>
<summary>10. Liveness probe fail → chuyện gì? Readiness probe fail → chuyện gì?</summary>

**Liveness** fail → kubelet **restart container** (RESTARTS tăng). **Readiness** fail → K8s **gỡ Pod
khỏi endpoints, không route traffic, KHÔNG restart** — Pod chỉ "chưa sẵn sàng, chờ chút". Dùng sai:
hoặc restart oan (liveness quá nhạy), hoặc gửi traffic vào Pod chưa sẵn sàng (thiếu readiness).
</details>

<details>
<summary>11. 3 kiểu action của probe? Vì sao chọn <code>httpGet</code> thay vì <code>tcpSocket</code> lại quan trọng?</summary>

`httpGet` (200–399 = ok), `tcpSocket` (mở được TCP = ok), `exec` (exit 0 = ok). Lab thật: xóa
`index.html` → nginx **vẫn sống, port 80 vẫn mở**, chỉ trả 404 → `httpGet` bắt được (fail), nhưng
`tcpSocket` sẽ **luôn pass** (port vẫn mở) → không phát hiện lỗi. `httpGet` kiểm "app trả nội dung
đúng"; `tcpSocket` chỉ kiểm "port có mở".
</details>

<details>
<summary>12. RESTARTS tăng nhưng Pod không bị xóa — 3 tầng nào đang xảy ra?</summary>

| Tầng | Bị tạo mới? | Bằng chứng lab thật |
|---|---|---|
| Pod object (etcd) | KHÔNG | tên vẫn `live`, `AGE` tăng 56s→71s, `uid`/`podIP` không đổi |
| Container (process) | CÓ, bị kill & chạy lại | `RESTARTS: 2`, `Created container: web (x3 over 94s)` |
| Filesystem trong container | về image gốc | `index.html` tự có lại sau restart |

`Created ... (x3)` = 1 lần khai sinh + 2 lần restart (khớp `RESTARTS: 2`). Container restart **tại
chỗ** là việc của kubelet; delete-rồi-tạo-lại (Pod mới) mới là việc của controller — Pod trần không có.
</details>

<details>
<summary>13. Ý nghĩa <code>initialDelaySeconds</code> / <code>periodSeconds</code> / <code>failureThreshold</code>?</summary>

- `initialDelaySeconds` (lab: 5) — đợi bao lâu sau khi container start rồi mới probe **lần đầu**; cho
 app warm-up. Quá nhỏ với app start chậm (JVM, load model) → CrashLoopBackOff dù code đúng → dùng
 `startupProbe`.
- `periodSeconds` (lab: 5) — cứ mỗi 5s probe 1 lần. Nhỏ = phát hiện nhanh nhưng tốn/gây tải; lớn =
 nhẹ nhưng downtime lâu hơn.
- `failureThreshold` (lab: 1; mặc định K8s **3**) — số lần fail **liên tiếp** mới hành động. Đặt 1 để
 thấy nhanh trong lab; production để ≥3 để chịu nhiễu (hiccup lẻ không kéo cả container restart). Xen
 1 lần pass là bộ đếm **reset về 0**.

Công thức thời gian phát hiện lỗi: `detection = failureThreshold × periodSeconds` (lab: 1×5 = 5s;
mặc định: 3×10 = 30s).
</details>

<details>
<summary>14. Vì sao Pod <code>live</code> restart xong lại <code>Running</code>, và khi nào thành CrashLoopBackOff?</summary>

nginx image mỗi lần start chạy entrypoint **dựng lại** web-root mặc định → `index.html` có lại →
probe kế tiếp pass → `READY 1/1` → nó **tự lành** giữa các lần bạn phá. `CrashLoopBackOff` xảy ra khi
container fail lặp lại **mà không tự lành**; kubelet giãn dần khoảng chờ restart 10s→20s→40s→… tối đa
5 phút để khỏi restart điên cuồng. Lab này không rơi vào vì nginx phục hồi tức thì.
</details>

## Bắc cầu sang Kubernetes production

<details>
<summary>15. Các bài học này dùng lại ở cụm thật thế nào?</summary>

- Pod trần hiếm khi dùng thẳng ở prod — luôn qua **Deployment** (hoặc StatefulSet/DaemonSet). Trên
 cụm thật `kubectl -n <namespace> get pods` sẽ thấy Pod có `ownerReferences` trỏ ReplicaSet → đó là
 lý do chúng tự hồi.
- `kubectl logs [--previous]` + `describe` (Events) là **2 lệnh đầu tiên** mỗi khi Pod không healthy.
- Probe bạn viết = thứ giữ **rolling update không downtime** (readiness gác cửa trước khi Pod mới nhận
 traffic) — sẽ gặp ở module Deployment.

| Pod (module này) | Kubernetes production |
|---|---|
| Pod trần, `ownerReferences` rỗng | luôn qua Deployment → tự hồi |
| `describe`/`logs --previous` debug 1 Pod | 2 lệnh đầu tiên khi Pod lỗi trên cụm |
| liveness restart / readiness no-traffic | rolling update không downtime |
| YAML + `apply` idempotent | GitOps, mọi thay đổi qua Git |
</details>

## Ôn tập — đào sâu

<details>
<summary>16. VÌ SAO phải tách liveness & readiness — kịch bản nào chứng minh gộp lại là sai?</summary>

Hai probe trả lời **2 câu hỏi khác nhau**: liveness = "còn *sống* không hay treo cứng?" (fail → **restart**,
để CHỮA); readiness = "đã *sẵn sàng nhận việc* chưa?" (fail → **ngắt traffic**, để BẢO VỆ user).

Kịch bản chứng minh — app **warmup 20s** (nạp cache/kết nối DB): lúc này nó **sống nhưng chưa sẵn sàng**.
- Chỉ có liveness → probe fail lúc warmup → K8s tưởng chết → **restart** → warmup lại từ đầu → **restart-loop
 vĩnh viễn**, không bao giờ lên.
- Đúng: readiness fail lúc warmup (chỉ ngắt traffic, chờ), liveness nới lỏng để không giết oan → xong warmup
 → readiness pass → mới nhận traffic.

Ngược lại, app **treo deadlock** (còn process, không xử lý): chỉ liveness (restart) mới cứu; readiness một
mình sẽ để nó "ngắt traffic" nằm đó mãi, không ai chữa. → **Một cái CHỮA, một cái BẢO VỆ; gộp thì hoặc giết
oan, hoặc không chữa.**
</details>

<details>
<summary>17. Bẫy timing: <code>initialDelaySeconds</code> KHÔNG tính vào thời gian phát hiện "app treo giữa chừng"</summary>

`initialDelaySeconds` chỉ áp dụng **một lần lúc container mới start** (ân hạn app bật lên). Khi app đang chạy
ngon rồi **mới treo**, kubelet đã probe đều đặn — không còn initialDelay → **không cộng** vào thời gian phát
hiện chết.

Thời gian tệ nhất từ lúc treo → bị kết luận chết (`periodSeconds: 5`, `failureThreshold: 3`):

```
app treo ────┐
             │ (tới ~5s trôi qua trước khi probe kế chạy — độ trễ bắt fail đầu)
   probe#1 FAIL  ← +5s   (fail 1/3)
   probe#2 FAIL  ← +5s   (fail 2/3)
   probe#3 FAIL  ← +5s   (fail 3/3) → RESTART
```

- Từ **fail đầu → bị kết luận chết** = `(failureThreshold − 1) × periodSeconds` = `(3−1)×5 = 10s`.
- Cộng tối đa 1 `periodSeconds` (~5s) độ trễ bắt được fail đầu (treo ngay sau probe vừa pass) → **tệ nhất
 ~15–20s**. Con số "detection = failureThreshold × periodSeconds" (câu 13) là ước lượng nhanh; công thức chính
 xác cho "treo giữa chừng" là `(threshold−1)×period` + tối đa 1 period độ trễ.
</details>

