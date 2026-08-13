# 05 · Service — địa chỉ ổn định cho Pod ephemeral

Trước: [04 · ReplicaSet & Deployment](../04-k8s-deployment/k8s-deployment.md) · kế tiếp: ConfigMap/Secret & Probes.

**Mục tiêu:** hiểu vì sao Service là lớp địa chỉ cần thiết khi Pod đổi IP liên tục; nắm label selector nối Service → Pod; phân biệt rõ 4 loại Service; tạo được ClusterIP + NodePort cả `kubectl expose` lẫn YAML; tra `endpoints`, test gọi bằng DNS nội bộ.
**Nền:** đã có Deployment chạy được — Service là thứ cho phép gọi vào các Pod của Deployment đó mà không cần biết IP.
**⏱** 50–65 phút · **Sân:** host local (OrbStack Kubernetes).

> Mỗi mục: **Chốt → Vì sao → Cơ chế → Dùng/không → Làm → Kết quả** (output để đối chiếu). Đọc để *hiểu*, gõ để *thấy*.

## Tiền đề (1 lần)
```bash
kubectl config use-context orbstack
kubectl get nodes    # 1 node STATUS=Ready
```

---

## 1. Vì sao cần Service — Pod ephemeral, IP không ổn định

**Chốt:** Pod nhận IP sau khi schedule và mất IP khi chết — client không thể hard-code địa chỉ đó; Service cung cấp một endpoint ổn định (IP + DNS) đứng trước tập Pod.

- **Pod ephemeral:** IP chỉ có sau khi Pod được đặt lên node; Pod chết → Pod mới IP mới.
- **Scale out:** mỗi replica có IP riêng, client không thể liệt kê hết.
- **Service:** object riêng biệt, tồn tại độc lập với vòng đời Pod — Pod chết, Service vẫn còn địa chỉ.
- **Endpoints object:** K8s tự tạo, liệt kê IP thực của Pod khớp selector — cập nhật tự động khi Pod ra/vào.

**Vì sao:** nếu microservice A gọi trực tiếp IP của Pod B mà Pod B bị reschedule, A gọi vào địa chỉ chết → lỗi 502/connection refused. Không có Service, mọi caller phải tự discovery Pod — không thực tế ở quy mô.

**Cơ chế:** `kube-proxy` chạy trên mỗi worker node, lắng nghe API server và cập nhật **iptables/ipvs rules** để route traffic từ `ClusterIP` → IP thực của Pod trong Endpoints. Từ góc nhìn caller: gọi vào một virtual IP duy nhất — kernel intercept và forward đến Pod thật. CoreDNS đăng ký tên `<service-name>` trong namespace → caller dùng tên thay IP.

> **Ẩn dụ:** Service giống số tổng đài công ty — nhân viên (Pod) ra vào liên tục nhưng số điện thoại (ClusterIP/DNS) không đổi; tổng đài tự chuyển máy đến người thật đang làm việc.

**Dùng / không:** dùng Service cho mọi giao tiếp giữa Pod — kể cả nội bộ cùng cluster. **Phản đề:** với batch job chạy 1 lần hoặc Pod không cần nhận traffic (worker chỉ consume queue) → không cần Service; tạo thừa chỉ tốn tài nguyên.

**Làm:**
```bash
# Tạo Deployment làm nền cho cả lab
kubectl create deployment web --image=nginx:alpine --replicas=2
kubectl get pods -o wide    # ghi nhớ IP của 2 Pod — chúng khác nhau

# Quan sát: xóa 1 Pod → Deployment tạo Pod mới với IP khác
OLD_POD=$(kubectl get pod -l app=web -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod "$OLD_POD"
kubectl get pods -o wide    # Pod mới, IP mới
```

**Kết quả:**
```text
$ kubectl get pods -o wide   # trước khi xóa
NAME                   READY   STATUS    IP           NODE
web-7d4f9c-xkp2r       1/1     Running   10.42.0.11   orbstack
web-7d4f9c-mq8tn       1/1     Running   10.42.0.12   orbstack

$ kubectl get pods -o wide   # sau kubectl delete pod web-7d4f9c-xkp2r
NAME                   READY   STATUS    IP           NODE
web-7d4f9c-mq8tn       1/1     Running   10.42.0.12   orbstack
web-7d4f9c-n9pz1       1/1     Running   10.42.0.14   orbstack   ← IP mới
```
→ **Verify:** Pod mới có IP khác — đây là lý do cần Service.

---

## 2. Label selector — cơ chế nối Service → Pod

