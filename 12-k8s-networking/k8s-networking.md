# 12 · Networking nâng cao — Ingress, NetworkPolicy, CNI & DNS

> **Chặng 6 · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Compose → Kubernetes · kế tiếp: Scheduling · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu tại sao cần Ingress thay vì chỉ dùng Service; cấu hình Ingress định tuyến theo host và path; viết NetworkPolicy kiểm soát traffic giữa namespace; nắm mô hình IP phẳng của CNI; tra cứu Service qua CoreDNS và kiểm chứng bằng Pod tạm.

**Nền:** đã làm quen Service (lab 05), biết Pod có IP riêng, deployment có nhiều replica. Lab này mở rộng sang lớp L7 (Ingress), lớp bảo mật mạng (NetworkPolicy), và lớp hạ tầng IP (CNI + DNS).

---

## Tiền đề
```bash
kubectl config use-context kind-lab
kubectl get nodes
```

```text
NAME                 STATUS   ROLES           AGE   VERSION
kind-lab-control     Ready    control-plane   2d    v1.29.2
kind-lab-worker      Ready    <none>          2d    v1.29.2
kind-lab-worker2     Ready    <none>          2d    v1.29.2
```

→ **Verify:** 3 node STATUS=Ready trước khi bắt đầu.

---

## 1. Service ôn nhanh → vì sao cần Ingress

**Chốt:** ClusterIP chỉ dùng nội bộ, NodePort xấu và tốn port, LoadBalancer tốn 1 IP công cộng mỗi Service — Ingress gom nhiều Service sau 1 IP duy nhất nhờ định tuyến L7.

- **ClusterIP:** IP nội bộ cluster, không ra ngoài internet — chỉ Pod/Service khác trong cluster truy cập được.
- **NodePort:** mở 1 port (30000–32767) trên mỗi node, truy cập từ ngoài qua `<nodeIP>:<nodePort>` — xấu, không dùng được tên miền, phải nhớ port.
- **LoadBalancer:** yêu cầu cloud provider cấp IP công cộng — mỗi Service tốn 1 IP, 10 Service = 10 IP và 10 lần chi phí.
- **Ingress:** 1 LoadBalancer IP duy nhất đứng trước, phân phối đến nhiều Service theo `host` hoặc `path` — chuẩn của production.

**Vì sao:** khi ứng dụng có nhiều service (`web`, `api`, `admin`), dùng LoadBalancer riêng cho từng cái vừa tốn tiền vừa khó quản lý tên miền. Ingress giải quyết bằng 1 điểm vào, nhiều backend.

**Cơ chế:** Ingress là tầng L7 (HTTP/HTTPS) — nó đọc header `Host` và URL path để quyết định forward đến Service nào. Ngược lại, Service thông thường hoạt động ở L4 (TCP/UDP), không biết gì về HTTP header.

> 💡 **Ẩn dụ:** Service = một quầy lễ tân riêng biệt; Ingress = bảng điều hướng trung tâm tại sảnh — khách đến 1 cửa, được chỉ đến đúng quầy theo tên hoặc mục đích.

**So sánh các loại Service:**

| Loại | Truy cập từ ngoài? | IP dùng | Phù hợp |
|---|---|---|---|
| ClusterIP | Không | Cluster nội bộ | Service nội bộ giữa các Pod |
| NodePort | Được | Node IP + port | Dev/testing tạm |
| LoadBalancer | Được | 1 IP cloud mỗi svc | Khi cần 1 service public |
| **Ingress** | **Được** | **1 IP cho nhiều svc** | **Production — chuẩn** |

**Dùng / KHÔNG:**
- Ingress cho toàn bộ traffic HTTP/HTTPS từ internet vào cluster.
- **Phản đề:** Ingress không phù hợp cho protocol không phải HTTP (gRPC qua HTTP/2 cần cấu hình thêm, TCP thuần cần TCP proxy riêng). Ingress cũng cần Ingress Controller được cài trước mới hoạt động.

**Làm:**
```bash
# xem các Service hiện tại trong default namespace
kubectl get services

# tạo 2 Deployment + ClusterIP Service để demo Ingress
kubectl create deployment web --image=nginx:alpine --replicas=2
kubectl expose deployment web --port=80 --name=web-svc

kubectl create deployment api --image=hashicorp/http-echo:latest --replicas=1
kubectl expose deployment api --port=5678 --name=api-svc
```

