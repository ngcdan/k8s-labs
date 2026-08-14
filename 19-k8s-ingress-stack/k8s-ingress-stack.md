# 19 · Ingress stack production — MetalLB + ingress-nginx + cert-manager

> **Chặng Platform · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Troubleshooting cụm · kế tiếp: Longhorn — block storage · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu tại sao bare-metal cluster không có LoadBalancer IP (Service treo `<pending>`); cài MetalLB để cấp IP từ pool local; cài ingress-nginx đứng trước nhiều Service; tự động hoá TLS với cert-manager; và ghép toàn bộ stack để expose 1 app HTTPS hoàn chỉnh qua `curl -k https://demo.local`.

**Nền:** đã hiểu Service các loại (lab 05), Ingress resource cơ bản (lab 12), và kind-lab 3-node đang chạy sẵn. Lab này xây tầng production trên nền đó — MetalLB giải quyết LB, ingress-nginx là controller, cert-manager lo TLS.

> ⚠ **Lưu ý:** chạy trên **kind-lab 3-node** (nhẹ, hợp Mac Mini M4 24 GB — không cần multipass như lab 15-18). **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi cài thật.**

---

## Tiền đề
```bash
kubectl config use-context kind-lab
kubectl get nodes
```

```text
NAME                 STATUS   ROLES           AGE   VERSION
kind-lab-control     Ready    control-plane   3d    v1.29.2
kind-lab-worker      Ready    <none>          3d    v1.29.2
kind-lab-worker2     Ready    <none>          3d    v1.29.2
```

→ **Verify:** 3 node STATUS=Ready trước khi bắt đầu. Cụm kind tạo bằng config 3-node (xem `cluster/kind-config.yml` trong repo).

---

## 1. MetalLB — LoadBalancer trên bare-metal

**Chốt:** cloud cluster (EKS, GKE) tự có cloud provider cấp IP công cộng khi tạo Service type LoadBalancer; bare-metal và kind không có — Service treo `EXTERNAL-IP=<pending>` mãi mãi. **MetalLB** là software LoadBalancer, cấp IP từ một pool IP bạn tự khai báo và dùng ARP/L2 để announce — nên máy trong cùng mạng LAN truy cập được.

- **Vấn đề gốc:** K8s không có built-in LB cho bare-metal; cloud LB integration là plugin riêng từng provider. Thiếu plugin → Service LB không có IP → Ingress Controller không có IP → không vào được từ ngoài.
- **MetalLB L2 mode:** controller giữ danh sách IP pool; khi Service LB được tạo, speaker Pod trên 1 node "nhận" IP đó và reply ARP — các máy trong LAN thấy IP → node đó, rồi node forward vào Service.
- **IPAddressPool:** khai báo dải IP MetalLB được dùng (phải thuộc dải LAN hay docker network, không overlap Pod/Service CIDR).
- **L2Advertisement:** bật chế độ ARP/NDP announcement cho pool vừa khai báo.
- **Giới hạn L2:** chỉ 1 node "giữ" IP tại 1 thời điểm (node đó là speaker); node đó chết → failover sang node khác mất vài giây. Không load-balance thật sự ở L2 — chỉ là failover. Để LB thật sự dùng mode BGP (cần router hỗ trợ).

**Vì sao:** không có MetalLB (hoặc tương đương như kube-vip), mọi Service type LoadBalancer trên bare-metal đều treo `<pending>` — ingress-nginx không có IP → không expose được. MetalLB là bước nền tảng bắt buộc cho stack này.

**Cơ chế:** MetalLB triển khai 2 thành phần: `controller` (Deployment, gán IP từ pool cho Service) và `speaker` (DaemonSet, chạy trên mọi node, làm ARP/BGP). Khi `kubectl apply` 1 Service type LoadBalancer, controller thấy không có IP → lấy 1 IP từ `IPAddressPool` → ghi vào `.status.loadBalancer.ingress[0].ip`. Speaker node được chọn bắt đầu reply ARP cho IP đó trong LAN.