**Chốt:** Service không biết tên Pod; nó dùng `selector` (tập key=value) khớp với `metadata.labels` của Pod để xây Endpoints — Pod nào khớp thì được đưa vào pool nhận traffic.

- **`spec.selector`** trong Service YAML: tập key=value phải có đủ trong `metadata.labels` của Pod.
- **Endpoints object** tên trùng Service: chứa danh sách `IP:Port` của Pod khớp selector — K8s tự duy trì.
- **Deployment cũng dùng selector:** `spec.selector.matchLabels` quản Pod của Deployment — label trong `spec.template.metadata.labels` phải khớp.
- **Nhiều Service, cùng Pod:** có thể chạy Song song hai Service trỏ vào cùng tập Pod (vd expose port khác nhau).

**Vì sao:** tách label selector khỏi tên Pod giúp Service hoạt động được với Deployment (Pod tên random) và Blue/Green (đổi selector sang version mới không cần đổi Service).

**Cơ chế:** khi Pod thay đổi labels hoặc khi Pod mới được tạo, `endpoint-controller` (trong controller manager) so `spec.selector` của Service với labels thực tế của mọi Pod trong namespace → cập nhật Endpoints. Nếu `selector` sai (ví dụ label Pod là `app=web` nhưng Service dùng `app=nginx`) → Endpoints rỗng (`<none>`) → mọi request vào Service sẽ không đến đâu (connection refused).

> **Ẩn dụ:** selector như filter tag trong email — mail đến hộp shared inbox (Service) được chuyển cho người nào có tag "frontend" (label `app=web`); người mới join (Pod mới) tự động nhận mail nếu có tag đó.

**Dùng / không:** giữ label rõ nghĩa (`app`, `version`, `component`). **Phản đề:** selector quá rộng (ví dụ chỉ `tier=frontend`) vô tình gom Pod không liên quan vào cùng Service → traffic bị route nhầm.

**Làm:**
```bash
# Kiểm tra label của Pod
kubectl get pods --show-labels     # cột LABELS: app=web

# Tạo Service ClusterIP khớp label app=web
kubectl expose deployment web --port=80 --target-port=80 --name=web-svc
kubectl get svc web-svc
kubectl get endpoints web-svc      # 2 IP:80 của 2 Pod
```

**Kết quả:**
```text
$ kubectl get svc web-svc
NAME      TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)   AGE
web-svc   ClusterIP   10.96.14.233   <none>        80/TCP    5s

$ kubectl get endpoints web-svc
NAME      ENDPOINTS                       AGE
web-svc   10.42.0.12:80,10.42.0.14:80    5s   ← 2 IP Pod đang chạy
```
→ **Verify:** Endpoints có đúng 2 IP khớp `kubectl get pods -o wide`.

---

## 3. 4 loại Service — ClusterIP, NodePort, LoadBalancer, ExternalName

**Chốt:** 4 `type` Service mở rộng phạm vi tiếp cận theo tầng — ClusterIP (nội bộ cluster) → NodePort (host/mạng LAN) → LoadBalancer (external IP thật) → ExternalName (alias DNS ra ngoài cluster).

![[service-types.excalidraw]]

- **ClusterIP** (mặc định): chỉ có IP nội bộ cluster — Pod trong cluster gọi được, bên ngoài không. Phổ biến nhất cho giao tiếp pod-to-pod.
- **NodePort**: mở thêm một static port (dải 30000–32767) trên **mọi node**. Caller ngoài gọi `<NodeIP>:<nodePort>` → node proxy vào ClusterIP → Pod. Dưới NodePort luôn có ClusterIP.
- **LoadBalancer**: cloud provider (hoặc MetalLB on-prem) cấp một **external IP** đứng trước cluster. Bên dưới tự tạo NodePort + ClusterIP. Caller ngoài dùng external IP.
- **ExternalName**: Service không trỏ Pod nào trong cluster mà là **alias DNS** cho tên ngoài (vd `api.acmecorp.com`). Địa chỉ ngoài thay đổi → chỉ update Service YAML, Pod không đổi code.

| Type | Ai gọi được | Yêu cầu thêm |
|---|---|---|
| ClusterIP | Pod trong cluster | Không |
| NodePort | Host / mạng LAN | Phải biết NodeIP + nodePort |
| LoadBalancer | Internet / bất kỳ | Cloud LB hoặc MetalLB |
| ExternalName | Pod trong cluster (alias ra ngoài) | DNS name bên ngoài |

**Vì sao:** không có tầng type, mọi Service đều hoặc là "hở internet" hoặc "không gọi được từ ngoài" — không có lựa chọn trung gian. Type cho phép chọn đúng mức độ exposure.