**Kết quả:**
```text
$ kubectl get services
NAME         TYPE        CLUSTER-IP       EXTERNAL-IP   PORT(S)   AGE
api-svc      ClusterIP   10.96.141.32     <none>        5678/TCP  5s
kubernetes   ClusterIP   10.96.0.1        <none>        443/TCP   2d
web-svc      ClusterIP   10.96.87.204     <none>        80/TCP    8s
```

→ **Verify:** cả 2 service type=ClusterIP, EXTERNAL-IP=`<none>` — chưa thể truy cập từ ngoài.

---

## 2. Ingress — định tuyến L7 (host/path)

**Chốt:** Ingress resource chỉ là manifest khai báo routing rules — cần cài thêm **Ingress Controller** (nginx, Traefik…) mới thực sự xử lý traffic; thiếu controller thì Ingress không làm gì.

- **Ingress resource:** YAML khai báo rule (host X → service A, path /api → service B). K8s lưu vào etcd nhưng không tự xử lý.
- **Ingress Controller:** Pod chạy thật (thường nginx hoặc Traefik), watch Ingress resource, tự cập nhật config của mình theo rule mới.
- **pathType Prefix:** `/api` khớp `/api`, `/api/users`, `/api/v2/...` — khớp tất cả có prefix đó.
- **pathType Exact:** `/health` chỉ khớp đúng `/health`, không khớp `/health/live`.
- **Host-based routing:** rule `host: web.example.com` — dựa vào HTTP header `Host`.
- **TLS:** khai báo `tls:` + Secret chứa cert/key → Ingress Controller terminate HTTPS.

![[ingress-netpol.excalidraw]]

**Vì sao:** tách biệt "khai báo routing" (developer làm) và "thực thi routing" (ops cài controller) — developer không cần biết nginx config, chỉ cần viết YAML Ingress chuẩn.

**Cơ chế:** Ingress Controller chạy như một Deployment thường trong cluster (thường namespace `ingress-nginx`). Nó watch API server, mỗi khi có Ingress resource mới/thay đổi, controller tự sinh nginx.conf tương ứng và reload. Traffic thật: client → LoadBalancer IP → Controller Pod → Service → Pod.

> 💡 **Ẩn dụ:** Ingress resource = bản đồ chỉ đường viết sẵn; Ingress Controller = tài xế đọc bản đồ và thực sự lái xe. Không có tài xế thì bản đồ chỉ là tờ giấy.

**Dùng / KHÔNG:**
- Ingress cho HTTP/HTTPS, nhiều service, path/host-based routing.
- **Phản đề:** đừng nhét mọi config vào 1 Ingress object — với nhiều team, nên tách thành nhiều Ingress resource (mỗi team quản resource của mình). Ingress Controller thuần không xử lý được TCP/UDP (cần IngressClass khác hoặc dùng annotation nginx thêm).

**Làm:**

```bash
# kiểm tra Ingress Controller đã cài chưa (kind-lab có nginx ingress)
kubectl get pods -n ingress-nginx
```

```yaml
# /tmp/web-ingress.yml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: demo-ingress
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  rules:
  - host: demo.local
    http:
      paths:
      - path: /web
        pathType: Prefix
        backend:
          service:
            name: web-svc
            port:
              number: 80
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: api-svc
            port:
              number: 5678
```

```bash
kubectl apply -f /tmp/web-ingress.yml

# xem ADDRESS (IP của Ingress Controller, do MetalLB cấp)
kubectl get ingress demo-ingress
```

**Kết quả:**
```text
$ kubectl get pods -n ingress-nginx
NAME                                        READY   STATUS    RESTARTS   AGE
ingress-nginx-controller-5f64d6b7b9-k9txz   1/1     Running   0          2d

$ kubectl get ingress demo-ingress
NAME           CLASS   HOSTS        ADDRESS        PORTS   AGE
demo-ingress   nginx   demo.local   172.18.0.240   80      12s
```

```bash
# test routing: thêm host vào /etc/hosts tạm
echo "172.18.0.240 demo.local" | sudo tee -a /etc/hosts

curl http://demo.local/web
curl http://demo.local/api
```

```text
$ curl http://demo.local/web
<!DOCTYPE html>
<html>
<head>
<title>Welcome to nginx!</title>

$ curl http://demo.local/api
hello-world
```

→ **Verify:** `kubectl get ingress` thấy cột ADDRESS có IP (không phải `<none>`); 2 path route đến 2 service khác nhau.

---

## 3. NetworkPolicy — tường lửa Pod