> 💡 **Ẩn dụ:** cloud LB = bảo vệ tòa nhà do chủ tòa (cloud provider) thuê sẵn; MetalLB = bạn tự thuê bảo vệ và phân công họ giữ từng cửa. Bạn phải tự khai báo "tôi có bao nhiêu cửa" (IPAddressPool) rồi MetalLB lo phân công.

**So sánh options trên bare-metal:**

| Giải pháp | Mode | Ưu | Nhược |
|---|---|---|---|
| MetalLB L2 | ARP/NDP | Đơn giản, không cần router | 1 node làm speaker, failover chậm hơn |
| MetalLB BGP | BGP | LB thật, multi-path | Cần router hỗ trợ BGP |
| kube-vip | ARP/BGP | Nhẹ hơn, hay dùng cho control-plane VIP | Ít tính năng hơn MetalLB |
| NodePort | — | Không cần thêm gì | Port xấu, khó dùng tên miền |

**Dùng / KHÔNG:**
- MetalLB L2 phù hợp lab, homelab, cụm nhỏ bare-metal cùng subnet.
- **Phản đề:** MetalLB L2 không scale tốt cho traffic lớn (bottleneck tại 1 speaker node). Production quy mô lớn dùng BGP mode hoặc cloud LB. Dải IP trong `IPAddressPool` phải không overlap với DHCP pool — tránh conflict IP.

**Làm:**

```bash
# cài MetalLB qua manifest chính thức
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml

# đợi controller và speaker sẵn sàng
kubectl wait --namespace metallb-system \
  --for=condition=ready pod \
  --selector=app=metallb \
  --timeout=120s

# kiểm tra Pod MetalLB
kubectl get pods -n metallb-system
```

```text
$ kubectl get pods -n metallb-system
NAME                          READY   STATUS    RESTARTS   AGE
controller-68bf958bcb-x7k9p   1/1     Running   0          45s
speaker-4gdfp                 1/1     Running   0          45s
speaker-mnt7s                 1/1     Running   0          45s
speaker-v8r2k                 1/1     Running   0          45s
```

→ 1 controller + 3 speaker (1 mỗi node). Tiếp theo khai báo IP pool:

```bash
# xem docker network của kind để biết subnet
docker network inspect kind | grep Subnet
```

```text
"Subnet": "172.18.0.0/16"
```

```yaml
# /tmp/metallb-pool.yml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: kind-pool
  namespace: metallb-system
spec:
  addresses:
  - 172.18.0.200-172.18.0.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: kind-l2adv
  namespace: metallb-system
spec:
  ipAddressPools:
  - kind-pool
```

```bash
kubectl apply -f /tmp/metallb-pool.yml

# verify pool đã tạo
kubectl get ipaddresspool -n metallb-system
kubectl get l2advertisement -n metallb-system
```

```text
$ kubectl get ipaddresspool -n metallb-system
NAME        AUTO ASSIGN   AVOID BUGGY IPS   ADDRESSES
kind-pool   true          false             ["172.18.0.200-172.18.0.250"]

$ kubectl get l2advertisement -n metallb-system
NAME         IPADDRESSPOOLS   IPADDRESSPOOL SELECTORS   INTERFACES
kind-l2adv   ["kind-pool"]
```

```bash
# test nhanh: tạo Service LB thử
kubectl create deployment test-lb --image=nginx:alpine
kubectl expose deployment test-lb --type=LoadBalancer --port=80

# kiểm tra EXTERNAL-IP — phải có IP từ pool (không còn <pending>)
kubectl get svc test-lb
```

```text
$ kubectl get svc test-lb
NAME      TYPE           CLUSTER-IP      EXTERNAL-IP     PORT(S)        AGE
test-lb   LoadBalancer   10.96.215.44    172.18.0.200    80:31442/TCP   8s
```

→ **Verify:** `EXTERNAL-IP` hiện IP từ pool `172.18.0.200-172.18.0.250` (không còn `<pending>`). Dọn test: `kubectl delete deployment test-lb && kubectl delete svc test-lb`.