**Cơ chế:** NodePort = ClusterIP + port mở thêm trên node; LoadBalancer = NodePort + external IP do LB controller cấp. Chuỗi route thực tế: external IP → nodePort trên node → ClusterIP iptables rule → Pod IP.

**Dùng / không:** dev/debug trên local → NodePort; prod external traffic → LoadBalancer (kèm Ingress ở tầng trên); nội bộ cluster → ClusterIP; bridge external service → ExternalName. **Phản đề:** đừng dùng NodePort cho prod khi đã có LoadBalancer — NodePort expose port trên mọi node, khó firewall và port không đẹp (30000+).

**Làm:**
```bash
# ClusterIP đã có ở mục 2 — xem lại
kubectl get svc web-svc

# NodePort: expose thêm để gọi từ host
kubectl expose deployment web --port=80 --target-port=80 \
  --type=NodePort --name=web-np
kubectl get svc web-np    # cột PORT(S): 80:<nodePort>/TCP

NODE_PORT=$(kubectl get svc web-np -o jsonpath='{.spec.ports[0].nodePort}')
curl http://localhost:$NODE_PORT
```

**Kết quả:**
```text
$ kubectl get svc web-np
NAME     TYPE       CLUSTER-IP     EXTERNAL-IP   PORT(S)        AGE
web-np   NodePort   10.96.87.210   <none>        80:31842/TCP   4s

$ curl http://localhost:31842
<!DOCTYPE html>
<html>
<head><title>Welcome to nginx!</title>    ← gọi từ host vào Pod qua NodePort
```
→ **Verify:** `PORT(S)` có dạng `80:<nodePort>/TCP`; `curl localhost:<nodePort>` trả nginx response.

> **Thực chạy — trên OrbStack, ClusterIP GỌI ĐƯỢC từ host (không phổ quát!).** `curl http://<clusterIP>` từ máy Mac trả nginx `exit=0` — trái với lý thuyết "ClusterIP chỉ nội bộ cluster". Lý do: OrbStack (giống Docker Desktop) tự cắm route dải mạng cluster vào host — kiểm chứng `netstat -rn | grep <subnet>` sẽ thấy route qua `bridgeXXX`. Trên cụm kind/cloud KHÔNG có route này → `curl` ClusterIP từ host sẽ treo. **Đừng dựa vào hành vi này**; cách portable để lộ Service ra ngoài vẫn là NodePort/LoadBalancer.

---

## 4. port vs targetPort — ánh xạ cổng trong YAML

**Chốt:** `port` là cổng của Service (caller dùng), `targetPort` là cổng của container trong Pod (Service forward tới) — hai con số này thường bằng nhau nhưng không bắt buộc.

- **`port`**: cổng **Service** — caller trong cluster gọi vào `ClusterIP:port`.
- **`targetPort`**: cổng **container** — Service forward tới đây trong Pod.
- **`nodePort`** (chỉ NodePort/LoadBalancer): cổng mở trên node host; bỏ qua → K8s tự assign trong dải 30000–32767.
- Tên cổng (`name: http`) dùng được thay số — hữu ích khi container thay đổi port mà không muốn sửa YAML caller.

**Vì sao:** container có thể chạy port 3000 (Node.js app) nhưng caller muốn gọi port 80 (convention HTTP) → `port: 80, targetPort: 3000`. Không có `targetPort` riêng, buộc phải thay đổi app để bind port theo caller — không hợp lý.

**Cơ chế:** kube-proxy dùng `targetPort` để viết iptables rule DNAT — gói tin đến `ClusterIP:port` được rewrite đích thành `PodIP:targetPort`. Nếu `targetPort` không khớp port container thực sự lắng nghe → connection refused tại Pod.

**Dùng / không:** `port` ≠ `targetPort` khi muốn normalize giao diện Service mà không đổi app. **Phản đề:** nếu app đang bind đúng port caller muốn (vd nginx bind 80) thì `port=targetPort=80` là đủ — đừng thêm phức tạp.

**Làm:**
```bash
cat > /tmp/web-clusterip.yml <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: web-clusterip
spec:
  type: ClusterIP
  selector:
    app: web
  ports:
  # caller dùng cổng 8080
  - port: 8080
    # container nginx lắng nghe cổng 80
    targetPort: 80
EOF
kubectl apply -f /tmp/web-clusterip.yml
kubectl get svc web-clusterip

cat > /tmp/web-nodeport.yml <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: web-nodeport
spec:
  type: NodePort
  selector:
    app: web
  ports:
  - port: 80
    targetPort: 80
    # tùy chọn — bỏ thì K8s tự assign
    nodePort: 31000
EOF
kubectl apply -f /tmp/web-nodeport.yml
curl http://localhost:31000
```