**Chốt:** mặc định K8s **all-allow** — mọi Pod nói chuyện được với mọi Pod; NetworkPolicy tạo "tường lửa" tại Pod level, nhưng chỉ có tác dụng khi CNI hỗ trợ (Cilium, Calico có — kube-proxy thuần không).

- **Mặc định all-allow:** không có NetworkPolicy nào → mọi Pod trong cluster đều kết nối được với nhau, kể cả cross-namespace.
- **Default-deny:** tạo NetworkPolicy `podSelector: {}` không có rule ingress/egress → chặn toàn bộ traffic vào/ra namespace đó.
- **podSelector:** chọn Pod target bằng label (`app: web`) — policy chỉ áp lên Pod khớp.
- **namespaceSelector:** cho phép traffic từ namespace cụ thể (chọn bằng label namespace).
- **ipBlock:** allow/deny theo CIDR — dùng khi cần kiểm soát traffic tới IP ngoài cluster.
- **ingress vs egress:** `ingress` = traffic vào Pod; `egress` = traffic ra khỏi Pod. Có thể định nghĩa cả 2.
- **CNI requirement:** NetworkPolicy resource chỉ là manifest khai báo — CNI plugin mới là thứ thực thi. Cụm chỉ có kube-proxy không enforce NetworkPolicy.

**Vì sao:** zero-trust networking — theo mô hình này, mọi thứ bị deny mặc định, chỉ cho phép traffic thật sự cần. Khi `api` namespace bị compromise, NetworkPolicy ngăn attacker lateral-move sang `database` namespace.

**Cơ chế:** Cilium (CNI của lab này) dùng eBPF trên kernel để enforce policy — mỗi khi có NetworkPolicy mới, Cilium agent trên node cập nhật eBPF program, lọc packet trước khi nó đến Pod. Không cần iptables rule phức tạp.

> 💡 **Ẩn dụ:** NetworkPolicy = hệ thống badge reader trong văn phòng — mặc định mọi người vào được mọi phòng; khi bật, chỉ ai có badge đúng mới qua cửa. Badge reader = Cilium/Calico; không có reader = chìa khóa YAML không làm được gì.

**Dùng / KHÔNG:**
- Default-deny cho mọi namespace production, rồi mở dần từng rule cần thiết.
- **Phản đề:** đừng áp NetworkPolicy mà không test trước — một policy sai có thể chặn health probe → Pod không healthy → rolling update thất bại. Luôn test với Pod debug trước khi apply trên production.

**Làm:**

```bash
# tạo namespace frontend và backend
kubectl create namespace frontend
kubectl create namespace backend

# label namespace để namespaceSelector hoạt động
kubectl label namespace frontend env=frontend
kubectl label namespace backend env=backend

# tạo Pod trong backend namespace
kubectl run db-pod --image=nginx:alpine -n backend --labels=app=db
```

```yaml
# /tmp/netpol-default-deny.yml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: backend
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
```

```yaml
# /tmp/netpol-allow-frontend.yml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-frontend
  namespace: backend
spec:
  podSelector:
    matchLabels:
      app: db
  policyTypes:
  - Ingress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          env: frontend
    ports:
    - protocol: TCP
      port: 80
```

```bash
kubectl apply -f /tmp/netpol-default-deny.yml
kubectl apply -f /tmp/netpol-allow-frontend.yml

kubectl get networkpolicy -n backend
```

**Kết quả:**
```text
$ kubectl get networkpolicy -n backend
NAME                POD-SELECTOR   AGE
allow-from-frontend app=db         5s
default-deny-all    <none>         8s
```

```bash
# test: Pod trong frontend namespace → được phép
kubectl run test-frontend --image=busybox:1.36 -n frontend --rm -it \
  --restart=Never -- wget -qO- http://db-pod.backend.svc.cluster.local --timeout=3

# test: Pod trong default namespace → bị chặn (treo 3s rồi timeout)
kubectl run test-default --image=busybox:1.36 -n default --rm -it \
  --restart=Never -- wget -qO- http://db-pod.backend.svc.cluster.local --timeout=3
```

```text
# từ frontend → thành công
Welcome to nginx!

# từ default → bị chặn
wget: download timed out
```

→ **Verify:** wget từ `frontend` namespace trả HTML nginx; từ `default` namespace timeout — NetworkPolicy đang enforce.

---

## 4. CNI & Pod network model

**Chốt:** mỗi Pod nhận 1 IP duy nhất, phẳng, routable trong cluster — không cần NAT; CNI plugin là thứ cấp IP đó và cài route, không phải K8s core.