![[ingress-stack-flow.excalidraw]]

---

## 2. ingress-nginx — Ingress controller

**Chốt:** Ingress resource (YAML routing rules) chỉ là dữ liệu trong etcd — không có Ingress Controller thì không ai enforce. **ingress-nginx** là controller phổ biến nhất: chạy như một Deployment, watch API server, tự sinh nginx.conf theo mọi Ingress resource trong cluster, và nhận traffic qua 1 Service LoadBalancer duy nhất (IP từ MetalLB).

- **Ingress Controller = software proxy thật:** ingress-nginx chạy nginx bên trong, tự reload khi có Ingress resource mới/thay đổi — developer chỉ cần viết YAML Ingress, controller tự lo config nginx.
- **IngressClass:** tên class (`nginx`) gắn vào Ingress resource để chỉ định controller nào xử lý — hữu ích khi có nhiều controller trong 1 cụm (nginx + Traefik).
- **1 Service LoadBalancer cho toàn cluster:** ingress-nginx tạo 1 Service type LoadBalancer → nhận 1 IP từ MetalLB → tất cả domain/path routing qua IP đó; phía sau controller phân traffic đến đúng Service theo Ingress rules.
- **Host-based vs path-based:** `host: web.example.com` phân theo HTTP header `Host`; `path: /api` phân theo URL. Có thể kết hợp cả 2 trong cùng 1 rule.
- **annotation:** ingress-nginx dùng annotation để cấu hình nâng cao không có trong Ingress spec chuẩn (vd `nginx.ingress.kubernetes.io/rewrite-target`, rate limiting, CORS…).

**Vì sao:** mỗi Service public mà cài LoadBalancer riêng = tốn N IP (và N lần phí cloud). ingress-nginx gom tất cả qua 1 điểm, định tuyến L7 — vừa tiết kiệm IP, vừa dễ quản lý TLS tập trung.

**Cơ chế:** ingress-nginx-controller Pod watch K8s API cho resource loại `Ingress`. Mỗi khi có thay đổi, controller gọi template engine sinh `nginx.conf` mới, rồi `nginx -s reload` để áp dụng mà không drop connection (graceful reload). Traffic path thật: `client → MetalLB IP → ingress-nginx Pod → ClusterIP Service → Pod`.

> 💡 **Ẩn dụ:** ingress-nginx là tổng đài viên tại sảnh — khách gọi 1 số (IP MetalLB), tổng đài đọc họ muốn gặp ai (host/path), chuyển máy đến đúng phòng ban (Service). Mỗi Ingress resource = 1 trang danh bạ mới tổng đài được cập nhật tự động.

**So sánh Ingress Controller phổ biến:**

| Controller | Backend | Ưu điểm | Phù hợp |
|---|---|---|---|
| ingress-nginx | nginx | Phổ biến, nhiều tài liệu, annotation phong phú | Hầu hết use-case |
| Traefik | Traefik | Auto-discover, dashboard đẹp, tích hợp Let's Encrypt tốt | Homelab, ít config |
| HAProxy Ingress | HAProxy | Performance cao, advanced LB | High-traffic production |
| AWS ALB Controller | AWS ALB | Native AWS, không tốn resource trong cluster | EKS thuần |

**Dùng / KHÔNG:**
- ingress-nginx cho HTTP/HTTPS với host/path routing — đây là chuẩn production phổ biến nhất.
- **Phản đề:** ingress-nginx xử lý TCP/UDP thuần cần config thêm (`ConfigMap` `tcp-services`/`udp-services`) — không out-of-the-box như HTTP. Nếu app dùng gRPC hoặc WebSocket cần annotation riêng để tránh proxy buffer làm hỏng stream.

**Làm:**

```bash
# cài ingress-nginx qua manifest (version hỗ trợ kind)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml

# đợi controller ready
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s

# kiểm tra Pod và Service
kubectl get pods -n ingress-nginx
kubectl get svc -n ingress-nginx
```

