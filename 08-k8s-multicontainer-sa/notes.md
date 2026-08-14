# Kubernetes — Multi-container Pod & ServiceAccount

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành + giải thích đầy đủ ở [k8s-multicontainer-sa.md](k8s-multicontainer-sa.md).

## Multi-container patterns

<details>
<summary>1. initContainers vs containers — khai báo ở đâu, vòng đời khác gì?</summary>

`initContainers[]` và `containers[]` là **2 danh sách tách biệt**; vị trí khai báo quyết định vòng đời.
**init**: chạy TRƯỚC, tuần tự (từng cái xong hẳn mới tới cái kế), phải `exit 0` rồi **chết luôn**; chỉ khi TẤT
CẢ init xong app mới khởi; fail → Pod restart lại init từ đầu (nên phải idempotent). **containers**: chạy SONG
SONG, sống DÀI HẠN (app phục vụ + sidecar), chết thì kubelet restart giữ sống. Ẩn dụ: init = công nhân đổ móng
rồi rời đi; containers = gia đình ở dài hạn.
</details>

<details>
<summary>2. Pod có 3 init container, init thứ 2 fail → K8s làm gì?</summary>

Pod restart, chạy lại **toàn bộ** init container **từ cái đầu tiên** (không phải chỉ cái fail). Vì thế init
phải **idempotent** — chạy lại nhiều lần cho cùng kết quả.
</details>

<details>
<summary>3. (thực chạy) Init container <code>nslookup my-svc</code> kẹt mãi dù đã tạo Service — vì sao?</summary>

busybox (musl libc) **không iterate search domain đầy đủ** như glibc → chỉ hỏi `my-svc.svc.cluster.local`,
thiếu `.default.` → mãi NXDOMAIN dù Service `my-svc.default.svc.cluster.local` đã tồn tại. Fix: dùng **FQDN**
`my-svc.default.svc.cluster.local`. Bài học: **trong Pod dùng FQDN, đừng dựa search domain — nhất là busybox.**
Phụ: CoreDNS cache NXDOMAIN ~30s nên kể cả tên đúng cũng có thể trễ tới 30s sau khi tạo Service.
</details>

<details>
<summary>4. 2 container trong 1 Pod chia sẻ file bằng gì? Chia sẻ network thế nào?</summary>

**Volume** (`emptyDir` hoặc PVC) mount vào cả hai qua cùng `name` → "hai cửa sổ một phòng". **Network**: cùng
network namespace → cùng 1 IP, gọi nhau qua `localhost:<port>` (phải khác port). KHÔNG chia sẻ PID namespace
(không thấy process của nhau, trừ khi `shareProcessNamespace: true`).
</details>

<details>
<summary>5. Adapter vs Ambassador — khác nhau ở HƯỚNG nào? Ví dụ?</summary>

Cả hai là sidecar chuyên biệt. **Adapter** xử lý output ĐẾN: đọc output app → transform → expose format chuẩn
(vd `nginx-prometheus-exporter` đọc `/nginx_status`, expose `/metrics` cho Prometheus). **Ambassador** xử lý
request ĐI: app gọi `localhost:<port>` → proxy ra ngoài kèm auth/TLS (vd `kubectl proxy` forward tới API server
kèm SA token). Adapter = phiên dịch tài liệu; ambassador = phiên dịch hội thoại.
</details>

## AuthN / AuthZ / ServiceAccount

<details>
<summary>6. AuthN vs AuthZ — câu hỏi khác nhau? Error code?</summary>

**AuthN** = "mày là ai?" (xác minh cert/JWT) → fail = **401** Unauthorized (token hết hạn/sai). **AuthZ** = "mày
được làm gì?" (RBAC verb+resource+ns) → fail = **403** Forbidden (danh tính đúng nhưng thiếu RoleBinding). Mẹo:
401 = danh tính hỏng; 403 = thiếu quyền.
</details>

<details>
<summary>7. ServiceAccount có phải để cấp cho developer (con người) không?</summary>