- **Pod network model:** mỗi Pod có 1 IP riêng (không share IP với Pod khác); Pod trên node A và Pod trên node B gọi nhau trực tiếp bằng IP — không qua NAT.
- **CNI (Container Network Interface):** spec interface giữa K8s và network plugin; khi Pod được tạo, kubelet gọi CNI plugin để: cấp IP, gắn vào network interface, cài route.
- **Cilium + eBPF:** thay vì dùng iptables, Cilium dùng eBPF program inject vào kernel — nhanh hơn, ít overhead hơn, hỗ trợ NetworkPolicy natively.
- **Pod CIDR:** dải IP cấp cho Pod, khác với Service CIDR (ClusterIP). Thường `10.244.x.x` (flannel), `10.42.x.x` (k3s/Cilium default).
- **EndpointSlice:** thay cho Endpoint cũ; K8s lưu danh sách IP:port của Pod backing mỗi Service ở đây — kube-proxy/Cilium đọc EndpointSlice để biết forward đến đâu.

**Vì sao:** mô hình IP phẳng đơn giản hoá networking — developer viết code gọi IP/hostname trực tiếp, không cần biết Pod đang chạy trên node nào. Đây là khác biệt lớn so với Docker Swarm overlay.

**Cơ chế:** khi scheduler đặt Pod lên node, kubelet gọi CNI binary (ví dụ `/opt/cni/bin/cilium-cni`). CNI plugin: (1) cấp IP từ pool CIDR của node, (2) tạo virtual ethernet pair (`veth`), (3) đặt 1 đầu vào network namespace của Pod, (4) cài route trên host để traffic tới IP đó đi đúng vào Pod namespace. Các node biết route tới Pod CIDR của nhau qua BGP hoặc VXLAN tunnel (tùy CNI).

> 💡 **Ẩn dụ:** Pod CIDR như dải số phòng trong khách sạn — mỗi tầng (node) có dải riêng, lễ tân (CNI) cấp số phòng và chỉ đường. Khách (traffic) đến số phòng nào đều được dẫn đúng, không cần biết ở tầng mấy.

**Dùng / KHÔNG:**
- Xem Pod CIDR để debug connectivity: `kubectl get pods -o wide` cho thấy IP thực.
- **Phản đề:** đừng hardcode Pod IP trong config — Pod IP thay đổi mỗi lần Pod được tạo lại. Luôn dùng Service name + CoreDNS.

**Làm:**

```bash
# xem IP của từng Pod và node chúng đang chạy
kubectl get pods -o wide -A | grep -v kube-system | head -10

# xem Pod CIDR của từng node
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'

# xem EndpointSlice của web-svc
kubectl get endpointslice -l kubernetes.io/service-name=web-svc
```

**Kết quả:**
```text
$ kubectl get pods -o wide -A | grep -v kube-system | head -10
NAMESPACE   NAME              READY   STATUS    IP           NODE
default     web-6d4cf9-bxk2p  1/1     Running   10.244.1.5   kind-lab-worker
default     web-6d4cf9-mnt7s  1/1     Running   10.244.2.3   kind-lab-worker2
backend     db-pod            1/1     Running   10.244.1.8   kind-lab-worker

$ kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'
kind-lab-control   10.244.0.0/24
kind-lab-worker    10.244.1.0/24
kind-lab-worker2   10.244.2.0/24

$ kubectl get endpointslice -l kubernetes.io/service-name=web-svc
NAME            ADDRESSTYPE   PORTS   ENDPOINTS              AGE
web-svc-7xk2p   IPv4          80      10.244.1.5,10.244.2.3  2m
```

→ **Verify:** Pod trên 2 node khác nhau có IP thuộc 2 subnet khác nhau; EndpointSlice liệt kê đúng IP của các Pod backing `web-svc`.

---

## 5. CoreDNS — DNS nội bộ cluster

**Chốt:** CoreDNS là DNS server nội bộ cluster, cho phép Pod gọi nhau bằng tên `<service>.<namespace>.svc.cluster.local` thay vì phải nhớ ClusterIP.