```text
$ kubectl get pods -n ingress-nginx
NAME                                        READY   STATUS    RESTARTS   AGE
ingress-nginx-controller-7d9b8b8b6d-pq4zk   1/1     Running   0          55s

$ kubectl get svc -n ingress-nginx
NAME                                 TYPE           CLUSTER-IP      EXTERNAL-IP     PORT(S)                      AGE
ingress-nginx-controller             LoadBalancer   10.96.173.41    172.18.0.201    80:31080/TCP,443:31443/TCP   55s
ingress-nginx-controller-admission   ClusterIP      10.96.204.17    <none>          443/TCP                      55s
```

→ **Verify:** `ingress-nginx-controller` có `EXTERNAL-IP=172.18.0.201` (IP từ pool MetalLB, không phải `<pending>`). Đây là IP duy nhất nhận toàn bộ traffic vào cluster.

```bash
# xem IngressClass đã được đăng ký
kubectl get ingressclass
```

```text
$ kubectl get ingressclass
NAME    CONTROLLER             PARAMETERS   AGE
nginx   k8s.io/ingress-nginx   <none>       60s
```

→ **Verify:** IngressClass `nginx` đã có — Ingress resource dùng `ingressClassName: nginx` sẽ được controller này xử lý.

---

## 3. cert-manager — TLS tự động