**Kết quả:**
```text
$ kubectl get svc web-clusterip
NAME           TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)    AGE
web-clusterip  ClusterIP   10.96.55.101   <none>        8080/TCP   3s

$ curl http://localhost:31000
<!DOCTYPE html>
<html>
<head><title>Welcome to nginx!</title>    ← nodePort 31000 → targetPort 80 → nginx
```
→ **Verify:** `PORT(S)` của `web-clusterip` là `8080/TCP` (không phải 80); `curl localhost:31000` trả response.

---

## 5. kube-proxy, Endpoints, và DNS nội bộ

**Chốt:** `kube-proxy` cập nhật iptables/ipvs trên mỗi node để route traffic vào Pod thật; Endpoints liệt kê IP Pod đang sống; CoreDNS cho phép gọi Service bằng tên — Pod không cần biết IP nhau.

- **`kube-proxy`**: chạy trên mỗi worker node, watch API server, ghi iptables/ipvs rules — packet đến `ClusterIP` được DNAT sang `PodIP` thật.
- **Endpoints**: object cùng tên với Service, tự cập nhật — Pod chết → IP bị xóa; Pod mới → IP được thêm. `kubectl get endpoints <svc>` là chẩn đoán đầu tiên khi Service không route được.
- **CoreDNS**: resolve `<service-name>` (cùng namespace) hoặc `<service-name>.<namespace>.svc.cluster.local` (khác namespace) → ClusterIP. Pod chỉ cần tên Service, không cần biết IP.

**Vì sao:** Endpoints rỗng (`<none>`) = selector sai → biết ngay mà không cần tcpdump. DNS nội bộ giải phóng config khỏi hardcode IP — thêm/xóa Pod không cần cập nhật client.

**Cơ chế flow đầy đủ:** Pod A gọi `http://web-svc:80` → CoreDNS resolve `web-svc` → `ClusterIP 10.96.14.233` → iptables rule (do kube-proxy viết) DNAT sang `10.42.0.12:80` (IP Pod thật trong Endpoints) → Pod B nhận request.

> **Ẩn dụ:** CoreDNS như danh bạ nội bộ công ty (gọi tên phòng, không cần số IP bàn); kube-proxy như tổng đài tự động (biết bàn nào đang hoạt động và forward đúng).

**Dùng / không:** luôn gọi Service qua tên DNS trong code — không hardcode ClusterIP (IP có thể thay đổi nếu Service bị xóa/tạo lại). **Phản đề:** nếu cần debug tầng iptables thật (`iptables -t nat -L | grep <ClusterIP>`) thì cần vào node host — không làm được từ trong Pod thông thường.

**Làm:**
```bash
kubectl get endpoints web-clusterip    # IP:80 của các Pod đang chạy

# Test DNS nội bộ: chạy Pod tạm, gọi tên Service
kubectl run tmp --image=nginx:alpine --restart=Never -it --rm \
  -- sh -c 'apk add -q curl && curl http://web-clusterip:8080'

# Xem Endpoints tự cập nhật khi scale
kubectl scale deployment web --replicas=3
kubectl get endpoints web-clusterip    # lúc này 3 IP
```

**Kết quả:**
```text
$ kubectl get endpoints web-clusterip
NAME           ENDPOINTS                                  AGE
web-clusterip  10.42.0.12:80,10.42.0.14:80               12s

# Trong Pod tạm — curl bằng tên Service:
$ curl http://web-clusterip:8080
<!DOCTYPE html>
<html>
<head><title>Welcome to nginx!</title>   ← gọi bằng tên, không cần biết IP

$ kubectl get endpoints web-clusterip   # sau scale --replicas=3
NAME           ENDPOINTS                                         AGE
web-clusterip  10.42.0.12:80,10.42.0.14:80,10.42.0.15:80       35s   ← 3 IP
```
→ **Verify:** curl trong Pod tạm thành công dùng tên Service; scale lên 3 → Endpoints có 3 IP.

> **Thực chạy — `Endpoints` deprecated ở v1.33+, dùng `EndpointSlice`.** Trên cụm v1.34, `kubectl get endpoints` in cảnh báo `v1 Endpoints is deprecated in v1.33+; use discovery.k8s.io/v1 EndpointSlice`. Object `Endpoints` cũ gom TẤT CẢ IP vào 1 object → phình to gây tải khi Service có hàng nghìn Pod; `EndpointSlice` chia thành nhiều slice (~100 IP/slice). Xem bản mới: `kubectl get endpointslice -l kubernetes.io/service-name=<svc>`. Lab thật: scale 2→3 → slice thêm IP thứ 3 (`.27`) tự động, không đụng Service.