- **FQDN format:** `<svc-name>.<namespace>.svc.cluster.local` — ví dụ `web-svc.default.svc.cluster.local` resolve ra ClusterIP của `web-svc`.
- **Short form:** trong cùng namespace, chỉ cần `web-svc`; cross-namespace cần `web-svc.frontend`.
- **Pod DNS:** Pod cũng có DNS record dạng `<pod-ip-dashes>.<namespace>.pod.cluster.local` nhưng ít dùng — dùng Service thay.
- **/etc/resolv.conf:** mỗi Pod được inject `nameserver 10.96.0.10` (ClusterIP của CoreDNS) và `search default.svc.cluster.local svc.cluster.local cluster.local` — lý do short name `web-svc` hoạt động.
- **CoreDNS Pod:** chạy trong namespace `kube-system`, thường 2 replica để HA.

**Vì sao:** Pod IP thay đổi khi Pod được tạo lại, nhưng Service name không đổi. DNS cho phép dùng tên ổn định, tách biệt code khỏi IP cụ thể — đây là "service discovery" trong K8s.

**Cơ chế:** khi Pod gọi `web-svc`, hệ điều hành trong container gửi DNS query đến `10.96.0.10` (CoreDNS). CoreDNS tìm trong bảng internal (sync từ K8s API) và trả về ClusterIP. Sau đó kube-proxy/Cilium forward traffic từ ClusterIP tới Pod thật qua EndpointSlice.

> 💡 **Ẩn dụ:** CoreDNS như danh bạ điện thoại nội bộ công ty — gõ tên phòng ban ("kế toán") ra số máy lẻ, không cần nhớ số thật. Số máy lẻ (ClusterIP) không đổi dù nhân viên (Pod) thay.

**Dùng / KHÔNG:**
- Luôn dùng `<svc>.<ns>` hoặc FQDN để gọi cross-namespace.
- **Phản đề:** đừng dùng Pod IP trực tiếp trong config file — Pod chết là IP mất, service discovery sẽ không hoạt động.

**Làm:**

```bash
# kiểm tra CoreDNS đang chạy
kubectl get pods -n kube-system -l k8s-app=kube-dns

# xem /etc/resolv.conf của Pod
kubectl exec web-$(kubectl get pod -l app=web -o name | head -1 | cut -d/ -f2) -- cat /etc/resolv.conf

# tạo Pod tạm để test DNS lookup
kubectl run dns-test --image=busybox:1.36 --rm -it --restart=Never -- \
  sh -c 'nslookup web-svc && nslookup web-svc.default.svc.cluster.local'
```

**Kết quả:**
```text
$ kubectl get pods -n kube-system -l k8s-app=kube-dns
NAME                       READY   STATUS    RESTARTS   AGE
coredns-5d78c9869d-4n8xr   1/1     Running   0          2d
coredns-5d78c9869d-vt6zw   1/1     Running   0          2d

$ kubectl exec web-6d4cf9-bxk2p -- cat /etc/resolv.conf
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5

$ kubectl run dns-test --image=busybox:1.36 --rm -it --restart=Never -- \
  sh -c 'nslookup web-svc && nslookup web-svc.default.svc.cluster.local'
Server:    10.96.0.10
Address 1: 10.96.0.10 kube-dns.kube-system.svc.cluster.local

Name:      web-svc
Address 1: 10.96.87.204 web-svc.default.svc.cluster.local

Server:    10.96.0.10
Address 1: 10.96.0.10 kube-dns.kube-system.svc.cluster.local

Name:      web-svc.default.svc.cluster.local
Address 1: 10.96.87.204 web-svc.default.svc.cluster.local

pod "dns-test" deleted
```

→ **Verify:** `nslookup web-svc` trả IP `10.96.x.x` (ClusterIP của `web-svc`); nameserver là `10.96.0.10` (CoreDNS); Pod tự xoá sau khi chạy xong (`--rm`).

---

## 🧹 Dọn dẹp

```bash
kubectl delete ingress demo-ingress
kubectl delete service web-svc api-svc
kubectl delete deployment web api

kubectl delete networkpolicy default-deny-all allow-from-frontend -n backend
kubectl delete pod db-pod -n backend --ignore-not-found

kubectl delete namespace frontend backend

# xoá entry /etc/hosts tạm (chỉnh thủ công hoặc:)
sudo sed -i '' '/172.18.0.240 demo.local/d' /etc/hosts
```

---

## Đủ khi
① Service ClusterIP/NodePort/LoadBalancer khác Ingress thế nào, và vì sao production dùng Ingress · ② Ingress resource vs Ingress Controller — thiếu Controller thì Ingress không làm gì · ③ NetworkPolicy hoạt động thế nào, cần CNI gì, default-deny setup ra sao · ④ Pod network model: mỗi Pod 1 IP phẳng, CNI cấp IP và route — vì sao không dùng Pod IP trực tiếp · ⑤ CoreDNS resolve `<svc>.<ns>.svc.cluster.local`, và vì sao short name `web-svc` hoạt động trong cùng namespace.