**KHÔNG.** SA dành cho **máy/workload** (Pod, CI/CD, controller). Con người = **User**, xác thực qua cert/OIDC
(K8s không có bảng "users", không `kubectl create user`). Cấp quyền cho developer trên namespace → vẫn RBAC
Role/RoleBinding nhưng `subject.kind: User` (hoặc `Group`), không phải `ServiceAccount`. Nguyên tắc: **người →
User/OIDC; máy → ServiceAccount**; cả hai cùng bị RBAC gác, cùng dùng namespace để cô lập.
</details>

<details>
<summary>8. Pod không khai <code>serviceAccountName</code> nhận SA nào? Token mount ở đâu?</summary>

Nhận SA `default` của namespace — **admission controller tự gán**. Token mount tại
`/var/run/secrets/kubernetes.io/serviceaccount/` gồm 3 file: `token` (JWT), `ca.crt`, `namespace`.
</details>

<details>
<summary>9. K8s 1.24+ thay đổi gì về SA token?</summary>

Trước 1.24: token là **Secret** `kubernetes.io/service-account-token`, **không hết hạn** (lộ = sống mãi).
Từ 1.24: **projected volume** — JWT **ngắn hạn** (~1h), **tự rotate**, không tạo Secret riêng. Bảo mật hơn:
cửa sổ rủi ro hẹp nếu token lộ.
</details>

<details>
<summary>10. Role vs ClusterRole? Deny-by-default nghĩa là gì? (thực chạy)</summary>

`Role` scope **1 namespace**; `ClusterRole` scope **toàn cluster** (và resource không namespaced như `nodes`).
**Deny-by-default**: SA mới không có quyền gì cho tới khi RoleBinding tường minh. Lab thật: SA `service-reader`
có Role đọc `services` → `can-i list services` = **yes**, nhưng `list pods` = **no** (Role không nhắc `pods`);
curl qua ambassador khớp: services trả `ServiceList`, pods trả `403 Forbidden`.
</details>

## Bắc cầu sang production

<details>
<summary>11. kubeconfig là gì, cơ chế thế nào?</summary>

kubeconfig = "địa chỉ cụm + chứng minh thư của bạn". 3 khối: **clusters** (nối tới server nào + CA verify),
**users** (bạn là ai + cert/token — AuthN), **contexts** (ghép cluster+user+namespace). kubectl đọc file →
`current-context` → mở HTTPS tới API server (verify cert, đính credential) → API server chạy AuthN → AuthZ →
trả kết quả. kubectl là client "trần", mọi tri thức về cụm nằm trong kubeconfig; **file không chứa quyền** (RBAC
ở trong cụm). credential là bí mật → không commit, `chmod 600`, lộ thì rotate. Kiểm quyền: `kubectl auth whoami`
+ `kubectl auth can-i --list -n <ns>`.
</details>

<details>
<summary>12. Các pattern/khái niệm này dùng ở cụm thật thế nào?</summary>

- **Multi-container**: sidecar log-shipper (Fluent Bit), service-mesh proxy (Envoy/Istio), adapter metrics
 exporter, ambassador proxy tới DB/cache — đều dựa shared localhost + volume.
- **Init container**: chờ DB/migration, clone config, set quyền trước khi app chạy.
- **SA + RBAC least-privilege**: mỗi workload (CI runner, operator, monitoring agent) một SA quyền tối thiểu —
 compromise 1 workload không lan ra cả cluster.
- **User/OIDC + namespace**: cấp cho developer quyền `edit` trong namespace của team họ, cô lập khỏi team khác.

| Module này | Kubernetes production |
|---|---|
| shared localhost/volume | sidecar mesh, log shipper, adapter, ambassador |
| init container | chờ dependency, migration, chuẩn bị |
| SA + Role + RoleBinding | least-privilege cho mỗi workload |
| User/Group + RBAC + namespace | phân quyền developer theo team |
</details>