---

## Dọn dẹp
```bash
kubectl delete svc web-svc web-np web-clusterip web-nodeport --ignore-not-found
kubectl delete deployment web --ignore-not-found
rm -f /tmp/web-clusterip.yml /tmp/web-nodeport.yml
```

---

## Đủ khi (nói trơn bằng lời mình)
① vì sao không thể dùng IP Pod làm địa chỉ liên lạc · ② label selector hoạt động thế nào, xem Endpoints ở đâu · ③ 4 loại Service, mỗi loại dùng khi nào · ④ `port` vs `targetPort` vs `nodePort` khác nhau thế nào · ⑤ DNS nội bộ: gọi Service bằng tên, không cần IP.

## Recall — tự kiểm (cuối buổi)
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

1. Pod có thể thay đổi IP khi nào? Tại sao không dùng IP Pod làm địa chỉ gọi?
2. Service biết nên route traffic đến Pod nào nhờ cơ chế gì?
3. `kubectl get endpoints <svc>` hiển thị gì? Khi nào nó thay đổi?
4. ClusterIP khác NodePort ở điểm gì cụ thể?
5. LoadBalancer type cần thêm thành phần nào mà ClusterIP/NodePort không cần?
6. ExternalName dùng khi nào? Lợi ích so với hard-code URL trong Pod?
7. `port: 8080, targetPort: 80` nghĩa là gì?
8. Làm sao Pod A gọi Pod B mà không biết IP của B?
9. `kube-proxy` làm gì trên mỗi node?
10. Muốn mở port tạm từ host vào Pod để debug, không tạo Service thật, dùng lệnh gì?

### Đáp án

1. Pod đổi IP khi bị reschedule (Pod chết → Pod mới IP mới), khi scale out (Pod mới = IP mới), khi mới schedule (IP chỉ có sau khi Pod được đặt lên node). Không dùng IP Pod vì client không biết trước và IP thay đổi liên tục.
2. Label selector: Service dùng `spec.selector` (key=value) so với `metadata.labels` của Pod. Pod khớp → Endpoints của Service chứa IP Pod đó.
3. Danh sách `IP:Port` của Pod đang chạy mà Service đang track. Tự cập nhật khi Pod chết (xóa IP) hoặc Pod mới lên (thêm IP).
4. ClusterIP: chỉ có IP nội bộ cluster, bên ngoài không gọi được. NodePort: mở thêm một port tĩnh (30000–32767) trên mọi node — bên ngoài gọi `<NodeIP>:<nodePort>`.
5. Cần cloud load balancer (AWS ELB, GCP LB, Azure LB) hoặc on-prem thay thế như MetalLB để cấp external IP.
6. Dùng khi cần alias cho external domain/IP hay thay đổi. Lợi ích: Pod chỉ gọi tên Service (ví dụ `external-api`); nếu địa chỉ thật thay đổi, chỉ update YAML Service — không sửa code trong Pod.
7. Caller trong cluster gọi Service tại cổng 8080; Service forward tới cổng 80 của container bên trong Pod.
8. Dùng tên Service làm hostname: `curl http://web-clusterip:8080`. CoreDNS resolve tên Service → ClusterIP → kube-proxy route vào Pod.
9. Lắng nghe API server, cập nhật iptables/ipvs rules trên node để route traffic từ ClusterIP → Pod IP thực của Endpoints.
10. `kubectl port-forward <pod|deploy/name|svc/name> <local-port>:<container-port>` — không tạo Service, chỉ mở tunnel tạm từ host.

---

## Bắc cầu sang Kubernetes production (LoadBalancer = MetalLB)
Trên cụm thật (`kubectl -n <namespace> get svc`), hầu hết Service nội bộ dùng **ClusterIP** để các microservice gọi nhau bằng tên — không cần biết IP Pod. Traffic từ ngoài vào cluster qua **LoadBalancer type** do cloud provider hoặc **MetalLB** (on-prem) cấp IP — đây là lý do `kubectl get svc` ở namespace ingress có cột `EXTERNAL-IP` thật thay vì `<pending>`. Khi Pod nào đó không gọi được service khác → `kubectl get endpoints <svc>` là bước đầu tiên: nếu `<none>` thì selector sai label.

---