---

## Recall
1. ClusterIP, NodePort, LoadBalancer, Ingress — loại nào phù hợp để expose nhiều microservice ra internet với 1 IP công cộng?
2. Ingress resource và Ingress Controller khác nhau thế nào? Nếu chỉ apply Ingress YAML mà không cài Controller thì điều gì xảy ra?
3. `pathType: Prefix` vs `pathType: Exact` — `/api` với `Prefix` khớp `/api/users` không?
4. K8s mặc định có cho phép mọi Pod nói chuyện với nhau không? Cần làm gì để default-deny?
5. NetworkPolicy có tác dụng trên cụm chỉ dùng kube-proxy (không có CNI như Cilium/Calico) không?
6. `podSelector: {}` trong NetworkPolicy nghĩa là gì?
7. Mỗi Pod nhận bao nhiêu IP? IP đó có bền vững không? Nên dùng gì thay thế?
8. CNI làm gì khi Pod được tạo? Kể 3 việc cụ thể.
9. FQDN đầy đủ để gọi `api-svc` trong namespace `backend` từ namespace khác là gì?
10. `nslookup web-svc` trong Pod trả về địa chỉ loại gì — Pod IP hay ClusterIP?

### Đáp án

1. **Ingress** — 1 IP công cộng, định tuyến theo host/path đến nhiều Service.
2. Ingress resource = YAML khai báo routing rules, lưu vào etcd. Ingress Controller = Pod thực sự xử lý traffic (nginx, Traefik...). Thiếu Controller thì Ingress resource tồn tại trong etcd nhưng không có gì enforce → traffic không đến đúng Service.
3. Có — `Prefix` khớp mọi path bắt đầu bằng `/api`, bao gồm `/api/users`, `/api/v2/...`
4. Mặc định **all-allow** — mọi Pod nói chuyện được với nhau. Để default-deny: tạo NetworkPolicy với `podSelector: {}` (chọn tất cả Pod trong namespace) và `policyTypes: [Ingress, Egress]` nhưng không có rule nào → chặn toàn bộ.
5. **Không** — NetworkPolicy chỉ là manifest khai báo; CNI plugin (Cilium, Calico) mới là thứ enforce. kube-proxy thuần không đọc NetworkPolicy.
6. `podSelector: {}` = chọn **tất cả Pod** trong namespace — policy áp dụng cho toàn bộ Pod, không lọc theo label.
7. Mỗi Pod nhận **1 IP** từ CNI. IP **không bền vững** — Pod chết và tạo lại thì IP mới. Nên dùng **Service name + DNS** thay vì Pod IP.
8. CNI khi Pod được tạo: (1) cấp IP từ dải CIDR của node, (2) tạo virtual ethernet pair (`veth`) nối host và Pod network namespace, (3) cài route trên host để traffic đến IP đó đi đúng vào Pod.
9. `api-svc.backend.svc.cluster.local`
10. **ClusterIP** của Service — DNS resolve `web-svc` ra ClusterIP (10.96.x.x), không phải Pod IP trực tiếp.

---

## Bắc cầu sang production

Trên cụm production, traffic chạy đúng luồng này: **internet → LoadBalancer IP → Ingress Controller → Service → Pod**. Ingress Controller terminate TLS (Let's Encrypt qua cert-manager), định tuyến theo subdomain hoặc path, và log access như nginx thông thường.

NetworkPolicy **default-deny** là chuẩn zero-trust — mọi namespace production đều có policy chặn toàn bộ ingress/egress, rồi mở từng rule cụ thể (frontend → backend port 8080, backend → database port 5432). Khi một Pod bị compromise, policy ngăn attacker di chuyển ngang sang namespace khác.

CoreDNS là thứ làm cho service discovery hoạt động trong code: thay vì hardcode IP, ứng dụng gọi `http://api-svc.backend:8080` — DNS resolve ra ClusterIP, ClusterIP forward đến Pod thật qua EndpointSlice.

---

## 📎 Nguồn & xem lại

- [course-catalog](../../wiki/notebook/k8s/course-catalog.md) — toàn bộ roadmap và link khóa học
- `course/` — video bài giảng (bản `vi-hardsub` phụ đề Việt) để xem lại từng khái niệm