**Chốt:** TLS thủ công = tạo key, tạo CSR, ký cert, copy Secret vào cluster, nhớ renew trước 90 ngày — lặp đi lặp lại và hay quên. **cert-manager** tự động toàn bộ vòng đời: watch Ingress/Certificate resource → liên hệ CA (self-signed, nội bộ, hoặc Let's Encrypt) → tạo Secret TLS → renew tự động trước khi hết hạn.

- **ClusterIssuer:** định nghĩa CA phát cert (tồn tại ở cấp cluster, không gắn namespace). Hai loại hay dùng:
  - `selfSigned` — cert tự ký (chỉ lab/dev, browser cảnh báo "not trusted").
  - `CA` — dùng 1 CA cert/key bạn tạo sẵn, ký cert cho domain nội bộ.
  - `acme` (Let's Encrypt) — production, cert public, browser tin ngay, miễn phí, cần domain thật + port 80/443 mở.
- **Certificate resource:** khai báo "tôi muốn cert cho domain X, do Issuer Y ký, lưu vào Secret Z". cert-manager fulfill request này tự động.
- **Annotation trên Ingress:** `cert-manager.io/cluster-issuer: <tên>` → cert-manager tự tạo Certificate resource từ Ingress TLS config — không cần viết Certificate riêng.
- **Secret TLS:** sau khi cert được cấp, cert-manager lưu vào Secret type `kubernetes.io/tls` gồm `tls.crt` và `tls.key`. Ingress Controller đọc Secret này để terminate HTTPS.
- **Auto-renew:** cert-manager renew cert khi còn ~30 ngày (mặc định) — không cần can thiệp thủ công.

**Vì sao:** không có cert-manager, khi Let's Encrypt cert 90 ngày hết hạn mà quên renew → toàn bộ HTTPS site bị lỗi. cert-manager biến việc này thành "set and forget". Trong môi trường nhiều service (10-50 Ingress), tự động hoá cert là bắt buộc.

**Cơ chế:** cert-manager gồm controller chính và webhook. Khi Ingress có annotation `cert-manager.io/cluster-issuer`, cert-manager tạo Certificate resource tương ứng. Controller xử lý Certificate: (1) tạo private key, (2) tạo CSR, (3) gửi challenge đến CA (với ACME: HTTP-01 hoặc DNS-01 challenge), (4) CA verify và trả cert, (5) cert-manager lưu `tls.crt` + `tls.key` vào Secret được chỉ định. Ingress Controller mount Secret này để terminate TLS.

> 💡 **Ẩn dụ:** cert-manager là thư ký tự động lo giấy phép hoạt động — bạn chỉ cần nói "tôi muốn mở chi nhánh ở địa chỉ này" (Certificate/Ingress), thư ký tự liên hệ cơ quan cấp phép (CA), nộp hồ sơ, và nhắc renew trước khi giấy hết hạn.

**Bảng so sánh Issuer type:**

| Issuer type | Cert trust | Dùng khi | Cần gì |
|---|---|---|---|
| `selfSigned` | Chỉ trust trong cụm | Lab nội bộ, test nhanh | Không cần gì thêm |
| `CA` | Trust trong LAN/corp nếu inject CA cert | Dev/staging nội bộ | CA cert + key sẵn |
| `acme` (Let's Encrypt) | Public trust (browser tin) | Production, domain thật | Domain + port 80/443 mở ra internet |

**Dùng / KHÔNG:**
- `selfSigned` hoặc `CA` cho lab/dev nội bộ, ACME cho production có domain thật.
- **Phản đề:** Let's Encrypt rate limit 50 cert/domain/tuần — đừng test bằng production ClusterIssuer; dùng Let's Encrypt **staging** server (`acme-staging-v02`) khi thử nghịch. cert ACME staging không được browser tin nhưng không bị rate-limit.

**Làm:**

```bash
# cài cert-manager qua manifest
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.1/cert-manager.yaml

# đợi tất cả Pod cert-manager ready
kubectl wait --namespace cert-manager \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/instance=cert-manager \
  --timeout=120s

kubectl get pods -n cert-manager
```

```text
$ kubectl get pods -n cert-manager
NAME                                      READY   STATUS    RESTARTS   AGE
cert-manager-6d8d6b5dbb-7kxpj             1/1     Running   0          70s
cert-manager-cainjector-74d4c9dc9-8fnvw   1/1     Running   0          70s
cert-manager-webhook-5c9d8cc5cb-rqlt2     1/1     Running   0          70s
```

Tạo ClusterIssuer self-signed (phù hợp lab kind):

```yaml
# /tmp/clusterissuer-selfsigned.yml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-issuer
spec:
  selfSigned: {}
```

```bash
kubectl apply -f /tmp/clusterissuer-selfsigned.yml

# verify ClusterIssuer sẵn sàng
kubectl get clusterissuer selfsigned-issuer
```

```text
$ kubectl get clusterissuer selfsigned-issuer
NAME                READY   AGE
selfsigned-issuer   True    8s
```

→ **Verify:** `READY=True` — cert-manager sẵn sàng cấp cert. Tiếp theo ghép end-to-end.

---

## 4. Ghép end-to-end — app HTTPS qua ingress

**Chốt:** deploy 1 app → Service ClusterIP → Ingress với TLS → cert-manager tự cấp cert → `curl -k https://demo.local` trả app qua HTTPS. Đây là chuỗi hoàn chỉnh: **internet → MetalLB IP → ingress-nginx → Service → Pod**, với TLS terminate tại ingress-nginx.

- **Luồng traffic chi tiết:**
  1. DNS resolve `demo.local` → IP MetalLB (`172.18.0.201`).
  2. Client TCP connect port 443 → MetalLB speaker forward vào node → Service `ingress-nginx-controller` port 443.
  3. ingress-nginx Pod nhận TLS handshake, lookup cert từ Secret (do cert-manager tạo), terminate TLS.
  4. ingress-nginx đọc HTTP header `Host: demo.local`, khớp Ingress rule → forward đến ClusterIP Service của app.
  5. Service forward đến Pod (qua kube-proxy/eBPF EndpointSlice).
- **`curl -k`:** flag `-k` (insecure) bỏ qua verify CA — cần thiết khi dùng self-signed cert vì browser/curl không có CA này trong trust store. Production với Let's Encrypt cert không cần `-k`.
- **`--resolve`:** trick inject DNS vào curl mà không cần sửa `/etc/hosts` — `curl --resolve demo.local:443:172.18.0.201 https://demo.local`.
- **Certificate READY:** sau khi apply Ingress với annotation cert-manager, chờ `kubectl get certificate -w` hiện `READY=True` trước khi test HTTPS.

**Vì sao:** đây là mẫu production chuẩn — mọi cluster Kubernetes expose HTTP/HTTPS đều dùng cấu trúc này (hoặc biến thể với Traefik thay nginx). Nắm flow này = nắm cách debug khi HTTPS không hoạt động (check từng tầng: MetalLB IP → controller Pod log → certificate status → Service endpoint).

**Cơ chế:** khi apply Ingress với `tls:` block và annotation `cert-manager.io/cluster-issuer`, cert-manager controller nhận event → tạo Certificate object → tạo private key → với `selfSigned` issuer: tự ký cert ngay (không cần outbound HTTP challenge) → lưu vào Secret `demo-local-tls`. ingress-nginx nhận event Secret mới → reload nginx với cert này → bắt đầu serve HTTPS.

> 💡 **Ẩn dụ:** chuỗi này như lắp đặt bảo mật tòa nhà: MetalLB = cổng chính (1 địa chỉ), ingress-nginx = bảo vệ (kiểm tra thẻ và chỉ đường), cert-manager = đơn vị cấp thẻ ra vào (TLS cert), Service + Pod = các phòng trong tòa.

**Dùng / KHÔNG:**
- Stack này cho toàn bộ workload web production — event-driven, tự renew, dễ audit qua GitOps.
- **Phản đề:** với cluster cực nhỏ (1 node, 1 service), stack này hơi nặng — có thể dùng `k3s` với Traefik và Let's Encrypt tích hợp sẵn, hoặc thậm chí Caddy đơn giản hơn. Stack MetalLB + ingress-nginx + cert-manager phát huy khi có nhiều service và nhiều domain.

**Làm:**

Bước 1 — deploy app demo và Service:

```bash
kubectl create deployment demo-app --image=hashicorp/http-echo:latest \
  -- /http-echo -text="Hello from ingress stack!"

kubectl expose deployment demo-app --port=5678 --name=demo-svc
```

```text
$ kubectl get svc demo-svc
NAME       TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
demo-svc   ClusterIP   10.96.88.122    <none>        5678/TCP   5s
```

Bước 2 — apply Ingress với TLS và annotation cert-manager:

```yaml
# /tmp/demo-ingress-tls.yml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: demo-ingress-tls
  annotations:
    cert-manager.io/cluster-issuer: selfsigned-issuer
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - demo.local
    secretName: demo-local-tls
  rules:
  - host: demo.local
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: demo-svc
            port:
              number: 5678
```

```bash
kubectl apply -f /tmp/demo-ingress-tls.yml

# đợi cert-manager cấp cert
kubectl get certificate -w
```

```text
$ kubectl get certificate -w
NAME            READY   SECRET          AGE
demo-local-tls  False   demo-local-tls  3s
demo-local-tls  True    demo-local-tls  8s
```

```bash
# xem chi tiết cert
kubectl describe certificate demo-local-tls
```

```text
$ kubectl describe certificate demo-local-tls
Name:         demo-local-tls
Namespace:    default
...
Status:
  Conditions:
    Message:               Certificate is up to date and has not expired
    Reason:                Ready
    Status:                True
    Type:                  Ready
  Not After:               2026-11-11T07:30:00Z
  Not Before:              2026-08-13T07:30:00Z
  Renewal Time:            2026-10-12T07:30:00Z
```

```bash
# xem Secret TLS được tạo
kubectl get secret demo-local-tls -o yaml | grep type
```

```text
type: kubernetes.io/tls
```

```bash
# kiểm tra Ingress có ADDRESS chưa
kubectl get ingress demo-ingress-tls
```

```text
$ kubectl get ingress demo-ingress-tls
NAME               CLASS   HOSTS        ADDRESS         PORTS     AGE
demo-ingress-tls   nginx   demo.local   172.18.0.201    80, 443   35s
```

```bash
# test HTTPS qua --resolve (không cần sửa /etc/hosts)
curl -k --resolve demo.local:443:172.18.0.201 https://demo.local
```

```text
$ curl -k --resolve demo.local:443:172.18.0.201 https://demo.local
Hello from ingress stack!
```

```bash
# xem cert được serve (kiểm tra subject)
curl -k --resolve demo.local:443:172.18.0.201 \
  -v https://demo.local 2>&1 | grep -E "subject:|issuer:|expire"
```

```text
* subject: CN=demo.local
* start date: Aug 13 07:30:00 2026 GMT
* expire date: Nov 11 07:30:00 2026 GMT
* issuer: CN=demo.local
```

→ **Verify:** `curl -k` trả `Hello from ingress stack!`; cert subject `CN=demo.local`; `kubectl get certificate` READY=True; Ingress ADDRESS=`172.18.0.201`. Luồng đầy đủ: curl → MetalLB `172.18.0.201:443` → ingress-nginx (terminate TLS với cert từ Secret `demo-local-tls`) → `demo-svc:5678` → Pod.

---

## 🧹 Dọn dẹp

```bash
kubectl delete ingress demo-ingress-tls
kubectl delete service demo-svc
kubectl delete deployment demo-app
kubectl delete certificate demo-local-tls --ignore-not-found
kubectl delete secret demo-local-tls --ignore-not-found

# giữ lại MetalLB, ingress-nginx, cert-manager cho lab sau nếu muốn
# hoặc dọn toàn bộ:
kubectl delete -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.1/cert-manager.yaml
kubectl delete -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml
kubectl delete -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml
```

---

## Đủ khi
① Vì sao Service LoadBalancer treo `<pending>` trên bare-metal và MetalLB giải quyết thế nào · ② MetalLB L2 mode hoạt động thế nào, giới hạn so với BGP · ③ ingress-nginx là gì, khác Ingress resource thế nào, và tại sao chỉ cần 1 IP cho nhiều service · ④ cert-manager tự động hoá TLS ra sao, ClusterIssuer `selfSigned` vs ACME khác gì · ⑤ debug HTTPS không lên: check theo thứ tự MetalLB IP → controller log → certificate status → Service endpoint.

---

## Recall
1. Tại sao `kubectl get svc` hiện `EXTERNAL-IP=<pending>` trên kind/bare-metal? MetalLB giải quyết vấn đề này thế nào?
2. MetalLB L2 mode dùng giao thức gì để announce IP? Giới hạn chính của L2 mode so với BGP là gì?
3. `IPAddressPool` và `L2Advertisement` trong MetalLB có vai trò gì? Thiếu `L2Advertisement` thì sao?
4. Ingress resource và Ingress Controller khác nhau thế nào? ingress-nginx làm gì khi có Ingress mới được apply?
5. `IngressClass` dùng để làm gì? Khi nào cần có nhiều IngressClass trong cùng 1 cụm?
6. cert-manager dùng `ClusterIssuer` type `selfSigned` vs `acme` — cert nào được browser tin ngoài internet?
7. Khi Ingress có annotation `cert-manager.io/cluster-issuer`, cert-manager làm gì? Cert được lưu ở đâu?
8. Sau khi `kubectl apply` Ingress TLS, làm sao biết cert đã được cấp thành công?
9. `curl -k` vs `curl` thường — `-k` nghĩa là gì, khi nào cần?
10. Nếu `curl -k https://demo.local` trả `Connection refused`, bạn debug theo thứ tự nào?

### Đáp án

1. K8s không có built-in LB cho bare-metal; cloud LB integration là plugin riêng từng provider — thiếu plugin → không có gì cấp IP → `<pending>`. MetalLB triển khai `controller` (gán IP từ pool) và `speaker` DaemonSet (announce qua ARP/BGP) → Service nhận IP từ pool tự khai báo.
2. L2 mode dùng **ARP** (IPv4) hoặc **NDP** (IPv6) để announce IP. Giới hạn: chỉ **1 node làm speaker** tại 1 thời điểm — bottleneck và failover chậm hơn BGP (BGP cho phép multi-path, cân bằng thật sự ở router).
3. `IPAddressPool` khai báo dải IP MetalLB được dùng; `L2Advertisement` bật ARP announcement cho pool đó. Thiếu `L2Advertisement` → pool có IP nhưng không announce → client trong LAN không biết IP đó ở đâu → không reach được.
4. Ingress resource = YAML khai báo routing rules, lưu etcd, không tự enforce. Ingress Controller = Pod thật (ingress-nginx) watch API, sinh nginx.conf, reload khi có Ingress mới — mới là thứ thực sự xử lý traffic.
5. `IngressClass` gắn tên class (vd `nginx`) cho controller, Ingress resource dùng `ingressClassName` để chỉ định controller xử lý. Cần nhiều IngressClass khi có nhiều controller (nginx cho web public, Traefik cho internal, hoặc 2 nginx instance khác nhau cho prod vs staging).
6. `selfSigned` — cert tự ký, không có CA trong public trust store → browser cảnh báo "not trusted". `acme` với Let's Encrypt — CA được browser tin sẵn → không cảnh báo. Chỉ cert ACME production mới được browser tin ngoài internet.
7. cert-manager tạo `Certificate` resource → tạo private key + CSR → liên hệ Issuer để ký → lưu `tls.crt` + `tls.key` vào **Secret** `kubernetes.io/tls` có tên do `tls.secretName` trong Ingress chỉ định. ingress-nginx đọc Secret này để terminate HTTPS.
8. `kubectl get certificate -w` đợi cột `READY=True`; hoặc `kubectl describe certificate <name>` xem `Status.Conditions` — `Type: Ready, Status: True, Message: Certificate is up to date and has not expired`.
9. `-k` = `--insecure` — curl bỏ qua verify CA của cert server. Cần khi dùng self-signed cert vì curl không có CA đó trong trust store. Production Let's Encrypt không cần `-k`.
10. Thứ tự debug: (1) `kubectl get svc -n ingress-nginx` — EXTERNAL-IP có IP chưa? (MetalLB); (2) `kubectl get pods -n ingress-nginx` — controller Running? (3) `kubectl logs -n ingress-nginx <controller-pod>` — có lỗi routing? (4) `kubectl get certificate` — READY=True? (5) `kubectl get endpoints demo-svc` — có Pod endpoints? (6) `kubectl get pod` — Pod Running?

---

## Bắc cầu sang production

Trên cụm production bare-metal, thay `selfSigned` bằng ClusterIssuer ACME Let's Encrypt:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
    - http01:
        ingress:
          class: nginx
```

Với ACME HTTP-01: cert-manager tự tạo Pod tạm để Let's Encrypt verify quyền sở hữu domain qua `http://<domain>/.well-known/acme-challenge/<token>` — cần domain trỏ về IP công cộng của ingress-nginx và port 80 mở. Sau verify, cert được cấp tự động, renew 30 ngày trước hạn.

Dải IP trong `IPAddressPool` trên bare-metal thường là IP LAN tĩnh đã được reserve (không để DHCP cấp) — ví dụ `192.168.1.200-192.168.1.210`. Với cloud VM không có L2 chung, dùng MetalLB BGP hoặc cloud LB thật thay L2 mode.

---

## 📎 Nguồn

- [course-catalog](../../wiki/notebook/k8s/course-catalog.md) — toàn bộ roadmap và link khóa học
- [kubernetes.io/docs/concepts/services-networking/ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/) — Ingress spec
- [kubernetes.io/docs/concepts/services-networking/ingress-controllers](https://kubernetes.io/docs/concepts/services-networking/ingress-controllers/) — danh sách Ingress Controller
- [kubernetes.io/docs/tasks/administer-cluster/network-policy-provider](https://kubernetes.io/docs/tasks/administer-cluster/network-policy-provider/) — CNI và NetworkPolicy
